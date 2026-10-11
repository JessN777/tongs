"""Bounded, validated reads of one review file at a commit.

The terminal and the desktop sidecar both read file content through this
service, so the input rules, the size limits and the outcome wording are the
same in both apps. Expected outcomes (too large, binary, missing, unsupported)
come back as ``FileRead`` values; authentication, rate-limit and network
failures still raise ``ServiceError``.
"""

from __future__ import annotations

import asyncio
import functools
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tongs.diff.generate import GenerationTooLargeError, generate_file_diff
from tongs.diff.models import DiffFile, FileStatus
from tongs.errors import (
    ContentTooLargeError,
    NotFoundError,
    UnsupportedFileReadError,
    ValidationError,
)
from tongs.forges.base import (
    ForgeClient,
    is_full_commit_sha,
    read_file_at_ref,
    read_merge_base,
    revision_base_is_merge_base,
)
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import ReviewRef, ReviewRevision

_MIB = 1024 * 1024

#: The longest accepted file path, in UTF-8 bytes.
MAX_PATH_BYTES = 4096
#: How many leading bytes are checked for a NUL, as git does, to call a file
#: binary.
BINARY_SNIFF_BYTES = 8000
#: The most bytes the in-memory file cache counts across all files.
MEMORY_CACHE_BYTES = 32 * _MIB
# Bytes counted for each memory cache entry on top of its body, so many empty
# files cannot grow the cache without bound.
_ENTRY_OVERHEAD_BYTES = 256
# Bytes counted for each decoded line on top of its text: a str object and
# its slot in the lines tuple take about this much, so a file of short lines
# costs far more than its body.
_LINE_OVERHEAD_BYTES = 64
#: How many generated file diffs the service keeps in memory.
GENERATED_CACHE_ENTRIES = 8
#: The most bytes the generated diff cache counts across all its diffs.
GENERATED_CACHE_BYTES = 64 * _MIB
# Bytes counted for each line of a generated diff on top of its text: a
# DiffLine object with its fields takes about this much.
_DIFF_LINE_OVERHEAD_BYTES = 192
#: How many merge bases the service keeps in memory.
MERGE_BASE_CACHE_ENTRIES = 64
# File modes that hold no text to diff: a symbolic link and a submodule.
_NON_TEXT_MODES = frozenset({"120000", "160000"})

_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# NUL, the other C0 control characters and DEL.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class FileReadStatus(StrEnum):
    """The outcome of one file read."""

    OK = "ok"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class FileText:
    """A text file split into lines.

    ``lines`` holds the file split on ``"\\n"`` only, so a ``"\\r"`` stays in
    its line's content, as in the diff parser. The empty element after a final
    newline is dropped and recorded as ``ends_with_newline``. An empty file has
    no lines and counts as ending with a newline, because it has no last line
    without one. ``byte_size`` is the size of the body the forge returned.
    """

    lines: tuple[str, ...]
    ends_with_newline: bool
    byte_size: int


@dataclass(frozen=True, slots=True)
class FileRead:
    """One file read: its status, its text when OK, and a message otherwise."""

    status: FileReadStatus
    text: FileText | None
    message: str


@dataclass(frozen=True, slots=True)
class FileReadLimits:
    """The most bytes and lines a file read accepts."""

    max_bytes: int
    max_lines: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_lines <= 0:
            raise ValueError("file read limits must be positive")


#: Limits for expanding diff context in a review.
EXPAND_LIMITS = FileReadLimits(max_bytes=2 * _MIB, max_lines=20_000)
#: Limits for reading one side of a diff that is generated locally.
GENERATE_LIMITS = FileReadLimits(max_bytes=5 * _MIB, max_lines=100_000)

BINARY_MESSAGE = "This file is binary at this commit."
NOT_FOUND_MESSAGE = (
    "The forge has no copy of this file at this commit. Refresh the review."
)
UNSUPPORTED_MESSAGE = "This forge can't read files at a commit."

NOT_TRUNCATED_MESSAGE = (
    "The forge sent this file's diff, so there is nothing to generate."
)
NO_MERGE_BASE_MESSAGE = (
    "Couldn't find where this branch started, so the diff can't be generated."
)
GENERATE_BINARY_MESSAGE = "This file is binary, so there is no text diff to generate."
GENERATE_LINK_MESSAGE = "Symbolic links and submodules have no text diff to generate."


class GenerationStatus(StrEnum):
    """The outcome of generating one file's diff locally."""

    OK = "ok"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    NOT_TRUNCATED = "not_truncated"
    NO_MERGE_BASE = "no_merge_base"


@dataclass(frozen=True, slots=True)
class GeneratedDiff:
    """One generated file diff, or why there is none.

    ``file`` is the forge's file with locally computed hunks and
    ``new_text`` the version at the review head (None for a deleted file), so
    a caller can expand context around the hunks without another read.
    """

    status: GenerationStatus
    file: DiffFile | None
    new_text: FileText | None
    message: str


class ForgeCall(Protocol):
    """Run one forge awaitable and translate its failure, as the session does."""

    def __call__[T](
        self, awaitable: Awaitable[T], *, operation: str, hostname: str
    ) -> Awaitable[T]: ...


async def _translate_call[T](
    awaitable: Awaitable[T], *, operation: str, hostname: str
) -> T:
    try:
        return await awaitable
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001 - Sanitize forge boundary.
        raise translate_error(error, operation=operation, hostname=hostname) from None


class ReviewFileService:
    """Read review files at a commit, bounded, validated and cached.

    ``get_client`` returns the forge client for an issued review, and ``call``
    runs each forge read so the session records rate limits and translates
    failures. A byte-bounded in-memory cache sits above the client's own
    response cache.
    """

    def __init__(
        self,
        *,
        get_client: Callable[[ReviewRef, str], Awaitable[ForgeClient]],
        call: ForgeCall = _translate_call,
        memory_cache_bytes: int = MEMORY_CACHE_BYTES,
    ) -> None:
        if memory_cache_bytes < 0:
            raise ValueError("memory_cache_bytes must not be negative")
        self._get_client = get_client
        self._call = call
        self._memory: _SizedLRU[_MemoryKey, FileText] = _SizedLRU(
            memory_cache_bytes, _entry_size
        )
        self._generated: _SizedLRU[_GeneratedKey, GeneratedDiff] = _SizedLRU(
            GENERATED_CACHE_BYTES,
            _generated_size,
            max_entries=GENERATED_CACHE_ENTRIES,
        )
        # Generations whose worker thread is still running, by the same key as
        # the generated cache. A thread cannot be stopped, so a cancelled call
        # leaves it running; a later call for the same file waits for it
        # instead of starting another.
        self._running: dict[_GeneratedKey, _RunningGeneration] = {}
        self._merge_bases: _EntryLRU[_MergeBaseKey, str] = _EntryLRU(
            MERGE_BASE_CACHE_ENTRIES
        )

    async def read_file(
        self,
        review: ReviewRef,
        path: str,
        ref: str,
        *,
        limits: FileReadLimits = EXPAND_LIMITS,
    ) -> FileRead:
        """Read ``path`` at commit ``ref`` of ``review``'s repository.

        ``ref`` must be a full lowercase commit SHA, and ``path`` a plain
        repository path; anything else raises ``ServiceError(INVALID_INPUT)``
        before any request. Too large, binary, missing and unsupported files
        come back as a status with a message.
        """
        validate_commit_sha(ref)
        validate_file_path(path)
        repository = review.repository
        # The client lookup also proves the session is open and issued this
        # review's repository, so a memory cache hit never skips those checks.
        client = await self._get_client(review, "read_review_file")
        key = (repository.hostname, repository.project_path, ref, path)
        cached = self._memory.get(key)
        if cached is not None:
            return _within_limits(cached, limits)

        async def fetch() -> bytes | FileReadStatus:
            try:
                return await read_file_at_ref(
                    client,
                    repository.project_path,
                    path,
                    ref,
                    max_bytes=limits.max_bytes,
                )
            except ContentTooLargeError:
                return FileReadStatus.TOO_LARGE
            except NotFoundError:
                return FileReadStatus.NOT_FOUND
            except UnsupportedFileReadError:
                return FileReadStatus.UNSUPPORTED

        result = await self._call(
            fetch(), operation="read_review_file", hostname=repository.hostname
        )
        if isinstance(result, FileReadStatus):
            return _failed(result, limits)
        if not isinstance(result, bytes):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid file.",
            )
        if len(result) > limits.max_bytes:
            return _failed(FileReadStatus.TOO_LARGE, limits)
        if b"\0" in result[:BINARY_SNIFF_BYTES]:
            return _failed(FileReadStatus.BINARY, limits)
        text = decode_file_text(result)
        if len(text.lines) > limits.max_lines:
            return _failed(FileReadStatus.TOO_LARGE, limits)
        self._memory.put(key, text)
        return FileRead(FileReadStatus.OK, text, "")

    async def generate_file_diff(
        self, review: ReviewRef, revision: ReviewRevision, file: DiffFile
    ) -> GeneratedDiff:
        """Compute the diff of a file the forge withheld, from its two versions.

        Only an ``is_truncated`` file is generated. The old version is read at
        the merge base of the revision, never at a GitHub base branch tip, and
        the new one at the review head, both within ``GENERATE_LIMITS`` and at
        the same time. The diff is computed in a worker thread. Too large,
        binary, missing and unsupported versions, and a merge base the forge
        cannot find, come back as a status with a message; authentication,
        rate-limit and network failures raise ``ServiceError``. A cancelled
        call stops both reads and keeps nothing; a worker thread already
        computing the diff runs to its end, and a call for the same file
        while it runs waits for that thread instead of starting another.
        """
        refused = _generation_refusal(file)
        if refused is not None:
            return refused
        needs_old = file.status is not FileStatus.ADDED
        needs_new = file.status is not FileStatus.DELETED
        validate_commit_sha(revision.head_sha)
        if needs_old:
            validate_commit_sha(revision.base_sha)
            validate_file_path(file.old_path)
        if needs_new:
            validate_file_path(file.new_path)
        repository = review.repository
        client = await self._get_client(review, "generate_file_diff")
        old_ref = ""
        if needs_old:
            merge_base = await self._merge_base(client, review, revision)
            if merge_base is None:
                return _generation_failed(GenerationStatus.NO_MERGE_BASE)
            old_ref = merge_base
        key = (
            repository.hostname,
            repository.project_path,
            file.old_path if needs_old else "",
            file.new_path if needs_new else "",
            old_ref,
            revision.head_sha,
        )
        cached = self._generated.get(key)
        if cached is not None:
            return cached
        running = self._running.get(key)
        if running is not None:
            return await self._finish_generation(key, running)

        reads: list[Awaitable[FileRead]] = []
        if needs_old:
            reads.append(
                self.read_file(review, file.old_path, old_ref, limits=GENERATE_LIMITS)
            )
        if needs_new:
            reads.append(
                self.read_file(
                    review, file.new_path, revision.head_sha, limits=GENERATE_LIMITS
                )
            )
        results = await _read_together(reads)
        failure = _read_failure(results)
        if failure is not None:
            return failure
        texts = iter(result.text for result in results)
        old_text = next(texts) if needs_old else None
        new_text = next(texts) if needs_new else None
        # Another call may have started, or finished, the same generation
        # during the reads.
        cached = self._generated.get(key)
        if cached is not None:
            return cached
        running = self._running.get(key)
        if running is None:
            running = self._start_generation(key, file, old_text, new_text)
        return await self._finish_generation(key, running)

    def _start_generation(
        self,
        key: _GeneratedKey,
        file: DiffFile,
        old_text: FileText | None,
        new_text: FileText | None,
    ) -> _RunningGeneration:
        """Run ``generate_file_diff`` in a worker thread, known by ``key``."""
        future = asyncio.get_running_loop().run_in_executor(
            None, functools.partial(generate_file_diff, file, old_text, new_text)
        )
        running = _RunningGeneration(future, new_text)
        self._running[key] = running

        def finished(done: asyncio.Future[DiffFile]) -> None:
            if self._running.get(key) is running:
                del self._running[key]
            # Nobody may wait any more: read the outcome so it is never
            # reported as unretrieved.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        return running

    async def _finish_generation(
        self, key: _GeneratedKey, running: _RunningGeneration
    ) -> GeneratedDiff:
        """Wait for a running generation without stopping it on a cancel."""
        try:
            generated = await asyncio.shield(running.future)
        except GenerationTooLargeError as error:
            return GeneratedDiff(
                GenerationStatus.TOO_LARGE,
                None,
                None,
                diff_too_large_message(error.limit_lines),
            )
        result = GeneratedDiff(GenerationStatus.OK, generated, running.new_text, "")
        self._generated.put(key, result)
        return result

    async def _merge_base(
        self, client: ForgeClient, review: ReviewRef, revision: ReviewRevision
    ) -> str | None:
        """Return the commit the review's branch started from, or None."""
        if revision_base_is_merge_base(client):
            return revision.base_sha
        repository = review.repository
        key = (
            repository.hostname,
            repository.project_path,
            revision.base_sha,
            revision.head_sha,
        )
        cached = self._merge_bases.get(key)
        if cached is not None:
            return cached

        async def fetch() -> str | None:
            try:
                return await read_merge_base(
                    client,
                    repository.project_path,
                    revision.base_sha,
                    revision.head_sha,
                )
            except (
                ContentTooLargeError,
                NotFoundError,
                UnsupportedFileReadError,
                ValidationError,
            ):
                return None

        merge_base = await self._call(
            fetch(), operation="find_merge_base", hostname=repository.hostname
        )
        if not is_full_commit_sha(merge_base):
            return None
        self._merge_bases.put(key, merge_base)
        return merge_base

    def clear(self) -> None:
        """Forget every file, generated diff and merge base kept in memory.

        Running generations are forgotten too: one that finished just before
        the clear may not have left ``_running`` yet, and must not be reused.
        A generation still in its thread finishes for the callers waiting.
        """
        self._memory.clear()
        self._generated.clear()
        self._merge_bases.clear()
        self._running.clear()

    def close(self) -> None:
        """Release the in-memory caches when the session closes."""
        self.clear()


def validate_commit_sha(ref: object) -> None:
    """Require a full lowercase SHA-1 or SHA-256 commit id."""
    if not isinstance(ref, str) or _FULL_SHA.fullmatch(ref) is None:
        raise ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "The commit must be a full commit SHA.",
        )


def validate_file_path(path: object) -> None:
    """Require a plain repository path before it goes into a forge URL.

    Git trees never hold an empty path, a control character, a leading
    ``/`` or an empty, ``.`` or ``..`` segment, so these rules only refuse
    paths that could change which URL is built.
    """
    if not isinstance(path, str) or not path:
        raise _invalid_path()
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid_path() from None
    if len(encoded) > MAX_PATH_BYTES or _CONTROL_CHARACTERS.search(path):
        raise _invalid_path()
    if path.startswith("/"):
        raise _invalid_path()
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise _invalid_path()


def decode_file_text(data: bytes) -> FileText:
    """Decode a file body as UTF-8 and split it on ``"\\n"`` only.

    Invalid UTF-8 becomes U+FFFD. Other line separators, such as ``"\\r"`` or
    U+2028, stay in the line's content.
    """
    lines = data.decode("utf-8", errors="replace").split("\n")
    ends_with_newline = lines[-1] == ""
    if ends_with_newline:
        lines.pop()
    return FileText(tuple(lines), ends_with_newline, len(data))


def too_large_message(limits: FileReadLimits) -> str:
    """Describe the limits a file went past."""
    return (
        f"File too large to expand (over {_size_text(limits.max_bytes)} or "
        f"{limits.max_lines:,} lines)."
    )


def _size_text(size: int) -> str:
    if size % _MIB == 0:
        return f"{size // _MIB} MiB"
    if size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size:,} bytes"


def _within_limits(text: FileText, limits: FileReadLimits) -> FileRead:
    if text.byte_size > limits.max_bytes or len(text.lines) > limits.max_lines:
        return _failed(FileReadStatus.TOO_LARGE, limits)
    return FileRead(FileReadStatus.OK, text, "")


def _failed(status: FileReadStatus, limits: FileReadLimits) -> FileRead:
    messages = {
        FileReadStatus.BINARY: BINARY_MESSAGE,
        FileReadStatus.NOT_FOUND: NOT_FOUND_MESSAGE,
        FileReadStatus.UNSUPPORTED: UNSUPPORTED_MESSAGE,
    }
    message = messages.get(status) or too_large_message(limits)
    return FileRead(status, None, message)


def generate_too_large_message(limits: FileReadLimits = GENERATE_LIMITS) -> str:
    """Describe the per-version limits of local generation."""
    return (
        f"This file is too large to generate locally (limit "
        f"{_size_text(limits.max_bytes)} or {limits.max_lines:,} lines per version)."
    )


def diff_too_large_message(limit_lines: int) -> str:
    """Describe a generated diff with too many lines to show."""
    return f"The generated diff is too large to show (over {limit_lines:,} lines)."


def _generation_refusal(file: DiffFile) -> GeneratedDiff | None:
    """Refuse a file local generation does not apply to, before any request."""
    if not file.is_truncated:
        return _generation_failed(GenerationStatus.NOT_TRUNCATED)
    if file.is_binary:
        return _generation_failed(GenerationStatus.BINARY)
    if file.old_mode in _NON_TEXT_MODES or file.new_mode in _NON_TEXT_MODES:
        return GeneratedDiff(
            GenerationStatus.UNSUPPORTED, None, None, GENERATE_LINK_MESSAGE
        )
    return None


def _generation_failed(status: GenerationStatus) -> GeneratedDiff:
    messages = {
        GenerationStatus.BINARY: GENERATE_BINARY_MESSAGE,
        GenerationStatus.NOT_FOUND: NOT_FOUND_MESSAGE,
        GenerationStatus.UNSUPPORTED: UNSUPPORTED_MESSAGE,
        GenerationStatus.NOT_TRUNCATED: NOT_TRUNCATED_MESSAGE,
        GenerationStatus.NO_MERGE_BASE: NO_MERGE_BASE_MESSAGE,
    }
    message = messages.get(status) or generate_too_large_message()
    return GeneratedDiff(status, None, None, message)


# The order in which a failed version decides the outcome of a generation.
_READ_FAILURES = (
    (FileReadStatus.BINARY, GenerationStatus.BINARY),
    (FileReadStatus.TOO_LARGE, GenerationStatus.TOO_LARGE),
    (FileReadStatus.NOT_FOUND, GenerationStatus.NOT_FOUND),
    (FileReadStatus.UNSUPPORTED, GenerationStatus.UNSUPPORTED),
)


def _read_failure(results: list[FileRead]) -> GeneratedDiff | None:
    statuses = {result.status for result in results}
    for read_status, generation_status in _READ_FAILURES:
        if read_status in statuses:
            return _generation_failed(generation_status)
    return None


async def _read_together(reads: list[Awaitable[FileRead]]) -> list[FileRead]:
    """Run reads at the same time; a failure or a cancellation stops them all."""
    tasks = [asyncio.ensure_future(read) for read in reads]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _invalid_path() -> ServiceError:
    return ServiceError(ServiceErrorCode.INVALID_INPUT, "The file path is invalid.")


_MemoryKey = tuple[str, str, str, str]
_GeneratedKey = tuple[str, str, str, str, str, str]
_MergeBaseKey = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class _RunningGeneration:
    """A generation whose worker thread is running, with the new version."""

    future: asyncio.Future[DiffFile]
    new_text: FileText | None


def _entry_size(text: FileText) -> int:
    """The bytes a decoded file counts against the file cache's budget."""
    lines = len(text.lines) * _LINE_OVERHEAD_BYTES
    return text.byte_size + lines + _ENTRY_OVERHEAD_BYTES


def _generated_size(generated: GeneratedDiff) -> int:
    """The bytes a generated diff counts against the generated cache's budget.

    Its new version is counted too: the generated diff keeps it alive even
    after the file cache lets it go.
    """
    size = _ENTRY_OVERHEAD_BYTES
    if generated.new_text is not None:
        size += _entry_size(generated.new_text)
    if generated.file is not None:
        for hunk in generated.file.hunks:
            size += len(hunk.header) + _DIFF_LINE_OVERHEAD_BYTES
            for line in hunk.lines:
                size += len(line.content) + _DIFF_LINE_OVERHEAD_BYTES
    return size


class _SizedLRU[K, V]:
    """Keep values within a byte budget, least recently used first out.

    ``size_of`` tells what each value counts against ``max_bytes``; a value
    larger than the whole budget is not kept. ``max_entries`` also bounds
    how many values are kept, when given.
    """

    def __init__(
        self,
        max_bytes: int,
        size_of: Callable[[V], int],
        *,
        max_entries: int | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._size_of = size_of
        self._max_entries = max_entries
        self._entries: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._size = 0

    @property
    def size(self) -> int:
        """The bytes the kept values count, in all."""
        return self._size

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: K, value: V) -> None:
        size = self._size_of(value)
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._size -= previous[1]
        if size > self._max_bytes:
            return
        self._entries[key] = (value, size)
        self._size += size
        while self._size > self._max_bytes or (
            self._max_entries is not None and len(self._entries) > self._max_entries
        ):
            _key, (_value, evicted) = self._entries.popitem(last=False)
            self._size -= evicted

    def clear(self) -> None:
        self._entries.clear()
        self._size = 0


class _EntryLRU[K, V]:
    """Keep at most ``max_entries`` values, least recently used first out."""

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


__all__ = [
    "EXPAND_LIMITS",
    "GENERATE_LIMITS",
    "FileRead",
    "FileReadLimits",
    "FileReadStatus",
    "FileText",
    "GeneratedDiff",
    "GenerationStatus",
    "ReviewFileService",
    "decode_file_text",
    "validate_commit_sha",
    "validate_file_path",
]
