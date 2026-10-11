"""Narrow, UI-independent workspace utility authority."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from urllib.parse import urlsplit

from tongs.config import Config
from tongs.forges.models import (
    JobLogTail,
    job_log_truncation_note,
    log_from_line_start,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import JobRef, ReviewRef, ReviewSnapshot

MAX_EDITOR_LOG_BYTES = 4 * 1024 * 1024
# Room kept for the note that starts a cut export, whose sizes may change by a
# few digits after a further cut.
_EDITOR_LOG_NOTE_ROOM = 128
# Trimming converges in one or two steps; the bound only stops a bad input.
_EDITOR_LOG_TRIMS = 4
# An export's JSON-encoded size may be 7/4 of its byte budget: 7 MiB for the
# 4 MiB default, inside the 8 MiB desktop response frame with room for the
# rest of the response. Ordinary logs stay far below it.
_EDITOR_LOG_ENCODED_RATIO = 7
MAX_EDITOR_COMMAND_BYTES = 16 * 1024
MAX_EDITOR_ARGUMENTS = 64
MAX_EDITOR_ARGUMENT_BYTES = 4096
MAX_REVIEW_URL_BYTES = 4096

_TERMINAL_EDITORS = frozenset(
    {"joe", "less", "micro", "more", "nano", "nvim", "pico", "vi", "vim"}
)

# Launchers that hand the file to a running instance and exit at once unless
# told to wait. Each maps to the flag that makes it wait and every spelling of
# that flag it accepts, so an existing flag is never added twice.
_HANDOFF_LAUNCHERS: Mapping[str, tuple[str, frozenset[str]]] = {
    "code": ("--wait", frozenset({"--wait", "-w"})),
    "code-insiders": ("--wait", frozenset({"--wait", "-w"})),
    "codium": ("--wait", frozenset({"--wait", "-w"})),
    "subl": ("--wait", frozenset({"--wait", "-w"})),
    "gvim": ("-f", frozenset({"-f", "--nofork"})),
    "kate": ("--block", frozenset({"--block", "-b"})),
    "gedit": ("--wait", frozenset({"--wait", "-w"})),
}

# Vim's remote flags hand the file to a running Vim server and return at once.
# Each maps to the spelling that waits until the server is done with the file.
_VIM_REMOTE_WAIT: Mapping[str, str] = {
    "--remote": "--remote-wait",
    "--remote-silent": "--remote-wait-silent",
    "--remote-tab": "--remote-tab-wait",
    "--remote-tab-silent": "--remote-tab-wait-silent",
}
_VIM_REMOTE_FLAGS = frozenset(_VIM_REMOTE_WAIT) | frozenset(_VIM_REMOTE_WAIT.values())
_VIM_LAUNCHERS = frozenset({"gvim", "vim"})

# Most often the slots are held by exports an editor that returned at once left
# behind, so the message names the fix as well as the wait.
_CAPACITY_EXCEEDED_MESSAGE = (
    "The private editor export limit was reached. Editors that return at once "
    "keep their exports until they expire after 24 hours. Add the editor's wait "
    "flag, such as code --wait."
)

# emacsclient waits for the running Emacs by default; these flags make it
# return at once, so they are dropped.
_EMACSCLIENT_NO_WAIT = frozenset({"-n", "--no-wait"})


class EditorPlanStatus(str, Enum):
    """Safe outcomes produced before Electron starts an editor process."""

    READY = "ready"
    DISABLED = "disabled"
    MISSING = "missing"
    MALFORMED = "malformed"
    TERMINAL_UNSUPPORTED = "terminal_unsupported"
    LOG_TOO_LARGE = "log_too_large"
    CAPACITY_EXCEEDED = "capacity_exceeded"


@dataclass(frozen=True, slots=True)
class ReviewUrl:
    """Credential-free HTTPS URL bound to an admitted review."""

    review: ReviewRef
    url: str


@dataclass(frozen=True, slots=True)
class EditorLogPlan:
    """Trusted editor argv and bounded log content for the Electron main process."""

    status: EditorPlanStatus
    message: str
    job: JobRef
    argv: tuple[str, ...] = ()
    content: str | None = None
    reservation: EditorReservation | None = None


@dataclass(frozen=True, slots=True)
class EditorReservation:
    """Opaque reservation bound to one slot and generated export basename."""

    slot: int
    token: str
    export_name: str


class WorkspaceUtilityService:
    """Resolve renderer-safe identities into narrowly scoped local utilities."""

    def __init__(
        self,
        *,
        config: Config,
        get_review: Callable[[ReviewRef], Awaitable[ReviewSnapshot]],
        get_job_log_tail: Callable[[JobRef, int], Awaitable[JobLogTail]],
        clear_cache: Callable[[], Awaitable[None]],
        reserve_editor_export: Callable[
            [int, str], Awaitable[EditorReservation | None]
        ],
        release_editor_export: Callable[[int, str], Awaitable[bool]],
        environment: Mapping[str, str] | None = None,
        max_editor_log_bytes: int = MAX_EDITOR_LOG_BYTES,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_editor_log_bytes <= 0:
            raise ValueError("max_editor_log_bytes must be positive")
        self._config = config
        self._get_review = get_review
        self._get_job_log_tail = get_job_log_tail
        self._clear_cache = clear_cache
        self._reserve_editor_export = reserve_editor_export
        self._release_editor_export = release_editor_export
        self._environment = os.environ if environment is None else environment
        self._max_editor_log_bytes = max_editor_log_bytes
        self._token_factory = token_factory or _new_editor_token

    async def review_url(self, review: ReviewRef) -> ReviewUrl:
        """Return the current URL only when it belongs to the admitted forge."""
        snapshot = await self._get_review(review)
        if snapshot.ref != review:
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The current review identity could not be confirmed.",
            )
        url = snapshot.detail.web_url
        url_bytes = _utf8_length(url) if isinstance(url, str) else None
        parsed = None
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except (TypeError, ValueError):
            hostname = None
        if (
            not isinstance(url, str)
            or not url
            or url_bytes is None
            or url_bytes > MAX_REVIEW_URL_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
            or parsed is None
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or hostname is None
            or hostname.casefold() != review.repository.hostname.casefold()
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The current review URL is not a permitted HTTPS forge URL.",
            )
        return ReviewUrl(review, url)

    async def clear_shared_cache(self) -> None:
        """Clear only the shared API cache, leaving durable review drafts intact."""
        try:
            await self._clear_cache()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ServiceError(
                ServiceErrorCode.INTERNAL,
                "The shared API cache could not be cleared.",
            ) from error

    async def prepare_editor_log(self, job: JobRef) -> EditorLogPlan:
        """Build a bounded launch plan for one admitted job identity."""
        status, message, argv = self._editor_argv()
        if status is not EditorPlanStatus.READY:
            return EditorLogPlan(status, message, job)

        token = self._token_factory()
        reservation = await self._reserve_editor_export(job.job_id, token)
        if reservation is None:
            return EditorLogPlan(
                EditorPlanStatus.CAPACITY_EXCEEDED,
                _CAPACITY_EXCEEDED_MESSAGE,
                job,
            )
        try:
            # Only the end of the log is read, so a log of any size is held in
            # bounded memory, as the desktop log view reads it.
            tail = await self._get_job_log_tail(job, self._max_editor_log_bytes)
        except BaseException:
            await self._release_reservation(reservation)
            raise
        if not isinstance(tail, JobLogTail) or not isinstance(tail.text, str):
            await self._release_reservation(reservation)
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid job log.",
            )
        try:
            content = _editor_log_content(tail, self._max_editor_log_bytes)
        except UnicodeEncodeError as error:
            await self._release_reservation(reservation)
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The job log could not be encoded safely.",
            ) from error
        if content is None:
            await self._release_reservation(reservation)
            return EditorLogPlan(
                EditorPlanStatus.LOG_TOO_LARGE,
                "The job log is too large to export to an external editor.",
                job,
            )
        return EditorLogPlan(
            status,
            message,
            job,
            argv=argv,
            content=content,
            reservation=reservation,
        )

    async def _release_reservation(self, reservation: EditorReservation) -> None:
        with suppress(Exception):
            await self._release_editor_export(reservation.slot, reservation.token)
            # A retained row stays bounded and is reclaimed on a later operation.

    def _editor_argv(self) -> tuple[EditorPlanStatus, str, tuple[str, ...]]:
        enabled = self._config.external_editor_enabled
        if not isinstance(enabled, bool):
            return (
                EditorPlanStatus.MALFORMED,
                "The external editor setting is malformed.",
                (),
            )
        if not enabled:
            return (
                EditorPlanStatus.DISABLED,
                "External editor access is disabled in Tongs configuration.",
                (),
            )

        command = self._config.editor_command
        if not isinstance(command, str):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )
        if not command:
            command = self._environment.get("VISUAL", "") or self._environment.get(
                "EDITOR", ""
            )
        if not command:
            return (
                EditorPlanStatus.MISSING,
                "Configure [editor].command, $VISUAL, or $EDITOR to use an external editor.",
                (),
            )
        if (
            not isinstance(command, str)
            or (_command_bytes := _utf8_length(command)) is None
            or _command_bytes > MAX_EDITOR_COMMAND_BYTES
            or any(
                ord(character) < 32 or ord(character) == 127 for character in command
            )
        ):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )
        try:
            argv = tuple(shlex.split(command, posix=True))
        except ValueError:
            argv = ()
        if (
            not argv
            or len(argv) > MAX_EDITOR_ARGUMENTS
            or any(
                not argument
                or (_argument_bytes := _utf8_length(argument)) is None
                or _argument_bytes > MAX_EDITOR_ARGUMENT_BYTES
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in argument
                )
                for argument in argv
            )
        ):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )

        executable = PurePath(argv[0]).name.casefold()
        # A Vim remote hands the file to a running server, which may be graphical.
        vim_remote = executable in _VIM_LAUNCHERS and any(
            argument in _VIM_REMOTE_FLAGS for argument in argv[1:]
        )
        terminal_only = (executable in _TERMINAL_EDITORS and not vim_remote) or (
            executable in {"emacs", "emacsclient"}
            and any(argument in {"-nw", "-t", "--tty"} for argument in argv[1:])
        )
        if terminal_only:
            return (
                EditorPlanStatus.TERMINAL_UNSUPPORTED,
                "Configure a wait-capable graphical editor; terminal editors cannot attach to Tongs Desktop.",
                (),
            )
        argv = _with_wait_flag(executable, argv)
        return (
            EditorPlanStatus.READY,
            "The configured editor launch plan is ready.",
            argv,
        )


def _editor_log_content(tail: JobLogTail, budget: int) -> str | None:
    """Return the export text of ``tail`` within ``budget`` UTF-8 bytes.

    The text must also fit the desktop response frame, where it travels as a
    JSON string and control characters such as ANSI escapes take up to six
    bytes each, so its encoded size is capped too. A log over either keeps its
    end, after the note the desktop log view shows for a cut log, so the
    editor and the view describe the same cut the same way. ``None`` means
    the log cannot fit, not even as a note.
    """
    encoded_budget = budget * _EDITOR_LOG_ENCODED_RATIO // 4
    text = tail.text
    truncated = tail.truncated
    for _ in range(_EDITOR_LOG_TRIMS):
        content = (
            job_log_truncation_note(len(text.encode("utf-8")), tail.total_bytes) + text
            if truncated
            else text
        )
        excess = max(
            len(content.encode("utf-8")) - budget,
            _encoded_size(content) - encoded_budget,
        )
        if excess <= 0:
            return content
        # Each character adds at least one byte to the measure, so dropping
        # the excess plus the note's room in characters makes enough space.
        text = log_from_line_start(text[excess + _EDITOR_LOG_NOTE_ROOM :])
        truncated = True
    return None


def _encoded_size(text: str) -> int:
    """Return the bytes ``text`` takes as a JSON string, without the quotes."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _with_wait_flag(executable: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    """Make a known hand-off launcher wait, so the export stays alive while in use."""
    if executable == "emacsclient":
        return (
            argv[0],
            *(
                argument
                for argument in argv[1:]
                if argument not in _EMACSCLIENT_NO_WAIT
            ),
        )
    if executable in _VIM_LAUNCHERS:
        argv = (
            argv[0],
            *(_VIM_REMOTE_WAIT.get(argument, argument) for argument in argv[1:]),
        )
    handoff = _HANDOFF_LAUNCHERS.get(executable)
    if handoff is None:
        return argv
    flag, spellings = handoff
    if any(argument in spellings for argument in argv[1:]):
        return argv
    if len(argv) >= MAX_EDITOR_ARGUMENTS:
        return argv
    return (argv[0], flag, *argv[1:])


def _utf8_length(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _new_editor_token() -> str:
    return secrets.token_hex(16)


__all__ = [
    "MAX_EDITOR_LOG_BYTES",
    "EditorLogPlan",
    "EditorPlanStatus",
    "EditorReservation",
    "ReviewUrl",
    "WorkspaceUtilityService",
]
