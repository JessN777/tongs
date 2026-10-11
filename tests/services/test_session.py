"""Tests for the shared application session and read boundary."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.errors import AuthError, ForgeError, NotFoundError, PartialResultsError
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    ForgeMutationResult,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    User,
)
from tongs.forges.registry import ForgeRegistry
from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services import (
    ApplicationSession,
    CancelPipelineCommand,
    CloseReviewCommand,
    GeneralComment,
    JobRef,
    MutationStatus,
    PipelineMutationTarget,
    PipelineRef,
    RepositoryRef,
    RepositorySnapshot,
    RetryPipelineCommand,
    ReviewActionTarget,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewScope,
    ServiceError,
    ServiceErrorCode,
    ServiceEventKind,
)
from tongs.services.review_mutations import DiffAnchor, DiffSide
from tongs.services.review_mutations import InlineComment as InlineCommentCommand
from tongs.state.drafts import (
    DraftContent,
    DraftState,
    DraftStore,
    GeneralDraftComment,
    RecoveryWarning,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")


def make_summary(
    host: ForgeHost = GITHUB_HOST,
    project: str = "acme/widgets",
    number: int = 7,
    *,
    updated_at: datetime = NOW,
    local_path: str = "",
) -> MRSummary:
    return MRSummary(
        forge_host=host,
        repo_path=project,
        local_path=local_path,
        number=number,
        title="Review widgets",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=NOW,
        updated_at=updated_at,
        web_url=f"https://{host.hostname}/{project}/pull/{number}",
    )


def make_detail(
    host: ForgeHost = GITHUB_HOST,
    project: str = "acme/widgets",
    number: int = 7,
    *,
    head: str = "head-1",
    base: str = "base-1",
    start: str | None = None,
    local_path: str = "",
) -> MRDetail:
    summary = make_summary(host, project, number, local_path=local_path)
    return MRDetail(
        **summary.__dict__,
        description="Details",
        head_sha=head,
        base_sha=base,
        start_sha=start,
    )


class FakeCache:
    def __init__(self, *, open_error: Exception | None = None) -> None:
        self.open_error = open_error
        self.open_calls = 0
        self.close_calls = 0
        self.clear_calls = 0

    async def open(self) -> None:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error

    async def close(self) -> None:
        self.close_calls += 1

    async def clear(self) -> None:
        self.clear_calls += 1


class FakeDraftStore:
    recovery_warnings: tuple[object, ...] = ()

    def __init__(self) -> None:
        self.open_calls = 0
        self.recover_calls = 0
        self.close_calls = 0

    async def open(self) -> None:
        self.open_calls += 1

    async def recover_incomplete_attempts(self) -> tuple[object, ...]:
        self.recover_calls += 1
        return ()

    async def close(self) -> None:
        self.close_calls += 1


class BlockingDraftStore(FakeDraftStore):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.started = asyncio.Event()

    async def open(self) -> None:
        self.open_calls += 1
        if self.phase == "open":
            self.started.set()
            await asyncio.Event().wait()

    async def recover_incomplete_attempts(self) -> tuple[object, ...]:
        self.recover_calls += 1
        if self.phase == "recover":
            self.started.set()
            await asyncio.Event().wait()
        return ()


class BlockingCache(FakeCache):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def open(self) -> None:
        self.open_calls += 1
        self.started.set()
        await self.release.wait()


class CancellationResistantOpenCache(BlockingCache):
    async def open(self) -> None:
        self.open_calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.release.wait()


class BlockingCloseCache(FakeCache):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.started.set()
        await self.release.wait()


class FatalStartup(BaseException):
    pass


class FatalOpenCache(FakeCache):
    async def open(self) -> None:
        self.open_calls += 1
        raise FatalStartup("process-control exit")


class FakeClient:
    supports_batched_review = True
    supports_thread_resolution = True
    supports_draft_notes = False
    supports_unapprove = False
    supports_job_cancel = True

    def __init__(self, detail: MRDetail | None = None) -> None:
        self.detail_results = [detail or make_detail()]
        self.diff = [{"filename": "src/widget.py", "patch": "@@ -1 +1 @@"}]
        self.my_reviews = [make_summary()]
        self.my_mrs = [make_summary(number=8)]
        self.repository_reviews = [make_summary()]
        self.get_mr_fresh_calls = 0
        self.get_mr_diff_fresh_calls = 0
        self.list_mrs_calls = 0
        self.cancel_get_mr = False
        self.mutation_calls: list[tuple[str, str, int]] = []
        self.ci_mutation_started = asyncio.Event()
        self.ci_mutation_release: asyncio.Event | None = None
        self.review_mutation_started = asyncio.Event()
        self.review_mutation_release: asyncio.Event | None = None

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        self.get_mr_fresh_calls += 1
        if self.cancel_get_mr:
            await asyncio.Event().wait()
        if len(self.detail_results) > 1:
            return self.detail_results.pop(0)
        return self.detail_results[0]

    async def get_mr_diff_fresh(self, repo_path: str, number: int) -> list[dict]:
        self.get_mr_diff_fresh_calls += 1
        return self.diff

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        self.list_mrs_calls += 1
        return self.repository_reviews

    async def list_my_reviews(self) -> list[MRSummary]:
        return self.my_reviews

    async def list_my_mrs(self) -> list[MRSummary]:
        return self.my_mrs

    async def get_mr_discussions(self, repo_path: str, number: int) -> list[Discussion]:
        comment = InlineComment("1", User("alice"), "note", NOW, "src/widget.py")
        return [Discussion("thread-1", True, comment)]

    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]:
        return [Commit("abc", "abc", "Title", "Message", User("alice"))]

    async def list_pipelines(
        self, repo_path: str, per_page: int = 20
    ) -> list[Pipeline]:
        return [Pipeline(11, CIStatus.SUCCESS, "main", "abc", "https://ci")]

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        return [Pipeline(12, CIStatus.RUNNING, "feature", "def", "https://ci")]

    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]:
        return [PipelineJob(21, "test", "verify", CIStatus.SUCCESS)]

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        return "safe log"

    async def retry_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        await self._mutate_ci("retry_pipeline", repo_path, pipeline_id)

    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        await self._mutate_ci("cancel_pipeline", repo_path, pipeline_id)

    async def retry_job(self, repo_path: str, job_id: int) -> None:
        await self._mutate_ci("retry_job", repo_path, job_id)

    async def cancel_job(self, repo_path: str, job_id: int) -> None:
        await self._mutate_ci("cancel_job", repo_path, job_id)

    async def _mutate_ci(self, action: str, repo_path: str, item_id: int) -> None:
        self.mutation_calls.append((action, repo_path, item_id))
        self.ci_mutation_started.set()
        if self.ci_mutation_release is not None:
            await self.ci_mutation_release.wait()

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        self.review_mutation_started.set()
        if self.review_mutation_release is not None:
            await self.review_mutation_release.wait()
        return ForgeMutationResult("note-1", comment_id="note-1")

    async def invalidate_review_reads(self, repo_path: str, number: int) -> bool:
        return True


class FirstCancelResistantReviewClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.review_cancellations = 0

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        self.review_mutation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.review_cancellations += 1
            await asyncio.Event().wait()
        raise AssertionError("blocked review mutation unexpectedly resumed")


class CancellationResistantReviewClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.review_cancellations = 0
        self.review_mutation_release = asyncio.Event()

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        self.review_mutation_started.set()
        while not self.review_mutation_release.is_set():
            try:
                await self.review_mutation_release.wait()
            except asyncio.CancelledError:
                self.review_cancellations += 1
        return ForgeMutationResult("note-1", comment_id="note-1")


class BlockingMRActionClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.mr_action_started = asyncio.Event()

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        self.mr_action_started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked MR action unexpectedly resumed")


class FirstCancelResistantMRActionClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.mr_action_started = asyncio.Event()
        self.mr_action_cancellations = 0

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        self.mr_action_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.mr_action_cancellations += 1
            await asyncio.Event().wait()
        raise AssertionError("blocked MR action unexpectedly resumed")


class CancellationResistantMRActionClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.mr_action_started = asyncio.Event()
        self.mr_action_release = asyncio.Event()
        self.mr_action_cancellations = 0

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        self.mr_action_started.set()
        while not self.mr_action_release.is_set():
            try:
                await self.mr_action_release.wait()
            except asyncio.CancelledError:
                self.mr_action_cancellations += 1
        return ForgeMutationResult("review-7")


class BlockingReviewClient(FakeClient):
    def __init__(self, detail: MRDetail | None = None) -> None:
        super().__init__(detail)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def list_my_reviews(self) -> list[MRSummary]:
        self.started.set()
        await self.release.wait()
        return self.my_reviews


class _PartialClient(FakeClient):
    """A client whose searches read only some of their results."""

    def __init__(self, cause: ForgeError) -> None:
        super().__init__()
        self.cause = cause

    def _partial(self) -> PartialResultsError:
        return PartialResultsError(
            [
                make_summary(number=7),
                make_summary(number=9, updated_at=NOW + timedelta(minutes=1)),
            ],
            self.cause,
        )

    async def list_my_reviews(self) -> list[MRSummary]:
        raise self._partial()

    async def list_my_mrs(self) -> list[MRSummary]:
        raise self._partial()


class FakeRegistry:
    def __init__(
        self,
        clients: dict[str, object] | None = None,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.clients = clients or {"github.com": FakeClient()}
        self.close_error = close_error
        self.get_client_calls: list[str] = []
        self.close_calls = 0
        self.hosts = {
            "github.com": GITHUB_HOST,
            "gitlab.com": GITLAB_HOST,
        }

    def active_hostnames(self) -> list[str]:
        return list(self.clients)

    def get_host(self, hostname: str) -> ForgeHost | None:
        return self.hosts.get(hostname)

    async def get_client(self, hostname: str) -> ForgeClient:
        self.get_client_calls.append(hostname)
        result = self.clients[hostname]
        if isinstance(result, Exception):
            raise result
        return cast(ForgeClient, result)

    async def close_all(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ClosingAwareRegistry(FakeRegistry):
    def __init__(self, clients: dict[str, object]) -> None:
        super().__init__(clients)
        self.closed = False
        self.get_client_after_close: list[str] = []

    async def get_client(self, hostname: str) -> ForgeClient:
        if self.closed:
            self.get_client_after_close.append(hostname)
        return await super().get_client(hostname)

    async def close_all(self) -> None:
        self.closed = True
        await super().close_all()


class BlockingGetClientRegistry(ClosingAwareRegistry):
    def __init__(self, clients: dict[str, object]) -> None:
        super().__init__(clients)
        self.get_client_started = asyncio.Event()
        self.get_client_release = asyncio.Event()
        self.block_get_client = False

    async def get_client(self, hostname: str) -> ForgeClient:
        if self.block_get_client:
            self.get_client_started.set()
            await self.get_client_release.wait()
        return await super().get_client(hostname)


class HostBlockingRegistry(FakeRegistry):
    """Hold one host's client acquisition, as a hung token lookup would."""

    def __init__(self, clients: dict[str, object], *, blocked: str) -> None:
        super().__init__(clients)
        self.blocked = blocked
        self.armed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_client(self, hostname: str) -> ForgeClient:
        if self.armed and hostname == self.blocked:
            self.started.set()
            await self.release.wait()
        return await super().get_client(hostname)


def make_repo(
    root: Path,
    *,
    hostname: str = "github.com",
    project: str = "acme/widgets",
    forge_type: ForgeType = ForgeType.GITHUB,
) -> Repo:
    remote = Remote(
        "origin", f"https://{hostname}/{project}.git", hostname, project, forge_type
    )
    return Repo(root / project.split("/")[-1], (remote,), remote)


async def start_session(
    registry: FakeRegistry,
    *,
    cache: FakeCache | None = None,
    discoverer=None,
    event_queue_size: int = 4,
) -> ApplicationSession:
    kwargs = {}
    if discoverer is not None:
        kwargs["discoverer"] = discoverer
    session = ApplicationSession(
        config=Config(max_parallel=2),
        cache=cache or FakeCache(),
        draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        forge_registry=registry,
        event_queue_size=event_queue_size,
        **kwargs,
    )
    return await session.start()


@pytest.mark.asyncio
async def test_clear_cache_uses_session_owned_cache_without_draft_lifecycle() -> None:
    cache = FakeCache()
    drafts = FakeDraftStore()
    session = ApplicationSession(
        config=Config(max_parallel=2),
        cache=cache,
        draft_store=cast(DraftStore, drafts),
        forge_registry=FakeRegistry(),
    )
    await session.start()

    await session.clear_cache()

    assert cache.clear_calls == 1
    assert drafts.open_calls == 1
    assert drafts.close_calls == 0
    await session.close()


class TestReferences:
    @pytest.mark.parametrize(
        ("hostname", "project"),
        [
            ("github.com/path", "acme/widgets"),
            ("github.com", "widgets"),
            ("github.com", "../widgets"),
            ("github.com", "acme\\team/widgets"),
        ],
    )
    def test_rejects_noncanonical_repository_refs(
        self, hostname: str, project: str
    ) -> None:
        with pytest.raises(ValueError):
            RepositoryRef(hostname, project)

    def test_review_number_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ReviewRef(RepositoryRef("github.com", "acme/widgets"), 0)

    @pytest.mark.parametrize(
        "hostnames",
        [
            ["github.com"],
            ("GitHub.com",),
            ("github.com", "github.com"),
        ],
    )
    def test_review_query_rejects_invalid_host_restrictions(
        self, hostnames: object
    ) -> None:
        with pytest.raises((TypeError, ValueError)):
            ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=hostnames)  # type: ignore[arg-type]


class TestLifecycle:
    def test_draft_store_and_path_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            ApplicationSession(
                draft_db_path=tmp_path / "drafts.db",
                draft_store=cast(DraftStore, FakeDraftStore()),
            )

    @pytest.mark.asyncio
    async def test_session_opens_recovers_and_exposes_one_draft_store(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "drafts.db"
        setup = DraftStore(path)
        await setup.open()
        revision = ReviewRevision("head-1", "base-1")
        draft = await setup.create_draft(
            ReviewRef(RepositoryRef("github.com", "acme/widgets"), 7),
            revision,
            DraftContent(comments=(GeneralDraftComment(uuid4(), "comment"),)),
        )
        attempt = await setup.lock_submission(draft.id, draft.version)
        await setup.close()
        store = DraftStore(path)
        session = await ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_store=store,
            forge_registry=FakeRegistry(),
        ).start()

        assert session.drafts is store
        assert (await store.get_attempt(attempt.id)).state is DraftState.UNKNOWN

        await session.close()

    @pytest.mark.asyncio
    async def test_session_starts_past_corrupt_attempt_and_reports_it(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "drafts.db"
        setup = DraftStore(path)
        await setup.open()
        revision = ReviewRevision("head-1", "base-1")
        attempts = []
        for number in (7, 8):
            draft = await setup.create_draft(
                ReviewRef(RepositoryRef("github.com", "acme/widgets"), number),
                revision,
                DraftContent(body="private text"),
            )
            attempts.append(await setup.lock_submission(draft.id, draft.version))
        await setup.close()
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE submission_attempts SET updated_at = 'bad' WHERE id = ?",
                (str(attempts[0].id),),
            )

        session = await ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_db_path=path,
            forge_registry=FakeRegistry(),
        ).start()

        assert session.recovery_warnings == (
            RecoveryWarning(
                attempts[0].id,
                review=ReviewRef(RepositoryRef("github.com", "acme/widgets"), 7),
            ),
        )
        assert "private text" not in session.recovery_warnings[0].describe()
        recovered = await session.drafts.get_attempt(attempts[1].id)
        assert recovered.state is DraftState.UNKNOWN
        await session.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["open", "recover"])
    async def test_close_cancels_draft_startup_before_shared_resources(
        self, phase: str
    ) -> None:
        cache = FakeCache()
        drafts = BlockingDraftStore(phase)
        registry = FakeRegistry()
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=cast(DraftStore, drafts),
            forge_registry=registry,
        )
        start = asyncio.create_task(session.start())
        await drafts.started.wait()

        await session.close()

        with pytest.raises(asyncio.CancelledError):
            await start
        assert drafts.close_calls == 1
        assert cache.close_calls == 1
        assert registry.close_calls == 0

    @pytest.mark.asyncio
    async def test_submission_closes_before_mutations_store_and_shared_resources(
        self,
    ) -> None:
        order: list[str] = []

        class OrderedDraftStore(FakeDraftStore):
            async def close(self) -> None:
                order.append("drafts")
                await super().close()

        class OrderedCache(FakeCache):
            async def close(self) -> None:
                order.append("cache")
                await super().close()

        class OrderedRegistry(FakeRegistry):
            async def close_all(self) -> None:
                order.append("registry")
                await super().close_all()

        drafts = OrderedDraftStore()
        session = await ApplicationSession(
            config=Config(),
            cache=OrderedCache(),
            draft_store=cast(DraftStore, drafts),
            forge_registry=OrderedRegistry(),
        ).start()
        session.review_submissions.close = AsyncMock(
            side_effect=lambda: order.append("submissions")
        )
        session.mr_actions.close = AsyncMock(
            side_effect=lambda: order.append("mr_actions")
        )
        session.ci_mutations.close = AsyncMock(
            side_effect=lambda: order.append("ci_mutations")
        )
        session.review_mutations.close = AsyncMock(
            side_effect=lambda: order.append("review_mutations")
        )

        await session.close()

        assert order == [
            "submissions",
            "mr_actions",
            "ci_mutations",
            "review_mutations",
            "drafts",
            "registry",
            "cache",
        ]

    @pytest.mark.asyncio
    async def test_submission_close_failure_preserves_dependencies_for_retry(
        self,
    ) -> None:
        cache = FakeCache()
        drafts = FakeDraftStore()
        registry = FakeRegistry()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=cast(DraftStore, drafts),
            forge_registry=registry,
        ).start()
        submission_close = AsyncMock(
            side_effect=[RuntimeError("owner still active"), None]
        )
        session.review_submissions.close = submission_close

        with pytest.raises(ServiceError) as raised:
            await session.close()

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert drafts.close_calls == 0
        assert registry.close_calls == 0
        assert cache.close_calls == 0
        assert session.ci_mutations._closed is False
        assert session.review_mutations._closed is False
        assert session.mr_actions._closed is False

        await session.close()

        assert submission_close.await_count == 2
        assert drafts.close_calls == 1
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_final_close_after_failed_cleanup_closes_dependencies(
        self,
    ) -> None:
        cache = FakeCache()
        drafts = FakeDraftStore()
        registry = FakeRegistry()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=cast(DraftStore, drafts),
            forge_registry=registry,
        ).start()
        submission_close = AsyncMock(side_effect=RuntimeError("owner still active"))
        session.review_submissions.close = submission_close

        with pytest.raises(ServiceError):
            await session.close()
        assert (drafts.close_calls, registry.close_calls, cache.close_calls) == (
            0,
            0,
            0,
        )

        with pytest.raises(ServiceError) as raised:
            await session.close(final=True)

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert session.shutdown_error is raised.value
        assert submission_close.await_count == 2
        assert (drafts.close_calls, registry.close_calls, cache.close_calls) == (
            1,
            1,
            1,
        )
        assert session.ci_mutations._closed is True
        assert session.review_mutations._closed is True
        assert session.mr_actions._closed is True
        with pytest.raises(ServiceError) as closed:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert closed.value.code is ServiceErrorCode.CLOSED

        # Closing again does not reopen or close anything twice.
        with pytest.raises(ServiceError):
            await session.close(final=True)
        assert (drafts.close_calls, registry.close_calls, cache.close_calls) == (
            1,
            1,
            1,
        )

    @pytest.mark.asyncio
    async def test_final_close_after_clean_shutdown_is_a_no_op(self) -> None:
        cache = FakeCache()
        drafts = FakeDraftStore()
        registry = FakeRegistry()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=cast(DraftStore, drafts),
            forge_registry=registry,
        ).start()

        await session.close(final=True)
        await session.close(final=True)

        assert session.shutdown_error is None
        assert (drafts.close_calls, registry.close_calls, cache.close_calls) == (
            1,
            1,
            1,
        )

    @pytest.mark.asyncio
    async def test_final_close_requested_during_a_failing_close_closes_stores(
        self,
    ) -> None:
        cache = FakeCache()
        drafts = FakeDraftStore()
        registry = FakeRegistry()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=cast(DraftStore, drafts),
            forge_registry=registry,
        ).start()
        started = asyncio.Event()
        release = asyncio.Event()

        async def failing_close() -> None:
            started.set()
            await release.wait()
            raise RuntimeError("owner still active")

        session.review_submissions.close = failing_close  # type: ignore[method-assign]
        plain = asyncio.create_task(session.close())
        await started.wait()
        final = asyncio.create_task(session.close(final=True))
        await asyncio.sleep(0)
        release.set()

        with pytest.raises(ServiceError):
            await plain
        with pytest.raises(ServiceError) as raised:
            await final

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert (drafts.close_calls, registry.close_calls, cache.close_calls) == (
            1,
            1,
            1,
        )

    @pytest.mark.asyncio
    async def test_final_close_stops_every_sqlite_worker_thread(
        self, tmp_path: Path
    ) -> None:
        before = set(threading.enumerate())
        session = await ApplicationSession(
            config=Config(),
            cache_path=tmp_path / "cache.db",
            draft_db_path=tmp_path / "drafts.db",
            forge_registry=FakeRegistry(),
            shutdown_timeout=0.5,
        ).start()
        workers = [
            thread
            for thread in set(threading.enumerate()) - before
            if "_connection_worker_thread" in thread.name
        ]
        assert len(workers) == 2
        session.review_submissions.close = AsyncMock(
            side_effect=RuntimeError("owner still active")
        )

        with pytest.raises(ServiceError):
            await session.close()
        assert all(thread.is_alive() for thread in workers)

        with pytest.raises(ServiceError):
            await session.close(final=True)
        for thread in workers:
            await asyncio.to_thread(thread.join, 5)
        assert [thread.is_alive() for thread in workers] == [False, False]

    @pytest.mark.asyncio
    async def test_final_close_lets_a_running_draft_write_finish_first(
        self, tmp_path: Path
    ) -> None:
        drafts = DraftStore(tmp_path / "drafts.db")
        session = await ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_store=drafts,
            forge_registry=FakeRegistry(),
        ).start()
        session.review_submissions.close = AsyncMock(
            side_effect=RuntimeError("owner still active")
        )
        # A serialized draft store operation is running.
        await drafts._operation_lock.acquire()

        final = asyncio.create_task(session.close(final=True))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not final.done()
        assert drafts._db is not None

        drafts._operation_lock.release()
        with pytest.raises(ServiceError):
            await final
        assert drafts._db is None

    @pytest.mark.asyncio
    async def test_active_submission_settles_before_store_and_forge_close(
        self, tmp_path: Path
    ) -> None:
        cache = FakeCache()
        client = FakeClient()
        client.review_mutation_release = asyncio.Event()
        registry = FakeRegistry({"github.com": client})
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_db_path=tmp_path / "drafts.db",
            forge_registry=registry,
        ).start()
        repository = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repository.ref, 7)
        snapshot = await session.get_review(review)
        assert snapshot.revision is not None
        draft = await session.drafts.create_draft(
            review,
            snapshot.revision,
            DraftContent(comments=(GeneralDraftComment(uuid4(), "comment"),)),
        )
        owner = asyncio.create_task(
            session.review_submissions.start(draft.id, draft.version)
        )
        await client.review_mutation_started.wait()

        await session.close()
        progress = await owner

        assert progress.outcome.value == "unknown"
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("owner_kind", "resists_first_cancel"),
        [
            ("ci", False),
            ("mr_action", False),
            ("mr_action", True),
            ("review", False),
            ("review", True),
        ],
    )
    async def test_close_settles_owner_before_registry_and_cache(
        self, owner_kind: str, resists_first_cancel: bool
    ) -> None:
        client: FakeClient
        if owner_kind == "ci":
            client = FakeClient()
            client.ci_mutation_release = asyncio.Event()
            forge_method, started = "cancel_pipeline", client.ci_mutation_started
        elif owner_kind == "mr_action":
            client = (
                FirstCancelResistantMRActionClient()
                if resists_first_cancel
                else BlockingMRActionClient()
            )
            forge_method, started = "close_mr", client.mr_action_started
        else:
            client = (
                FirstCancelResistantReviewClient()
                if resists_first_cancel
                else FakeClient()
            )
            client.review_mutation_release = asyncio.Event()
            forge_method, started = "add_comment", client.review_mutation_started
        forge_call_settled = asyncio.Event()
        blocking_call = getattr(client, forge_method)

        async def tracked_call(*args: object) -> object:
            try:
                return await blocking_call(*args)
            finally:
                forge_call_settled.set()

        setattr(client, forge_method, tracked_call)
        closed: list[tuple[str, bool]] = []

        class OrderedRegistry(FakeRegistry):
            async def close_all(self) -> None:
                closed.append(("registry", forge_call_settled.is_set()))
                await super().close_all()

        class OrderedCache(FakeCache):
            async def close(self) -> None:
                closed.append(("cache", forge_call_settled.is_set()))
                await super().close()

        session = await start_session(
            OrderedRegistry({"github.com": client}), cache=OrderedCache()
        )
        session.mr_actions._close_timeout = 0.01
        session.review_mutations._close_timeout = 0.01
        repository = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repository.ref, 7)
        if owner_kind == "ci":
            execution = session.ci_mutations.execute(
                CancelPipelineCommand(
                    "close-ci", PipelineMutationTarget(PipelineRef(repository.ref, 11))
                )
            )
        elif owner_kind == "mr_action":
            snapshot = await session.get_review(review)
            assert snapshot.revision is not None
            execution = session.mr_actions.execute(
                CloseReviewCommand(
                    "close-mr-action",
                    ReviewActionTarget(review, snapshot.revision, MRState.OPEN),
                )
            )
        else:
            execution = session.review_mutations.execute(
                GeneralComment("close-review", review, "body")
            )
        owner = asyncio.create_task(execution)
        await started.wait()

        await session.close()

        with suppress(asyncio.CancelledError):
            await owner
        assert closed == [("registry", True), ("cache", True)]

    @pytest.mark.asyncio
    async def test_ci_hint_tasks_close_before_registry_and_cache(self) -> None:
        cache = FakeCache()
        client = FakeClient()
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry, cache=cache)
        repository = await session.open_repository("github.com", "acme/widgets")
        invalidation_started = asyncio.Event()
        invalidation_finished = asyncio.Event()

        async def invalidate(_pipeline: PipelineRef) -> None:
            invalidation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert registry.close_calls == 0
                assert cache.close_calls == 0
                invalidation_finished.set()

        session.ci_mutations._invalidate_pipeline = invalidate
        command = CancelPipelineCommand(
            "close-ci-hints",
            PipelineMutationTarget(PipelineRef(repository.ref, 11)),
        )
        owner = asyncio.create_task(session.ci_mutations.execute(command))
        await invalidation_started.wait()

        await session.close()

        with pytest.raises(asyncio.CancelledError):
            await owner
        assert invalidation_finished.is_set()
        assert session.ci_mutations._coordinator_tasks == set()
        assert session.ci_mutations._invalidation_tasks == set()
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_uncooperative_mr_action_preserves_resources_until_retry(
        self,
    ) -> None:
        cache = FakeCache()
        client = CancellationResistantMRActionClient()
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry, cache=cache)
        session.mr_actions._close_timeout = 0.01
        repository = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repository.ref, 7)
        snapshot = await session.get_review(review)
        assert snapshot.revision is not None
        command = CloseReviewCommand(
            "session-close-uncooperative-mr-action",
            ReviewActionTarget(review, snapshot.revision, MRState.OPEN),
        )
        owner = asyncio.create_task(session.mr_actions.execute(command))
        await client.mr_action_started.wait()

        with pytest.raises(ServiceError) as raised:
            await asyncio.wait_for(session.close(), timeout=0.2)

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert client.mr_action_cancellations == 2
        assert owner in session.mr_actions._owner_tasks
        assert registry.close_calls == 0
        assert cache.close_calls == 0

        client.mr_action_release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        record = session.mr_actions._operations[command.operation_id]
        assert record.receipt is not None
        assert record.receipt.outcome.value == "known"
        await session.close()
        assert session.mr_actions._owner_tasks == set()
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_uncooperative_review_owner_keeps_shared_resources_until_retry(
        self,
    ) -> None:
        cache = FakeCache()
        client = CancellationResistantReviewClient()
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry, cache=cache)
        session.review_mutations._close_timeout = 0.01
        repository = await session.open_repository("github.com", "acme/widgets")
        command = GeneralComment(
            "session-close-uncooperative-review",
            ReviewRef(repository.ref, 7),
            "body",
        )
        owner = asyncio.create_task(session.review_mutations.execute(command))
        await client.review_mutation_started.wait()

        with pytest.raises(ServiceError) as raised:
            await asyncio.wait_for(session.close(), timeout=0.2)

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert client.review_cancellations == 2
        assert owner in session.review_mutations._owner_tasks
        assert registry.close_calls == 0
        assert cache.close_calls == 0
        assert session.shutdown_error is raised.value

        client.review_mutation_release.set()
        outcome = await owner
        assert outcome.status is MutationStatus.KNOWN
        await session.close()
        assert session.review_mutations._owner_tasks == set()
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_context_manager_closes_owned_resources_once(self) -> None:
        cache = FakeCache()
        registry = FakeRegistry()
        async with ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=registry,
        ) as session:
            assert cache.close_calls == 0
        assert cache.close_calls == 1
        assert registry.close_calls == 1
        await session.close()
        assert cache.close_calls == 1
        assert registry.close_calls == 1
        with pytest.raises(ServiceError) as caught:
            await session.start()
        assert caught.value.code == ServiceErrorCode.CLOSED
        assert cache.open_calls == 1

    @pytest.mark.asyncio
    async def test_start_failure_closes_partially_opened_cache(self) -> None:
        cache = FakeCache(open_error=RuntimeError("ghp_secret"))
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        )
        with pytest.raises(ServiceError) as caught:
            await session.start()
        assert caught.value.code == ServiceErrorCode.INTERNAL
        assert "ghp_secret" not in str(caught.value)
        assert cache.open_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_base_exception_during_start_closes_cache_and_propagates(
        self,
    ) -> None:
        cache = FatalOpenCache()
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        )

        with pytest.raises(FatalStartup, match="process-control exit"):
            await session.start()

        assert cache.open_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_cancelled_start_still_closes_partial_cache(self) -> None:
        cache = BlockingCache()
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        )
        task = asyncio.create_task(session.start())
        await cache.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_close_cancels_stalled_start_and_closes_created_resources(
        self,
    ) -> None:
        cache = BlockingCache()
        registry = FakeRegistry()
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=registry,
        )
        start_task = asyncio.create_task(session.start())
        await cache.started.wait()

        close_task = asyncio.create_task(session.close())
        await close_task

        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert cache.close_calls == 1
        assert registry.close_calls == 0
        with pytest.raises(ServiceError) as caught:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert caught.value.code == ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_close_is_bounded_when_start_ignores_cancellation(self) -> None:
        cache = CancellationResistantOpenCache()
        session = ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=FakeRegistry(),
            shutdown_timeout=0.01,
        )
        start_task = asyncio.create_task(session.start())
        await cache.started.wait()

        with pytest.raises(ServiceError) as caught:
            await asyncio.wait_for(session.close(), timeout=0.1)
        assert caught.value.code == ServiceErrorCode.SHUTDOWN_FAILED
        assert cache.close_calls == 1

        cache.release.set()
        with pytest.raises(ServiceError) as start_error:
            await start_task
        assert start_error.value.code == ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_cancelled_close_still_finishes_cleanup(self) -> None:
        cache = BlockingCloseCache()
        registry = FakeRegistry()
        session = await start_session(registry, cache=cache)
        close_task = asyncio.create_task(session.close())
        await cache.started.wait()

        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()
        cache.release.set()

        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert cache.close_calls == 1
        assert registry.close_calls == 1
        with pytest.raises(ServiceError) as caught:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert caught.value.code == ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_cancelled_registry_cleanup_still_closes_cache(self) -> None:
        cache = FakeCache()
        registry = FakeRegistry(close_error=asyncio.CancelledError())
        session = await start_session(registry, cache=cache)

        with pytest.raises(ServiceError) as caught:
            await session.close()
        assert caught.value.code == ServiceErrorCode.SHUTDOWN_FAILED
        assert registry.close_calls == 1
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_shutdown_timeout_closes_session_with_safe_error(self) -> None:
        cache = BlockingCloseCache()
        session = await ApplicationSession(
            config=Config(),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=FakeRegistry(),
            shutdown_timeout=0.01,
        ).start()

        with pytest.raises(ServiceError) as caught:
            await session.close()
        assert caught.value.code == ServiceErrorCode.SHUTDOWN_FAILED
        assert cache.close_calls == 1
        with pytest.raises(ServiceError) as closed:
            session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        assert closed.value.code == ServiceErrorCode.CLOSED


class TestResourceIssuance:
    def test_owned_resource_accessors_require_started_session(self) -> None:
        session = ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=FakeRegistry(),
        )

        for accessor in ("cache", "forge_registry", "local_repositories"):
            with pytest.raises(ServiceError) as caught:
                getattr(session, accessor)
            assert caught.value.code is ServiceErrorCode.NOT_STARTED

    @pytest.mark.asyncio
    async def test_owned_resource_accessors_return_exact_session_objects(
        self,
    ) -> None:
        cache = FakeCache()
        registry = FakeRegistry()
        session = await start_session(registry, cache=cache)

        assert session.cache is cache
        assert session.forge_registry is registry
        assert session.local_repositories == ()

        await session.close()
        for accessor in ("cache", "forge_registry", "local_repositories"):
            with pytest.raises(ServiceError) as caught:
                getattr(session, accessor)
            assert caught.value.code is ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_owned_registry_bounds_each_host_by_max_parallel(self) -> None:
        session = await ApplicationSession(
            config=Config(max_parallel=3),
            cache=FakeCache(),
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        ).start()
        registry = session.forge_registry
        assert isinstance(registry, ForgeRegistry)
        assert registry.max_parallel == 3
        await session.close()

    @pytest.mark.asyncio
    async def test_invalid_max_parallel_fails_before_building_a_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[object] = []
        monkeypatch.setattr(
            "tongs.services.session.ForgeRegistry",
            lambda *args, **kwargs: built.append(kwargs),
        )
        cache = FakeCache()
        session = ApplicationSession(
            config=Config(max_parallel=0),
            cache=cache,
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
        )
        with pytest.raises(ServiceError) as caught:
            await session.start()
        assert caught.value.code == ServiceErrorCode.INVALID_RESPONSE
        assert built == []
        assert cache.close_calls == 1

    @pytest.mark.asyncio
    async def test_pending_client_for_one_host_does_not_block_another(self) -> None:
        github = FakeClient()
        gitlab = FakeClient(make_detail(GITLAB_HOST))
        gitlab.repository_reviews = [make_summary(GITLAB_HOST)]
        registry = HostBlockingRegistry(
            {"github.com": github, "gitlab.com": gitlab}, blocked="gitlab.com"
        )
        session = await start_session(registry)
        github_repo = await session.open_repository("github.com", "acme/widgets")
        registry.armed = True
        pending = asyncio.create_task(
            session.open_repository("gitlab.com", "acme/widgets")
        )
        await registry.started.wait()

        snapshot = await asyncio.wait_for(
            session.get_review(ReviewRef(github_repo.ref, 7)), 2
        )
        assert snapshot.ref.number == 7
        assert not pending.done()

        registry.release.set()
        opened = await asyncio.wait_for(pending, 2)
        assert opened.ref.hostname == "gitlab.com"
        await session.close()

    @pytest.mark.asyncio
    async def test_well_formed_unissued_same_host_fails_before_forge_call(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry)
        ref = ReviewRef(RepositoryRef("github.com", "other/repository"), 1)
        with pytest.raises(ServiceError) as caught:
            await session.get_review(ref)
        assert caught.value.code == ServiceErrorCode.RESOURCE_NOT_ISSUED
        assert registry.get_client_calls == []
        await session.close()

    @pytest.mark.asyncio
    async def test_discovery_issues_reference_without_exposing_local_path(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)

        def discoverer(*args, **kwargs):
            return [repo]

        registry = FakeRegistry()
        session = await start_session(registry, discoverer=discoverer)
        snapshots = await session.discover_repositories()
        assert snapshots[0].ref == RepositoryRef("github.com", "acme/widgets")
        assert not hasattr(snapshots[0], "local_path")
        assert snapshots[0].ref in session.issued_repositories
        await session.close()

    @pytest.mark.asyncio
    async def test_discovery_preserves_actual_local_inventory_without_admitting_unknown_host(
        self, tmp_path: Path
    ) -> None:
        admitted = make_repo(tmp_path)
        local_only = make_repo(
            tmp_path,
            hostname="code.example.test",
            project="internal/tools",
            forge_type=ForgeType.GITLAB,
        )

        session = await start_session(
            FakeRegistry(), discoverer=lambda *args, **kwargs: [local_only, admitted]
        )
        snapshots = await session.discover_repositories()

        assert session.local_repositories == (local_only, admitted)
        assert session.local_repositories[0] is local_only
        assert session.local_repositories[1] is admitted
        assert [snapshot.ref for snapshot in snapshots] == [
            RepositoryRef("github.com", "acme/widgets")
        ]
        assert not hasattr(snapshots[0], "local_path")
        await session.close()

    @pytest.mark.asyncio
    async def test_late_discovery_cannot_replace_newer_inventory(
        self, tmp_path: Path
    ) -> None:
        old_repo = make_repo(tmp_path, project="acme/old")
        new_repo = make_repo(tmp_path, project="acme/new")
        first_started = threading.Event()
        release_first = threading.Event()
        calls = 0

        def discoverer(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                assert release_first.wait(timeout=2)
                return [old_repo]
            return [new_repo]

        session = await start_session(FakeRegistry(), discoverer=discoverer)
        first = asyncio.create_task(session.discover_repositories())
        assert await asyncio.to_thread(first_started.wait, 2)
        second_result = await session.discover_repositories()
        release_first.set()
        first_result = await first

        expected = RepositoryRef("github.com", "acme/new")
        assert [snapshot.ref for snapshot in second_result] == [expected]
        assert [snapshot.ref for snapshot in first_result] == [expected]
        assert session.local_repositories == (new_repo,)
        await session.close()

    @pytest.mark.asyncio
    async def test_failed_refresh_preserves_last_usable_local_inventory(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        calls = 0

        def discoverer(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [repo]
            raise RuntimeError("private scanner detail")

        session = await start_session(FakeRegistry(), discoverer=discoverer)
        await session.discover_repositories()

        with pytest.raises(ServiceError) as caught:
            await session.discover_repositories()

        assert caught.value.code is ServiceErrorCode.INTERNAL
        assert "private scanner detail" not in str(caught.value)
        assert session.local_repositories == (repo,)
        await session.close()

    @pytest.mark.asyncio
    async def test_discovery_finishing_after_close_cannot_publish(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        started = threading.Event()
        release = threading.Event()

        def discoverer(*args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return [repo]

        session = await start_session(FakeRegistry(), discoverer=discoverer)
        discovery = asyncio.create_task(session.discover_repositories())
        assert await asyncio.to_thread(started.wait, 2)

        await session.close()
        release.set()
        with pytest.raises(ServiceError) as caught:
            await discovery

        assert caught.value.code is ServiceErrorCode.CLOSED
        with pytest.raises(ServiceError) as local_access:
            _ = session.local_repositories
        assert local_access.value.code is ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_explicit_open_validates_then_issues_repository(self) -> None:
        registry = FakeRegistry()
        client = cast(FakeClient, registry.clients["github.com"])
        session = await start_session(registry)
        snapshot = await session.open_repository("github.com", "acme/widgets")
        assert snapshot.ref in session.issued_repositories
        assert client.list_mrs_calls == 1
        await session.close()


class TestReviewReads:
    @pytest.mark.asyncio
    async def test_personal_query_only_contacts_selected_discovered_host(
        self, tmp_path: Path
    ) -> None:
        github = FakeClient()
        github.my_reviews = [
            make_summary(project="acme/widgets"),
            make_summary(project="personal/elsewhere", number=9),
        ]
        gitlab = FakeClient(make_detail(GITLAB_HOST, start="start-1"))
        gitlab.my_reviews = [make_summary(GITLAB_HOST, project="team/tools")]
        registry = FakeRegistry({"github.com": github, "gitlab.com": gitlab})
        session = await start_session(
            registry,
            discoverer=lambda *args, **kwargs: [make_repo(tmp_path)],
        )
        await session.discover_repositories()
        registry.get_client_calls.clear()

        page = await session.list_reviews(
            ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=("github.com",))
        )

        assert [item.ref.repository.project_path for item in page.items] == [
            "acme/widgets",
            "personal/elsewhere",
        ]
        assert registry.get_client_calls == ["github.com"]
        await session.close()

    @pytest.mark.asyncio
    async def test_empty_host_restriction_performs_no_forge_lookup(self) -> None:
        registry = FakeRegistry({"github.com": FakeClient()})
        session = await start_session(registry)

        page = await session.list_reviews(
            ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=())
        )

        assert page.items == ()
        assert page.failures == ()
        assert registry.get_client_calls == []
        await session.close()

    @pytest.mark.asyncio
    async def test_unknown_host_restriction_fails_before_forge_lookup(self) -> None:
        registry = FakeRegistry({"github.com": FakeClient()})
        session = await start_session(registry)

        with pytest.raises(ServiceError) as caught:
            await session.list_reviews(
                ReviewQuery(
                    ReviewScope.MY_REVIEWS,
                    hostnames=("enterprise.example",),
                )
            )

        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert registry.get_client_calls == []
        await session.close()

    @pytest.mark.asyncio
    async def test_repository_must_match_host_restriction_before_lookup(
        self, tmp_path: Path
    ) -> None:
        registry = FakeRegistry(
            {"github.com": FakeClient(), "gitlab.com": FakeClient()}
        )
        session = await start_session(
            registry,
            discoverer=lambda *args, **kwargs: [make_repo(tmp_path)],
        )
        repository = (await session.discover_repositories())[0].ref
        registry.get_client_calls.clear()

        with pytest.raises(ServiceError) as caught:
            await session.list_reviews(
                ReviewQuery(
                    ReviewScope.MY_REVIEWS,
                    repository=repository,
                    hostnames=("gitlab.com",),
                )
            )

        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert registry.get_client_calls == []
        await session.close()

    @pytest.mark.asyncio
    async def test_all_open_host_restriction_uses_only_matching_issued_repositories(
        self, tmp_path: Path
    ) -> None:
        github = FakeClient()
        gitlab = FakeClient(make_detail(GITLAB_HOST, start="start-1"))
        gitlab.repository_reviews = [make_summary(GITLAB_HOST, project="team/tools")]
        registry = FakeRegistry({"github.com": github, "gitlab.com": gitlab})
        session = await start_session(
            registry,
            discoverer=lambda *args, **kwargs: [
                make_repo(tmp_path),
                make_repo(
                    tmp_path,
                    hostname="gitlab.com",
                    project="team/tools",
                    forge_type=ForgeType.GITLAB,
                ),
            ],
        )
        await session.discover_repositories()
        registry.get_client_calls.clear()

        page = await session.list_reviews(
            ReviewQuery(ReviewScope.ALL_OPEN, hostnames=("github.com",))
        )

        assert {item.ref.repository.hostname for item in page.items} == {"github.com"}
        assert registry.get_client_calls == ["github.com"]
        await session.close()

    @pytest.mark.asyncio
    async def test_inbox_preserves_success_when_other_host_fails(self) -> None:
        github = FakeClient()
        github.my_reviews = [make_summary(updated_at=NOW + timedelta(minutes=1))]
        registry = FakeRegistry(
            {
                "github.com": github,
                "gitlab.com": AuthError("github_pat_secret should not escape"),
            }
        )
        session = await start_session(registry)
        page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        assert [item.ref.number for item in page.items] == [7]
        assert page.items[0].ref.repository in session.issued_repositories
        assert len(page.failures) == 1
        assert page.failures[0].code == ServiceErrorCode.AUTHENTICATION_FAILED
        assert "secret" not in page.failures[0].message
        await session.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", [ReviewScope.MY_REVIEWS, ReviewScope.MY_MRS])
    async def test_partial_search_keeps_items_and_reports_failure(
        self, scope: ReviewScope
    ) -> None:
        client = _PartialClient(
            NotFoundError("Not found: github_pat_secret should not escape")
        )
        session = await start_session(FakeRegistry({"github.com": client}))

        page = await session.list_reviews(ReviewQuery(scope))

        assert [item.ref.number for item in page.items] == [9, 7]
        assert len(page.failures) == 1
        failure = page.failures[0]
        assert failure.hostname == "github.com"
        assert failure.code == ServiceErrorCode.NOT_FOUND
        assert failure.message.startswith("Some reviews could not be listed.")
        assert "secret" not in failure.message
        assert failure.repository is None
        await session.close()

    @pytest.mark.asyncio
    async def test_truncated_search_reports_an_incomplete_list(self) -> None:
        client = _PartialClient(ForgeError("GitHub search matched 1500 results"))
        session = await start_session(FakeRegistry({"github.com": client}))

        page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))

        assert len(page.items) == 2
        assert [failure.message for failure in page.failures] == [
            "Some reviews could not be listed, so this list is incomplete."
        ]
        await session.close()

    @pytest.mark.asyncio
    async def test_failed_github_search_fetch_lists_the_other_reviews(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200,
                    json={
                        "total_count": 3,
                        "incomplete_results": False,
                        "items": [
                            {
                                "number": number,
                                "repository_url": (
                                    "https://api.github.com/repos/acme/widgets"
                                ),
                            }
                            for number in (1, 2, 3)
                        ],
                    },
                )
            number = int(request.url.path.rsplit("/", 1)[1])
            if number == 2:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "number": number,
                    "title": f"PR {number}",
                    "state": "open",
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": f"2026-09-0{number}T00:00:00Z",
                    "user": {"login": "alice"},
                    "head": {"ref": "feature", "sha": "abc"},
                    "base": {"ref": "main", "sha": "def"},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=GITHUB_HOST.api_base
        ) as http:
            client = GitHubClient(GITHUB_HOST, http)
            session = await start_session(FakeRegistry({"github.com": client}))
            page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
            await session.close()

        assert [item.ref.number for item in page.items] == [3, 1]
        assert [(f.hostname, f.code) for f in page.failures] == [
            ("github.com", ServiceErrorCode.NOT_FOUND)
        ]

    @pytest.mark.asyncio
    async def test_review_models_hide_process_local_routing_data(self) -> None:
        host = ForgeHost(
            "github.com", ForgeType.GITHUB, "https://private-api.example.test"
        )
        detail = make_detail(host, local_path="/home/user/private/widgets")
        client = FakeClient(detail)
        client.my_reviews = [
            make_summary(host, local_path="/home/user/private/widgets")
        ]
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)

        page = await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        assert page.items[0].summary.forge_host.api_base == ""
        assert page.items[0].summary.local_path == ""
        snapshot = await session.get_review(page.items[0].ref)
        assert snapshot.detail.forge_host.api_base == ""
        assert snapshot.detail.local_path == ""
        await session.close()

    @pytest.mark.asyncio
    async def test_missing_revision_keeps_review_detail_available(self) -> None:
        client = FakeClient(make_detail(head="", base=""))
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        snapshot = await session.get_review(ReviewRef(repo.ref, 7))
        assert snapshot.detail.description == "Details"
        assert snapshot.revision is None
        assert snapshot.revision_error is not None
        assert snapshot.revision_error.code == ServiceErrorCode.REVISION_UNAVAILABLE
        await session.close()

    @pytest.mark.asyncio
    async def test_missing_gitlab_start_sha_keeps_review_detail_available(self) -> None:
        client = FakeClient(make_detail(GITLAB_HOST, start=None))
        client.repository_reviews = [make_summary(GITLAB_HOST)]
        registry = FakeRegistry({"gitlab.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("gitlab.com", "acme/widgets")

        snapshot = await session.get_review(ReviewRef(repo.ref, 7))

        assert snapshot.detail.description == "Details"
        assert snapshot.revision is None
        assert snapshot.revision_error is not None
        assert snapshot.revision_error.code == ServiceErrorCode.REVISION_UNAVAILABLE
        await session.close()

    @pytest.mark.asyncio
    async def test_discussion_commit_and_ci_reads_use_issued_repository(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repo.ref, 7)
        assert (await session.get_discussions(review))[0].id == "thread-1"
        assert (await session.get_commits(review))[0].sha == "abc"
        assert (await session.list_pipelines(repo.ref))[0].id == 11
        assert (await session.list_review_pipelines(review))[0].id == 12
        assert (await session.get_pipeline_jobs(PipelineRef(repo.ref, 11)))[0].id == 21
        assert await session.get_job_log(JobRef(repo.ref, 21)) == "safe log"
        await session.close()

    @pytest.mark.asyncio
    async def test_cancelled_forge_read_propagates_cancellation(self) -> None:
        client = FakeClient()
        client.cancel_get_mr = True
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        task = asyncio.create_task(session.get_review(ReviewRef(repo.ref, 7)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.close()

    @pytest.mark.asyncio
    async def test_delayed_read_cannot_create_clients_or_issue_refs_after_close(
        self,
    ) -> None:
        github = BlockingReviewClient()
        gitlab = FakeClient(make_detail(GITLAB_HOST, start="start-1"))
        gitlab.my_reviews = [make_summary(GITLAB_HOST, project="other/widgets")]
        registry = ClosingAwareRegistry({"github.com": github, "gitlab.com": gitlab})
        session = await ApplicationSession(
            config=Config(max_parallel=1),
            cache=FakeCache(),
            draft_store=FakeDraftStore(),  # type: ignore[arg-type]
            forge_registry=registry,
        ).start()
        read_task = asyncio.create_task(
            session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        )
        await github.started.wait()

        await session.close()
        github.release.set()

        with pytest.raises(ServiceError) as caught:
            await read_task
        assert caught.value.code == ServiceErrorCode.CLOSED
        assert registry.get_client_after_close == []
        assert session._issued_repositories == set()

    @pytest.mark.asyncio
    async def test_client_created_during_close_is_closed_and_not_returned(
        self,
    ) -> None:
        client = FakeClient()
        registry = BlockingGetClientRegistry({"github.com": client})
        session = await ApplicationSession(
            config=Config(max_parallel=2),
            cache=FakeCache(),
            draft_store=cast(DraftStore, FakeDraftStore()),
            forge_registry=registry,
            shutdown_timeout=0.05,
        ).start()
        repository = await session.open_repository("github.com", "acme/widgets")
        registry.block_get_client = True
        read_task = asyncio.create_task(
            session.get_review(ReviewRef(repository.ref, 7))
        )
        await registry.get_client_started.wait()
        # A pending client acquisition (such as a slow token lookup) does not
        # hold the client lock, so close finishes without waiting for it.
        await session.close()
        assert registry.close_calls == 1

        # The acquisition then finishes after the registry was closed, so the
        # read path closes the late client again before refusing.
        registry.get_client_release.set()

        with pytest.raises(ServiceError) as caught:
            await read_task
        assert caught.value.code == ServiceErrorCode.CLOSED
        assert client.get_mr_fresh_calls == 0
        assert registry.close_calls == 2

    @pytest.mark.asyncio
    async def test_late_client_closes_registry_when_coordinator_timed_out(
        self,
    ) -> None:
        client = FakeClient()
        registry = BlockingGetClientRegistry({"github.com": client})
        session = await ApplicationSession(
            config=Config(max_parallel=2),
            cache=FakeCache(),
            draft_store=cast(DraftStore, FakeDraftStore()),
            forge_registry=registry,
            shutdown_timeout=0.05,
        ).start()
        repository = await session.open_repository("github.com", "acme/widgets")
        registry.block_get_client = True
        read_task = asyncio.create_task(
            session.get_review(ReviewRef(repository.ref, 7))
        )
        await registry.get_client_started.wait()
        # Another client acquisition holds the lock past the close bound, so
        # the coordinator never reaches the registry.
        await session._client_lock.acquire()
        with pytest.raises(ServiceError):
            await session.close()
        assert registry.close_calls == 0
        session._client_lock.release()

        registry.get_client_release.set()

        with pytest.raises(ServiceError) as caught:
            await read_task
        assert caught.value.code == ServiceErrorCode.CLOSED
        assert client.get_mr_fresh_calls == 0
        assert registry.close_calls == 1


class TestRevisionBoundDiff:
    @pytest.mark.asyncio
    async def test_stable_complete_revision_labels_fresh_raw_diff(self) -> None:
        client = FakeClient(make_detail(start="start-1"))
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        result = await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert result.revision.head_sha == "head-1"
        assert result.revision.base_sha == "base-1"
        assert result.revision.start_sha == "start-1"
        assert result.changes[0]["filename"] == "src/widget.py"
        with pytest.raises(TypeError):
            result.changes[0]["filename"] = "changed.py"  # type: ignore[index]
        assert client.get_mr_fresh_calls == 2
        assert client.get_mr_diff_fresh_calls == 1
        assert result.changes_overflow is False
        await session.close()

    @pytest.mark.asyncio
    async def test_forge_capped_file_list_is_reported(self) -> None:
        client = FakeClient(
            replace(make_detail(start="start-1"), changes_overflow=True)
        )
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        result = await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert result.changes_overflow is True
        await session.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "second",
        [
            make_detail(head="head-2", base="base-1", start="start-1"),
            make_detail(head="head-1", base="base-2", start="start-1"),
            make_detail(head="head-1", base="base-1", start="start-2"),
        ],
    )
    async def test_any_revision_change_rejects_diff(self, second: MRDetail) -> None:
        client = FakeClient()
        client.detail_results = [make_detail(start="start-1"), second]
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        with pytest.raises(ServiceError) as caught:
            await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert caught.value.code == ServiceErrorCode.REVISION_CHANGED
        await session.close()

    @pytest.mark.asyncio
    async def test_cached_client_fresh_methods_bypass_stale_diff_cache(self) -> None:
        inner = FakeClient()
        cache = AsyncMock()
        cache.get_json.side_effect = lambda key: (
            [{"filename": "stale.py"}] if key.endswith(":diff") else None
        )
        client = CachedForgeClient(
            cast(ForgeClient, inner), cache, hostname="github.com"
        )
        registry = FakeRegistry({"github.com": client})
        session = await start_session(registry)
        repo = await session.open_repository("github.com", "acme/widgets")
        cache.get_json.reset_mock()
        result = await session.get_raw_diff(ReviewRef(repo.ref, 7))
        assert result.changes[0]["filename"] == "src/widget.py"
        cache.get_json.assert_not_awaited()
        assert inner.get_mr_fresh_calls == 2
        assert inner.get_mr_diff_fresh_calls == 1
        await session.close()


class ForgettingRegistry(FakeRegistry):
    """Record which hosts a read asks to look up again."""

    def __init__(self, clients: dict[str, object] | None = None) -> None:
        super().__init__(clients)
        self.forgotten: list[str | None] = []
        self.refresh_modes: list[bool] = []

    def forget_auth_failures(
        self, hostname: str | None = None, *, refresh: bool = False
    ) -> None:
        self.forgotten.append(hostname)
        self.refresh_modes.append(refresh)


class PruningCache(FakeCache):
    def __init__(self, *, prune_error: Exception | None = None) -> None:
        super().__init__()
        self.prune_error = prune_error
        self.prune_calls = 0

    async def prune(self) -> None:
        self.prune_calls += 1
        if self.prune_error is not None:
            raise self.prune_error


class TestExplicitRefresh:
    """An explicit refresh reads past the list cache (#309)."""

    @staticmethod
    async def _cached_session(
        tmp_path: Path,
    ) -> tuple[ApplicationSession, FakeClient, CacheStore, RepositoryRef]:
        store = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=1)
        await store.open()
        inner = FakeClient()
        client = CachedForgeClient(
            cast(ForgeClient, inner), store, hostname="github.com"
        )
        registry = ForgettingRegistry({"github.com": client})
        session = await start_session(registry)
        repository = await session.open_repository("github.com", "acme/widgets")
        inner.list_mrs_calls = 0
        return session, inner, store, repository.ref

    @pytest.mark.asyncio
    @pytest.mark.parametrize("paged", [False, True])
    async def test_fresh_all_open_bypasses_the_cached_list_and_stores_the_new_one(
        self, tmp_path: Path, paged: bool
    ) -> None:
        session, inner, store, ref = await self._cached_session(tmp_path)

        def query(*, fresh: bool = False) -> ReviewQuery:
            return ReviewQuery(
                ReviewScope.ALL_OPEN, repository=ref, paged=paged, fresh=fresh
            )

        first = await session.list_reviews(query())
        await session.list_reviews(query())
        assert inner.list_mrs_calls == 1
        assert [item.ref.number for item in first.items] == [7]

        # The review was merged and another opened; the cache still holds the
        # old list for up to a minute.
        inner.repository_reviews = [make_summary(number=9)]
        stale = await session.list_reviews(query())
        assert [item.ref.number for item in stale.items] == [7]

        refreshed = await session.list_reviews(query(fresh=True))
        assert inner.list_mrs_calls == 2
        assert [item.ref.number for item in refreshed.items] == [9]

        # What the refresh read is what the cache now serves.
        again = await session.list_reviews(query())
        assert inner.list_mrs_calls == 2
        assert [item.ref.number for item in again.items] == [9]
        await session.close()
        await store.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("paged", [False, True])
    async def test_fresh_all_open_keeps_cached_diffs(
        self, tmp_path: Path, paged: bool
    ) -> None:
        session, inner, store, ref = await self._cached_session(tmp_path)
        query = ReviewQuery(ReviewScope.ALL_OPEN, repository=ref, paged=paged)
        await session.list_reviews(query)
        diff_key = "github.com:acme/widgets:mr:7:diff"
        await store.put_json(diff_key, [{"filename": "cached.py"}], 300)

        await session.list_reviews(replace(query, fresh=True))

        # The refresh reread the list but left the review's diff cached.
        assert inner.list_mrs_calls == 2
        assert await store.get_json(diff_key) == [{"filename": "cached.py"}]
        await session.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_ci_action_invalidation_drops_lists_and_keeps_diffs(
        self, tmp_path: Path
    ) -> None:
        session, inner, store, ref = await self._cached_session(tmp_path)
        query = ReviewQuery(ReviewScope.ALL_OPEN, repository=ref)
        await session.list_reviews(query)
        diff_key = "github.com:acme/widgets:mr:7:diff"
        await store.put_json(diff_key, [{"filename": "cached.py"}], 300)

        await session._invalidate_pipeline_reads(PipelineRef(ref, 11))

        assert await store.get_json("github.com:acme/widgets:mrs:open") is None
        assert await store.get_json(diff_key) == [{"filename": "cached.py"}]
        await session.list_reviews(query)
        assert inner.list_mrs_calls == 2
        await session.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_fresh_read_forgets_failed_sign_ins_only_for_hosts_it_reads(
        self,
    ) -> None:
        registry = ForgettingRegistry(
            {"github.com": FakeClient(), "gitlab.com": FakeClient()}
        )
        session = await start_session(registry)

        await session.list_reviews(
            ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=("github.com",))
        )
        assert registry.forgotten == []

        await session.list_reviews(
            ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=("github.com",), fresh=True)
        )
        assert registry.forgotten == ["github.com"]

        registry.forgotten.clear()
        await session.list_reviews(ReviewQuery(ReviewScope.MY_MRS, fresh=True))
        assert registry.forgotten == ["github.com", "gitlab.com"]
        # A refresh never orphans a running lookup or drops a fresh failure.
        assert registry.refresh_modes == [True, True, True]
        await session.close()

    @pytest.mark.asyncio
    async def test_fresh_read_looks_up_a_failed_sign_in_again(self) -> None:
        now = [0.0]
        registry = ForgeRegistry(clock=lambda: now[0])
        session = await start_session(cast(FakeRegistry, registry))
        query = ReviewQuery(ReviewScope.MY_REVIEWS, hostnames=("github.com",))
        with patch(
            "tongs.forges.registry.resolve_token",
            side_effect=AuthError("No credentials found for github.com"),
        ) as resolved:
            first = await session.list_reviews(query)
            remembered = await session.list_reviews(query)
            assert resolved.call_count == 1

            # The user fixes their sign-in and refreshes well inside the
            # failure's minute.
            now[0] = 10.0
            refreshed = await session.list_reviews(replace(query, fresh=True))
            assert resolved.call_count == 2

        for page in (first, remembered, refreshed):
            assert [failure.code for failure in page.failures] == [
                ServiceErrorCode.AUTHENTICATION_FAILED
            ]
        await session.close()

    @pytest.mark.asyncio
    async def test_concurrent_fresh_reads_share_one_sign_in_lookup(
        self, tmp_path: Path
    ) -> None:
        # The desktop Refresh button sends one fresh read per repository, up
        # to eight at a time, and keeps going after a repository fails.
        repos = [make_repo(tmp_path, project=f"acme/repo{i}") for i in range(12)]
        now = [0.0]
        registry = ForgeRegistry(clock=lambda: now[0])
        session = await start_session(
            cast(FakeRegistry, registry), discoverer=lambda *args, **kwargs: repos
        )
        snapshots = await session.discover_repositories()
        assert len(snapshots) == 12
        started = threading.Event()
        release = threading.Event()

        def resolve(_hostname: str, _forge_type: ForgeType) -> str:
            started.set()
            assert release.wait(10)
            raise AuthError("No credentials found for github.com")

        def fresh_read(snapshot: RepositorySnapshot) -> ReviewQuery:
            return ReviewQuery(
                ReviewScope.ALL_OPEN, repository=snapshot.ref, paged=True, fresh=True
            )

        with patch(
            "tongs.forges.registry.resolve_token", side_effect=resolve
        ) as resolved:
            release.set()
            await session.list_reviews(replace(fresh_read(snapshots[0]), fresh=False))
            assert resolved.call_count == 1

            # Signed out and refreshing: the burst shares one new lookup.
            now[0] = 30.0
            started.clear()
            release.clear()
            burst = asyncio.gather(
                *(session.list_reviews(fresh_read(s)) for s in snapshots[:8])
            )
            assert await asyncio.to_thread(started.wait, 10)
            await asyncio.sleep(0.05)
            release.set()
            pages = list(await asyncio.wait_for(burst, 10))
            assert resolved.call_count == 2

            # The rest of the same refresh reuses the failure it just saw.
            pages.extend(
                await asyncio.gather(
                    *(session.list_reviews(fresh_read(s)) for s in snapshots[8:])
                )
            )
            assert resolved.call_count == 2

            # A later refresh looks again.
            now[0] = 40.0
            pages.append(await session.list_reviews(fresh_read(snapshots[0])))
            assert resolved.call_count == 3

        assert len(pages) == 13
        for page in pages:
            assert page.items == ()
            assert [failure.code for failure in page.failures] == [
                ServiceErrorCode.AUTHENTICATION_FAILED
            ]
        await session.close()

    @pytest.mark.asyncio
    async def test_fresh_read_works_with_a_registry_without_failed_sign_ins(
        self,
    ) -> None:
        session = await start_session(FakeRegistry())

        page = await session.list_reviews(
            ReviewQuery(ReviewScope.MY_REVIEWS, fresh=True)
        )

        assert [item.ref.number for item in page.items] == [7]
        assert page.failures == ()
        await session.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["retry", "cancel"])
    async def test_ci_action_invalidates_the_repository_list(
        self, tmp_path: Path, action: str
    ) -> None:
        session, inner, store, ref = await self._cached_session(tmp_path)
        query = ReviewQuery(ReviewScope.ALL_OPEN, repository=ref, paged=True)
        await session.list_reviews(query)
        await session.list_reviews(query)
        assert inner.list_mrs_calls == 1

        target = PipelineMutationTarget(PipelineRef(ref, 11))
        command = (
            RetryPipelineCommand("ci-retry", target)
            if action == "retry"
            else CancelPipelineCommand("ci-cancel", target)
        )
        receipt = await session.ci_mutations.execute(command)
        assert receipt.error is None
        assert receipt.resync_required is False

        inner.repository_reviews = [make_summary(number=9)]
        page = await session.list_reviews(query)
        assert inner.list_mrs_calls == 2
        assert [item.ref.number for item in page.items] == [9]
        await session.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_session_start_prunes_expired_cache_entries(self) -> None:
        cache = PruningCache()
        session = await start_session(FakeRegistry(), cache=cache)

        assert cache.prune_calls == 1
        await session.close()

    @pytest.mark.asyncio
    async def test_failed_prune_does_not_fail_session_start(self) -> None:
        cache = PruningCache(prune_error=sqlite3.OperationalError("locked"))
        session = await start_session(FakeRegistry(), cache=cache)

        assert cache.prune_calls == 1
        await session.list_reviews(ReviewQuery(ReviewScope.MY_REVIEWS))
        await session.close()


class TestEvents:
    @pytest.mark.asyncio
    async def test_events_are_ordered_and_overflow_requests_resync(self) -> None:
        registry = FakeRegistry()
        session = await start_session(registry, event_queue_size=1)
        stream = session.events()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        first = await pending
        assert first.sequence == 1

        session.emit_change(ServiceEventKind.REVIEW_CHANGED)
        session.emit_change(ServiceEventKind.PIPELINE_CHANGED)
        overflow = await anext(stream)
        assert overflow.sequence == 3
        assert overflow.kind == ServiceEventKind.RESYNC_REQUIRED
        await stream.aclose()
        await session.close()

    @pytest.mark.asyncio
    async def test_close_finishes_pending_event_stream(self) -> None:
        session = await start_session(FakeRegistry())
        stream = session.events()
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await session.close()
        with pytest.raises(StopAsyncIteration):
            await pending


class RevisionReadClient(FakeClient):
    """Count revision-only reads apart from full detail reads."""

    def __init__(self, detail: MRDetail | None = None) -> None:
        super().__init__(detail)
        self.get_mr_revision_calls = 0

    async def get_mr_revision(self, repo_path: str, number: int) -> MRDetail:
        self.get_mr_revision_calls += 1
        return self.detail_results[0]


class TestRevisionOnlyGuards:
    @pytest.mark.asyncio
    async def test_github_diff_reads_the_pull_request_once_before_and_once_after_its_pages(
        self,
    ) -> None:
        paths: list[str] = []
        pull = {
            "number": 7,
            "title": "Review widgets",
            "state": "open",
            "draft": False,
            "html_url": "https://github.com/acme/widgets/pull/7",
            "created_at": "2026-09-07T00:00:00Z",
            "updated_at": "2026-09-07T00:00:00Z",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "head-1"},
            "base": {"ref": "main", "sha": "base-1"},
            "changed_files": 1,
        }
        files = [
            {
                "filename": "src/widget.py",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/repos/acme/widgets/pulls":
                return httpx.Response(200, json=[])
            if request.url.path == "/repos/acme/widgets/pulls/7":
                return httpx.Response(200, json=pull)
            if request.url.path == "/repos/acme/widgets/pulls/7/files":
                return httpx.Response(200, json=files)
            return httpx.Response(500, json={"message": "unexpected request"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=GITHUB_HOST.api_base
        ) as http:
            client = GitHubClient(GITHUB_HOST, http)
            session = await start_session(FakeRegistry({"github.com": client}))
            repository = await session.open_repository("github.com", "acme/widgets")
            paths.clear()
            raw = await session.get_raw_diff(ReviewRef(repository.ref, 7))
            await session.close()

        first_page = paths.index("/repos/acme/widgets/pulls/7/files")
        assert paths[:first_page] == ["/repos/acme/widgets/pulls/7"]
        assert paths == [
            "/repos/acme/widgets/pulls/7",
            "/repos/acme/widgets/pulls/7/files",
            "/repos/acme/widgets/pulls/7",
        ]
        assert raw.revision == ReviewRevision("head-1", "base-1", None)
        assert raw.changes[0]["filename"] == "src/widget.py"

    @pytest.mark.asyncio
    async def test_mutation_guards_use_revision_reads_and_detail_stays_full(
        self,
    ) -> None:
        client = RevisionReadClient()
        client.diff = [
            {
                "filename": "src/widget.py",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ]
        session = await start_session(FakeRegistry({"github.com": client}))
        repository = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repository.ref, 7)

        await session.review_mutations.validate(
            InlineCommentCommand(
                "inline-guard",
                review,
                ReviewRevision("head-1", "base-1", None),
                DiffAnchor("src/widget.py", "src/widget.py", 1, DiffSide.RIGHT),
                "body",
            )
        )
        await session.mr_actions.capabilities(review)

        assert client.get_mr_fresh_calls == 0
        assert client.get_mr_revision_calls > 0
        assert client.get_mr_diff_fresh_calls == 1

        snapshot = await session.get_review(review)
        assert snapshot.detail.ci_status is CIStatus.SUCCESS
        assert client.get_mr_fresh_calls == 1
        await session.close()
