"""Failure-oriented tests for durable review draft submission."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from tongs.errors import AuthError, NetworkError, ValidationError
from tongs.forges.github import GitHubClient
from tongs.forges.models import (
    CIStatus,
    Discussion,
    ForgeHost,
    ForgeMutationResult,
    MRDetail,
    MRState,
    ReviewDecision,
    User,
)
from tongs.forges.models import (
    InlineComment as ForgeInlineComment,
)
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ForgeCapabilities,
    RawDiffSnapshot,
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.review_mutations import (
    MutationOutcome,
    MutationReceipt,
    MutationStatus,
    ReviewMutationService,
)
from tongs.services.review_submission import (
    ReviewSubmissionService,
    SubmissionOutcome,
    SubmissionStepKind,
)
from tongs.state.drafts import (
    DiffSide,
    DraftConflictError,
    DraftContent,
    DraftNotFoundError,
    DraftSnapshot,
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
    context_fingerprint,
)

REVISION = ReviewRevision("head-1", "base-1", "start-1")
REF = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 42)
PATCH = "@@ -10,2 +10,2 @@\n context\n-old\n+new"


def _snapshot(
    forge: ForgeType = ForgeType.GITHUB,
    revision: ReviewRevision | None = REVISION,
    *,
    batched_review: bool = True,
) -> ReviewSnapshot:
    hostname = "github.com" if forge is ForgeType.GITHUB else "gitlab.example.com"
    ref = ReviewRef(RepositoryRef(hostname, "acme/widgets"), 42)
    detail = MRDetail(
        forge_host=ForgeHost(hostname, forge, ""),
        repo_path="acme/widgets",
        local_path="",
        number=42,
        title="Review",
        author=User("alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        web_url="",
        head_sha=revision.head_sha if revision else "",
        base_sha=revision.base_sha if revision else "",
        start_sha=revision.start_sha if revision else None,
    )
    if revision is None:
        error = ServiceError(
            ServiceErrorCode.REVISION_UNAVAILABLE, "Revision unavailable."
        )
        return ReviewSnapshot(
            ref,
            detail,
            None,
            ForgeCapabilities(batched_review, True, True, True, True),
            error,
        )
    return ReviewSnapshot(
        ref,
        detail,
        revision,
        ForgeCapabilities(batched_review, True, True, True, True),
    )


def _anchor(revision: ReviewRevision = REVISION) -> InlineAnchor:
    return InlineAnchor(
        revision,
        "old.py",
        "new.py",
        None,
        11,
        DiffSide.NEW,
        context_fingerprint(("context", "new")),
    )


def _content(
    *comments: GeneralDraftComment | InlineDraftComment | ReplyDraftComment,
    body: str = "",
    verdict: DraftVerdict | None = None,
) -> DraftContent:
    return DraftContent(body, verdict, tuple(comments))


def _client(**overrides: object) -> SimpleNamespace:
    methods: dict[str, object] = {
        "add_comment": AsyncMock(
            return_value=ForgeMutationResult("general-remote", "general-remote")
        ),
        "create_inline_comment": AsyncMock(
            return_value=ForgeMutationResult(
                "inline-remote", "inline-note", "inline-thread"
            )
        ),
        "reply_to_discussion": AsyncMock(
            return_value=ForgeMutationResult("reply-remote", "reply-note", "thread-9")
        ),
        "submit_review": AsyncMock(return_value=ForgeMutationResult("review-remote")),
        "invalidate_review_reads": AsyncMock(return_value=True),
        "supports_thread_resolution": True,
        "supports_batched_review": True,
    }
    methods.update(overrides)
    return SimpleNamespace(**methods)


def _discussion() -> Discussion:
    root = ForgeInlineComment(
        "root-7", User("alice"), "root", datetime.now(UTC), "new.py"
    )
    return Discussion("thread-9", True, root, resolvable=True)


def _mutation_service(
    client: SimpleNamespace,
    snapshot: ReviewSnapshot,
    *,
    timeout: float = 0.1,
    emit_change: Callable[[ServiceEventKind, ReviewRef, ReviewRevision | None], None]
    | None = None,
) -> ReviewMutationService:
    if snapshot.detail.forge_host.forge_type is ForgeType.GITHUB:
        change = {
            "filename": "new.py",
            "previous_filename": "old.py",
            "status": "modified",
            "patch": PATCH,
        }
    else:
        change = {"old_path": "old.py", "new_path": "new.py", "diff": PATCH}
    return ReviewMutationService(
        get_client=AsyncMock(return_value=client),
        get_review=AsyncMock(return_value=snapshot),
        get_diff=AsyncMock(
            return_value=RawDiffSnapshot(snapshot.ref, REVISION, (change,))
        ),
        get_discussions=AsyncMock(return_value=(_discussion(),)),
        emit_change=emit_change or Mock(),
        timeout=timeout,
    )


async def _services(
    db_path: Path,
    content: DraftContent,
    *,
    forge: ForgeType = ForgeType.GITHUB,
    client: SimpleNamespace | None = None,
    snapshot: ReviewSnapshot | None = None,
    emit_change: Callable[[ServiceEventKind, ReviewRef, ReviewRevision | None], None]
    | None = None,
    draft_revision: ReviewRevision = REVISION,
) -> tuple[
    DraftStore,
    ReviewSubmissionService,
    SimpleNamespace,
    ReviewSnapshot,
    object,
]:
    current = snapshot or _snapshot(forge)
    actual_client = client or _client()
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(current.ref, draft_revision, content)
    mutations = _mutation_service(actual_client, current, emit_change=emit_change)
    service = ReviewSubmissionService(
        store=store,
        mutations=mutations,
        get_review=AsyncMock(return_value=current),
    )
    return store, service, actual_client, current, draft


@pytest.mark.asyncio
async def test_github_inline_content_uses_one_native_revision_bound_batch(
    tmp_path: Path,
) -> None:
    inline = InlineDraftComment(uuid4(), "inline", _anchor())
    store, service, client, _snapshot_value, draft = await _services(
        tmp_path / "drafts.db",
        _content(inline, body="summary", verdict=DraftVerdict.APPROVE),
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert progress.atomic is True
    assert [(step.id, step.kind, step.comment_ids) for step in progress.steps] == [
        ("batch", SubmissionStepKind.GITHUB_REVIEW, (inline.id,))
    ]
    client.submit_review.assert_awaited_once_with(
        "acme/widgets",
        42,
        ReviewDecision.APPROVED,
        "summary",
        [
            {
                "path": "new.py",
                "line": 11,
                "side": "RIGHT",
                "body": "inline",
            }
        ],
        head_sha="head-1",
    )
    assert progress.receipts[0].remote_id == "review-remote"
    await store.close()


@pytest.mark.asyncio
async def test_completed_and_reconciled_submissions_leave_no_lock_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "drafts.db"
    lock_dir = tmp_path / "drafts.db.locks"
    store, service, _, _, draft = await _services(
        path, _content(GeneralDraftComment(uuid4(), "comment"))
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert list(lock_dir.iterdir()) == []
    await store.close()

    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)
    assert unknown.outcome is SubmissionOutcome.UNKNOWN
    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.MARK_SUBMITTED
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert list(lock_dir.iterdir()) == []
    await store.close()


@pytest.mark.asyncio
async def test_github_mixed_content_preserves_order_then_one_final_review(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    async def general(*_args: object) -> ForgeMutationResult:
        order.append("general")
        return ForgeMutationResult("general", "general")

    async def inline(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("inline")
        return ForgeMutationResult("inline", "inline", "inline-thread")

    async def reply(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("reply")
        return ForgeMutationResult("reply", "reply", "thread-9")

    async def review(*_args: object, **_kwargs: object) -> ForgeMutationResult:
        order.append("review")
        return ForgeMutationResult("review")

    client = _client(
        add_comment=AsyncMock(side_effect=general),
        create_inline_comment=AsyncMock(side_effect=inline),
        reply_to_discussion=AsyncMock(side_effect=reply),
        submit_review=AsyncMock(side_effect=review),
    )
    comments = (
        GeneralDraftComment(uuid4(), "general"),
        InlineDraftComment(uuid4(), "inline", _anchor()),
        ReplyDraftComment(uuid4(), "reply", "thread-9"),
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(*comments, body="summary", verdict=DraftVerdict.REQUEST_CHANGES),
        client=client,
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert progress.atomic is False
    assert order == ["general", "inline", "reply", "review"]
    client.add_comment.assert_awaited_once_with("acme/widgets", 42, "general")
    client.submit_review.assert_awaited_once_with(
        "acme/widgets",
        42,
        ReviewDecision.CHANGES_REQUESTED,
        "summary",
        None,
        head_sha="head-1",
    )
    await store.close()


@pytest.mark.asyncio
async def test_github_mixed_comment_only_does_not_create_empty_review(
    tmp_path: Path,
) -> None:
    comments = (
        GeneralDraftComment(uuid4(), "general"),
        InlineDraftComment(uuid4(), "inline", _anchor()),
    )
    store, service, client, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(*comments, verdict=DraftVerdict.COMMENT),
    )

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    assert [step.kind for step in progress.steps] == [
        SubmissionStepKind.GENERAL_COMMENT,
        SubmissionStepKind.INLINE_COMMENT,
    ]
    client.submit_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_gitlab_known_comment_rejection_pauses_before_verdict_and_explicit_resume(
    tmp_path: Path,
) -> None:
    inline = AsyncMock(side_effect=AuthError("secret"))
    client = _client(create_inline_comment=inline)
    snapshot = _snapshot(ForgeType.GITLAB)
    comment = InlineDraftComment(uuid4(), "inline", _anchor())
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(comment, verdict=DraftVerdict.APPROVE),
        client=client,
        snapshot=snapshot,
    )

    paused = await service.start(draft.id, draft.version)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.code == "authentication_failed"
    client.submit_review.assert_not_awaited()
    inline.side_effect = None
    inline.return_value = ForgeMutationResult("inline", "inline", "thread")

    submitted = await service.resume(paused.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert inline.await_count == 2
    client.submit_review.assert_awaited_once()
    recovered = await store.get_attempt(paused.attempt_id)
    assert [
        (item.ordinal, item.step_id) for item in recovered.retry_authorizations
    ] == [(1, paused.failure.step_id)]
    await store.close()


@pytest.mark.asyncio
async def test_gitlab_request_changes_fails_preflight_without_remote_write(
    tmp_path: Path,
) -> None:
    client = _client()
    snapshot = _snapshot(ForgeType.GITLAB)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(
            GeneralDraftComment(uuid4(), "general"),
            verdict=DraftVerdict.REQUEST_CHANGES,
        ),
        client=client,
        snapshot=snapshot,
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code is ServiceErrorCode.UNSUPPORTED
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    client.add_comment.assert_not_awaited()
    client.submit_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_whole_plan_anchor_preflight_precedes_first_remote_write(
    tmp_path: Path,
) -> None:
    general = GeneralDraftComment(uuid4(), "general first")
    invalid_anchor = InlineAnchor(
        REVISION,
        "old.py",
        "new.py",
        None,
        999,
        DiffSide.NEW,
        context_fingerprint(("missing",)),
    )
    inline = InlineDraftComment(uuid4(), "invalid inline", invalid_anchor)
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(general, inline), client=client
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code is ServiceErrorCode.INVALID_INPUT
    client.add_comment.assert_not_awaited()
    client.create_inline_comment.assert_not_awaited()
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    await store.close()


@pytest.mark.asyncio
async def test_get_reconstructs_durable_plan_without_network_read(
    tmp_path: Path,
) -> None:
    comment = GeneralDraftComment(uuid4(), "comment")
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(comment)
    )
    submitted = await service.start(draft.id, draft.version)
    service._get_review = AsyncMock(
        side_effect=AssertionError("unexpected network read")
    )

    current = await service.get(submitted.attempt_id)

    assert current.steps == submitted.steps
    assert current.atomic == submitted.atomic
    assert current.plan_available is True
    service._get_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_get_distinguishes_recovered_attempt_without_a_plan(
    tmp_path: Path,
) -> None:
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
    )
    attempt = await store.lock_submission(draft.id, draft.version)
    service._get_review = AsyncMock(
        side_effect=AssertionError("unexpected network read")
    )

    current = await service.get(attempt.id)

    assert current.steps == ()
    assert current.plan_available is False
    service._get_review.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "draft_revision", "code"),
    [
        (_snapshot(revision=None), REVISION, ServiceErrorCode.REVISION_UNAVAILABLE),
        (
            _snapshot(revision=ReviewRevision("new-head", "base-1", "start-1")),
            REVISION,
            ServiceErrorCode.REVISION_CHANGED,
        ),
        (
            _snapshot(
                ForgeType.GITLAB, revision=ReviewRevision("head-1", "base-1", None)
            ),
            ReviewRevision("head-1", "base-1", None),
            ServiceErrorCode.REVISION_UNAVAILABLE,
        ),
    ],
    ids=["missing", "changed", "gitlab-without-start"],
)
async def test_incomplete_or_changed_revision_unlocks_without_dispatch(
    tmp_path: Path,
    snapshot: ReviewSnapshot,
    draft_revision: ReviewRevision,
    code: ServiceErrorCode,
) -> None:
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / f"{snapshot.ref.repository.hostname}.db",
        _content(GeneralDraftComment(uuid4(), "general")),
        client=client,
        snapshot=snapshot,
        draft_revision=draft_revision,
    )

    with pytest.raises(ServiceError) as raised:
        await service.start(draft.id, draft.version)

    assert raised.value.code is code
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    client.add_comment.assert_not_awaited()
    await store.close()


@pytest.mark.asyncio
async def test_unknown_step_requires_reconciliation_and_never_repeats_receipt(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    add = AsyncMock(
        side_effect=[
            ForgeMutationResult("first", "first"),
            NetworkError("timeout"),
            ForgeMutationResult("second", "second"),
        ]
    )
    client = _client(add_comment=add)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(first, second), client=client
    )

    unknown = await service.start(draft.id, draft.version)

    assert unknown.outcome is SubmissionOutcome.UNKNOWN
    assert unknown.completed_step_ids == (f"comment:{first.id.hex}",)
    assert unknown.unknown_step_ids == (f"comment:{second.id.hex}",)
    durable = await service.get(unknown.attempt_id)
    assert durable.steps == unknown.steps
    assert durable.resync_required is True
    assert durable.plan_available is True
    with pytest.raises(ServiceError) as raised:
        await service.resume(unknown.attempt_id)
    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert add.await_count == 2

    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETRY_REMAINING
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 3
    assert [call.args[2] for call in add.await_args_list] == [
        "first",
        "second",
        "second",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_return_editable_retains_ambiguous_remainder_and_frozen_evidence(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    add = AsyncMock(
        side_effect=[ForgeMutationResult("first", "first"), NetworkError("timeout")]
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(first, second, body="summary", verdict=DraftVerdict.APPROVE),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)

    editable = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )
    saved = await store.get_draft(draft.id)
    historical = await store.get_attempt(unknown.attempt_id)

    assert editable.outcome is SubmissionOutcome.EDITABLE
    assert saved.comments == (second,)
    assert saved.body == "summary"
    assert saved.verdict is DraftVerdict.APPROVE
    assert historical.snapshot.comments == (first, second)
    assert historical.receipts[0].step_id == f"comment:{first.id.hex}"
    assert historical.unknown_outcomes[0].step_id == f"comment:{second.id.hex}"
    await store.close()


@pytest.mark.asyncio
async def test_mark_submitted_records_explicit_decision_without_replay(
    tmp_path: Path,
) -> None:
    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)

    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.MARK_SUBMITTED
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 1
    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_confirmed_remote_result_with_receipt_write_failure_becomes_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, client, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
    )

    async def fail_receipt(*_args: object, **_kwargs: object) -> object:
        raise DraftStoreError("disk full")

    monkeypatch.setattr(store, "record_receipt", fail_receipt)
    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    assert progress.failure and progress.failure.code == "storage_failed"
    assert progress.unknown_step_ids == (progress.steps[0].id,)
    client.add_comment.assert_awaited_once()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_failure", ["cache", "event"])
async def test_known_resync_requirement_survives_completion_get_and_reopen(
    tmp_path: Path, refresh_failure: str
) -> None:
    path = tmp_path / "drafts.db"
    result = ForgeMutationResult(
        "general-remote",
        "general-remote",
        cache_invalidated=refresh_failure != "cache",
    )
    client = _client(add_comment=AsyncMock(return_value=result))
    emit_change = (
        Mock(side_effect=RuntimeError("event delivery failed"))
        if refresh_failure == "event"
        else Mock()
    )
    store, service, _, snapshot, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
        emit_change=emit_change,
    )

    submitted = await service.start(draft.id, draft.version)
    current = await service.get(submitted.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert submitted.resync_required is True
    assert current.resync_required is True
    assert current.receipts[0].resync_required is True
    await store.close()

    reopened_store = DraftStore(path)
    await reopened_store.open()
    reopened_service = ReviewSubmissionService(
        store=reopened_store,
        mutations=_mutation_service(_client(), snapshot),
        get_review=AsyncMock(side_effect=AssertionError("unexpected network read")),
    )

    reopened = await reopened_service.get(submitted.attempt_id)

    assert reopened.outcome is SubmissionOutcome.SUBMITTED
    assert reopened.resync_required is True
    assert reopened.receipts[0].resync_required is True
    reopened_service._get_review.assert_not_awaited()
    await reopened_store.close()


@pytest.mark.asyncio
async def test_known_resync_requirement_survives_later_rejection(
    tmp_path: Path,
) -> None:
    first = GeneralDraftComment(uuid4(), "first")
    second = GeneralDraftComment(uuid4(), "second")
    client = _client(
        add_comment=AsyncMock(
            side_effect=[
                ForgeMutationResult(
                    "first-remote", "first-remote", cache_invalidated=False
                ),
                AuthError("rejected"),
            ]
        )
    )
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", _content(first, second), client=client
    )

    paused = await service.start(draft.id, draft.version)
    current = await service.get(paused.attempt_id)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.code == "authentication_failed"
    assert paused.resync_required is True
    assert current.resync_required is True
    assert current.receipts[0].resync_required is True
    await store.close()


@pytest.mark.asyncio
async def test_failed_final_transition_retains_ownership_for_same_session_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client()
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    original = store._transition_owned
    failed = False

    async def fail_first_completion(
        attempt_id: object, target: DraftState, **kwargs: object
    ) -> object:
        nonlocal failed
        if target is DraftState.SUBMITTED and not failed:
            failed = True
            raise DraftStoreError("temporary final write failure")
        return await original(attempt_id, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_transition_owned", fail_first_completion)

    paused = await service.start(draft.id, draft.version)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.retryable is True
    assert paused.attempt_id in store._held_attempt_locks

    submitted = await service.resume(paused.attempt_id)

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    client.add_comment.assert_awaited_once()
    assert paused.attempt_id not in store._held_attempt_locks
    await store.close()


@pytest.mark.asyncio
async def test_restart_recovers_failed_final_transition_without_remote_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    client = _client()
    store, service, _, snapshot, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    original = store._transition_owned
    failed = False

    async def fail_first_completion(
        attempt_id: object, target: DraftState, **kwargs: object
    ) -> object:
        nonlocal failed
        if target is DraftState.SUBMITTED and not failed:
            failed = True
            raise DraftStoreError("temporary final write failure")
        return await original(attempt_id, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_transition_owned", fail_first_completion)
    paused = await service.start(draft.id, draft.version)
    await store.close()

    restarted_store = DraftStore(path)
    await restarted_store.open()
    recovered = await restarted_store.recover_incomplete_attempts()
    restarted_service = ReviewSubmissionService(
        store=restarted_store,
        mutations=_mutation_service(client, snapshot),
        get_review=AsyncMock(return_value=snapshot),
    )

    assert recovered[0].id == paused.attempt_id
    assert recovered[0].state is DraftState.UNKNOWN
    submitted = await restarted_service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETRY_REMAINING
    )

    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    client.add_comment.assert_awaited_once()
    await restarted_store.close()


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_returns_durable_unknown_without_replay(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def hang(*_args: object) -> ForgeMutationResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = _client(add_comment=AsyncMock(side_effect=hang))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    task = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    task.cancel()
    progress = await task

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    assert client.add_comment.await_count == 1
    with pytest.raises(ServiceError):
        await service.resume(progress.attempt_id)
    assert client.add_comment.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_submission_and_rejects_new_work(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    async def hang(*_args: object) -> ForgeMutationResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=AsyncMock(side_effect=hang)),
    )
    owner = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    await service.close()
    progress = await owner

    assert progress.outcome is SubmissionOutcome.UNKNOWN
    with pytest.raises(ServiceError) as raised:
        await service.get(progress.attempt_id)
    assert raised.value.code is ServiceErrorCode.CLOSED
    assert service._active_tasks == set()
    await store.close()


@pytest.mark.asyncio
async def test_close_retries_cancellation_of_submission_owner(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancellations = 0

    class FirstCancelResistantMutations:
        async def validate(
            self, _command: object, *, diff_scope: object = None
        ) -> None:
            return None

        async def execute(
            self, _command: object, *, diff_scope: object = None
        ) -> object:
            nonlocal cancellations
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations += 1
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations += 1
                    raise
            raise AssertionError("unreachable")

    async with DraftStore(tmp_path / "drafts.db") as store:
        draft = await store.create_draft(
            REF, REVISION, _content(GeneralDraftComment(uuid4(), "comment"))
        )
        service = ReviewSubmissionService(
            store=store,
            mutations=FirstCancelResistantMutations(),  # type: ignore[arg-type]
            get_review=AsyncMock(return_value=_snapshot()),
            # The second cancellation persists recovery through real SQLite I/O.
            # A 10 ms disk/scheduler deadline is unreliable on shared runners.
            close_timeout=1.0,
        )
        owner = asyncio.create_task(service.start(draft.id, draft.version))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await service.close()

            with pytest.raises(asyncio.CancelledError):
                await owner
            assert cancellations == 2
            attempt = (await store.list_recovery_attempts())[0]
            assert attempt.unknown_outcomes[0].step_id.startswith("comment:")
            assert service._active_tasks == set()
        finally:
            # A failed assertion or close must not strand the SQLite worker.
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
            await service.close()


@pytest.mark.asyncio
async def test_failed_close_can_finish_after_uncooperative_owner_settles(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class ResistantMutations:
        async def validate(
            self, _command: object, *, diff_scope: object = None
        ) -> None:
            return None

        async def execute(
            self, command: object, *, diff_scope: object = None
        ) -> MutationOutcome:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            operation_id = command.operation_id  # type: ignore[attr-defined]
            return MutationOutcome(
                operation_id,
                MutationStatus.KNOWN,
                MutationReceipt(operation_id, "known"),
            )

    store = DraftStore(tmp_path / "drafts.db")
    await store.open()
    draft = await store.create_draft(
        REF, REVISION, _content(GeneralDraftComment(uuid4(), "comment"))
    )
    service = ReviewSubmissionService(
        store=store,
        mutations=ResistantMutations(),  # type: ignore[arg-type]
        get_review=AsyncMock(return_value=_snapshot()),
        close_timeout=0.01,
    )
    owner = asyncio.create_task(service.start(draft.id, draft.version))
    await entered.wait()

    with pytest.raises(RuntimeError, match="did not stop"):
        await service.close()
    assert owner in service._active_tasks

    release.set()
    assert (await owner).outcome is SubmissionOutcome.SUBMITTED
    await service.close()
    assert service._active_tasks == set()
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_explicit_resume_advances_rejected_step_once(
    tmp_path: Path,
) -> None:
    add = AsyncMock(side_effect=AuthError("denied"))
    client = _client(add_comment=add)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    paused = await service.start(draft.id, draft.version)
    add.side_effect = None
    add.return_value = ForgeMutationResult("known", "known")

    first, second = await asyncio.gather(
        service.resume(paused.attempt_id), service.resume(paused.attempt_id)
    )

    assert first.outcome is SubmissionOutcome.SUBMITTED
    assert second.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 2
    recovered = await store.get_attempt(paused.attempt_id)
    assert len(recovered.retry_authorizations) == 1
    await store.close()


@pytest.mark.asyncio
async def test_second_start_during_submission_is_refused_and_posts_once(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_comment(*_args: object) -> ForgeMutationResult:
        entered.set()
        await release.wait()
        return ForgeMutationResult("known", "known")

    client = _client(add_comment=AsyncMock(side_effect=blocked_comment))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=client,
    )
    first = asyncio.create_task(service.start(draft.id, draft.version))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # A second start must be refused at once, never join the attempt in flight.
        with pytest.raises((DraftConflictError, DraftStateError)):
            await asyncio.wait_for(service.start(draft.id, draft.version), 5)
    finally:
        release.set()
        submitted = await first
        await store.close()
    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert client.add_comment.await_count == 1


def _assert_draft_content(saved: DraftSnapshot, content: DraftContent) -> None:
    assert saved.body == content.body
    assert saved.verdict is content.verdict
    assert saved.comments == content.comments


@pytest.mark.asyncio
async def test_github_self_approval_422_returns_to_editing_without_another_request(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            422,
            json={
                "message": "Unprocessable Entity",
                "errors": ["Can not approve your own pull request"],
            },
        )

    content = _content(
        InlineDraftComment(uuid4(), "inline note", _anchor()),
        body="looks good",
        verdict=DraftVerdict.APPROVE,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    ) as http:
        client = GitHubClient(
            ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com"), http
        )
        store, service, _, _, draft = await _services(
            tmp_path / "drafts.db", content, client=client
        )
        paused = await service.start(draft.id, draft.version)

        assert paused.outcome is SubmissionOutcome.PAUSED
        assert paused.failure is not None
        assert paused.failure.retryable is False
        assert requests == [("POST", "/repos/acme/widgets/pulls/42/reviews")]
        assert store.holds_attempt(paused.attempt_id)

        editable = await service.reconcile(
            paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
        )

        assert editable.outcome is SubmissionOutcome.EDITABLE
        assert editable.attempt_id == paused.attempt_id
        assert editable.draft_id == draft.id
        assert editable.frozen_version == draft.version
        assert len(requests) == 1
    saved = await store.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    assert saved.version == draft.version + 2
    _assert_draft_content(saved, content)
    assert not store.holds_attempt(paused.attempt_id)
    # The rejected attempt never reached the forge, so nothing durable remains.
    with pytest.raises(DraftNotFoundError):
        await store.get_attempt(paused.attempt_id)
    await store.close()


@pytest.mark.asyncio
async def test_never_dispatched_paused_attempt_returns_to_editing_by_cancelling(
    tmp_path: Path,
) -> None:
    content = _content(GeneralDraftComment(uuid4(), "comment"), body="summary")
    store, service, client, _, draft = await _services(tmp_path / "drafts.db", content)
    store.begin_dispatch = AsyncMock(  # type: ignore[method-assign]
        side_effect=DraftStoreError("journal unavailable")
    )
    paused = await service.start(draft.id, draft.version)
    del store.begin_dispatch

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.failure and paused.failure.code == "storage_failed"

    editable = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert editable.outcome is SubmissionOutcome.EDITABLE
    saved = await store.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    _assert_draft_content(saved, content)
    client.add_comment.assert_not_awaited()
    client.submit_review.assert_not_awaited()
    with pytest.raises(DraftNotFoundError):
        await store.get_attempt(paused.attempt_id)
    await store.close()


@pytest.mark.asyncio
async def test_receipt_bearing_paused_attempt_returns_only_editable_remainder(
    tmp_path: Path,
) -> None:
    comment = GeneralDraftComment(uuid4(), "confirmed comment")
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 400"))
    client = _client(submit_review=verdict)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(comment, body="summary", verdict=DraftVerdict.APPROVE),
        forge=ForgeType.GITLAB,
        client=client,
    )
    paused = await service.start(draft.id, draft.version)

    assert paused.outcome is SubmissionOutcome.PAUSED
    assert paused.completed_step_ids == (f"comment:{comment.id.hex}", "body")
    assert client.add_comment.await_count == 2
    assert verdict.await_count == 1

    editable = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert editable.outcome is SubmissionOutcome.EDITABLE
    saved = await store.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    assert saved.comments == ()
    assert saved.body == ""
    assert saved.verdict is DraftVerdict.APPROVE
    historical = await store.get_attempt(paused.attempt_id)
    assert historical.snapshot.comments == (comment,)
    assert [receipt.step_id for receipt in historical.receipts] == [
        f"comment:{comment.id.hex}",
        "body",
    ]
    assert [item.resolution for item in historical.reconciliations] == [
        ReconciliationResolution.RETURN_EDITABLE
    ]
    assert historical.unknown_outcomes == ()
    assert client.add_comment.await_count == 2
    assert verdict.await_count == 1
    assert not store.holds_attempt(paused.attempt_id)
    await store.close()


@pytest.mark.asyncio
async def test_paused_return_to_editing_keeps_journaled_dispatch_as_unknown(
    tmp_path: Path,
) -> None:
    content = _content(body="summary", verdict=DraftVerdict.APPROVE)
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 422"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db", content, client=_client(submit_review=verdict)
    )
    paused = await service.start(draft.id, draft.version)
    # A journaled dispatch whose remote result is not known must survive.
    await store.begin_dispatch(paused.attempt_id, "batch", "journaled-op")

    editable = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert editable.outcome is SubmissionOutcome.EDITABLE
    assert editable.unknown_step_ids == ("batch",)
    historical = await store.get_attempt(paused.attempt_id)
    assert [item.step_id for item in historical.unknown_outcomes] == ["batch"]
    saved = await store.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    _assert_draft_content(saved, content)
    assert verdict.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_failed_second_transition_leaves_a_valid_unknown_attempt(
    tmp_path: Path,
) -> None:
    comment = GeneralDraftComment(uuid4(), "confirmed comment")
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 400"))
    client = _client(submit_review=verdict)
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(comment, verdict=DraftVerdict.APPROVE),
        forge=ForgeType.GITLAB,
        client=client,
    )
    paused = await service.start(draft.id, draft.version)
    store.reconcile_attempt = AsyncMock(  # type: ignore[method-assign]
        side_effect=DraftStoreError("disk full")
    )

    stopped = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )
    del store.reconcile_attempt

    assert stopped.outcome is SubmissionOutcome.UNKNOWN
    assert stopped.failure and stopped.failure.code == "storage_failed"
    assert (await store.get_attempt(paused.attempt_id)).state is DraftState.UNKNOWN
    assert (await store.get_draft(draft.id)).state is DraftState.UNKNOWN
    assert [item.id for item in await store.list_recovery_attempts()] == [
        paused.attempt_id
    ]

    editable = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert editable.outcome is SubmissionOutcome.EDITABLE
    saved = await store.get_draft(draft.id)
    assert saved.comments == ()
    assert saved.verdict is DraftVerdict.APPROVE
    assert client.add_comment.await_count == 1
    assert verdict.await_count == 1
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution",
    [ReconciliationResolution.RETRY_REMAINING, ReconciliationResolution.MARK_SUBMITTED],
)
async def test_paused_attempt_admits_only_return_to_editing(
    tmp_path: Path, resolution: ReconciliationResolution
) -> None:
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 422"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(verdict=DraftVerdict.APPROVE),
        client=_client(submit_review=verdict),
    )
    paused = await service.start(draft.id, draft.version)

    with pytest.raises(ServiceError) as raised:
        await service.reconcile(paused.attempt_id, resolution)

    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTING
    assert store.holds_attempt(paused.attempt_id)
    assert verdict.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_paused_attempt_owned_by_another_process_is_not_returned(
    tmp_path: Path,
) -> None:
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 422"))
    store, service, _, snapshot, draft = await _services(
        tmp_path / "drafts.db",
        _content(verdict=DraftVerdict.APPROVE),
        client=_client(submit_review=verdict),
    )
    paused = await service.start(draft.id, draft.version)
    other_store = DraftStore(tmp_path / "drafts.db")
    await other_store.open()
    other = ReviewSubmissionService(
        store=other_store,
        mutations=_mutation_service(_client(), snapshot),
        get_review=AsyncMock(return_value=snapshot),
    )

    with pytest.raises(ServiceError) as raised:
        await other.reconcile(
            paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
        )

    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert "another tongs process" in raised.value.message
    assert not other_store.holds_attempt(paused.attempt_id)
    assert store.holds_attempt(paused.attempt_id)
    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTING
    await other_store.close()
    await store.close()


async def _cancel_while_gated(
    store: DraftStore,
    method: str,
    call: Callable[[], object],
) -> asyncio.Task[object]:
    """Cancel a service call while one store transition is in flight.

    The real transition commits, then its return is held at a gate until the
    caller's cancellation is delivered, so the test observes what the service
    does with a transition that committed after its caller went away.
    """
    real = getattr(store, method)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated(*args: object, **kwargs: object) -> object:
        result = await real(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    setattr(store, method, gated)
    task = asyncio.ensure_future(call())  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    delattr(store, method)
    return task


@pytest.mark.asyncio
async def test_cancel_inside_lock_returns_draft_to_editing_and_propagates(
    tmp_path: Path,
) -> None:
    content = _content(GeneralDraftComment(uuid4(), "comment"), body="summary")
    store, service, client, _, draft = await _services(tmp_path / "drafts.db", content)

    task = await _cancel_while_gated(
        store,
        "lock_submission",
        lambda: service.start(draft.id, draft.version),
    )

    assert task.cancelled()
    saved = await store.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    _assert_draft_content(saved, content)
    assert await store.list_recovery_attempts(include_owned=True) == ()
    client.add_comment.assert_not_awaited()
    client.submit_review.assert_not_awaited()
    progress = await service.start(saved.id, saved.version)
    assert progress.outcome is SubmissionOutcome.SUBMITTED
    await store.close()


@pytest.mark.asyncio
async def test_cancel_inside_lock_with_failed_cancel_parks_attempt_unknown(
    tmp_path: Path,
) -> None:
    content = _content(GeneralDraftComment(uuid4(), "comment"))
    store, service, client, _, draft = await _services(tmp_path / "drafts.db", content)
    store.cancel_submission = AsyncMock(  # type: ignore[method-assign]
        side_effect=DraftStoreError("disk full")
    )

    await _cancel_while_gated(
        store,
        "lock_submission",
        lambda: service.start(draft.id, draft.version),
    )
    del store.cancel_submission

    (attempt,) = await store.list_recovery_attempts(include_owned=True)
    assert attempt.state is DraftState.UNKNOWN
    assert not store.holds_attempt(attempt.id)
    assert (await store.get_draft(draft.id)).state is DraftState.UNKNOWN
    client.add_comment.assert_not_awaited()
    editable = await service.reconcile(
        attempt.id, ReconciliationResolution.RETURN_EDITABLE
    )
    assert editable.outcome is SubmissionOutcome.EDITABLE
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "draft_state"),
    [
        (ReconciliationResolution.RETRY_REMAINING, DraftState.UNKNOWN),
        (ReconciliationResolution.RETURN_EDITABLE, DraftState.EDITABLE),
        (ReconciliationResolution.MARK_SUBMITTED, DraftState.SUBMITTED),
    ],
)
async def test_cancel_inside_reconcile_never_leaves_an_owned_submitting_attempt(
    tmp_path: Path,
    resolution: ReconciliationResolution,
    draft_state: DraftState,
) -> None:
    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)
    assert unknown.outcome is SubmissionOutcome.UNKNOWN

    task = await _cancel_while_gated(
        store,
        "reconcile_attempt",
        lambda: service.reconcile(unknown.attempt_id, resolution),
    )

    assert task.cancelled()
    assert not store.holds_attempt(unknown.attempt_id)
    assert (await store.get_draft(draft.id)).state is draft_state
    historical = await store.get_attempt(unknown.attempt_id)
    assert [item.resolution for item in historical.reconciliations] == [resolution]
    assert add.await_count == 1
    if draft_state is DraftState.UNKNOWN:
        assert historical.state is DraftState.UNKNOWN
        assert [item.id for item in await store.list_recovery_attempts()] == [
            unknown.attempt_id
        ]
    await store.close()


@pytest.mark.asyncio
async def test_retry_remaining_whose_preflight_fails_returns_to_unknown(
    tmp_path: Path,
) -> None:
    add = AsyncMock(
        side_effect=[NetworkError("timeout"), ForgeMutationResult("done", "done")]
    )
    client = _client(add_comment=add)
    current = _snapshot()
    store = DraftStore(tmp_path / "drafts.db")
    await store.open()
    draft = await store.create_draft(
        current.ref, REVISION, _content(GeneralDraftComment(uuid4(), "comment"))
    )
    get_review = AsyncMock(return_value=current)
    service = ReviewSubmissionService(
        store=store,
        mutations=_mutation_service(client, current),
        get_review=get_review,
    )
    unknown = await service.start(draft.id, draft.version)
    get_review.return_value = _snapshot(
        revision=ReviewRevision("head-2", "base-1", "start-1")
    )

    with pytest.raises(ServiceError) as raised:
        await service.reconcile(
            unknown.attempt_id, ReconciliationResolution.RETRY_REMAINING
        )

    assert raised.value.code is ServiceErrorCode.REVISION_CHANGED
    assert not store.holds_attempt(unknown.attempt_id)
    assert (await store.get_attempt(unknown.attempt_id)).state is DraftState.UNKNOWN
    assert (await store.get_draft(draft.id)).state is DraftState.UNKNOWN
    assert [item.id for item in await store.list_recovery_attempts()] == [
        unknown.attempt_id
    ]
    assert add.await_count == 1

    get_review.return_value = current
    submitted = await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETRY_REMAINING
    )
    assert submitted.outcome is SubmissionOutcome.SUBMITTED
    assert add.await_count == 2
    await store.close()


def _persist_anchor(db_path: Path, draft_id: object, **changes: object) -> None:
    """Rewrite the stored inline anchors of one draft row in place."""
    with sqlite3.connect(db_path) as db:
        (raw,) = db.execute(
            "SELECT comments_json FROM drafts WHERE id = ?", (str(draft_id),)
        ).fetchone()
        comments = json.loads(raw)
        for comment in comments:
            if comment["kind"] == "inline":
                comment["anchor"].update(changes)
        db.execute(
            "UPDATE drafts SET comments_json = ? WHERE id = ?",
            (json.dumps(comments), str(draft_id)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False], ids=["matching", "mismatched"])
async def test_persisted_stale_anchor_is_judged_by_its_revision_alone(
    tmp_path: Path, matching: bool
) -> None:
    db_path = tmp_path / "drafts.db"
    current = _snapshot()
    store = DraftStore(db_path)
    await store.open()
    draft = await store.create_draft(
        current.ref, REVISION, _content(InlineDraftComment(uuid4(), "note", _anchor()))
    )
    await store.close()
    changes: dict[str, object] = {"stale": True}
    if not matching:
        changes["revision"] = {
            "head_sha": "head-0",
            "base_sha": REVISION.base_sha,
            "start_sha": REVISION.start_sha,
        }
    _persist_anchor(db_path, draft.id, **changes)
    store = DraftStore(db_path)
    await store.open()
    client = _client()
    service = ReviewSubmissionService(
        store=store,
        mutations=_mutation_service(client, current),
        get_review=AsyncMock(return_value=current),
    )

    if matching:
        progress = await service.start(draft.id, draft.version)
        assert progress.outcome is SubmissionOutcome.SUBMITTED
        client.submit_review.assert_awaited_once()
    else:
        with pytest.raises(ServiceError) as raised:
            await service.start(draft.id, draft.version)
        assert raised.value.code is ServiceErrorCode.REVISION_CHANGED
        assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
        client.submit_review.assert_not_awaited()
    await store.close()


def _fail_unknown_writes(
    store: DraftStore, monkeypatch: pytest.MonkeyPatch, *, times: int = 1
) -> list[int]:
    """Make the next outcome-unknown transitions fail like a full disk.

    The real ``mark_attempt_unknown`` still runs around the failing write, so
    ownership is released exactly as it is after a real storage failure.
    """
    original = store._transition_owned
    failures: list[int] = []

    async def failing(
        attempt_id: object, target: DraftState, **kwargs: object
    ) -> object:
        if target is DraftState.UNKNOWN and len(failures) < times:
            failures.append(1)
            raise DraftStoreError("disk full")
        return await original(attempt_id, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_transition_owned", failing)
    return failures


@pytest.mark.asyncio
async def test_failed_first_return_transition_says_a_restart_recovers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    comment = GeneralDraftComment(uuid4(), "confirmed comment")
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 400"))
    client = _client(submit_review=verdict)
    store, service, _, snapshot, draft = await _services(
        path,
        _content(comment, verdict=DraftVerdict.APPROVE),
        forge=ForgeType.GITLAB,
        client=client,
    )
    paused = await service.start(draft.id, draft.version)
    failures = _fail_unknown_writes(store, monkeypatch)

    stopped = await service.reconcile(
        paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert failures == [1]
    assert stopped.outcome is SubmissionOutcome.PAUSED
    assert stopped.failure is not None
    assert stopped.failure.code == "storage_failed"
    assert stopped.failure.retryable is False
    assert "could not be returned to editing" in stopped.failure.message
    assert "Restart tongs" in stopped.failure.message
    assert not store.holds_attempt(paused.attempt_id)
    assert (await store.get_draft(draft.id)).state is DraftState.PARTIAL

    with pytest.raises(ServiceError) as raised:
        await service.reconcile(
            paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE
        )

    assert raised.value.code is ServiceErrorCode.CONFLICT
    assert "another tongs process" not in raised.value.message
    assert "Restart tongs" in raised.value.message
    await store.close()

    restarted = DraftStore(path)
    await restarted.open()
    (recovered,) = await restarted.recover_incomplete_attempts()
    restarted_service = ReviewSubmissionService(
        store=restarted,
        mutations=_mutation_service(client, snapshot),
        get_review=AsyncMock(return_value=snapshot),
    )
    editable = await restarted_service.reconcile(
        recovered.id, ReconciliationResolution.RETURN_EDITABLE
    )

    assert editable.outcome is SubmissionOutcome.EDITABLE
    saved = await restarted.get_draft(draft.id)
    assert saved.state is DraftState.EDITABLE
    assert saved.comments == ()
    assert saved.verdict is DraftVerdict.APPROVE
    assert client.add_comment.await_count == 1
    assert verdict.await_count == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_get_reports_editable_once_an_unknown_attempt_returned_to_editing(
    tmp_path: Path,
) -> None:
    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        tmp_path / "drafts.db",
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)
    before = await service.get(unknown.attempt_id)

    await service.reconcile(
        unknown.attempt_id, ReconciliationResolution.RETURN_EDITABLE
    )
    # A caller that lost the reconcile response reads the attempt again.
    after = await service.get(unknown.attempt_id)

    assert before.outcome is SubmissionOutcome.UNKNOWN
    assert (await store.get_attempt(unknown.attempt_id)).state is DraftState.UNKNOWN
    assert after.outcome is SubmissionOutcome.EDITABLE
    assert after.unknown_step_ids == before.unknown_step_ids
    assert (await store.get_draft(draft.id)).state is DraftState.EDITABLE
    assert add.await_count == 1
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_retry_whose_unknown_write_fails_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    add = AsyncMock(side_effect=NetworkError("timeout"))
    store, service, _, _, draft = await _services(
        path,
        _content(GeneralDraftComment(uuid4(), "comment")),
        client=_client(add_comment=add),
    )
    unknown = await service.start(draft.id, draft.version)
    failures = _fail_unknown_writes(store, monkeypatch)

    task = await _cancel_while_gated(
        store,
        "reconcile_attempt",
        lambda: service.reconcile(
            unknown.attempt_id, ReconciliationResolution.RETRY_REMAINING
        ),
    )

    assert task.cancelled()
    assert failures == [1]
    assert not store.holds_attempt(unknown.attempt_id)
    assert add.await_count == 1
    await store.close()

    restarted = DraftStore(path)
    await restarted.open()
    recovered = await restarted.recover_incomplete_attempts()
    assert [item.id for item in recovered] == [unknown.attempt_id]
    assert recovered[0].state is DraftState.UNKNOWN
    await restarted.close()


@pytest.mark.asyncio
async def test_cancelled_dispatch_whose_unknown_write_fails_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    store, service, client, _, draft = await _services(
        path, _content(GeneralDraftComment(uuid4(), "comment"))
    )
    failures = _fail_unknown_writes(store, monkeypatch)

    task = await _cancel_while_gated(
        store,
        "begin_dispatch",
        lambda: service.start(draft.id, draft.version),
    )

    assert task.cancelled()
    assert failures == [1]
    client.add_comment.assert_not_awaited()
    # Ownership is released, so the attempt waits for startup recovery.
    assert store._held_attempt_locks == {}
    assert (await store.get_draft(draft.id)).state is DraftState.SUBMITTING
    await store.close()

    restarted = DraftStore(path)
    await restarted.open()
    (recovered,) = await restarted.recover_incomplete_attempts()
    # The journaled dispatch survives as an unknown marker, never a replay.
    assert recovered.state is DraftState.UNKNOWN
    assert [item.step_id for item in recovered.unknown_outcomes] == [
        f"comment:{draft.comments[0].id.hex}"
    ]
    await restarted.close()


@pytest.mark.asyncio
async def test_cancelled_return_whose_first_unknown_write_fails_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.db"
    comment = GeneralDraftComment(uuid4(), "confirmed comment")
    verdict = AsyncMock(side_effect=ValidationError("Validation failed: 400"))
    store, service, _, _, draft = await _services(
        path,
        _content(comment, verdict=DraftVerdict.APPROVE),
        forge=ForgeType.GITLAB,
        client=_client(submit_review=verdict),
    )
    paused = await service.start(draft.id, draft.version)
    failures = _fail_unknown_writes(store, monkeypatch)
    real = store.mark_attempt_unknown
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated(*args: object, **kwargs: object) -> object:
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "mark_attempt_unknown", gated)
    task = asyncio.ensure_future(
        service.reconcile(paused.attempt_id, ReconciliationResolution.RETURN_EDITABLE)
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert failures == [1]
    assert not store.holds_attempt(paused.attempt_id)
    assert (await store.get_draft(draft.id)).state is DraftState.PARTIAL
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", [ForgeType.GITHUB, ForgeType.GITLAB])
async def test_thirty_comment_submission_reads_the_diff_once(
    tmp_path: Path, forge: ForgeType
) -> None:
    comments = tuple(
        InlineDraftComment(uuid4(), f"inline {index}", _anchor()) for index in range(30)
    )
    store, service, client, _snapshot_value, draft = await _services(
        tmp_path / "drafts.db", _content(*comments), forge=forge
    )
    get_diff = service._mutations._get_diff

    progress = await service.start(draft.id, draft.version)

    assert progress.outcome is SubmissionOutcome.SUBMITTED
    get_diff.assert_awaited_once()
    if forge is ForgeType.GITHUB:
        client.submit_review.assert_awaited_once()
        assert len(client.submit_review.await_args.args[4]) == 30
    else:
        assert client.create_inline_comment.await_count == 30
    await store.close()
