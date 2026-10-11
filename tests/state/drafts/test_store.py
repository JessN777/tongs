"""Failure-oriented tests for durable review draft storage."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import sqlite3
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite
import pytest
from platformdirs import user_cache_dir, user_data_dir

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.scanner.repo import ForgeType
from tongs.services import RepositoryRef, ReviewRef, ReviewRevision
from tongs.services.session import ApplicationSession
from tongs.state.drafts import (
    MAX_DRAFT_BODY_BYTES,
    MAX_DRAFT_COMMENTS,
    DiffSide,
    DraftAttemptOwnedError,
    DraftConflictError,
    DraftContent,
    DraftCorruptionError,
    DraftLimitError,
    DraftNotFoundError,
    DraftNotOpenError,
    DraftPermissionError,
    DraftReceiptConflictError,
    DraftSchemaError,
    DraftState,
    DraftStateError,
    DraftStore,
    DraftStoreError,
    DraftVerdict,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
    SubmissionAttempt,
    SubmissionPlanRecord,
    SubmissionPlanStepRecord,
    context_fingerprint,
    default_draft_db_path,
)
from tongs.state.drafts import _locks as locks_module
from tongs.state.drafts import store as store_module
from tongs.state.drafts._locks import AttemptLock

REVIEW = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 17)
REVISION = ReviewRevision("head-1", "base-1", "start-1")


def make_content(label: str = "draft") -> DraftContent:
    anchor = InlineAnchor(
        REVISION,
        "src/old.py",
        "src/new.py",
        10,
        11,
        DiffSide.NEW,
        context_fingerprint(("before", "selected", "after")),
        start_line=9,
        start_side=DiffSide.OLD,
    )
    return DraftContent(
        body=f"{label} body",
        verdict=DraftVerdict.REQUEST_CHANGES,
        comments=(
            GeneralDraftComment(uuid4(), f"{label} general"),
            InlineDraftComment(uuid4(), f"{label} inline", anchor),
            ReplyDraftComment(uuid4(), f"{label} reply", "thread-9"),
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "application-data" / "drafts.db"


@pytest.mark.asyncio
async def test_round_trip_restart_preserves_ordered_stable_content(
    db_path: Path,
) -> None:
    content = make_content()
    first = DraftStore(db_path)
    await first.open()
    created = await first.create_draft(REVIEW, REVISION, content)
    await first.close()

    second = DraftStore(db_path)
    await second.open()
    recovered = await second.get_draft(created.id)
    await second.close()

    assert recovered == created
    assert tuple(comment.id for comment in recovered.comments) == tuple(
        comment.id for comment in content.comments
    )


@pytest.mark.asyncio
async def test_submission_plan_is_write_once_and_survives_reopen(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, make_content())
    attempt = await store.lock_submission(draft.id, draft.version)
    comment_id = attempt.snapshot.comments[0].id
    plan = SubmissionPlanRecord(
        ForgeType.GITHUB,
        False,
        (
            SubmissionPlanStepRecord("comment:one", "general_comment", (comment_id,)),
            SubmissionPlanStepRecord("review", "verdict"),
        ),
    )

    recorded = await store.record_plan(attempt.id, plan)
    duplicate = await store.record_plan(attempt.id, plan)

    assert recorded.plan == plan
    assert duplicate.plan == plan
    with pytest.raises(DraftStoreError, match="bound differently"):
        await store.record_plan(
            attempt.id,
            SubmissionPlanRecord(
                ForgeType.GITHUB,
                True,
                (SubmissionPlanStepRecord("batch", "github_review", (comment_id,)),),
            ),
        )
    await store.close()

    reopened = DraftStore(db_path)
    await reopened.open()
    assert (await reopened.get_attempt(attempt.id)).plan == plan
    await reopened.close()


def test_default_database_is_separate_from_evictable_cache() -> None:
    path = default_draft_db_path()

    assert path.name == "drafts.db"
    assert path.is_relative_to(Path(user_data_dir("tongs")))
    assert not path.is_relative_to(Path(user_cache_dir("tongs")))


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
async def test_database_and_sidecars_are_private(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    await store.create_draft(REVIEW, REVISION)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        assert sidecar.exists()
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    attempt = await store.lock_submission((await store.list_drafts())[0].id, 1)
    lock_file = lock_dir / f"{attempt.id}.lock"
    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    await store.close()
    # Releasing ownership deletes the file; it carries no state of its own.
    assert not lock_file.exists()


@pytest.mark.asyncio
async def test_save_against_changed_revision_returns_stale_anchor_without_retargeting(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    caller = make_content()
    changed = ReviewRevision("head-1", "different-base", "start-1")

    saved = await store.save_draft(
        draft.id, draft.version, caller, current_revision=changed
    )

    inline = saved.comments[1]
    assert isinstance(inline, InlineDraftComment)
    assert inline.anchor.revision == REVISION
    assert saved.version == 2
    # The saved snapshot is assessed against the caller's current revision.
    assert inline.anchor.stale is True
    # The stored version is not retargeted, so a plain read is not stale.
    stored = (await store.get_draft(draft.id)).comments[1]
    assert isinstance(stored, InlineDraftComment)
    assert stored.anchor.stale is False
    # Staleness is derived from the revisions, never read back from storage.
    assessed = saved.assessed_against(changed).comments[1]
    assert isinstance(assessed, InlineDraftComment)
    assert assessed.anchor.stale is True
    current = saved.assessed_against(REVISION).comments[1]
    assert isinstance(current, InlineDraftComment)
    assert current.anchor.stale is False
    await store.close()


@pytest.mark.asyncio
async def test_stored_stale_flag_is_ignored_on_read(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, make_content())
    await store.close()
    with sqlite3.connect(db_path) as db:
        (raw,) = db.execute(
            "SELECT comments_json FROM drafts WHERE id = ?", (str(draft.id),)
        ).fetchone()
        comments = json.loads(raw)
        for comment in comments:
            if comment["kind"] == "inline":
                comment["anchor"]["stale"] = True
        db.execute(
            "UPDATE drafts SET comments_json = ? WHERE id = ?",
            (json.dumps(comments), str(draft.id)),
        )

    reopened = DraftStore(db_path)
    await reopened.open()
    loaded = await reopened.get_draft(draft.id)
    listed = await reopened.list_drafts(review=REVIEW)

    assert [
        comment.anchor.stale
        for snapshot in (loaded, *listed)
        for comment in snapshot.comments
        if isinstance(comment, InlineDraftComment)
    ] == [False, False]
    await reopened.close()


@pytest.mark.asyncio
async def test_cas_contention_preserves_winner_and_both_caller_payloads(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    one, two = DraftStore(db_path), DraftStore(db_path)
    await asyncio.gather(one.open(), two.open())
    first_content, second_content = make_content("one"), make_content("two")

    results = await asyncio.gather(
        one.save_draft(
            draft.id, draft.version, first_content, current_revision=REVISION
        ),
        two.save_draft(
            draft.id, draft.version, second_content, current_revision=REVISION
        ),
        return_exceptions=True,
    )

    winner = next(result for result in results if not isinstance(result, Exception))
    conflict = next(
        result for result in results if isinstance(result, DraftConflictError)
    )
    assert conflict.current == winner
    assert conflict.caller_content in {first_content, second_content}
    assert conflict.caller_content != winner.content
    assert await one.get_draft(draft.id) == winner
    await asyncio.gather(one.close(), two.close())


@pytest.mark.asyncio
async def test_same_store_serializes_concurrent_create_and_edit(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    created = await asyncio.gather(
        store.create_draft(REVIEW, REVISION),
        store.create_draft(REVIEW, REVISION),
    )
    assert len(await store.list_drafts()) == 2

    one, two = make_content("one"), make_content("two")
    results = await asyncio.gather(
        store.save_draft(
            created[0].id, created[0].version, one, current_revision=REVISION
        ),
        store.save_draft(
            created[0].id, created[0].version, two, current_revision=REVISION
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, DraftConflictError) for result in results) == 1
    await store.close()


@pytest.mark.asyncio
async def test_discard_requires_current_version_and_preserves_conflicting_draft(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    saved = await store.save_draft(
        draft.id, draft.version, make_content(), current_revision=REVISION
    )

    with pytest.raises(DraftConflictError) as raised:
        await store.discard_draft(saved.id, draft.version)
    assert raised.value.current == saved
    assert await store.get_draft(saved.id) == saved
    assert await store.discard_draft(saved.id, saved.version) == saved
    with pytest.raises(DraftNotFoundError):
        await store.get_draft(saved.id)
    await store.close()


@pytest.mark.asyncio
async def test_submission_freezes_version_and_receipts_are_insert_once(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, make_content())
    attempt = await store.lock_submission(draft.id, draft.version)

    assert attempt.frozen_version == draft.version
    assert attempt.snapshot == draft
    assert (await store.get_draft(draft.id)).version == draft.version + 1
    first = await store.record_receipt(attempt.id, "comment:1", "remote-1")
    duplicate = await store.record_receipt(
        attempt.id, "comment:1", "remote-1", resync_required=True
    )
    repeated = await store.record_receipt(attempt.id, "comment:1", "remote-1")
    assert first.receipts[0].resync_required is False
    assert duplicate.receipts[0].resync_required is True
    assert repeated.receipts == duplicate.receipts
    with pytest.raises(DraftReceiptConflictError):
        await store.record_receipt(attempt.id, "comment:1", "different")
    assert (await store.get_attempt(attempt.id)).receipts == duplicate.receipts
    await store.close()


@pytest.mark.asyncio
async def test_same_store_serializes_concurrent_receipts(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)

    await asyncio.gather(
        store.record_receipt(attempt.id, "comment:1", "remote-1"),
        store.record_receipt(attempt.id, "comment:2", "remote-2"),
    )

    persisted = await store.get_attempt(attempt.id)
    assert tuple(receipt.step_id for receipt in persisted.receipts) == (
        "comment:1",
        "comment:2",
    )
    await store.close()


@pytest.mark.asyncio
async def test_unknown_attempt_is_noneditable_and_nonreplayable_until_reconciled(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)
    unknown = await store.mark_attempt_unknown(attempt.id)

    with pytest.raises(DraftAttemptOwnedError):
        await store.record_receipt(attempt.id, "body", "remote")
    current = await store.get_draft(draft.id)
    with pytest.raises(DraftStateError):
        await store.save_draft(
            draft.id, current.version, make_content(), current_revision=REVISION
        )
    assert unknown.state == DraftState.UNKNOWN
    assert await store.list_recovery_attempts() == (unknown,)
    assert await store.list_recovery_attempts(include_owned=True) == (unknown,)

    resumed = await store.reconcile_attempt(
        attempt.id, ReconciliationResolution.RETRY_REMAINING
    )
    assert resumed.id == attempt.id
    completed = await store.complete_submission(attempt.id)
    assert completed.state == DraftState.SUBMITTED
    assert (await store.get_draft(draft.id)).state == DraftState.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_confirmed_receipt_survives_owner_exit_and_is_not_duplicated(
    db_path: Path,
) -> None:
    owner = DraftStore(db_path)
    await owner.open()
    draft = await owner.create_draft(REVIEW, REVISION)
    attempt = await owner.lock_submission(draft.id, draft.version)
    await owner.record_receipt(attempt.id, "comment:stable", "remote-1")
    await owner.close()

    restarted = DraftStore(db_path)
    await restarted.open()
    recovered = await restarted.recover_incomplete_attempts()
    assert len(recovered) == 1
    assert tuple(receipt.step_id for receipt in recovered[0].receipts) == (
        "comment:stable",
    )
    resumed = await restarted.reconcile_attempt(
        attempt.id, ReconciliationResolution.RETRY_REMAINING
    )
    assert resumed.id == attempt.id
    duplicate = await restarted.record_receipt(attempt.id, "comment:stable", "remote-1")
    assert len(duplicate.receipts) == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_return_editable_reconciliation_invalidates_prelock_editors(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.mark_attempt_unknown(attempt.id)
    await store.reconcile_attempt(attempt.id, ReconciliationResolution.RETURN_EDITABLE)

    current = await store.get_draft(draft.id)
    assert current.state == DraftState.EDITABLE
    assert current.version == draft.version + 2
    with pytest.raises(DraftConflictError):
        await store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    await store.close()


@pytest.mark.asyncio
async def test_second_store_recovery_skips_live_owner_then_recovers_after_close(
    db_path: Path,
) -> None:
    owner = DraftStore(db_path)
    observer = DraftStore(db_path)
    await owner.open()
    draft = await owner.create_draft(REVIEW, REVISION)
    attempt = await owner.lock_submission(draft.id, draft.version)
    await observer.open()

    assert await observer.recover_incomplete_attempts() == ()
    with pytest.raises(DraftAttemptOwnedError):
        await observer.reconcile_attempt(
            attempt.id, ReconciliationResolution.RETURN_EDITABLE
        )
    assert (await observer.get_attempt(attempt.id)).state == DraftState.SUBMITTING

    await owner.close()
    recovered = await observer.recover_incomplete_attempts()
    assert tuple(item.id for item in recovered) == (attempt.id,)
    assert recovered[0].state == DraftState.UNKNOWN
    await observer.close()


def _child_lock_submission(
    db_path: str, draft_id: str, version: int, ready: object
) -> None:
    async def run() -> None:
        store = DraftStore(Path(db_path))
        await store.open()
        await store.lock_submission(UUID(draft_id), version)
        ready.set()  # type: ignore[attr-defined]
        await asyncio.Event().wait()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Fedora process-lock proof")
async def test_child_process_death_releases_attempt_for_unknown_recovery(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(
        target=_child_lock_submission,
        args=(str(db_path), str(draft.id), draft.version, ready),
    )
    child.start()
    assert await asyncio.to_thread(ready.wait, 10)
    observer = DraftStore(db_path)
    await observer.open()
    attempt = (await observer.list_drafts())[0]
    assert attempt.state == DraftState.SUBMITTING
    # Opening the store leaves the live child's lock file in place.
    held = _lock_files(db_path)
    assert len(held) == 1
    assert await observer.recover_incomplete_attempts() == ()
    assert _lock_files(db_path) == held

    child.terminate()
    await asyncio.to_thread(child.join, 10)
    assert child.is_alive() is False
    recovered = await observer.recover_incomplete_attempts()
    assert len(recovered) == 1
    assert recovered[0].state == DraftState.UNKNOWN
    assert held == [f"{recovered[0].id}.lock"]
    assert _lock_files(db_path) == []
    await observer.close()


@pytest.mark.asyncio
async def test_concurrent_submission_lock_has_one_attempt_and_no_leaked_owner(
    db_path: Path,
) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(REVIEW, REVISION)
    await setup.close()
    one, two = DraftStore(db_path), DraftStore(db_path)
    await asyncio.gather(one.open(), two.open())

    results = await asyncio.gather(
        one.lock_submission(draft.id, draft.version),
        two.lock_submission(draft.id, draft.version),
        return_exceptions=True,
    )
    attempts = [result for result in results if not isinstance(result, Exception)]
    assert len(attempts) == 1
    assert (
        sum(
            isinstance(result, (DraftConflictError, DraftStateError))
            for result in results
        )
        == 1
    )
    await asyncio.gather(one.close(), two.close())

    recovery = DraftStore(db_path)
    await recovery.open()
    assert len(await recovery.recover_incomplete_attempts()) == 1
    await recovery.close()


@pytest.mark.asyncio
async def test_failed_duplicate_attempt_id_releases_extra_lock(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    first = await store.create_draft(REVIEW, REVISION)
    second = await store.create_draft(
        ReviewRef(REVIEW.repository, REVIEW.number + 1), REVISION
    )
    attempt_id = uuid4()
    first_attempt = await store.lock_submission(
        first.id, first.version, attempt_id=attempt_id
    )
    await store.complete_submission(first_attempt.id)

    with pytest.raises(DraftStoreError, match="lock failed"):
        await store.lock_submission(second.id, second.version, attempt_id=attempt_id)
    probe = AttemptLock.try_acquire(
        db_path.with_name(f"{db_path.name}.locks"), str(attempt_id)
    )
    assert probe is not None
    probe.release()
    replacement = await store.lock_submission(second.id, second.version)
    assert replacement.state == DraftState.SUBMITTING
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_lock_rolls_back_and_releases_attempt_owner(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    original = store_module._snapshot_to_json

    def cancel_after_lock(snapshot: object) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(store_module, "_snapshot_to_json", cancel_after_lock)
    with pytest.raises(asyncio.CancelledError):
        await store.lock_submission(draft.id, draft.version)
    monkeypatch.setattr(store_module, "_snapshot_to_json", original)

    assert (await store.get_draft(draft.id)).state == DraftState.EDITABLE
    attempt = await store.lock_submission(draft.id, draft.version)
    assert attempt.state == DraftState.SUBMITTING
    await store.close()


@pytest.mark.asyncio
async def test_close_waits_for_in_flight_transaction(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store._draft_in_transaction

    async def blocking_read(db: aiosqlite.Connection, draft_id: UUID) -> object:
        entered.set()
        await release.wait()
        return await original(db, draft_id)

    monkeypatch.setattr(store, "_draft_in_transaction", blocking_read)
    save = asyncio.create_task(
        store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    )
    await entered.wait()
    close = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert close.done() is False

    release.set()
    await save
    await close
    with pytest.raises(DraftNotOpenError):
        await store.get_draft(draft.id)


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_rejects_newer_schema(db_path: Path) -> None:
    first = DraftStore(db_path)
    await first.open()
    await first.close()
    second = DraftStore(db_path)
    await second.open()
    await second.close()
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        db.execute("PRAGMA user_version=99")

    with pytest.raises(DraftSchemaError, match="newer"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_declared_current_but_incomplete_schema_fails_on_open(
    db_path: Path,
) -> None:
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE drafts(id TEXT PRIMARY KEY)")
        db.execute("PRAGMA user_version=1")
    os.chmod(db_path, 0o600)

    with pytest.raises(DraftSchemaError, match="incomplete"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_without_destroying_existing_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy VALUES ('preserve-me')")
    os.chmod(db_path, 0o600)
    monkeypatch.setitem(
        store_module._MIGRATIONS,
        1,
        ("CREATE TABLE temporary(value TEXT)", "THIS IS NOT SQL"),
    )

    with pytest.raises(DraftStoreError):
        await DraftStore(db_path).open()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT value FROM legacy").fetchone() == ("preserve-me",)
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='temporary'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_corrupt_database_is_not_replaced(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    original = b"not a sqlite database"
    db_path.write_bytes(original)
    os.chmod(db_path, 0o600)

    with pytest.raises(DraftCorruptionError):
        await DraftStore(db_path).open()
    assert db_path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
async def test_permissive_existing_database_fails_closed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    db_path.touch(mode=0o644)

    with pytest.raises(DraftPermissionError, match="other users"):
        await DraftStore(db_path).open()


@pytest.mark.asyncio
async def test_database_creation_permission_error_is_explicit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = os.open

    def deny_database(path: object, flags: int, mode: int = 0o777) -> int:
        if Path(path) == db_path:
            raise PermissionError("denied")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny_database)
    with pytest.raises(DraftPermissionError, match="cannot open"):
        await DraftStore(db_path).open()
    assert db_path.exists() is False


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink policy")
async def test_attempt_lock_rejects_symlink_without_touching_target(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt_id = uuid4()
    target = db_path.parent / "unrelated"
    target.write_text("preserve")
    # A private regular target passes every other lock check, so only the
    # refusal to follow the symlink keeps the lock off it.
    target.chmod(0o600)
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    lock_dir.mkdir(mode=0o700)
    (lock_dir / f"{attempt_id}.lock").symlink_to(target)

    with pytest.raises(DraftPermissionError):
        await store.lock_submission(draft.id, draft.version, attempt_id=attempt_id)
    assert target.read_text() == "preserve"
    assert (await store.get_draft(draft.id)).state == DraftState.EDITABLE
    await store.close()


def _lock_files(db_path: Path) -> list[str]:
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    if not lock_dir.exists():
        return []
    return sorted(path.name for path in lock_dir.iterdir())


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlink while held")
async def test_completion_commits_then_unlinks_then_unlocks_attempt_lock(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    attempt = await store.lock_submission(draft.id, draft.version)
    lock_file = db_path.with_name(f"{db_path.name}.locks") / f"{attempt.id}.lock"
    assert _lock_files(db_path) == [lock_file.name]
    unlocks: list[tuple[bool, str]] = []
    original_flock = locks_module.fcntl.flock

    def recording_flock(fd: int, operation: int) -> None:
        if operation == locks_module.fcntl.LOCK_UN:
            with sqlite3.connect(db_path) as reader:
                (state,) = reader.execute(
                    "SELECT state FROM drafts WHERE id = ?", (str(draft.id),)
                ).fetchone()
            unlocks.append((lock_file.exists(), state))
        original_flock(fd, operation)

    monkeypatch.setattr(locks_module.fcntl, "flock", recording_flock)
    await store.complete_submission(attempt.id)
    monkeypatch.setattr(locks_module.fcntl, "flock", original_flock)

    # The submitted state is committed and the file is gone before unlocking.
    assert unlocks == [(False, DraftState.SUBMITTED.value)]
    assert _lock_files(db_path) == []
    await store.close()


@pytest.mark.asyncio
async def test_final_attempt_states_leave_no_lock_files(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    submitted = await store.create_draft(REVIEW, REVISION)
    cancelled = await store.create_draft(
        ReviewRef(REVIEW.repository, REVIEW.number + 1), REVISION
    )
    reconciled = await store.create_draft(
        ReviewRef(REVIEW.repository, REVIEW.number + 2), REVISION
    )

    first = await store.lock_submission(submitted.id, submitted.version)
    await store.complete_submission(first.id)
    second = await store.lock_submission(cancelled.id, cancelled.version)
    await store.cancel_submission(second.id)
    third = await store.lock_submission(reconciled.id, reconciled.version)
    await store.mark_attempt_unknown(third.id)
    await store.reconcile_attempt(third.id, ReconciliationResolution.MARK_SUBMITTED)

    assert _lock_files(db_path) == []
    await store.close()


def _age(path: Path) -> None:
    """Make a lock file look like a leftover from a crashed process."""
    old = time.time() - 3600
    os.utime(path, (old, old), follow_symlinks=False)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX lock file policy")
async def test_open_removes_only_stale_lock_files_it_can_lock(db_path: Path) -> None:
    setup = DraftStore(db_path)
    await setup.open()
    await setup.close()
    lock_dir = db_path.with_name(f"{db_path.name}.locks")
    lock_dir.mkdir(mode=0o700)
    stale = lock_dir / f"{uuid4()}.lock"
    stale.touch(mode=0o600)
    _age(stale)
    # A fresh unheld file may be a submission about to lock it.
    fresh = lock_dir / f"{uuid4()}.lock"
    fresh.touch(mode=0o600)
    live = AttemptLock.try_acquire(lock_dir, str(uuid4()))
    assert live is not None
    shared = lock_dir / f"{uuid4()}.lock"
    shared.touch()
    shared.chmod(0o644)
    unrelated = lock_dir / "notes.lock"
    unrelated.touch(mode=0o600)
    target = db_path.parent / "target"
    target.write_text("preserve")
    target.chmod(0o600)
    linked = lock_dir / f"{uuid4()}.lock"
    linked.symlink_to(target)
    # Old enough to be candidates, so the other guards decide.
    for path in (live.path, shared, unrelated, linked):
        _age(path)

    store = DraftStore(db_path)
    await store.open()

    assert not stale.exists()
    assert fresh.exists()
    # A lock another owner holds keeps its file and its ownership.
    assert live.path.exists()
    assert AttemptLock.try_acquire(lock_dir, live.path.stem) is None
    # Files that fail the private-file checks or are not attempt locks stay.
    assert shared.exists()
    assert unrelated.exists()
    assert linked.is_symlink()
    assert target.read_text() == "preserve"
    live.release()
    assert not live.path.exists()
    await store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inode re-check")
def test_attempt_lock_rejects_descriptor_to_unlinked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "locks"
    attempt_id = str(uuid4())
    holder = AttemptLock.try_acquire(lock_dir, attempt_id)
    assert holder is not None
    # A second process opened the path before the holder unlinked it.
    stale = os.open(holder.path, os.O_RDWR)
    holder.release()
    assert not holder.path.exists()
    stale_descriptors = [stale, os.dup(stale)]
    monkeypatch.setattr(
        locks_module, "_open_lock_file", lambda _path: stale_descriptors.pop()
    )

    # The old descriptor locks without blocking, but it names a deleted inode,
    # so both tries are refused instead of granting a second owner.
    assert AttemptLock.try_acquire(lock_dir, attempt_id) is None
    assert stale_descriptors == []
    monkeypatch.undo()

    fresh = AttemptLock.try_acquire(lock_dir, attempt_id)
    assert fresh is not None
    assert fresh.path.exists()
    assert AttemptLock.try_acquire(lock_dir, attempt_id) is None
    fresh.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inode re-check")
def test_attempt_lock_retries_once_after_stale_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "locks"
    attempt_id = str(uuid4())
    holder = AttemptLock.try_acquire(lock_dir, attempt_id)
    assert holder is not None
    stale = os.open(holder.path, os.O_RDWR)
    holder.release()
    opened: list[str] = []
    real_open = locks_module._open_lock_file

    def stale_then_real(path: Path) -> int:
        if not opened:
            opened.append("stale")
            return stale
        opened.append("fresh")
        return real_open(path)

    monkeypatch.setattr(locks_module, "_open_lock_file", stale_then_real)
    owner = AttemptLock.try_acquire(lock_dir, attempt_id)
    monkeypatch.undo()

    assert owner is not None
    assert opened == ["stale", "fresh"]
    # The retry owns the file now at the path, so nobody else can take it.
    assert owner.path.exists()
    assert AttemptLock.try_acquire(lock_dir, attempt_id) is None
    owner.release()
    assert not owner.path.exists()


@pytest.mark.asyncio
async def test_write_failure_preserves_last_valid_snapshot(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION)
    assert store._db is not None
    await store._db.execute(
        """
        CREATE TRIGGER simulate_full_disk BEFORE UPDATE ON drafts
        BEGIN SELECT RAISE(FAIL, 'database or disk is full'); END
        """
    )

    with pytest.raises(DraftStoreError, match="write failed"):
        await store.save_draft(
            draft.id, draft.version, make_content(), current_revision=REVISION
        )
    assert await store.get_draft(draft.id) == draft
    await store.close()


@pytest.mark.asyncio
async def test_attempt_read_keeps_one_snapshot_across_concurrent_receipt(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with DraftStore(db_path) as owner, DraftStore(db_path) as observer:
        draft = await owner.create_draft(REVIEW, REVISION)
        attempt = await owner.lock_submission(draft.id, draft.version)
        original = observer._row_to_attempt

        async def commit_between_reads(
            db: aiosqlite.Connection, row: aiosqlite.Row
        ) -> SubmissionAttempt:
            await owner.record_receipt(attempt.id, "step", "remote")
            return await original(db, row)

        monkeypatch.setattr(observer, "_row_to_attempt", commit_between_reads)
        observed = await observer.get_attempt(attempt.id)
        assert observed.state == DraftState.SUBMITTING
        assert observed.receipts == ()
        current = await owner.get_attempt(attempt.id)
        assert current.state == DraftState.PARTIAL
        assert current.receipts[0].remote_id == "remote"


@pytest.mark.asyncio
async def test_recovery_list_keeps_one_snapshot_during_reconciliation(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with DraftStore(db_path) as owner, DraftStore(db_path) as observer:
        draft = await owner.create_draft(REVIEW, REVISION)
        attempt = await owner.lock_submission(draft.id, draft.version)
        await owner.mark_attempt_unknown(attempt.id)
        original = observer._attempt_in_transaction

        async def reconcile_between_reads(
            db: aiosqlite.Connection, attempt_id: UUID
        ) -> SubmissionAttempt:
            await owner.reconcile_attempt(
                attempt_id, ReconciliationResolution.MARK_SUBMITTED
            )
            return await original(db, attempt_id)

        monkeypatch.setattr(
            observer, "_attempt_in_transaction", reconcile_between_reads
        )
        observed = await observer.list_recovery_attempts()
        assert len(observed) == 1
        assert observed[0].state == DraftState.UNKNOWN
        assert observed[0].reconciliations == ()
        assert (await owner.get_attempt(attempt.id)).state == DraftState.SUBMITTED


@pytest.mark.asyncio
async def test_failed_retry_reconciliation_releases_untransferred_lock(
    db_path: Path,
) -> None:
    async with DraftStore(db_path) as owner, DraftStore(db_path) as successor:
        draft = await owner.create_draft(REVIEW, REVISION)
        attempt = await owner.lock_submission(draft.id, draft.version)
        await owner.mark_attempt_unknown(attempt.id)
        assert owner._db is not None
        await owner._db.execute(
            """
            CREATE TRIGGER fail_retry BEFORE UPDATE ON submission_attempts
            WHEN NEW.state = 'submitting'
            BEGIN SELECT RAISE(FAIL, 'simulated retry write failure'); END
            """
        )
        with pytest.raises(DraftStoreError, match="reconciliation write failed"):
            await owner.reconcile_attempt(
                attempt.id, ReconciliationResolution.RETRY_REMAINING
            )
        await owner._db.execute("DROP TRIGGER fail_retry")
        resumed = await successor.reconcile_attempt(
            attempt.id, ReconciliationResolution.RETRY_REMAINING
        )
        assert resumed.state == DraftState.SUBMITTING
        assert resumed.reconciliations[-1].resolution == (
            ReconciliationResolution.RETRY_REMAINING
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", list(ReconciliationResolution))
async def test_detached_attempt_cannot_reconcile_over_new_submission(
    db_path: Path, resolution: ReconciliationResolution
) -> None:
    async with DraftStore(db_path) as store:
        draft = await store.create_draft(REVIEW, REVISION)
        old = await store.lock_submission(draft.id, draft.version)
        await store.mark_attempt_unknown(old.id)
        detached = await store.reconcile_attempt(
            old.id, ReconciliationResolution.RETURN_EDITABLE
        )
        editable = await store.get_draft(draft.id)
        current = await store.lock_submission(draft.id, editable.version)
        with pytest.raises(DraftStoreError, match="active"):
            await store.reconcile_attempt(old.id, resolution)
        assert await store.get_attempt(old.id) == detached
        assert await store.get_attempt(current.id) == current
        assert set(store._held_attempt_locks) == {current.id}


@pytest.mark.asyncio
async def test_cancelled_attempt_read_rolls_back_snapshot_transaction(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with DraftStore(db_path) as store:
        draft = await store.create_draft(REVIEW, REVISION)
        attempt = await store.lock_submission(draft.id, draft.version)
        original = store._row_to_attempt

        async def cancel_read(
            db: aiosqlite.Connection, row: aiosqlite.Row
        ) -> SubmissionAttempt:
            raise asyncio.CancelledError

        monkeypatch.setattr(store, "_row_to_attempt", cancel_read)
        with pytest.raises(asyncio.CancelledError):
            await store.get_attempt(attempt.id)
        assert store._db is not None
        assert store._db.in_transaction is False
        monkeypatch.setattr(store, "_row_to_attempt", original)
        assert await store.get_attempt(attempt.id) == attempt


@pytest.mark.asyncio
async def test_owned_active_attempts_are_listed_only_on_request(
    db_path: Path,
) -> None:
    async with DraftStore(db_path) as owner, DraftStore(db_path) as observer:
        submitting_draft = await owner.create_draft(REVIEW, REVISION)
        partial_draft = await owner.create_draft(REVIEW, REVISION)
        unknown_draft = await owner.create_draft(REVIEW, REVISION)
        submitting = await owner.lock_submission(
            submitting_draft.id, submitting_draft.version
        )
        partial = await owner.lock_submission(partial_draft.id, partial_draft.version)
        partial = await owner.record_receipt(partial.id, "comment:1", "remote-1")
        unknown = await owner.lock_submission(unknown_draft.id, unknown_draft.version)
        unknown = await owner.mark_attempt_unknown(unknown.id)

        assert partial.state == DraftState.PARTIAL
        assert [item.id for item in await owner.list_recovery_attempts()] == [
            unknown.id
        ]
        owned = await owner.list_recovery_attempts(include_owned=True)
        assert sorted(str(item.id) for item in owned) == sorted(
            str(item) for item in (submitting.id, partial.id, unknown.id)
        )
        assert {item.id: item.state for item in owned} == {
            submitting.id: DraftState.SUBMITTING,
            partial.id: DraftState.PARTIAL,
            unknown.id: DraftState.UNKNOWN,
        }
        # Ownership is process-local, so another store never sees them.
        assert [
            item.id
            for item in await observer.list_recovery_attempts(include_owned=True)
        ] == [unknown.id]

        await owner.complete_submission(submitting.id)
        assert {
            item.id for item in await owner.list_recovery_attempts(include_owned=True)
        } == {partial.id, unknown.id}


def _age_draft(db_path: Path, draft_id: UUID, age: timedelta) -> None:
    """Move a stored draft's last change into the past."""
    stamp = (datetime.now(UTC) - age).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE drafts SET updated_at = ? WHERE id = ?", (stamp, str(draft_id))
        )


def _row_counts(db_path: Path, draft_id: UUID) -> tuple[int, int]:
    with sqlite3.connect(db_path) as connection:
        drafts = connection.execute(
            "SELECT count(*) FROM drafts WHERE id = ?", (str(draft_id),)
        ).fetchone()[0]
        attempts = connection.execute(
            "SELECT count(*) FROM submission_attempts WHERE draft_id = ?",
            (str(draft_id),),
        ).fetchone()[0]
    return drafts, attempts


async def _submitted(store: DraftStore) -> tuple[UUID, UUID]:
    draft = await store.create_draft(REVIEW, REVISION, make_content("sent"))
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.record_receipt(attempt.id, "review", "remote-1")
    await store.complete_submission(attempt.id)
    return draft.id, attempt.id


@pytest.mark.asyncio
async def test_submitted_draft_stays_readable_then_is_pruned_after_the_window(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft_id, attempt_id = await _submitted(store)

    # The attempt stays readable right after completion, for a status poll.
    assert (await store.get_attempt(attempt_id)).state is DraftState.SUBMITTED
    assert (await store.get_draft(draft_id)).state is DraftState.SUBMITTED
    await store.close()

    _age_draft(db_path, draft_id, timedelta(hours=23))
    await store.open()
    assert (await store.get_draft(draft_id)).state is DraftState.SUBMITTED
    await store.close()
    assert _row_counts(db_path, draft_id) == (1, 1)

    _age_draft(db_path, draft_id, timedelta(hours=25))
    await store.open()
    with pytest.raises(DraftNotFoundError):
        await store.get_draft(draft_id)
    with pytest.raises(DraftNotFoundError):
        await store.get_attempt(attempt_id)
    await store.close()
    assert _row_counts(db_path, draft_id) == (0, 0)
    with sqlite3.connect(db_path) as connection:
        receipts = connection.execute(
            "SELECT count(*) FROM submission_receipts WHERE attempt_id = ?",
            (str(attempt_id),),
        ).fetchone()[0]
    assert receipts == 0


@pytest.mark.asyncio
async def test_prune_never_touches_editable_in_progress_or_unknown_drafts(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    editable = await store.create_draft(REVIEW, REVISION, make_content("old"))
    old_revision = await store.create_draft(
        REVIEW, ReviewRevision("head-0", "base-0", "start-0"), DraftContent("text")
    )
    submitting = await store.create_draft(REVIEW, REVISION, make_content("lock"))
    await store.lock_submission(submitting.id, submitting.version)
    partial = await store.create_draft(REVIEW, REVISION, make_content("part"))
    partial_attempt = await store.lock_submission(partial.id, partial.version)
    await store.record_receipt(partial_attempt.id, "comment:one", "remote-1")
    unknown = await store.create_draft(REVIEW, REVISION, make_content("unknown"))
    unknown_attempt = await store.lock_submission(unknown.id, unknown.version)
    await store.mark_attempt_unknown(unknown_attempt.id)
    # A draft marked submitted while its latest attempt is still unknown is
    # inconsistent; pruning must leave it for recovery rather than delete it.
    mismatched = await store.create_draft(REVIEW, REVISION, make_content("odd"))
    mismatched_attempt = await store.lock_submission(mismatched.id, mismatched.version)
    await store.mark_attempt_unknown(mismatched_attempt.id)
    await store.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE drafts SET state = ?, active_attempt_id = NULL WHERE id = ?",
            (DraftState.SUBMITTED.value, str(mismatched.id)),
        )
    kept = (editable, old_revision, submitting, partial, unknown, mismatched)
    for draft in kept:
        _age_draft(db_path, draft.id, timedelta(days=400))

    reopened = DraftStore(db_path)
    await reopened.open()
    states = {draft.id: (await reopened.get_draft(draft.id)).state for draft in kept}
    await reopened.close()

    assert states == {
        editable.id: DraftState.EDITABLE,
        old_revision.id: DraftState.EDITABLE,
        submitting.id: DraftState.SUBMITTING,
        partial.id: DraftState.PARTIAL,
        unknown.id: DraftState.UNKNOWN,
        mismatched.id: DraftState.SUBMITTED,
    }
    for draft in (submitting, partial, unknown, mismatched):
        assert _row_counts(db_path, draft.id) == (1, 1)


@pytest.mark.asyncio
async def test_completion_prunes_expired_drafts_but_keeps_the_new_one(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    expired_id, _expired_attempt = await _submitted(store)
    await store.close()
    _age_draft(db_path, expired_id, timedelta(days=2))
    # Opening with a three-day window keeps it; the window shrinks before the
    # next completion, which then prunes it.
    store = DraftStore(db_path, submitted_retention=timedelta(days=3))
    await store.open()
    assert (await store.get_draft(expired_id)).state is DraftState.SUBMITTED
    store.submitted_retention = timedelta(days=1)

    fresh_id, fresh_attempt = await _submitted(store)

    with pytest.raises(DraftNotFoundError):
        await store.get_draft(expired_id)
    assert (await store.get_attempt(fresh_attempt)).state is DraftState.SUBMITTED
    assert (await store.get_draft(fresh_id)).state is DraftState.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_marked_submitted_reconciliation_keeps_the_attempt_readable(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, make_content())
    attempt = await store.lock_submission(draft.id, draft.version)
    await store.mark_attempt_unknown(attempt.id)

    reconciled = await store.reconcile_attempt(
        attempt.id, ReconciliationResolution.MARK_SUBMITTED
    )

    assert reconciled.state is DraftState.SUBMITTED
    assert (await store.get_attempt(attempt.id)).state is DraftState.SUBMITTED
    await store.close()
    _age_draft(db_path, draft.id, timedelta(hours=25))
    await store.open()
    with pytest.raises(DraftNotFoundError):
        await store.get_attempt(attempt.id)
    await store.close()


@pytest.mark.asyncio
async def test_configured_retention_window_decides_what_is_pruned(
    db_path: Path,
) -> None:
    store = DraftStore(db_path)
    await store.open()
    recent_id, _ = await _submitted(store)
    older_id, _ = await _submitted(store)
    await store.close()
    _age_draft(db_path, recent_id, timedelta(days=2))
    _age_draft(db_path, older_id, timedelta(days=8))

    week = DraftStore(db_path, submitted_retention=timedelta(days=7))
    await week.open()
    assert (await week.get_draft(recent_id)).state is DraftState.SUBMITTED
    with pytest.raises(DraftNotFoundError):
        await week.get_draft(older_id)
    await week.close()


@pytest.mark.asyncio
async def test_session_passes_the_configured_window_to_the_draft_store(
    tmp_path: Path, db_path: Path
) -> None:
    store = DraftStore(db_path)
    await store.open()
    recent_id, _ = await _submitted(store)
    older_id, _ = await _submitted(store)
    await store.close()
    _age_draft(db_path, recent_id, timedelta(days=2))
    _age_draft(db_path, older_id, timedelta(days=8))

    class _NoHosts:
        def active_hostnames(self) -> list[str]:
            return []

        async def close_all(self) -> None:
            return None

    session = ApplicationSession(
        config=Config(submitted_draft_retention=timedelta(days=7)),
        cache=CacheStore(db_path=tmp_path / "cache.db"),
        draft_db_path=db_path,
        forge_registry=_NoHosts(),  # type: ignore[arg-type]
    )
    await session.start()
    try:
        assert session.drafts.submitted_retention == timedelta(days=7)
        assert (await session.drafts.get_draft(recent_id)).id == recent_id
        with pytest.raises(DraftNotFoundError):
            await session.drafts.get_draft(older_id)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_falls_back_to_the_default_window_for_an_invalid_config(
    tmp_path: Path, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = DraftStore(db_path)
    await store.open()
    recent_id, _ = await _submitted(store)
    older_id, _ = await _submitted(store)
    await store.close()
    _age_draft(db_path, recent_id, timedelta(hours=2))
    _age_draft(db_path, older_id, timedelta(hours=25))

    class _NoHosts:
        def active_hostnames(self) -> list[str]:
            return []

        async def close_all(self) -> None:
            return None

    # Only a Config built in code can hold a window load_config would refuse.
    session = ApplicationSession(
        config=Config(submitted_draft_retention=timedelta(minutes=5)),
        cache=CacheStore(db_path=tmp_path / "cache.db"),
        draft_db_path=db_path,
        forge_registry=_NoHosts(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="tongs.services.session"):
        await session.start()
    try:
        assert session.drafts.submitted_retention == store_module.SUBMITTED_RETENTION
        assert (await session.drafts.get_draft(recent_id)).id == recent_id
        with pytest.raises(DraftNotFoundError):
            await session.drafts.get_draft(older_id)
        assert [record.getMessage() for record in caplog.records] == [
            "Ignoring an invalid submitted draft retention window"
        ]
    finally:
        await session.close()


@pytest.mark.parametrize(
    "window", [timedelta(0), timedelta(minutes=59), timedelta(days=366)]
)
def test_retention_outside_the_accepted_range_is_rejected(
    db_path: Path, window: timedelta
) -> None:
    with pytest.raises(ValueError, match="between 1 hour and 365 days"):
        DraftStore(db_path, submitted_retention=window)


@pytest.mark.asyncio
async def test_create_and_save_enforce_the_draft_limits(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    too_many = DraftContent(
        comments=tuple(
            GeneralDraftComment(uuid4(), "note") for _ in range(MAX_DRAFT_COMMENTS + 1)
        )
    )
    with pytest.raises(DraftLimitError, match="at most 200 comments"):
        await store.create_draft(REVIEW, REVISION, too_many)
    assert await store.list_drafts(review=REVIEW) == ()

    at_limit = DraftContent(
        body="\u00e9" * (MAX_DRAFT_BODY_BYTES // 2),
        comments=tuple(
            GeneralDraftComment(uuid4(), "tab\tand\nnewline")
            for _ in range(MAX_DRAFT_COMMENTS)
        ),
    )
    draft = await store.create_draft(REVIEW, REVISION, at_limit)

    for content, message in (
        (
            DraftContent(body="\u00e9" * (MAX_DRAFT_BODY_BYTES // 2) + "x"),
            "summary is over",
        ),
        (
            DraftContent(comments=(GeneralDraftComment(uuid4(), "x" * 70_000),)),
            "A comment is over the 65,536-byte limit",
        ),
        (DraftContent(body="bell\x07"), "control character"),
        (
            DraftContent(comments=(GeneralDraftComment(uuid4(), "cr\r"),)),
            "control character",
        ),
        (DraftContent(body="\ud800"), "cannot be stored"),
    ):
        with pytest.raises(DraftLimitError, match=message) as raised:
            await store.save_draft(
                draft.id, draft.version, content, current_revision=REVISION
            )
        assert "x" * 100 not in str(raised.value)

    stored = await store.get_draft(draft.id)
    assert stored.version == draft.version
    assert stored.content == at_limit
    await store.close()


@pytest.mark.asyncio
async def test_a_stored_draft_over_the_limits_still_loads(db_path: Path) -> None:
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(REVIEW, REVISION, DraftContent("short"))
    await store.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE drafts SET body = ? WHERE id = ?", ("x" * 70_000, str(draft.id))
        )

    await store.open()
    loaded = await store.get_draft(draft.id)
    listed = await store.list_drafts(review=REVIEW)
    with pytest.raises(DraftLimitError):
        await store.save_draft(
            draft.id, draft.version, loaded.content, current_revision=REVISION
        )
    trimmed = await store.save_draft(
        draft.id, draft.version, DraftContent("short again"), current_revision=REVISION
    )
    await store.close()

    assert len(loaded.body) == 70_000
    assert [item.id for item in listed] == [draft.id]
    assert trimmed.body == "short again"
