"""Tool logic for tongs-mcp, kept apart from the MCP SDK.

Each tool is a plain async function that takes a started
:class:`ApplicationSession`, so tongs-mcp reads and writes through the same
shared services as the terminal and desktop apps: revision-bound approvals,
complete diffs, bounded lists and the shared sanitised error messages.
:mod:`tongs.mcp.server` only registers these functions with the SDK and turns
:class:`ToolFailure` into the SDK's tool error.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager

from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import DiffFile
from tongs.forges.models import CIStatus, MRState, MRSummary, ReviewDecision
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import (
    RepositoryRef,
    ReviewListItem,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
)
from tongs.services.review_mutations import (
    GeneralComment,
    MutationStatus,
    ReviewVerdict,
)
from tongs.services.session import ApplicationSession

LIST_STATES = ("open", "closed", "merged")
DEFAULT_LIST_LIMIT = 30
MAX_LIST_LIMIT = 100
MAX_DIFF_BYTES = 256 * 1024
PIPELINE_LIMIT = 20

_REPO_PATH_RE = re.compile(r"(?!.*\.\.)[\w.-]+(?:/[\w.-]+){2,}", re.ASCII)
# A full SHA-1 or SHA-256 commit id, as get_mr returns it.
_HEAD_SHA_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")
# Room kept under the diff cap for the closing notes.
_DIFF_NOTE_RESERVE = 512
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

HEAD_SHA_MISSING = (
    "head_sha is required. Call get_mr first and pass the head_sha it returns."
)
HEAD_SHA_MALFORMED = (
    "head_sha must be the full commit SHA that get_mr returned. "
    "Call get_mr first and pass its head_sha."
)
HEAD_SHA_STALE = (
    "The review has new commits since get_mr. Call get_mr again, check the "
    "changes, and approve with the new head_sha."
)


class ToolFailure(Exception):
    """A tool call failed with a message that is safe to show the client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


SessionFactory = Callable[[], ApplicationSession]


class SessionHolder:
    """Own the one application session a tongs-mcp process uses.

    The session starts on the first tool call and stays open until
    :meth:`close`, which the server calls when it shuts down. A session that
    failed to start is dropped, so the next call tries again.
    """

    def __init__(self, factory: SessionFactory = ApplicationSession) -> None:
        self._factory = factory
        self._session: ApplicationSession | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> ApplicationSession | None:
        """Return the started session, if one is open."""
        return self._session

    async def get(self) -> ApplicationSession:
        """Return the started session, starting it on first use."""
        async with self._lock:
            if self._session is None:
                session = self._factory()
                await session.start()
                self._session = session
            return self._session

    async def close(self) -> None:
        """Close the session, if one was started, as the process exits.

        This is the session's final close: no later close will retry, so the
        draft store, the registry and the cache close even after a failed
        review submission cleanup. Left open, their SQLite worker threads
        would keep the process alive after the server stops (#442).
        """
        async with self._lock:
            session, self._session = self._session, None
        if session is not None:
            await session.close(final=True)


async def open_session(holder: SessionHolder, operation: str) -> ApplicationSession:
    """Return the holder's started session, or fail with a safe message."""
    with _safe_errors(operation):
        return await holder.get()


@contextmanager
def _safe_errors(operation: str) -> Iterator[None]:
    """Turn any failure into the shared sanitised message for ``operation``.

    Service errors already carry a safe message. Anything else goes through
    :func:`translate_error`, which never copies error text, response bodies or
    credentials. The original exception is not chained.
    """
    try:
        yield
    except ToolFailure:
        raise
    except ServiceError as error:
        raise ToolFailure(f"{operation} failed: {error.message}") from None
    except Exception as error:  # noqa: BLE001 - Sanitize every failure.
        message = translate_error(error, operation=operation).message
        raise ToolFailure(f"{operation} failed: {message}") from None


def parse_repo_path(session: ApplicationSession, repo_path: str) -> RepositoryRef:
    """Check a 'hostname/owner/repo' path and return its repository identity.

    Only github.com, gitlab.com and the hosts configured in config.toml are
    accepted, the same set the terminal and desktop apps use. Any other host
    is rejected here, before a client is built, a token is looked up or a
    request is made.
    """
    if not isinstance(repo_path, str) or not _REPO_PATH_RE.fullmatch(repo_path):
        raise ToolFailure(f"repo_path must be 'hostname/owner/repo', got: {repo_path}")
    hostname, path = repo_path.split("/", 1)
    registry = session.forge_registry
    host = (
        registry.get_host(hostname) if hostname in registry.active_hostnames() else None
    )
    if host is None:
        raise ToolFailure(
            f"Host {hostname!r} is not configured; add it under [hosts.*] in "
            "config.toml to use it with tongs-mcp"
        )
    if host.forge_type == ForgeType.GITHUB and path.count("/") != 1:
        raise ToolFailure(
            f"repo_path for a GitHub host must be 'hostname/owner/repo', got: {repo_path}"
        )
    try:
        return RepositoryRef(hostname, path)
    except ValueError:
        raise ToolFailure(
            f"repo_path must be 'hostname/owner/repo', got: {repo_path}"
        ) from None


def parse_review(session: ApplicationSession, repo_path: str, number: int) -> ReviewRef:
    """Check a repository path and review number and return the review identity."""
    repository = parse_repo_path(session, repo_path)
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ToolFailure("number must be a positive integer.")
    return ReviewRef(repository, number)


async def admit_repository(session: ApplicationSession, ref: RepositoryRef) -> None:
    """Have the session issue ``ref`` if it has not yet.

    tongs-mcp reads any repository on a configured host, not only the ones a
    local scan found. The session's ``open_repository`` issues one with a
    single one-item list read, once per repository for the server's life.
    """
    if ref not in session.issued_repositories:
        await session.open_repository(ref.hostname, ref.project_path)


async def list_mrs(
    session: ApplicationSession,
    repo_path: str,
    state: str = "open",
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict[str, object]]:
    """List up to ``limit`` reviews in one page, most recently updated first.

    Open reviews get their CI status through the shared per-review lookup,
    for the returned rows only. Closed and merged reviews report CI as
    ``unknown`` where the forge list does not carry it.
    """
    repository = parse_repo_path(session, repo_path)
    if not isinstance(state, str) or state not in LIST_STATES:
        raise ToolFailure("state must be one of: open, closed, merged.")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_LIST_LIMIT
    ):
        raise ToolFailure(f"limit must be an integer from 1 to {MAX_LIST_LIMIT}.")
    with _safe_errors("list_mrs"):
        await admit_repository(session, repository)
        host = session.forge_registry.get_host(repository.hostname)
        # GitHub has no merged filter: a merged pull request is a closed one
        # with merged_at set, which the client reads as MERGED.
        merged_on_github = (
            state == "merged"
            and host is not None
            and host.forge_type == ForgeType.GITHUB
        )
        page = await session.list_reviews(
            ReviewQuery(
                ReviewScope.ALL_OPEN,
                repository=repository,
                state=MRState.CLOSED if merged_on_github else MRState(state),
                per_page=limit,
                paged=True,
                with_ci=False,
            )
        )
        if page.failures:
            raise ToolFailure(f"list_mrs failed: {page.failures[0].message}")
        items = page.items
        if merged_on_github:
            items = tuple(
                item for item in items if item.summary.state is MRState.MERGED
            )
        items = items[:limit]
        statuses = await _ci_statuses(session, items) if state == "open" else {}
        return [
            _summary_dict(item.summary, statuses.get(item.ref, item.summary.ci_status))
            for item in items
        ]


async def get_mr(
    session: ApplicationSession, repo_path: str, number: int
) -> dict[str, object]:
    """Read one review, including the head SHA that approve_mr needs."""
    review = parse_review(session, repo_path, number)
    with _safe_errors("get_mr"):
        await admit_repository(session, review.repository)
        snapshot = await session.get_review(review)
        mr = snapshot.detail
        revision = snapshot.revision
        head_sha = revision.head_sha if revision is not None else mr.head_sha
        base_sha = revision.base_sha if revision is not None else mr.base_sha
        return {
            "number": mr.number,
            "title": mr.title,
            "description": mr.description,
            "author": mr.author.username,
            "state": mr.state.value,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "ci_status": mr.ci_status.value,
            "is_draft": mr.is_draft,
            "has_conflicts": mr.has_conflicts,
            "additions": mr.additions,
            "deletions": mr.deletions,
            "approvals": [u.username for u in mr.approvals],
            "reviewers": [u.username for u in mr.reviewers],
            "labels": list(mr.labels),
            "web_url": mr.web_url,
            "head_sha": head_sha or None,
            "base_sha": base_sha or None,
        }


async def get_mr_diff(session: ApplicationSession, repo_path: str, number: int) -> str:
    """Read the whole diff of a review as unified diff text, capped in size."""
    review = parse_review(session, repo_path, number)
    with _safe_errors("get_mr_diff"):
        await admit_repository(session, review.repository)
        raw = await session.get_raw_diff(review)
        files = convert_forge_changes(raw.changes)
        return render_diff(raw.changes, files, overflow=raw.changes_overflow)


async def post_comment(
    session: ApplicationSession, repo_path: str, number: int, body: str
) -> str:
    """Post a general comment through the shared review mutation service."""
    review = parse_review(session, repo_path, number)
    try:
        command = GeneralComment(_operation_id("comment"), review, body)
    except (TypeError, ValueError):
        raise ToolFailure(
            "body must be non-empty text of at most 65536 bytes."
        ) from None
    with _safe_errors("post_comment"):
        await admit_repository(session, review.repository)
        outcome = await session.review_mutations.execute(command)
        if outcome.status is not MutationStatus.KNOWN:
            raise ToolFailure(
                "post_comment failed: The forge did not confirm the comment. "
                "Check the review before posting it again."
            )
        return "Comment posted successfully"


async def approve_mr(
    session: ApplicationSession, repo_path: str, number: int, head_sha: str
) -> str:
    """Approve a review at the head commit the caller read with get_mr.

    The approval goes through the shared review mutation service bound to the
    review's current revision, and the forge receives the head SHA, so an
    approval never lands on commits pushed after the caller read the review.
    """
    if head_sha is None or head_sha == "":
        raise ToolFailure(HEAD_SHA_MISSING)
    if not isinstance(head_sha, str) or not _HEAD_SHA_RE.fullmatch(head_sha):
        raise ToolFailure(HEAD_SHA_MALFORMED)
    expected = head_sha.lower()
    review = parse_review(session, repo_path, number)
    with _safe_errors("approve_mr"):
        await admit_repository(session, review.repository)
        snapshot = await session.get_review(review)
        revision = snapshot.revision
        if revision is None:
            error = snapshot.revision_error
            message = (
                error.message
                if error is not None
                else "The forge did not provide a complete review revision."
            )
            raise ToolFailure(f"approve_mr failed: {message}")
        if revision.head_sha.lower() != expected:
            raise ToolFailure(f"approve_mr failed: {HEAD_SHA_STALE}")
        command = ReviewVerdict(
            _operation_id("approve"), review, revision, ReviewDecision.APPROVED
        )
        try:
            outcome = await session.review_mutations.execute(command)
        except ServiceError as error:
            if error.code is ServiceErrorCode.REVISION_CHANGED:
                raise ToolFailure(f"approve_mr failed: {HEAD_SHA_STALE}") from None
            raise
        if outcome.status is not MutationStatus.KNOWN:
            raise ToolFailure(
                "approve_mr failed: The forge did not confirm the approval. "
                "Call get_mr to check whether it was applied before trying again."
            )
        return "MR approved successfully"


async def list_pipelines(
    session: ApplicationSession, repo_path: str, number: int
) -> list[dict[str, object]]:
    """List the most recent pipelines or workflow runs of a review."""
    review = parse_review(session, repo_path, number)
    with _safe_errors("list_pipelines"):
        await admit_repository(session, review.repository)
        pipelines = await session.list_review_pipelines(review, per_page=PIPELINE_LIMIT)
        return [
            {
                "id": p.id,
                "status": p.status.value,
                "ref": p.ref,
                "sha": p.sha[:7],
                "source": p.source,
                "duration_seconds": p.duration_seconds,
                "web_url": p.web_url,
            }
            for p in pipelines
        ]


def render_diff(
    changes: Sequence[Mapping[str, object]],
    files: Sequence[DiffFile],
    *,
    overflow: bool = False,
    max_bytes: int = MAX_DIFF_BYTES,
) -> str:
    """Render every changed file as unified diff text of at most ``max_bytes``.

    Each file gets ``---`` and ``+++`` headers. A file without a patch gets a
    marker line instead, such as ``[binary]``, ``[renamed]`` or
    ``[truncated]``. Output stops at the cap with a final note naming how many
    files were left out, and a forge-capped file list ends with a note too.
    """
    blocks = [_file_block(change, file) for change, file in zip(changes, files)]
    budget = max_bytes - _DIFF_NOTE_RESERVE
    parts: list[str] = []
    used = 0
    omitted = 0
    for index, block in enumerate(blocks):
        size = len(block.encode()) + 1
        if used + size <= budget:
            parts.append(block)
            used += size
            continue
        marker = "\n[truncated]"
        cut = _cut_block(block, budget - used - len(marker.encode()) - 1)
        if cut:
            parts.append(cut + marker)
            omitted = len(blocks) - index - 1
        else:
            omitted = len(blocks) - index
        parts.append(
            f"[diff stopped at the {max_bytes // 1024} KiB cap: "
            f"{omitted} of {len(blocks)} {'file' if len(blocks) == 1 else 'files'} "
            "left out]"
        )
        break
    if overflow:
        parts.append(
            "[overflow] The forge capped the changed-file list, so files past "
            "its limit are missing from this diff."
        )
    return "\n".join(parts)


def _file_block(change: Mapping[str, object], file: DiffFile) -> str:
    """Render one file: headers, then its patch or a marker for its shape."""
    patch = _patch_text(change).rstrip("\n")
    lines: list[str] = []
    has_header = patch.lstrip().startswith(("--- ", "diff --git "))
    if not (file.hunks and has_header):
        lines.append(f"--- a/{_safe_path(file.old_path)}")
        lines.append(f"+++ b/{_safe_path(file.new_path)}")
    if file.hunks:
        lines.append(patch.lstrip() if has_header else patch)
    lines.extend(_markers(file))
    return "\n".join(lines)


def _markers(file: DiffFile) -> list[str]:
    markers: list[str] = []
    if file.is_binary:
        markers.append("[binary]")
    if file.is_rename_only:
        markers.append("[renamed]")
    if file.is_mode_only:
        markers.append("[mode changed]")
    if file.is_empty:
        markers.append("[empty]")
    if file.is_truncated:
        markers.append("[truncated]")
    if file.is_unavailable or (not file.hunks and not markers):
        markers.append("[no patch]")
    return markers


def _cut_block(block: str, limit: int) -> str:
    """Return the whole lines of ``block`` that fit ``limit`` bytes.

    Nothing is returned unless both file headers fit.
    """
    kept: list[str] = []
    used = 0
    for line in block.split("\n"):
        size = len(line.encode()) + (1 if kept else 0)
        if used + size > limit:
            break
        kept.append(line)
        used += size
    return "\n".join(kept) if len(kept) >= 2 else ""


def _patch_text(change: Mapping[str, object]) -> str:
    for key in ("diff", "patch"):
        value = change.get(key)
        if isinstance(value, str):
            return value
    return ""


def _safe_path(path: str) -> str:
    """Keep a forge path on one header line."""
    return _CONTROL_CHARS.sub("?", path)


def _summary_dict(summary: MRSummary, ci_status: CIStatus) -> dict[str, object]:
    return {
        "number": summary.number,
        "title": summary.title,
        "author": summary.author.username,
        "source_branch": summary.source_branch,
        "target_branch": summary.target_branch,
        "ci_status": ci_status.value,
        "web_url": summary.web_url,
    }


async def _ci_statuses(
    session: ApplicationSession, items: Sequence[ReviewListItem]
) -> dict[ReviewRef, CIStatus]:
    """Look up CI for listed rows without it, through the shared lookup.

    Only the returned rows are looked up, with no more in flight than the
    terminal app allows for the host. A failed lookup, or a host that answered
    with a rate limit, leaves the row ``unknown``.
    """
    pending = [item for item in items if item.summary.ci_status is CIStatus.UNKNOWN]
    if not pending:
        return {}
    hostname = pending[0].ref.repository.hostname
    semaphore = asyncio.Semaphore(await _ci_lookup_limit(session, hostname))

    async def lookup(item: ReviewListItem) -> tuple[ReviewRef, CIStatus]:
        async with semaphore:
            if session.host_rate_limit(hostname) is not None:
                return item.ref, CIStatus.UNKNOWN
            try:
                status = await session.get_review_ci_status(
                    item.ref, head_sha=item.summary.head_commit_sha
                )
            except ServiceError:
                return item.ref, CIStatus.UNKNOWN
            return item.ref, status

    return dict(await asyncio.gather(*(lookup(item) for item in pending)))


async def _ci_lookup_limit(session: ApplicationSession, hostname: str) -> int:
    """Return how many CI lookups of a host may be in flight at once.

    This matches the terminal app: ``max_parallel``, or one full set of
    GitHub GraphQL batches while the client batches lookups.
    """
    max_parallel = session.config.max_parallel
    try:
        client = await session.forge_registry.get_client(hostname)
    except Exception:  # noqa: BLE001 - The lookup itself reports any failure.
        return max_parallel
    batched = getattr(client, "batched_ci_lookups", 1)
    if type(batched) is not int or batched <= 1:
        return max_parallel
    return max(max_parallel, batched)


def _operation_id(kind: str) -> str:
    return f"mcp-{kind}-{uuid.uuid4().hex}"
