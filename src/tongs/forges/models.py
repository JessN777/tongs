"""Shared data models for forge operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from tongs.scanner.repo import ForgeType


class MRState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CIStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ReviewDecision(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    REVIEW_REQUIRED = "review_required"
    NONE = "none"


class SourceCleanupStatus(Enum):
    """Native evidence for optional source-branch cleanup after a merge."""

    NOT_REQUESTED = "not_requested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ForgeMutationResult:
    """Stable remote identity returned after one confirmed forge write."""

    remote_id: str
    comment_id: str | None = None
    discussion_id: str | None = None
    cache_invalidated: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.remote_id, str) or not self.remote_id:
            raise ValueError("remote_id is required")
        if any(
            value is not None and (not isinstance(value, str) or not value)
            for value in (self.comment_id, self.discussion_id)
        ):
            raise ValueError("remote identities must be non-empty strings")


@dataclass(frozen=True, slots=True)
class ForgeMergeResult:
    """Confirmed merge identity with an independent source-cleanup outcome."""

    remote_id: str
    merge_sha: str
    source_cleanup: SourceCleanupStatus = SourceCleanupStatus.NOT_REQUESTED
    cache_invalidated: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.remote_id, str) or not self.remote_id:
            raise ValueError("remote_id is required")
        if not isinstance(self.merge_sha, str) or not self.merge_sha:
            raise ValueError("merge_sha is required")
        if not isinstance(self.source_cleanup, SourceCleanupStatus):
            raise TypeError("source_cleanup must be a SourceCleanupStatus")


@dataclass(frozen=True)
class ForgeHost:
    """Identifies a forge instance."""

    hostname: str
    forge_type: ForgeType
    api_base: str


@dataclass(frozen=True)
class User:
    username: str
    display_name: str = ""


@dataclass(frozen=True)
class MRSummary:
    """Lightweight MR for list views. No diff or comment data."""

    forge_host: ForgeHost
    repo_path: str
    local_path: str
    number: int
    title: str
    author: User
    state: MRState
    is_draft: bool
    source_branch: str
    target_branch: str
    ci_status: CIStatus
    created_at: datetime
    updated_at: datetime
    web_url: str
    comment_count: int = 0
    has_conflicts: bool = False
    labels: tuple[str, ...] = ()
    # GitHub-specific
    review_decision: ReviewDecision | None = None
    additions: int | None = None
    deletions: int | None = None
    # The source branch head commit, when the list response names it, so CI
    # can be looked up for one review after a list read without CI. It is
    # keyword-only, so it never takes a positional slot: MRDetail's own fields
    # keep their positions after MRSummary's. It is not named ``head_sha``
    # because MRDetail already has that field, filled from the revision the
    # detail read returns.
    head_commit_sha: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class MRPage:
    """One page of a repository's review list, newest update first."""

    items: tuple[MRSummary, ...]
    has_next: bool


@dataclass(frozen=True)
class MRDetail(MRSummary):
    """Full MR with description and metadata. Still no diff/comments."""

    description: str = ""
    merge_status: str = ""
    approvals: tuple[User, ...] = ()
    reviewers: tuple[User, ...] = ()
    assignees: tuple[User, ...] = ()
    changes_count: int = 0
    # GitLab-specific
    detailed_merge_status: str | None = None
    draft_notes_count: int | None = None
    # GitHub-specific
    status_check_rollup: str | None = None
    # Revision metadata, appended to preserve positional compatibility.
    head_sha: str = ""
    base_sha: str = ""
    start_sha: str | None = None
    # True when the forge capped the changed-file list, so changes_count is a
    # lower bound and the diff is partial. Appended for positional compatibility.
    changes_overflow: bool = False


@dataclass(frozen=True)
class InlineComment:
    """A comment anchored to a specific diff position."""

    id: str
    author: User
    body: str
    created_at: datetime
    file_path: str
    old_line: int | None = None
    new_line: int | None = None
    is_resolved: bool = False
    replies: tuple[InlineComment, ...] = ()


@dataclass(frozen=True)
class Discussion:
    """A comment thread (may or may not be inline)."""

    id: str
    is_inline: bool
    root_comment: InlineComment
    is_resolved: bool = False
    resolvable: bool = True


@dataclass(frozen=True)
class Pipeline:
    """CI pipeline / workflow run."""

    id: int
    status: CIStatus
    ref: str
    sha: str
    web_url: str
    source: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class PipelineJob:
    """A job within a pipeline."""

    id: int
    name: str
    stage: str
    status: CIStatus
    web_url: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    allow_failure: bool = False


@dataclass(frozen=True, slots=True)
class JobLogTail:
    """The end of a job log, kept within a byte budget.

    ``text`` holds at most the budget in UTF-8 bytes. ``total_bytes`` is the
    size of the whole log as the forge sent it, and ``truncated`` says whether
    earlier output was left out. A truncated tail starts on a line boundary
    unless its first line alone is longer than the budget.
    """

    text: str
    total_bytes: int
    truncated: bool

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        total_bytes: int,
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> JobLogTail:
        """Decode the end of ``data``, cut to ``max_bytes`` UTF-8 bytes.

        ``data`` is the end of a log of ``total_bytes`` bytes; one byte more
        than the budget lets a cut that falls on a line start keep that line.
        Undecodable bytes become replacement characters, and the cut is
        measured again after decoding, so the text never passes ``max_bytes``
        UTF-8 bytes whatever the source encoding.
        """
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        total_bytes = max(total_bytes, len(data))
        truncated = total_bytes > max_bytes
        # A cut right after a newline already starts a line. The byte before
        # the cut is known only when ``data`` holds more than the budget.
        at_line_start = False
        if len(data) > max_bytes:
            cut = len(data) - max_bytes
            at_line_start = data[cut - 1 : cut] == b"\n"
            data = data[cut:]
        try:
            text = data.decode(encoding, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        if truncated and not at_line_start:
            text = _from_line_start(text)
        encoded_length = len(text.encode("utf-8"))
        if encoded_length > max_bytes:
            # Each character takes at least one byte, so dropping the excess
            # in characters always fits.
            text = _from_line_start(text[encoded_length - max_bytes :])
            truncated = True
        return cls(text, total_bytes, truncated)


def _from_line_start(text: str) -> str:
    """Drop a partial first line, unless it is the only line there is."""
    newline = text.find("\n")
    if newline == -1 or newline == len(text) - 1:
        return text.lstrip("�")
    return text[newline + 1 :]


@dataclass(frozen=True)
class Commit:
    """A commit in an MR/PR."""

    sha: str
    short_sha: str
    title: str
    message: str
    author: User
    created_at: datetime | None = None
    web_url: str = ""
