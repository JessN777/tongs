"""Production desktop request dispatcher over one bounded NDJSON connection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import sys
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol, cast

from tongs.config import COLOR_SCHEMES
from tongs.desktop.assets import (
    ASSET_CHUNK_BYTES,
    AssetCatalog,
    AssetDescriptor,
    CoreAssetSpec,
)
from tongs.desktop.editor_exports import (
    EDITOR_EXPORT_ROOT_ENV,
    EditorExportLedger,
)
from tongs.desktop.protocol.ci_operations import (
    CI_CAPABILITY,
    CI_METHODS,
    CIOperations,
)
from tongs.desktop.protocol.diff_projection import (
    CachedDiff,
    ConvertedDiffCache,
    DiffLayout,
    context_wire,
    flatten_diff,
    flatten_file,
)
from tongs.desktop.protocol.messages import (
    DEFAULT_JSON_LIMITS,
    MAX_EVENT_FRAME_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_ITEMS,
    MAX_PENDING_REQUESTS,
    MAX_QUEUED_EVENTS,
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    MIN_JSON_DEPTH,
    MIN_JSON_ITEMS,
    CancelFrame,
    JsonLimits,
    JsonObject,
    JsonValue,
    ProtocolError,
    ProtocolErrorCode,
    RequestFrame,
    decode_frame,
    encode_event,
    encode_response,
    to_json_value,
)
from tongs.desktop.protocol.review_operations import (
    REVIEW_CAPABILITY,
    REVIEW_METHODS,
    ReviewOperations,
)
from tongs.desktop.protocol.state import (
    MAX_SNAPSHOT_RETAINED_BYTES,
    HandleKind,
    HandleRegistry,
    SnapshotStore,
    retained_size,
)
from tongs.desktop.protocol.utility_operations import (
    UTILITY_CAPABILITY,
    UTILITY_METHODS,
    UtilityOperations,
)
from tongs.diff.context import GapPosition, file_gaps, verify_new_side
from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import DiffFile
from tongs.errors import redact_credentials
from tongs.forges.models import JobLogTail, MRState, MRSummary, Pipeline, PipelineJob
from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopHostFacade,
    DesktopLocation,
    DesktopNotificationSeverity,
    DesktopPluginContractError,
    DesktopPluginError,
    DesktopPluginErrorCode,
    DesktopPluginManifest,
    DesktopPluginRecord,
    DesktopPluginState,
    DesktopReadKind,
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json,
    freeze_json_object,
)
from tongs.plugins.desktop import (
    JsonObject as PluginJsonObject,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import (
    ApplicationSession,
    JobRef,
    PipelineRef,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
    ReviewSnapshot,
    ServiceError,
    ServiceErrorCode,
    ServiceEvent,
)
from tongs.services.models import ReviewRevision, review_cursor_page
from tongs.services.review_files import FileReadStatus, GenerationStatus
from tongs.services.session import RATE_LIMIT_UNKNOWN_WAIT
from tongs.state.drafts.models import RecoveryWarning

_DIFF_SNAPSHOT = "diff"
_DISCUSSIONS_SNAPSHOT = "discussions"
_LOG_SNAPSHOT = "log"
_DISCUSSION_PAGE_ITEMS = 400
_DIFF_PAGE_ITEMS = 400

DIFF_CONTEXT_CAPABILITY = "diff_context"
#: The most unchanged lines one ``diff.context`` request asks for.
MAX_CONTEXT_LINES = 1000
#: The most encoded row bytes one ``diff.context`` response carries.
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
# Bounds on the integers a renderer may send: a diff snapshot holds at most
# 100,000 rows. A gap may start past any line count the service reads, so a
# line number is bounded only by the largest integer JSON carries exactly; a
# read of such a file comes back as too_large.
_MAX_FILE_INDEX = 100_000
_MAX_LINE_NUMBER = 2**53 - 1
_MAX_SNAPSHOT_ID_LENGTH = 100
# JSON values in one context row: the object and its five members.
_CONTEXT_ROW_VALUES = 6
# Generated diff snapshots kept on record to release after their last page.
_MAX_GENERATED_SNAPSHOTS = 64
# Requests that read a generated diff snapshot, kept so a cancel that arrives
# for one, even after its reply, releases the snapshot.
_MAX_GENERATED_REQUESTS = 256
# The last diff snapshot opened for each review, kept for this many reviews.
_MAX_LATEST_DIFFS = 256
_CONTEXT_PARAMS = frozenset({"review", "snapshot", "file_index", "new_start", "count"})
_GENERATE_PARAMS = frozenset({"review", "snapshot", "file_index"})

CONTEXT_MISMATCH_MESSAGE = (
    "The file at this commit does not match the diff. Refresh the review."
)
"""The terminal's wording for a file read that disagrees with the hunks."""
DIFF_EXPIRED_MESSAGE = "This diff is no longer open. Reload it to continue."
GENERATE_NO_ROOM_MESSAGE = (
    "The generated diff is too large to show with the other diffs already open. "
    "Close one and try again."
)
"""Desktop only: the terminal has no snapshot store, so it never refuses this."""

SUPPORTED_CAPABILITIES = frozenset(
    {
        "assets",
        "cancellation",
        CI_CAPABILITY,
        DIFF_CONTEXT_CAPABILITY,
        "events",
        "opaque_handles",
        "paged_diffs",
        "paged_discussions",
        "paged_logs",
        "plugins",
        REVIEW_CAPABILITY,
        "split_diffs",
        UTILITY_CAPABILITY,
    }
)
SUPPORTED_METHODS = (
    "assets.list",
    "assets.read",
    *CI_METHODS,
    "commits.list",
    "diff.context",
    "diff.generate",
    "diff.open",
    "diff.page",
    "discussions.list",
    "discussions.page",
    "host.set_location",
    "jobs.list",
    "logs.open",
    "logs.page",
    "pipelines.list",
    "plugins.invoke",
    "plugins.list",
    "repositories.discover",
    "repositories.open",
    "review_pipelines.list",
    *UTILITY_METHODS,
    *REVIEW_METHODS,
    "reviews.ci_status",
    "reviews.get",
    "reviews.list",
)

_MAX_PROTOCOL_ERRORS = 8
# Head commits remembered for ``reviews.ci_status``, one per listed review. No
# more reviews than review handles can be listed, so this only bounds the map.
_MAX_REVIEW_HEADS = 8192
# The longest rate-limit wait ``reviews.ci_status`` names, in seconds.
_MAX_RATE_LIMIT_WAIT_SECONDS = 86_400
_DEFAULT_SHUTDOWN_TIMEOUT = 5.0
# The waits a final session close makes in turn, each bounded by the session's
# shutdown timeout: a startup still running, the four mutation services (review
# submissions, MR actions, CI mutations and review mutations), the draft store,
# the forge registry and the cache. The sidecar bounds the whole close by this
# many timeouts, so it never cancels a close that keeps to its own bounds and
# leaves a store's SQLite worker thread running, which would keep the process
# alive after it should have exited (#442).
_FINAL_CLOSE_WAITS = 8
# The most a job log snapshot retains, measured as the snapshot store measures
# it (its JSON rows). A longer log keeps its end, which is where a failure
# shows. A quarter of the store leaves room for the diffs and discussions
# being read beside it, and the renderer's limit of 8 Mi characters never
# cuts what is kept here again (#314).
_MAX_LOG_SNAPSHOT_BYTES = MAX_SNAPSHOT_RETAINED_BYTES // 4
# A cut can grow the note naming it and split rows differently, so the tail is
# trimmed again a few times at most before the log is refused.
_MAX_LOG_TRIMS = 8
# Room for the truncation note's first line, whose sizes may grow by a few
# digits after a further cut.
_LOG_NOTE_ROOM = 128


class AsyncByteWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


class SessionResource(Protocol):
    @property
    def config(self) -> object: ...

    async def start(self) -> object: ...

    async def close(self, *, final: bool = False) -> None: ...


@dataclass(slots=True)
class RequestContext:
    request_id: str
    cancellation: DesktopCancellation
    mutation: bool = False
    dispatched: bool = False
    response_started: bool = False


type OperationHandler = Callable[[JsonObject, RequestContext], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class _Operation:
    handler: OperationHandler
    mutation: bool


@dataclass(slots=True)
class _PendingRequest:
    task: asyncio.Task[None]
    context: RequestContext


@dataclass(frozen=True, slots=True)
class _GeneratedSnapshot:
    """A generated diff snapshot: its review and the diff it came from."""

    review_handle: str
    source: str


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    sequence: int
    name: str
    data: object
    replaceable_key: str | None


class _EventBuffer:
    """Bounded event buffer with safe coalescing and explicit resync markers."""

    def __init__(self, max_events: int = MAX_QUEUED_EVENTS) -> None:
        self._max_events = max_events
        self._events: deque[_QueuedEvent] = deque()
        self._condition = asyncio.Condition()
        self._sequence = 0
        self._closed = False
        self._limits = DEFAULT_JSON_LIMITS

    @property
    def limits(self) -> JsonLimits:
        return self._limits

    async def set_limits(self, limits: JsonLimits) -> None:
        """Apply the negotiated JSON budget to queued and future events.

        A queued event that no longer fits the tighter budget is replaced by a
        resync marker instead of reaching a client that would reject it.
        """
        async with self._condition:
            self._limits = limits
            for event in self._events:
                try:
                    encode_event(event.sequence, event.name, event.data, limits=limits)
                except ProtocolError:
                    self._replace_with_resync("event_too_large")
                    self._condition.notify()
                    return

    async def publish(
        self, name: str, data: object, *, replaceable_key: str | None = None
    ) -> None:
        async with self._condition:
            if self._closed:
                return
            if replaceable_key is not None:
                for index, event in enumerate(self._events):
                    if event.replaceable_key == replaceable_key:
                        replacement = _QueuedEvent(
                            event.sequence, name, data, replaceable_key
                        )
                        try:
                            encode_event(
                                replacement.sequence, name, data, limits=self._limits
                            )
                        except ProtocolError:
                            self._replace_with_resync("event_too_large")
                        else:
                            self._events[index] = replacement
                        self._condition.notify()
                        return
            self._sequence += 1
            event = _QueuedEvent(self._sequence, name, data, replaceable_key)
            try:
                encode_event(event.sequence, name, data, limits=self._limits)
            except ProtocolError:
                self._replace_with_resync("event_too_large")
            else:
                if len(self._events) >= self._max_events:
                    self._replace_with_resync("queue_overflow")
                else:
                    self._events.append(event)
            self._condition.notify()

    async def get(self) -> _QueuedEvent | None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._events or self._closed)
            if self._events:
                return self._events.popleft()
            return None

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._events.clear()
            self._condition.notify_all()

    def _replace_with_resync(self, reason: str) -> None:
        self._events.clear()
        self._sequence += 1
        self._events.append(
            _QueuedEvent(
                self._sequence,
                "protocol.resync_required",
                {"reason": reason},
                "protocol.resync_required",
            )
        )


def _encode_event_or_resync(event: _QueuedEvent, limits: JsonLimits) -> bytes:
    """Encode a queued event, or a resync marker in its place if it overflows.

    The buffer already checks events when they are published, so this only
    guards against a budget that changed afterwards; the client then refetches
    instead of receiving a frame it would reject.
    """
    try:
        return encode_event(event.sequence, event.name, event.data, limits=limits)
    except ProtocolError:
        return encode_event(
            event.sequence,
            "protocol.resync_required",
            {"reason": "event_too_large"},
            limits=limits,
        )


class _UnavailableEditorExports:
    """Fail closed when a non-production caller omits the trusted export root."""

    async def reserve(self, _job_id: int, _token: str) -> None:
        return None

    async def release(self, _slot: int, _token: str) -> bool:
        return False


class DesktopSidecarServer:
    """Own one desktop session and dispatch strict protocol-major-1 requests."""

    def __init__(
        self,
        *,
        session: ApplicationSession | None = None,
        plugin_registry: DesktopPluginRegistry | None = None,
        core_assets: tuple[CoreAssetSpec, ...] = (),
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
        editor_exports: EditorExportLedger | None = None,
    ) -> None:
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        # The session's own close waits use the same timeout, so the bound on
        # the whole close below covers them. A session passed in is expected
        # to use a timeout no longer than this one.
        self._session = session or ApplicationSession(shutdown_timeout=shutdown_timeout)
        self._plugin_registry = plugin_registry
        self._core_assets = core_assets
        self._shutdown_timeout = shutdown_timeout
        export_root = os.environ.get(EDITOR_EXPORT_ROOT_ENV)
        self._editor_exports = editor_exports
        if self._editor_exports is None and export_root is not None:
            self._editor_exports = EditorExportLedger(Path(export_root))
        self._editor_export_authority = (
            self._editor_exports or _UnavailableEditorExports()
        )
        self._handles = HandleRegistry()
        self._review_cursors = _ReviewCursors()
        # The head commit each listed review had when its list was last read,
        # so a CI lookup never takes a commit from the renderer.
        self._review_heads: dict[ReviewRef, str] = {}
        self._snapshots = SnapshotStore()
        # Converted files of recent diff reads, so a layout switch re-projects
        # the read the reader sees instead of reading the forge again.
        self._diff_cache = ConvertedDiffCache()
        # One-file snapshots of locally generated diffs, released after their
        # last page so they never linger next to the review's own diff. One
        # the renderer stops reading is released when a cancel arrives for
        # any request that read it, or when its review opens a newer diff.
        self._generated_snapshots: OrderedDict[str, _GeneratedSnapshot] = OrderedDict()
        self._generated_requests: OrderedDict[str, str] = OrderedDict()
        # The diff snapshot each review last opened, which the renderer shows
        # until a newer open finishes.
        self._latest_diffs: OrderedDict[str, str] = OrderedDict()
        self._assets = AssetCatalog()
        self._operations: dict[str, _Operation] = {}
        self._pending: dict[str, _PendingRequest] = {}
        self._events = _EventBuffer()
        self._writer: AsyncByteWriter | None = None
        self._writer_lock = asyncio.Lock()
        self._event_task: asyncio.Task[None] | None = None
        self._service_event_task: asyncio.Task[None] | None = None
        self._handshaken = False
        self._json_limits = DEFAULT_JSON_LIMITS
        self._stopping = False
        self._location: DesktopLocation | None = None
        self._install_read_operations()
        self._install_ci_operations()
        self._install_review_operations()
        self._install_utility_operations()

    @property
    def session_id(self) -> str:
        return self._handles.session_id

    def register_operation(
        self, method: str, handler: OperationHandler, *, mutation: bool
    ) -> None:
        """Register a future operation with explicit cancellation semantics.

        A cancel frame signals a dispatched mutation but does not cancel its task
        or claim that remote work rolled back.
        """
        if method in self._operations:
            raise ValueError(f"Duplicate sidecar operation: {method}")
        self._operations[method] = _Operation(handler, mutation)

    async def run(self, reader: asyncio.StreamReader, writer: AsyncByteWriter) -> None:
        """Run until clean shutdown, EOF, or an unrecoverable framing error."""
        self._writer = writer
        protocol_errors = 0
        try:
            while not self._stopping:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._write_error(
                        None,
                        ProtocolError(
                            ProtocolErrorCode.FRAME_TOO_LARGE,
                            "The request frame exceeds the supported size.",
                        ),
                    )
                    break
                if raw == b"":
                    break
                try:
                    frame = decode_frame(raw)
                except ProtocolError as error:
                    protocol_errors += 1
                    await self._write_error(None, error)
                    if (
                        error.code is ProtocolErrorCode.FRAME_TOO_LARGE
                        or protocol_errors >= _MAX_PROTOCOL_ERRORS
                    ):
                        break
                    continue
                protocol_errors = 0
                if isinstance(frame, CancelFrame):
                    self._cancel(frame.request_id)
                    continue
                if frame.method == "handshake":
                    await self._handle_handshake(frame)
                    continue
                if not self._handshaken:
                    await self._write_error(
                        frame.request_id,
                        ProtocolError(
                            ProtocolErrorCode.HANDSHAKE_REQUIRED,
                            "A successful handshake is required before requests.",
                        ),
                    )
                    continue
                if frame.method == "shutdown":
                    try:
                        _require_params(frame.params, allowed=frozenset())
                    except ProtocolError as error:
                        await self._write_error(frame.request_id, error)
                        continue
                    await self._write_result(frame.request_id, {"accepted": True})
                    self._stopping = True
                    break
                await self._schedule(frame)
        finally:
            await self._shutdown()

    def _install_read_operations(self) -> None:
        operations: dict[str, OperationHandler] = {
            "assets.list": self._assets_list,
            "assets.read": self._assets_read,
            "commits.list": self._commits_list,
            "diff.context": self._diff_context,
            "diff.generate": self._diff_generate,
            "diff.open": self._diff_open,
            "diff.page": self._diff_page,
            "discussions.list": self._discussions_list,
            "discussions.page": self._discussions_page,
            "host.set_location": self._set_location,
            "jobs.list": self._jobs_list,
            "logs.open": self._logs_open,
            "logs.page": self._logs_page,
            "pipelines.list": self._pipelines_list,
            "plugins.invoke": self._plugins_invoke,
            "plugins.list": self._plugins_list,
            "repositories.discover": self._repositories_discover,
            "repositories.open": self._repositories_open,
            "review_pipelines.list": self._review_pipelines_list,
            "reviews.ci_status": self._reviews_ci_status,
            "reviews.get": self._reviews_get,
            "reviews.list": self._reviews_list,
        }
        for method, handler in operations.items():
            self.register_operation(method, handler, mutation=False)

    def _install_ci_operations(self) -> None:
        operations = CIOperations(session=self._session, handles=self._handles)
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    def _install_review_operations(self) -> None:
        operations = ReviewOperations(session=self._session, handles=self._handles)
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    def _install_utility_operations(self) -> None:
        operations = UtilityOperations(
            session=self._session,
            handles=self._handles,
            reservations=self._editor_export_authority,
        )
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    async def _handle_handshake(self, frame: RequestFrame) -> None:
        if self._handshaken:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.ALREADY_HANDSHAKEN,
                    "The desktop connection has already completed its handshake.",
                ),
            )
            return
        try:
            requested, client_limits = _parse_handshake(frame.params)
            await self._session.start()
            recovery_warnings = tuple(
                getattr(cast(object, self._session), "recovery_warnings", ())
            )
            _report_recovery_warnings(recovery_warnings)
            config = getattr(cast(object, self._session), "config", None)
            if self._plugin_registry is None:
                plugin_config = getattr(config, "plugin_config", {})
                self._plugin_registry = DesktopPluginRegistry(plugin_config)
            self._plugin_registry.discover()
            await self._plugin_registry.start_all(self._facade_for_plugin)
            self._assets.stage_core(self._core_assets)
            self._assets.stage_plugins(self._plugin_registry)
            self._json_limits = client_limits
            await self._events.set_limits(client_limits)
            self._handshaken = True
            await self._write_result(
                frame.request_id,
                {
                    "protocol_major": 1,
                    "core_version": _core_version(),
                    "session_id": self.session_id,
                    "capabilities": sorted(SUPPORTED_CAPABILITIES),
                    "accepted_capabilities": sorted(requested),
                    "methods": [*sorted(self._operations), "shutdown"],
                    "recovery_warnings": _recovery_notices(recovery_warnings),
                    "color_scheme": _color_scheme(config),
                    "limits": {
                        "request_frame_bytes": MAX_REQUEST_FRAME_BYTES,
                        "response_frame_bytes": MAX_RESPONSE_FRAME_BYTES,
                        "event_frame_bytes": MAX_EVENT_FRAME_BYTES,
                        "pending_requests": MAX_PENDING_REQUESTS,
                        "queued_events": MAX_QUEUED_EVENTS,
                        "asset_chunk_bytes": ASSET_CHUNK_BYTES,
                        "json_values": self._json_limits.values,
                        "json_depth": self._json_limits.depth,
                    },
                },
            )
            self._event_task = asyncio.create_task(self._pump_events())
            self._service_event_task = asyncio.create_task(self._pump_service_events())
        except ProtocolError as error:
            await self._write_error(frame.request_id, error)
        except ServiceError as error:
            await self._write_error(frame.request_id, _service_protocol_error(error))
            self._stopping = True
        except Exception:  # noqa: BLE001 - startup boundary must stay redacted.
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.INTERNAL,
                    "The desktop session could not be started.",
                ),
            )
            self._stopping = True

    async def _schedule(self, frame: RequestFrame) -> None:
        if self._stopping:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.SHUTTING_DOWN,
                    "The desktop sidecar is shutting down.",
                ),
            )
            return
        if frame.request_id in self._pending:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.DUPLICATE_REQUEST,
                    "The request id is already pending.",
                ),
            )
            return
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.TOO_MANY_REQUESTS,
                    "The pending request limit was reached.",
                    retryable=True,
                ),
            )
            return
        operation = self._operations.get(frame.method)
        if operation is None:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.UNKNOWN_METHOD,
                    "The requested desktop method is not supported.",
                ),
            )
            return
        # A reused request id must not carry a finished request's generated
        # snapshot, or cancelling the new request would release that one.
        self._generated_requests.pop(frame.request_id, None)
        context = RequestContext(
            frame.request_id, DesktopCancellation(), mutation=operation.mutation
        )
        if frame.method == "diff.page":
            snapshot = frame.params.get("snapshot")
            if isinstance(snapshot, str) and snapshot in self._generated_snapshots:
                self._remember_generated_request(frame.request_id, snapshot)
        task = asyncio.create_task(self._serve(frame, operation, context))
        self._pending[frame.request_id] = _PendingRequest(task, context)

    async def _serve(
        self, frame: RequestFrame, operation: _Operation, context: RequestContext
    ) -> None:
        try:
            context.dispatched = True
            result = await operation.handler(frame.params, context)
            context.response_started = True
            await self._write_result(frame.request_id, result)
        except asyncio.CancelledError:
            if context.response_started:
                raise
            with suppress(Exception):
                await self._write_error(
                    frame.request_id,
                    ProtocolError(
                        ProtocolErrorCode.REQUEST_CANCELLED,
                        "The request was cancelled.",
                        retryable=not context.mutation,
                        details={"outcome": "unknown"}
                        if context.mutation and context.dispatched
                        else None,
                    ),
                )
        except ProtocolError as error:
            await self._write_error(frame.request_id, error)
        except ServiceError as error:
            await self._write_error(frame.request_id, _service_protocol_error(error))
        except DesktopPluginContractError as error:
            await self._write_error(frame.request_id, _plugin_protocol_error(error))
        except (BrokenPipeError, ConnectionError):
            self._stopping = True
        except Exception as error:  # noqa: BLE001 - operation boundary must stay redacted.
            detail = redact_credentials(str(error))[:500]
            print(
                f"tongs desktop operation failed: method={frame.method} "
                f"error={type(error).__name__}: {detail}",
                file=sys.stderr,
            )
            if context.response_started:
                self._stopping = True
                return
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.INTERNAL,
                    "The desktop operation failed.",
                ),
            )
        finally:
            current = self._pending.get(frame.request_id)
            if current is not None and current.task is asyncio.current_task():
                self._pending.pop(frame.request_id, None)

    def _cancel(self, request_id: str) -> None:
        generated = self._generated_requests.pop(request_id, None)
        if generated is not None:
            # The renderer stopped reading this generated diff, so it goes
            # now instead of waiting for the store to need the room.
            self._release_generated(generated)
        pending = self._pending.get(request_id)
        if pending is None:
            return
        pending.context.cancellation.cancel()
        if not pending.context.mutation and not pending.context.response_started:
            pending.task.cancel()

    async def _repositories_discover(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        repositories = await self._session.discover_repositories()
        return {"repositories": [self._repository_wire(item) for item in repositories]}

    async def _repositories_open(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"hostname", "project_path"}),
            required=frozenset({"hostname", "project_path"}),
        )
        hostname = _text(params, "hostname", max_length=253)
        project_path = _text(params, "project_path", max_length=500)
        snapshot = await self._session.open_repository(hostname, project_path)
        return self._repository_wire(snapshot)

    async def _reviews_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """List one page of reviews.

        All Open reads one repository at a time, most recently updated first,
        so a response holds at most ``per_page`` reviews. It returns
        ``next_cursor`` when the forge has another page; passing it back as
        ``cursor`` reads that page. An All Open read without a repository is
        rejected: it could neither be paged nor stay inside the frame budget.
        An optional boolean ``fresh`` marks an explicit refresh, which reads
        past the service's list cache.

        Rows come without per-review CI requests where the forge allows it:
        their CI status is then ``unknown`` and ``reviews.ci_status`` fills it
        in for the rows the renderer shows.
        """
        _require_params(
            params,
            allowed=frozenset(
                {"scope", "repository", "state", "per_page", "cursor", "fresh"}
            ),
            required=frozenset({"scope"}),
        )
        scope, state, repository, per_page = self._review_query_params(params)
        fresh = _optional_bool(params, "fresh", False)
        if scope is ReviewScope.ALL_OPEN and repository is None:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "An All Open review list needs a repository.",
            )
        repository_handle = (
            cast(str, params["repository"]) if repository is not None else None
        )
        binding = (scope.value, state.value, per_page, repository_handle)
        cursor = None
        if params.get("cursor") is not None:
            if repository is None or scope is not ReviewScope.ALL_OPEN:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_PARAMS,
                    "The review list cursor is invalid.",
                )
            cursor = self._review_cursors.open(params["cursor"], binding)
        page = await self._session.list_reviews(
            ReviewQuery(
                scope,
                repository=repository,
                state=state,
                per_page=per_page,
                paged=True,
                cursor=cursor,
                fresh=fresh,
                with_ci=False,
            )
        )
        return {
            "next_cursor": self._review_cursors.seal(page.next_cursor, binding)
            if page.next_cursor is not None
            else None,
            **self._review_page_wire(page),
        }

    async def _reviews_all(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """Return every matching review at once, for in-process plugin reads.

        A plugin read never crosses the frame budget itself, so it keeps the
        whole-list shape plugins were written against: no cursor, and All Open
        walks every forge page of each repository.
        """
        _require_params(
            params,
            allowed=frozenset({"scope", "repository", "state", "per_page"}),
            required=frozenset({"scope"}),
        )
        scope, state, repository, per_page = self._review_query_params(params)
        page = await self._session.list_reviews(
            ReviewQuery(scope, repository=repository, state=state, per_page=per_page)
        )
        return self._review_page_wire(page)

    def _review_query_params(
        self, params: JsonObject
    ) -> tuple[ReviewScope, MRState, RepositoryRef | None, int]:
        try:
            scope = ReviewScope(_text(params, "scope", max_length=40))
            state = MRState(_optional_text(params, "state", "open", max_length=20))
        except ValueError:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The review scope or state is invalid.",
            ) from None
        repository = None
        if "repository" in params and params["repository"] is not None:
            repository = self._handles.resolve(
                params["repository"], HandleKind.REPOSITORY, RepositoryRef
            )
        per_page = _optional_int(params, "per_page", 100, minimum=1, maximum=100)
        return scope, state, repository, per_page

    def _review_page_wire(self, page: ReviewPage) -> JsonObject:
        return {
            "items": [self._review_list_wire(item) for item in page.items],
            "failures": [
                {
                    "code": failure.code.value,
                    "message": failure.message,
                    "retryable": failure.retryable,
                    "repository": self._handles.find(
                        HandleKind.REPOSITORY, failure.repository
                    )
                    if failure.repository is not None
                    else None,
                }
                for failure in page.failures
            ],
        }

    async def _reviews_ci_status(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """Read the CI status of one listed review.

        The lookup uses the head commit the review's last list read named, so
        it costs the forge's CI requests only. An optional boolean ``fresh``
        reads past the service's CI cache, as after an explicit refresh.

        ``retry_after_seconds`` is null unless the review's forge host is rate
        limited. A lookup on a host that already answered with a rate limit
        sends no request, and a lookup that meets one is not an error: both
        answer ``unknown`` with the seconds until the limit resets, so the
        renderer can tell a rate limit from a failure and wait it out.
        """
        _require_params(
            params,
            allowed=frozenset({"review", "fresh"}),
            required=frozenset({"review"}),
        )
        review = self._handles.resolve(params["review"], HandleKind.REVIEW, ReviewRef)
        fresh = _optional_bool(params, "fresh", False)
        hostname = review.repository.hostname
        wait = self._rate_limit_wait(hostname)
        if wait is not None:
            return {"ci_status": "unknown", "retry_after_seconds": wait}
        try:
            status = await self._session.get_review_ci_status(
                review, head_sha=self._review_heads.get(review, ""), fresh=fresh
            )
        except ServiceError as error:
            if error.code is not ServiceErrorCode.RATE_LIMITED:
                raise
            wait = self._rate_limit_wait(hostname)
            if wait is None:
                wait = _bounded_wait(
                    dict(error.details).get("retry_after_seconds"),
                    RATE_LIMIT_UNKNOWN_WAIT.total_seconds(),
                )
            return {"ci_status": "unknown", "retry_after_seconds": wait}
        return {"ci_status": status.value, "retry_after_seconds": None}

    def _rate_limit_wait(self, hostname: str) -> int | None:
        """Seconds until a rate-limited host takes reads again, or None."""
        limit = self._session.host_rate_limit(hostname)
        if limit is None:
            return None
        return _bounded_wait(
            (limit.until - datetime.now(UTC)).total_seconds(),
            RATE_LIMIT_UNKNOWN_WAIT.total_seconds(),
        )

    async def _reviews_get(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        review = self._review_param(params)
        snapshot = await self._session.get_review(review)
        return self._review_snapshot_wire(snapshot)

    async def _diff_open(self, params: JsonObject, _context: RequestContext) -> object:
        """Project a review's diff in one layout and return its first page.

        With ``from_snapshot`` naming a diff snapshot this connection projected
        for the same review, the files of that read are projected again from
        the sidecar's cache, so a layout switch makes no forge request and
        shows the same revision. A snapshot the cache no longer holds falls
        back to a fresh read. An open without it is a fresh read, and it drops
        the review's cached files before reading.

        With ``gaps`` true, expandable files also carry ``gap`` rows for the
        unchanged lines around their hunks. They come from the hunks alone,
        so asking for them reads nothing more from the forge.
        """
        _require_params(
            params,
            allowed=frozenset(
                {"review", "layout", "max_items", "from_snapshot", "gaps"}
            ),
            required=frozenset({"review"}),
        )
        gaps = _optional_bool(params, "gaps", False)
        try:
            layout = DiffLayout(
                _optional_text(params, "layout", "unified", max_length=20)
            )
        except ValueError:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The diff layout is invalid.",
            ) from None
        source = (
            _text(params, "from_snapshot", max_length=100)
            if "from_snapshot" in params
            else None
        )
        review_handle = _text(params, "review", max_length=100)
        review = self._handles.resolve(review_handle, HandleKind.REVIEW, ReviewRef)
        # The diff on screen may still be reading a generated file while this
        # one loads, so only generations from older diffs go now. A layout
        # switch names the diff on screen; a Refresh replaces the last one.
        on_screen = (
            source if source is not None else self._latest_diffs.get(review_handle)
        )
        self._release_review_generated(review_handle, keep=on_screen)
        cached = (
            None if source is None else self._diff_cache.lookup(review_handle, source)
        )
        if cached is None:
            self._diff_cache.drop(review_handle)
            raw = await self._session.get_raw_diff(review)
            files = convert_forge_changes(raw.changes)
            revision = cast(JsonObject, to_json_value(raw.revision))
        else:
            files = cached.files
            revision = cached.revision
        entries = flatten_diff(files, layout, gaps=gaps)
        snapshot_id = self._snapshots.create(
            review_handle,
            revision,
            entries,
            projection=layout.value,
            kind=_DIFF_SNAPSHOT,
        )
        if cached is None:
            self._diff_cache.store(
                review_handle, snapshot_id, revision, files, layout=layout
            )
        else:
            self._diff_cache.remember(review_handle, snapshot_id, layout=layout)
        self._latest_diffs[review_handle] = snapshot_id
        self._latest_diffs.move_to_end(review_handle)
        while len(self._latest_diffs) > _MAX_LATEST_DIFFS:
            self._latest_diffs.popitem(last=False)
        return self._snapshot_page_wire(
            self._snapshots.page(
                snapshot_id,
                review_handle,
                0,
                params.get("max_items", 400),
                kind=_DIFF_SNAPSHOT,
            )
        )

    async def _diff_page(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"snapshot", "resource", "cursor", "max_items"}),
            required=frozenset({"snapshot", "resource", "cursor"}),
        )
        self._handles.resolve(params["resource"], HandleKind.REVIEW, ReviewRef)
        page = self._snapshots.page(
            params["snapshot"],
            params["resource"],
            params["cursor"],
            params.get("max_items", _DIFF_PAGE_ITEMS),
            kind=_DIFF_SNAPSHOT,
        )
        if page.next_cursor is None:
            self._release_generated(page.snapshot_id)
        return self._snapshot_page_wire(page)

    async def _diff_context(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """Return unchanged lines of one file in a gap of an open diff.

        The renderer names the review, a diff snapshot this connection
        projected for it, the file's index in that snapshot and a run of new
        line numbers; never a path or a commit. The file is read at the head
        of the revision the snapshot shows and checked against its hunks
        before any line is returned. The lines come back inline, never as a
        retained snapshot, so expanding can never evict the diff being read.
        A response holds at most ``count`` lines, stops at the end of the gap
        and before ``MAX_CONTEXT_BYTES``; the renderer asks again from the
        next line for the rest.
        """
        _require_params(params, allowed=_CONTEXT_PARAMS, required=_CONTEXT_PARAMS)
        review_handle = _text(params, "review", max_length=100)
        snapshot = _text(params, "snapshot", max_length=_MAX_SNAPSHOT_ID_LENGTH)
        file_index = _required_int(params, "file_index", 0, _MAX_FILE_INDEX)
        new_start = _required_int(params, "new_start", 1, _MAX_LINE_NUMBER)
        count = _required_int(params, "count", 1, MAX_CONTEXT_LINES)
        review = self._handles.resolve(review_handle, HandleKind.REVIEW, ReviewRef)
        source = self._diff_source(review_handle, snapshot, file_index)
        if source is None:
            return _context_result("expired", DIFF_EXPIRED_MESSAGE)
        cached, file, _layout = source
        _require_gap_line(file, new_start)
        revision = _revision_from_wire(cached.revision)
        read = await self._session.review_files.read_file(
            review, file.new_path, revision.head_sha
        )
        text = read.text
        if read.status is not FileReadStatus.OK or text is None:
            return _context_result(read.status.value, read.message)
        if not verify_new_side(file, text.lines, text.ends_with_newline):
            return _context_result("mismatch", CONTEXT_MISMATCH_MESSAGE)
        rows = self._context_rows(file_index, file, text.lines, new_start, count)
        return {
            "status": "ok",
            "message": None,
            "line_count": len(text.lines),
            "rows": rows,
        }

    def _context_rows(
        self,
        file_index: int,
        file: DiffFile,
        lines: tuple[str, ...],
        new_start: int,
        count: int,
    ) -> list[JsonValue]:
        """Rows for the gap lines from ``new_start``, within every bound."""
        gap = next(
            (
                gap
                for gap in file_gaps(file, len(lines))
                if gap.length is not None
                and gap.new_start <= new_start < gap.new_start + gap.length
            ),
            None,
        )
        if gap is None:
            # The file ends where the last hunk ends: the gap after it is empty.
            return []
        end = min(new_start + count, gap.new_start + gap.length)
        # Half the negotiated value budget leaves ample room for the envelope.
        max_rows = max(1, (self._json_limits.values // 2) // _CONTEXT_ROW_VALUES)
        rows: list[JsonValue] = []
        size = 0
        for new_line in range(new_start, end):
            if len(rows) >= max_rows:
                break
            row = context_wire(
                file_index,
                gap.old_start + (new_line - gap.new_start),
                new_line,
                lines[new_line - 1],
            )
            row_size = len(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
            )
            if size + row_size > MAX_CONTEXT_BYTES:
                if not rows:
                    raise ProtocolError(
                        ProtocolErrorCode.RESPONSE_TOO_LARGE,
                        "This line is too long to show.",
                    )
                break
            rows.append(row)
            size += row_size
        return rows

    async def _diff_generate(
        self, params: JsonObject, context: RequestContext
    ) -> object:
        """Generate one truncated file's diff locally and return its first page.

        The file is resolved like ``diff.context``. On success the generated
        file is projected on its own, at its original index and in the layout
        of the snapshot it came from, into a new diff snapshot that is read
        with ``diff.page`` and released after its last page. The snapshot is
        created only when the store has room for it without evicting another
        one. A cancel frame stops the generation and keeps nothing. A cancel
        for this request or any of its pages, or a newer diff of the review
        once this one is no longer on screen, releases the snapshot early.
        """
        _require_params(params, allowed=_GENERATE_PARAMS, required=_GENERATE_PARAMS)
        review_handle = _text(params, "review", max_length=100)
        snapshot = _text(params, "snapshot", max_length=_MAX_SNAPSHOT_ID_LENGTH)
        file_index = _required_int(params, "file_index", 0, _MAX_FILE_INDEX)
        review = self._handles.resolve(review_handle, HandleKind.REVIEW, ReviewRef)
        source = self._diff_source(review_handle, snapshot, file_index)
        if source is None:
            return _generate_failure("expired", DIFF_EXPIRED_MESSAGE)
        cached, file, layout = source
        revision = _revision_from_wire(cached.revision)
        generated = await self._session.review_files.generate_file_diff(
            review, revision, file
        )
        if generated.status is not GenerationStatus.OK or generated.file is None:
            return _generate_failure(generated.status.value, generated.message)
        entries = flatten_file(file_index, generated.file, layout)
        if not self._snapshots.can_retain(
            len(entries), retained_size(cached.revision, entries)
        ):
            return _generate_failure("too_large", GENERATE_NO_ROOM_MESSAGE)
        snapshot_id = self._snapshots.create(
            review_handle,
            cached.revision,
            entries,
            projection=layout.value,
            kind=_DIFF_SNAPSHOT,
        )
        self._generated_snapshots[snapshot_id] = _GeneratedSnapshot(
            review_handle, snapshot
        )
        while len(self._generated_snapshots) > _MAX_GENERATED_SNAPSHOTS:
            self._generated_snapshots.popitem(last=False)
        self._remember_generated_request(context.request_id, snapshot_id)
        try:
            page = self._snapshots.page(
                snapshot_id, review_handle, 0, _DIFF_PAGE_ITEMS, kind=_DIFF_SNAPSHOT
            )
        except BaseException:
            self._release_generated(snapshot_id)
            raise
        if page.next_cursor is None:
            self._release_generated(snapshot_id)
        return {
            "status": "ok",
            "message": None,
            "generated": True,
            **self._snapshot_page_wire(page),
        }

    def _diff_source(
        self, review_handle: str, snapshot: str, file_index: int
    ) -> tuple[CachedDiff, DiffFile, DiffLayout] | None:
        """Resolve a file of an open diff, or None when the read is gone.

        Only a snapshot projected from the review's cached read resolves, so
        the file and the revision are the ones the reader sees. A snapshot of
        another review is refused outright.
        """
        layout = self._diff_cache.layout(review_handle, snapshot)
        if layout is None:
            owner = self._diff_cache.owner(snapshot)
            if owner is not None and owner != review_handle:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_PARAMS,
                    "The diff snapshot belongs to another review.",
                )
            return None
        cached = self._diff_cache.lookup(review_handle, snapshot)
        if cached is None:
            return None
        if file_index >= len(cached.files):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The file_index parameter is invalid.",
            )
        return cached, cached.files[file_index], layout

    def _release_generated(self, snapshot_id: str) -> None:
        if snapshot_id in self._generated_snapshots:
            del self._generated_snapshots[snapshot_id]
            self._snapshots.expire(snapshot_id)

    def _release_review_generated(
        self, review_handle: str, *, keep: str | None
    ) -> None:
        """Release a review's generated snapshots, except those from ``keep``."""
        for snapshot_id, generated in tuple(self._generated_snapshots.items()):
            if generated.review_handle == review_handle and generated.source != keep:
                self._release_generated(snapshot_id)

    def _remember_generated_request(self, request_id: str, snapshot_id: str) -> None:
        self._generated_requests[request_id] = snapshot_id
        self._generated_requests.move_to_end(request_id)
        while len(self._generated_requests) > _MAX_GENERATED_REQUESTS:
            self._generated_requests.popitem(last=False)

    async def _discussions_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """Open a discussions snapshot and return its first page.

        The threads of a large review exceed one frame's JSON value budget, so
        they are retained like a diff and read page by page with
        ``discussions.page``. Each page carries at most half the negotiated
        value budget, which leaves ample room for the envelope; a single
        thread larger than that fails only this request as too large. When
        the first page already holds every thread, or fails, the snapshot is
        released at once, so single-page reads never crowd the retention cap.
        """
        _require_params(
            params,
            allowed=frozenset({"review", "max_items"}),
            required=frozenset({"review"}),
        )
        review_handle = _text(params, "review", max_length=100)
        review = self._handles.resolve(review_handle, HandleKind.REVIEW, ReviewRef)
        entries = await self._discussion_entries(review)
        revision: JsonObject = {"discussion_count": len(entries)}
        snapshot_id = self._snapshots.create(
            review_handle, revision, entries, kind=_DISCUSSIONS_SNAPSHOT
        )
        try:
            value = self._discussions_page_wire(
                snapshot_id,
                review_handle,
                0,
                params.get("max_items", _DISCUSSION_PAGE_ITEMS),
            )
        except BaseException:
            self._snapshots.expire(snapshot_id)
            raise
        if value.get("next_cursor") is None:
            self._snapshots.expire(snapshot_id)
        return value

    async def _discussions_page(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"snapshot", "resource", "cursor", "max_items"}),
            required=frozenset({"snapshot", "resource", "cursor"}),
        )
        self._handles.resolve(params["resource"], HandleKind.REVIEW, ReviewRef)
        value = self._discussions_page_wire(
            params["snapshot"],
            params["resource"],
            params["cursor"],
            params.get("max_items", _DISCUSSION_PAGE_ITEMS),
        )
        # The renderer never reads a finished snapshot again, so the last page
        # releases it instead of letting it crowd diff and log snapshots.
        if value.get("next_cursor") is None:
            self._snapshots.expire(cast(str, value["snapshot_id"]))
        return value

    async def _discussions_all(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        """Return every thread at once, for in-process plugin reads.

        A plugin read never crosses the frame budget itself, so it keeps the
        whole-list shape plugins were written against.
        """
        review = self._review_param(params)
        return {"discussions": list(await self._discussion_entries(review))}

    async def _discussion_entries(self, review: ReviewRef) -> tuple[JsonObject, ...]:
        discussions = await self._session.get_discussions(review)
        return tuple(
            _discussion_wire(cast(JsonObject, to_json_value(discussion)))
            for discussion in discussions
        )

    def _discussions_page_wire(
        self, snapshot_id: object, resource: object, cursor: object, max_items: object
    ) -> JsonObject:
        page = self._snapshots.page(
            snapshot_id,
            resource,
            cursor,
            max_items,
            max_values=self._json_limits.values // 2,
            kind=_DISCUSSIONS_SNAPSHOT,
        )
        value = self._snapshot_page_wire(page)
        value["discussions"] = value.pop("entries")
        return value

    async def _commits_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        review = self._review_param(params)
        commits = await self._session.get_commits(review)
        return {
            "commits": [
                _absent_text_as_null(to_json_value(commit), _COMMIT_OPTIONAL_TEXT)
                for commit in commits
            ]
        }

    async def _pipelines_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"repository", "per_page"}),
            required=frozenset({"repository"}),
        )
        repository = self._handles.resolve(
            params["repository"], HandleKind.REPOSITORY, RepositoryRef
        )
        per_page = _optional_int(params, "per_page", 20, minimum=1, maximum=100)
        pipelines = await self._session.list_pipelines(repository, per_page=per_page)
        return {
            "pipelines": [self._pipeline_wire(repository, item) for item in pipelines]
        }

    async def _review_pipelines_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "per_page"}),
            required=frozenset({"review"}),
        )
        review = self._handles.resolve(params["review"], HandleKind.REVIEW, ReviewRef)
        per_page = _optional_int(params, "per_page", 20, minimum=1, maximum=100)
        pipelines = await self._session.list_review_pipelines(review, per_page=per_page)
        return {
            "pipelines": [
                self._pipeline_wire(review.repository, item) for item in pipelines
            ]
        }

    async def _jobs_list(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"pipeline"}),
            required=frozenset({"pipeline"}),
        )
        pipeline = self._handles.resolve(
            params["pipeline"], HandleKind.PIPELINE, PipelineRef
        )
        jobs = await self._session.get_pipeline_jobs(pipeline)
        return {"jobs": [self._job_wire(pipeline, item) for item in jobs]}

    async def _logs_open(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"job", "max_items"}),
            required=frozenset({"job"}),
        )
        job_handle = _text(params, "job", max_length=100)
        job = self._handles.resolve(job_handle, HandleKind.JOB, JobRef)
        log = await self._session.get_job_log_tail(job, _MAX_LOG_SNAPSHOT_BYTES)
        revision, entries = _log_snapshot(log, _MAX_LOG_SNAPSHOT_BYTES)
        snapshot_id = self._snapshots.create(
            job_handle, revision, entries, kind=_LOG_SNAPSHOT
        )
        return self._snapshot_page_wire(
            self._snapshots.page(
                snapshot_id,
                job_handle,
                0,
                params.get("max_items", 8),
                kind=_LOG_SNAPSHOT,
            )
        )

    async def _logs_page(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"snapshot", "resource", "cursor", "max_items"}),
            required=frozenset({"snapshot", "resource", "cursor"}),
        )
        self._handles.resolve(params["resource"], HandleKind.JOB, JobRef)
        return self._snapshot_page_wire(
            self._snapshots.page(
                params["snapshot"],
                params["resource"],
                params["cursor"],
                params.get("max_items", 8),
                kind=_LOG_SNAPSHOT,
            )
        )

    async def _plugins_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        registry = self._require_plugin_registry()
        descriptors = {
            (item.plugin_id, item.asset_id): item
            for item in self._assets.descriptors
            if item.plugin_id is not None
        }
        return {
            "plugins": [
                _plugin_record_wire(record, descriptors) for record in registry.plugins
            ]
        }

    async def _plugins_invoke(
        self, params: JsonObject, context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"plugin", "method", "params"}),
            required=frozenset({"plugin", "method", "params"}),
        )
        plugin_id = _text(params, "plugin", max_length=80)
        method = _text(params, "method", max_length=80)
        call_params = params["params"]
        if not isinstance(call_params, dict):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "Plugin invocation parameters must be an object.",
            )
        result = await self._require_plugin_registry().call(
            plugin_id,
            method,
            cast(PluginJsonObject, call_params),
            DesktopCallContext(
                context.request_id, context.cancellation, self._location
            ),
        )
        if result.error is not None:
            raise DesktopPluginContractError(result.error)
        return {"value": result.value}

    async def _assets_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        return {"assets": self._assets.descriptors}

    async def _assets_read(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"asset", "offset", "length"}),
            required=frozenset({"asset", "offset", "length"}),
        )
        return self._assets.read(params["asset"], params["offset"], params["length"])

    async def _set_location(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"location"}),
            required=frozenset({"location"}),
        )
        location = params["location"]
        if location is None:
            self._location = None
        elif isinstance(location, dict):
            self._location = DesktopLocation(
                freeze_json_object(cast(PluginJsonObject, location))
            )
        else:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "Desktop location must be an object or null.",
            )
        return {"accepted": True}

    def _review_param(self, params: JsonObject) -> ReviewRef:
        _require_params(
            params,
            allowed=frozenset({"review"}),
            required=frozenset({"review"}),
        )
        return self._handles.resolve(params["review"], HandleKind.REVIEW, ReviewRef)

    def _repository_wire(self, snapshot: RepositorySnapshot) -> JsonObject:
        return {
            "handle": self._handles.issue(HandleKind.REPOSITORY, snapshot.ref),
            "display_name": snapshot.display_name,
            "forge_type": snapshot.forge_type.value,
            "hostname": snapshot.ref.hostname,
        }

    def _review_list_wire(self, item: ReviewListItem) -> JsonObject:
        repository_handle = self._handles.issue(
            HandleKind.REPOSITORY, item.ref.repository
        )
        review_handle = self._handles.issue(HandleKind.REVIEW, item.ref)
        self._remember_head(item.ref, item.summary.head_commit_sha)
        return {
            "handle": review_handle,
            "repository": repository_handle,
            "summary": cast(JsonValue, _summary_wire(item.summary)),
        }

    def _remember_head(self, review: ReviewRef, head_sha: str) -> None:
        """Keep the head commit a list read named, replacing an older one."""
        self._review_heads.pop(review, None)
        if not head_sha:
            return
        if len(self._review_heads) >= _MAX_REVIEW_HEADS:
            self._review_heads.pop(next(iter(self._review_heads)))
        self._review_heads[review] = head_sha

    def _review_snapshot_wire(self, snapshot: ReviewSnapshot) -> JsonObject:
        repository_handle = self._handles.issue(
            HandleKind.REPOSITORY, snapshot.ref.repository
        )
        review_handle = self._handles.issue(HandleKind.REVIEW, snapshot.ref)
        result: JsonObject = {
            "handle": review_handle,
            "repository": repository_handle,
            "detail": cast(JsonValue, _detail_wire(snapshot)),
            "capabilities": cast(JsonValue, to_json_value(snapshot.capabilities)),
        }
        if snapshot.revision is not None:
            result["revision"] = to_json_value(snapshot.revision)
            result["revision_error"] = None
        else:
            assert snapshot.revision_error is not None
            result["revision"] = None
            result["revision_error"] = {
                "code": snapshot.revision_error.code.value,
                "message": snapshot.revision_error.message,
                "retryable": snapshot.revision_error.retryable,
            }
        return result

    def _pipeline_wire(
        self, repository: RepositoryRef, pipeline: Pipeline
    ) -> JsonObject:
        ref = PipelineRef(repository, pipeline.id)
        return {
            "handle": self._handles.issue(HandleKind.PIPELINE, ref),
            "value": _absent_text_as_null(
                to_json_value(pipeline), _PIPELINE_OPTIONAL_TEXT
            ),
        }

    def _job_wire(self, pipeline: PipelineRef, job: PipelineJob) -> JsonObject:
        ref = JobRef(pipeline.repository, job.id)
        return {
            "handle": self._handles.issue(HandleKind.JOB, ref, parent=pipeline),
            "value": _absent_text_as_null(to_json_value(job), _JOB_OPTIONAL_TEXT),
        }

    @staticmethod
    def _snapshot_page_wire(page: object) -> JsonObject:
        value = cast(JsonObject, to_json_value(page))
        value["resource"] = value.pop("resource_handle")
        value.pop("projection")
        return value

    def _facade_for_plugin(
        self, plugin_id: str, manifest: DesktopPluginManifest
    ) -> DesktopHostFacade:
        return _BoundPluginFacade(self, plugin_id, manifest)

    def _require_plugin_registry(self) -> DesktopPluginRegistry:
        if self._plugin_registry is None:
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "The desktop plugin registry is unavailable.",
            )
        return self._plugin_registry

    async def _dispatch_plugin_read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        method = {
            DesktopReadKind.REPOSITORIES: "repositories.discover",
            DesktopReadKind.REVIEWS: "reviews.list",
            DesktopReadKind.REVIEW: "reviews.get",
            DesktopReadKind.DISCUSSIONS: "discussions.list",
            DesktopReadKind.COMMITS: "commits.list",
            DesktopReadKind.PIPELINES: "pipelines.list",
            DesktopReadKind.JOBS: "jobs.list",
            DesktopReadKind.LOG: "logs.open",
        }[kind]
        whole_list_handlers = {
            DesktopReadKind.REVIEWS: self._reviews_all,
            DesktopReadKind.DISCUSSIONS: self._discussions_all,
        }
        handler = whole_list_handlers.get(kind, self._operations[method].handler)
        # The read gets its own token, linked one way to the caller's: the
        # caller cancelling stops the read, but a failed or abandoned read
        # never cancels the caller's token. A plugin often passes its lifetime
        # token, and one failed read must not stop every later one (#312).
        read_cancellation = DesktopCancellation()
        context = RequestContext(
            f"plugin-{id(read_cancellation):x}",
            read_cancellation,
            mutation=False,
            dispatched=True,
        )
        task = asyncio.create_task(
            handler(cast(JsonObject, _thaw_json(params)), context)
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        completed = False
        try:
            done, _pending = await asyncio.wait(
                (task, cancellation_task), return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done:
                raise asyncio.CancelledError
            result = freeze_json(cast(PluginJsonObject, to_json_value(task.result())))
            completed = True
            return result
        finally:
            if not completed:
                read_cancellation.cancel()
            task.cancel()
            cancellation_task.cancel()
            await _bounded_tasks_cleanup(
                (cast(asyncio.Task[object], task), cancellation_task),
                self._shutdown_timeout,
            )

    async def _publish_plugin_event(
        self, plugin_id: str, event_id: str, payload: FrozenJsonObject
    ) -> None:
        await self._events.publish(
            "plugin.event",
            {"plugin": plugin_id, "event": event_id, "payload": payload},
            replaceable_key=f"plugin:{plugin_id}:{event_id}",
        )

    async def _publish_plugin_notification(
        self,
        plugin_id: str,
        message: str,
        severity: DesktopNotificationSeverity,
    ) -> None:
        await self._events.publish(
            "plugin.notification",
            {"plugin": plugin_id, "message": message, "severity": severity.value},
        )

    async def _publish_plugin_focus(
        self, plugin_id: str, target_id: str, metadata: FrozenJsonObject
    ) -> None:
        await self._events.publish(
            "plugin.focus",
            {"plugin": plugin_id, "target": target_id, "metadata": metadata},
            replaceable_key=f"plugin-focus:{plugin_id}",
        )

    async def _pump_events(self) -> None:
        while True:
            event = await self._events.get()
            if event is None:
                return
            try:
                frame = _encode_event_or_resync(event, self._json_limits)
                await self._write(frame)
            except (ProtocolError, BrokenPipeError, ConnectionError):
                return

    async def _pump_service_events(self) -> None:
        try:
            async for event in self._session.events():
                await self._publish_service_event(event)
        except (asyncio.CancelledError, ServiceError):
            return

    async def _publish_service_event(self, event: ServiceEvent) -> None:
        resource_handle: str | None = None
        if isinstance(event.resource, RepositoryRef):
            resource_handle = self._handles.find(HandleKind.REPOSITORY, event.resource)
        elif isinstance(event.resource, ReviewRef):
            resource_handle = self._handles.find(HandleKind.REVIEW, event.resource)
            if resource_handle is not None:
                self._snapshots.expire_for_resource(resource_handle)
                self._diff_cache.drop(resource_handle)
        elif isinstance(event.resource, PipelineRef):
            resource_handle = self._handles.find(HandleKind.PIPELINE, event.resource)
        await self._events.publish(
            "service.changed",
            {
                "kind": event.kind.value,
                "resource": resource_handle,
                "revision": event.revision,
                "source_sequence": event.sequence,
            },
            replaceable_key=f"service:{event.kind.value}:{resource_handle or '*'}",
        )

    async def _write_result(self, request_id: str, result: object) -> None:
        await self._write(
            encode_response(request_id, result=result, limits=self._json_limits)
        )

    async def _write_error(self, request_id: str | None, error: ProtocolError) -> None:
        await self._write(
            encode_response(request_id, error=error, limits=self._json_limits)
        )

    async def _write(self, frame: bytes) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            await asyncio.wait_for(
                self._writer_lock.acquire(), timeout=self._shutdown_timeout
            )
        except TimeoutError as error:
            raise ConnectionError("desktop protocol writer is backlogged") from error
        try:
            writer.write(frame)
            drain_task = asyncio.create_task(writer.drain())
            try:
                _done, pending = await asyncio.wait(
                    (drain_task,), timeout=self._shutdown_timeout
                )
            except BaseException:
                drain_task.cancel()
                drain_task.add_done_callback(_consume_task)
                raise
            if pending:
                drain_task.cancel()
                drain_task.add_done_callback(_consume_task)
                raise ConnectionError("desktop protocol writer did not drain")
            drain_task.result()
        finally:
            self._writer_lock.release()

    async def _shutdown(self) -> None:
        if not self._stopping:
            self._stopping = True
        for pending in tuple(self._pending.values()):
            pending.context.cancellation.cancel()
            pending.task.cancel()
        tasks = {pending.task for pending in self._pending.values()}
        if tasks:
            _done, survivors = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
            for task in survivors:
                task.cancel()
                task.add_done_callback(_consume_task)
        self._pending.clear()

        if self._service_event_task is not None:
            self._service_event_task.cancel()
            await _bounded_task_cleanup(
                self._service_event_task, self._shutdown_timeout
            )

        registry = self._plugin_registry
        if registry is not None:
            await _bounded_cleanup(registry.stop_all(), self._shutdown_timeout)
        # The process exits next and no later close will retry, so this is the
        # final close: after a failed review submission cleanup it still closes
        # the draft store, the registry and the cache.
        await _bounded_cleanup(
            self._session.close(final=True),
            _FINAL_CLOSE_WAITS * self._shutdown_timeout,
        )
        if self._editor_exports is not None:
            await _bounded_cleanup(self._editor_exports.close(), self._shutdown_timeout)
        self._snapshots.clear()
        self._diff_cache.clear()
        self._generated_snapshots.clear()
        self._generated_requests.clear()
        self._latest_diffs.clear()
        self._handles.clear()
        self._assets.clear()
        await self._events.close()
        if self._event_task is not None:
            self._event_task.cancel()
            await _bounded_task_cleanup(self._event_task, self._shutdown_timeout)


class _BoundPluginFacade:
    """Facade bound to one manifest and one sidecar session."""

    def __init__(
        self,
        server: DesktopSidecarServer,
        plugin_id: str,
        manifest: DesktopPluginManifest,
    ) -> None:
        self._server = server
        self._plugin_id = plugin_id
        self._reads = frozenset(manifest.reads)
        self._methods = frozenset(item.id for item in manifest.methods)
        self._events = frozenset(item.id for item in manifest.events)
        self._focus_targets = frozenset(item.id for item in manifest.focus_targets)

    async def read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        if kind not in self._reads:
            raise _plugin_contract_error(
                self._plugin_id, "undeclared_read", "Plugin read is not declared."
            )
        return await self._server._dispatch_plugin_read(kind, params, cancellation)

    async def invoke(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> FrozenJsonValue:
        if method not in self._methods:
            raise _plugin_contract_error(
                self._plugin_id,
                "undeclared_method",
                "Plugin method is not declared.",
            )
        result = await self._server._require_plugin_registry().call(
            self._plugin_id,
            method,
            cast(PluginJsonObject, _thaw_json(params)),
            context,
        )
        if result.error is not None:
            raise DesktopPluginContractError(result.error)
        return result.value

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None:
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 500
            or any(
                ord(character) < 32 and character not in "\t\n" for character in message
            )
            or not isinstance(severity, DesktopNotificationSeverity)
        ):
            raise _plugin_contract_error(
                self._plugin_id, "invalid_data", "Plugin notification is invalid."
            )
        await self._server._publish_plugin_notification(
            self._plugin_id, message, severity
        )

    async def publish_event(self, event_id: str, payload: FrozenJsonObject) -> None:
        if event_id not in self._events:
            raise _plugin_contract_error(
                self._plugin_id, "undeclared_target", "Plugin event is not declared."
            )
        frozen = freeze_json_object(cast(PluginJsonObject, _thaw_json(payload)))
        await self._server._publish_plugin_event(self._plugin_id, event_id, frozen)

    def current_location(self) -> DesktopLocation | None:
        return self._server._location

    async def focus(self, target_id: str, metadata: FrozenJsonObject) -> None:
        if target_id not in self._focus_targets:
            raise _plugin_contract_error(
                self._plugin_id,
                "undeclared_target",
                "Plugin focus target is not declared.",
            )
        frozen = freeze_json_object(cast(PluginJsonObject, _thaw_json(metadata)))
        await self._server._publish_plugin_focus(self._plugin_id, target_id, frozen)


def _parse_handshake(params: JsonObject) -> tuple[frozenset[str], JsonLimits]:
    _require_params(
        params,
        allowed=frozenset(
            {"protocol_major", "core_version", "capabilities", "client", "limits"}
        ),
        required=frozenset({"protocol_major", "core_version", "capabilities"}),
    )
    major = params["protocol_major"]
    if type(major) is not int or major != 1:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The requested desktop protocol major is not supported.",
        )
    core_version = params["core_version"]
    if not isinstance(core_version, str) or core_version != _core_version():
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The desktop client requires a different Tongs core version.",
        )
    capabilities = params["capabilities"]
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "Handshake capabilities must be a list of names.",
        )
    requested = frozenset(capabilities)
    if len(requested) != len(capabilities) or not requested <= SUPPORTED_CAPABILITIES:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "One or more required desktop capabilities are not supported.",
        )
    if "client" in params:
        _text(params, "client", max_length=120)
    return requested, _parse_handshake_limits(params.get("limits"))


def _parse_handshake_limits(value: JsonValue | None) -> JsonLimits:
    """Negotiate the JSON budget from the limits the client declares.

    A client that declares no limits gets the sidecar defaults. Declared limits
    below the protocol floor, or of the wrong shape, fail the handshake closed.
    """
    if value is None:
        return DEFAULT_JSON_LIMITS
    if not isinstance(value, dict) or set(value) != {"json_values", "json_depth"}:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "Handshake limits must declare json_values and json_depth.",
        )
    values = value["json_values"]
    depth = value["json_depth"]
    if (
        type(values) is not int
        or type(depth) is not int
        or values < MIN_JSON_ITEMS
        or depth < MIN_JSON_DEPTH
    ):
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The desktop client JSON limits are incompatible with this sidecar.",
            details={
                "min_json_values": MIN_JSON_ITEMS,
                "min_json_depth": MIN_JSON_DEPTH,
                "max_json_values": MAX_JSON_ITEMS,
                "max_json_depth": MAX_JSON_DEPTH,
            },
        )
    return DEFAULT_JSON_LIMITS.negotiate(values, depth)


_REVIEW_CURSOR_PREFIX = "rc1"
_REVIEW_CURSOR_MAX_LENGTH = 64


class _ReviewCursors:
    """Seal session review cursors so the renderer can only return real ones.

    A cursor names the session's next page and carries a keyed digest over that
    page and the query it belongs to (scope, state, page size and repository
    handle). The key lives only in this server instance, so a cursor from a
    different query, a previous service run or any hand-written value fails
    validation instead of reaching the forge.
    """

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def seal(self, cursor: str, binding: tuple[str, str, int, str | None]) -> str:
        return f"{_REVIEW_CURSOR_PREFIX}.{cursor}.{self._digest(cursor, binding)}"

    def open(self, value: object, binding: tuple[str, str, int, str | None]) -> str:
        invalid = ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The review list cursor is invalid.",
        )
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= _REVIEW_CURSOR_MAX_LENGTH
            or not value.isascii()
        ):
            raise invalid
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != _REVIEW_CURSOR_PREFIX:
            raise invalid
        cursor, digest = parts[1], parts[2]
        try:
            review_cursor_page(cursor)
        except ValueError:
            raise invalid from None
        if not hmac.compare_digest(digest, self._digest(cursor, binding)):
            raise invalid
        return cursor

    def _digest(self, cursor: str, binding: tuple[str, str, int, str | None]) -> str:
        scope, state, per_page, repository = binding
        message = "\x1f".join(
            ("reviews.list", scope, state, str(per_page), repository or "", cursor)
        ).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:32]


def _require_params(
    params: JsonObject,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(params)
    if not required <= keys or not keys <= allowed:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The request parameters have unknown or missing fields.",
        )


def _text(params: JsonObject, key: str, *, max_length: int) -> str:
    value = params.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _optional_text(
    params: JsonObject, key: str, default: str, *, max_length: int
) -> str:
    if key not in params:
        return default
    return _text(params, key, max_length=max_length)


def _optional_bool(params: JsonObject, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _optional_int(
    params: JsonObject,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = params.get(key, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _required_int(params: JsonObject, key: str, minimum: int, maximum: int) -> int:
    if key not in params:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return _optional_int(params, key, minimum, minimum=minimum, maximum=maximum)


def _require_gap_line(file: DiffFile, new_start: int) -> None:
    """Refuse a line outside the file's gaps before anything is read.

    The gap after the last hunk has no known end until the file is read, so
    any line from its start on is accepted here and bounded after the read.
    """
    for gap in file_gaps(file):
        if gap.length is None:
            if gap.position is GapPosition.AFTER and new_start >= gap.new_start:
                return
        elif gap.new_start <= new_start < gap.new_start + gap.length:
            return
    raise ProtocolError(
        ProtocolErrorCode.INVALID_PARAMS,
        "The requested lines are not unchanged lines of this file.",
    )


def _revision_from_wire(value: JsonObject) -> ReviewRevision:
    head_sha = value.get("head_sha")
    base_sha = value.get("base_sha")
    start_sha = value.get("start_sha")
    if (
        isinstance(head_sha, str)
        and isinstance(base_sha, str)
        and (start_sha is None or isinstance(start_sha, str))
    ):
        try:
            return ReviewRevision(head_sha, base_sha, start_sha)
        except ValueError:
            pass
    raise ProtocolError(
        ProtocolErrorCode.INTERNAL,
        "The diff revision could not be resolved safely.",
    )


def _context_result(status: str, message: str) -> JsonObject:
    return {"status": status, "message": message, "line_count": None, "rows": []}


def _generate_failure(status: str, message: str) -> JsonObject:
    return {
        "status": status,
        "message": message,
        "generated": False,
        "snapshot_id": None,
        "resource": None,
        "revision": None,
        "cursor": None,
        "next_cursor": None,
        "entries": [],
    }


def _summary_wire(summary: MRSummary) -> JsonObject:
    return {
        "number": summary.number,
        "title": summary.title,
        "author": cast(JsonValue, to_json_value(summary.author)),
        "state": summary.state.value,
        "is_draft": summary.is_draft,
        "source_branch": summary.source_branch,
        "target_branch": summary.target_branch,
        "ci_status": summary.ci_status.value,
        "created_at": cast(JsonValue, to_json_value(summary.created_at)),
        "updated_at": cast(JsonValue, to_json_value(summary.updated_at)),
        "web_url": summary.web_url,
        "comment_count": summary.comment_count,
        "has_conflicts": summary.has_conflicts,
        "labels": cast(JsonValue, to_json_value(summary.labels)),
        "review_decision": cast(JsonValue, to_json_value(summary.review_decision)),
        "additions": summary.additions,
        "deletions": summary.deletions,
    }


def _detail_wire(snapshot: ReviewSnapshot) -> JsonObject:
    detail = snapshot.detail
    result = _summary_wire(detail)
    result.update(
        {
            "description": detail.description,
            "merge_status": detail.merge_status or None,
            "approvals": cast(JsonValue, to_json_value(detail.approvals)),
            "reviewers": cast(JsonValue, to_json_value(detail.reviewers)),
            "assignees": cast(JsonValue, to_json_value(detail.assignees)),
            "changes_count": detail.changes_count,
            "detailed_merge_status": detail.detailed_merge_status,
            "draft_notes_count": detail.draft_notes_count,
            "status_check_rollup": detail.status_check_rollup,
            # True when the forge capped the changed file list, so the diff
            # shows only part of the review. The shell reads absence as false.
            "changes_overflow": detail.changes_overflow,
        }
    )
    return result


# Text fields a forge may leave out, per projected model. The core models spell
# such an absent value as the empty string (or carry the forge's own null), while
# the desktop wire spells every absent optional text as null and rejects an
# empty string, so one missing value cannot make the client refuse a whole list.
# Fields not named here are required and keep whatever the core holds, so a
# broken required value still fails loudly at the client boundary.
_PIPELINE_OPTIONAL_TEXT = frozenset({"ref", "web_url", "source"})
_JOB_OPTIONAL_TEXT = frozenset({"stage", "web_url"})
_COMMIT_OPTIONAL_TEXT = frozenset({"web_url"})


def _absent_text_as_null(value: JsonValue, optional: frozenset[str]) -> JsonObject:
    """Return a projected model with each absent optional text as null."""
    projected = cast(JsonObject, value)
    return {
        key: (item or None) if key in optional else item
        for key, item in projected.items()
    }


def _discussion_wire(discussion: JsonObject) -> JsonObject:
    """Project one thread with its absent inline position expressed as null."""
    return {
        **discussion,
        "root_comment": _comment_wire(
            cast(JsonObject, discussion["root_comment"]),
        ),
    }


def _comment_wire(comment: JsonObject) -> JsonObject:
    """Emit the wire form of a comment without an inline position.

    A review-level note legitimately has no file. The core model spells that
    absence as an empty path, while the desktop wire spells every absent
    optional text as null, so one unpositioned note cannot make the client
    reject the whole discussion list.
    """
    return {
        **comment,
        "file_path": comment["file_path"] or None,
        "replies": [
            _comment_wire(cast(JsonObject, reply))
            for reply in cast(list[JsonValue], comment["replies"])
        ],
    }


def _log_snapshot(
    log: JobLogTail, budget: int
) -> tuple[JsonObject, tuple[JsonObject, ...]]:
    """Return the revision and rows of a log snapshot within ``budget``.

    ``budget`` is measured as ``SnapshotStore.create`` measures a snapshot, so
    a log that passes here is never refused by the store. A log that is
    already cut, or whose escaped rows outgrow the budget, keeps its end from
    a line boundary, after a first line that names the sizes.
    """
    text = log.text
    truncated = log.truncated
    for _ in range(_MAX_LOG_TRIMS):
        shown = (
            _log_truncation_note(text, log.total_bytes) + text if truncated else text
        )
        encoded = shown.encode("utf-8")
        revision: JsonObject = {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_count": len(encoded),
        }
        entries = _log_entries(encoded)
        excess = retained_size(revision, entries) - budget
        if excess <= 0:
            return revision, entries
        # Each character adds at least one byte to the measure, so dropping
        # the excess plus the note's room in characters makes enough space.
        text = _log_from_line_start(text[excess + _LOG_NOTE_ROOM :])
        truncated = True
    raise ProtocolError(
        ProtocolErrorCode.RESPONSE_TOO_LARGE,
        "The job log exceeds the session retention limit.",
    )


def _log_truncation_note(text: str, total_bytes: int) -> str:
    shown = len(text.encode("utf-8"))
    total = max(total_bytes, shown)
    return (
        f"[tongs] This job log is {_byte_size(total)}. Showing the last "
        f"{_byte_size(shown)}; earlier output is left out.\n"
    )


def _byte_size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MiB"
    if count >= 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count} bytes"


def _log_from_line_start(text: str) -> str:
    newline = text.find("\n")
    if newline == -1 or newline == len(text) - 1:
        return text
    return text[newline + 1 :]


def _log_entries(encoded: bytes) -> tuple[JsonObject, ...]:
    """Split UTF-8 log bytes into independently decodable bounded rows."""
    if not encoded:
        return ({"text": ""},)
    entries: list[JsonObject] = []
    start = 0
    target = 256 * 1024
    while start < len(encoded):
        end = min(start + target, len(encoded))
        while True:
            try:
                text = encoded[start:end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                end = start + error.start
                if end <= start:
                    raise ProtocolError(
                        ProtocolErrorCode.INTERNAL,
                        "The job log could not be encoded safely.",
                    ) from None
        entries.append({"text": text})
        start = end
    return tuple(entries)


def _plugin_record_wire(
    record: DesktopPluginRecord,
    descriptors: Mapping[tuple[str, str], AssetDescriptor],
) -> JsonObject:
    plugin_id = record.plugin_id
    state = record.state
    manifest = record.manifest
    error = record.error
    result: JsonObject = {
        "plugin_id": plugin_id,
        "state": state.value,
        "has_terminal_entry_point": record.has_terminal_entry_point,
        "has_desktop_entry_point": record.has_desktop_entry_point,
    }
    if error is not None:
        result["error"] = {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
        }
    else:
        result["error"] = None
    if manifest is None:
        result["manifest"] = None
        return result

    assets_available = state is DesktopPluginState.STARTED

    def asset_handle(asset_id: str) -> str | None:
        if not assets_available:
            return None
        descriptor = descriptors.get((plugin_id, asset_id))
        if descriptor is None:
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "A declared plugin asset is unavailable.",
            )
        return descriptor.handle

    result["manifest"] = {
        "title": manifest.title,
        "version": manifest.version,
        "api_major": manifest.compatibility.api_major,
        "modules": [
            {
                "id": module.id,
                "title": module.title,
                "entry_asset": asset_handle(module.entry_asset_id),
                "stylesheets": [
                    asset_handle(item) for item in module.stylesheet_asset_ids
                ],
            }
            for module in manifest.modules
        ],
        "navigation": [to_json_value(item) for item in manifest.navigation],
        "commands": [to_json_value(item) for item in manifest.commands],
        "methods": [item.id for item in manifest.methods],
        "events": [item.id for item in manifest.events],
        "focus_targets": [to_json_value(item) for item in manifest.focus_targets],
        "help_asset": asset_handle(manifest.help_asset_id)
        if manifest.help_asset_id is not None
        else None,
        "reads": [item.value for item in manifest.reads],
        "assets_available": assets_available,
    }
    return result


def _bounded_wait(value: object, default: float) -> int:
    """Whole seconds of a rate-limit wait, from 1 up to a day.

    ``value`` is a number of seconds or the text of one, as a service error
    detail carries it; anything else takes ``default``.
    """
    seconds = default
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        with suppress(ValueError):
            seconds = float(value)
    if not math.isfinite(seconds):
        seconds = default
    return min(_MAX_RATE_LIMIT_WAIT_SECONDS, max(1, math.ceil(seconds)))


def _service_protocol_error(error: ServiceError) -> ProtocolError:
    details: dict[str, str] = {"service_code": error.code.value}
    details.update(dict(error.details))
    return ProtocolError(
        ProtocolErrorCode.SERVICE_ERROR,
        error.message,
        retryable=error.retryable,
        details=details,
    )


def _plugin_protocol_error(error: DesktopPluginContractError) -> ProtocolError:
    return ProtocolError(
        ProtocolErrorCode.PLUGIN_ERROR,
        error.error.message,
        retryable=error.error.retryable,
        details={
            "plugin_id": error.error.plugin_id,
            "plugin_code": error.error.code.value,
        },
    )


def _plugin_contract_error(
    plugin_id: str, code: str, message: str
) -> DesktopPluginContractError:
    return DesktopPluginContractError(
        DesktopPluginError(DesktopPluginErrorCode(code), message, plugin_id)
    )


def _thaw_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    raise ProtocolError(
        ProtocolErrorCode.INVALID_PARAMS,
        "The plugin data is not valid JSON.",
    )


async def _bounded_cleanup(awaitable: Awaitable[object], timeout: float) -> None:
    task = asyncio.create_task(awaitable)
    _done, pending = await asyncio.wait((task,), timeout=timeout)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task)
        return
    with suppress(BaseException):
        task.result()


async def _bounded_task_cleanup(task: asyncio.Task[object], timeout: float) -> None:
    _done, pending = await asyncio.wait((task,), timeout=timeout)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task)
        return
    with suppress(BaseException):
        task.result()


async def _bounded_tasks_cleanup(
    tasks: tuple[asyncio.Task[object], ...], timeout: float
) -> None:
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_task)
    for task in done:
        _consume_task(task)


def _consume_task(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


def _color_scheme(config: object) -> str:
    """The ``[ui] color_scheme`` the desktop window starts from (#226).

    The loader already turns an unknown name into the default; a session
    without the setting, such as a test double, also gets the default.
    """
    value = getattr(config, "color_scheme", COLOR_SCHEMES[0])
    return (
        value if isinstance(value, str) and value in COLOR_SCHEMES else COLOR_SCHEMES[0]
    )


#: Bounds of the handshake's ``recovery_warnings``, matched by the desktop shell.
#: The text bound counts UTF-16 code units, as JavaScript's ``String.length``
#: does, so both sides measure a notice the same way.
MAX_RECOVERY_NOTICES = 20
MAX_RECOVERY_NOTICE_CHARS = 1000


def _recovery_notices(warnings: tuple[RecoveryWarning, ...]) -> list[JsonValue]:
    """Describe startup recovery warnings for the desktop to show the user.

    Each names the review to check on the forge. The list and each text are
    bounded so a damaged store can never make the handshake too large.
    """
    notices: list[JsonValue] = []
    for warning in warnings[:MAX_RECOVERY_NOTICES]:
        notices.append(_truncate_utf16(warning.describe(), MAX_RECOVERY_NOTICE_CHARS))
    return notices


def _utf16_length(text: str) -> int:
    """Return the length of ``text`` in UTF-16 code units, like JavaScript."""
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def _truncate_utf16(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-16 code units, ending in an ellipsis.

    The cut falls between code points, so it never splits a surrogate pair.
    """
    if _utf16_length(text) <= limit:
        return text
    budget = limit - 1
    used = 0
    end = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        end += 1
    return text[:end] + "…"


def _report_recovery_warnings(warnings: tuple[RecoveryWarning, ...]) -> None:
    """Write skipped submission attempts to the sidecar diagnostics stream."""
    for warning in warnings:
        print(
            f"tongs desktop draft recovery warning: {warning.describe()}",
            file=sys.stderr,
        )


def _core_version() -> str:
    try:
        return version("tongs")
    except PackageNotFoundError:
        return "0+unknown"


__all__ = [
    "SUPPORTED_CAPABILITIES",
    "SUPPORTED_METHODS",
    "AsyncByteWriter",
    "DesktopSidecarServer",
    "OperationHandler",
    "RequestContext",
]
