"""A job log over the snapshot budget opens as its bounded end (#314)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest

from tongs.config import Config
from tongs.desktop.protocol.server import (
    _MAX_LOG_SNAPSHOT_BYTES,
    DesktopSidecarServer,
    RequestContext,
    _log_snapshot,
)
from tongs.desktop.protocol.state import (
    MAX_SNAPSHOT_RETAINED_BYTES,
    HandleKind,
    retained_size,
)
from tongs.forges.models import JobLogTail
from tongs.plugins.desktop import DesktopCancellation
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import JobRef, RepositoryRef, ServiceEvent

_MIB = 1024 * 1024


class _LogSession:
    recovery_warnings: tuple[object, ...] = ()

    def __init__(self, log: bytes) -> None:
        self.config = Config()
        self.log = log
        self.budgets: list[int] = []

    async def start(self) -> _LogSession:
        return self

    async def close(self, *, final: bool = False) -> None:
        return None

    async def events(self) -> AsyncIterator[ServiceEvent]:
        if False:  # pragma: no cover - an empty event stream
            yield cast(ServiceEvent, None)

    async def get_job_log_tail(self, _job: JobRef, max_bytes: int) -> JobLogTail:
        self.budgets.append(max_bytes)
        return JobLogTail.from_bytes(self.log, len(self.log), max_bytes)


async def _open_whole_log(session: _LogSession) -> tuple[str, dict[str, object]]:
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    job = JobRef(RepositoryRef("git.example.com", "team/project"), 2)
    handle = server._handles.issue(HandleKind.JOB, job)
    page = cast(
        dict[str, object],
        await server._logs_open(
            {"job": handle, "max_items": 1000},
            RequestContext("log", DesktopCancellation()),
        ),
    )
    first = page
    rows: list[str] = []
    while True:
        rows.extend(
            cast(dict[str, str], row)["text"]
            for row in cast(list[object], page["entries"])
        )
        if page["next_cursor"] is None:
            break
        page = cast(
            dict[str, object],
            await server._logs_page(
                {
                    "snapshot": first["snapshot_id"],
                    "resource": handle,
                    "cursor": page["next_cursor"],
                    "max_items": 1000,
                },
                RequestContext("page", DesktopCancellation()),
            ),
        )
    return "".join(rows), cast(dict[str, object], first["revision"])


def test_the_log_budget_leaves_room_in_the_snapshot_store() -> None:
    assert _MAX_LOG_SNAPSHOT_BYTES == 8 * _MIB
    assert _MAX_LOG_SNAPSHOT_BYTES < MAX_SNAPSHOT_RETAINED_BYTES


@pytest.mark.asyncio
async def test_a_thirty_mib_log_opens_as_its_end_with_a_truncation_note() -> None:
    line = b"x" * 99 + b"\n"
    log = line * (30 * _MIB // len(line)) + b"the build failed here\n"
    session = _LogSession(log)

    text, revision = await _open_whole_log(session)

    assert session.budgets == [_MAX_LOG_SNAPSHOT_BYTES]
    lines = text.split("\n")
    assert lines[0].startswith("[tongs] This job log is 30.0 MiB. Showing the last ")
    assert lines[0].endswith("; earlier output is left out.")
    assert lines[1] == "x" * 99
    assert lines[-2] == "the build failed here"
    assert revision["byte_count"] == len(text.encode())
    assert len(text.encode()) <= _MAX_LOG_SNAPSHOT_BYTES


@pytest.mark.asyncio
async def test_an_escape_heavy_log_is_cut_by_the_store_measure() -> None:
    # Under the budget in UTF-8 bytes, but each escape and newline grows when
    # the store encodes its rows as JSON.
    line = b"\x1b[31mline\x1b[0m\n"
    log = line * ((_MAX_LOG_SNAPSHOT_BYTES - 1024) // len(line))
    assert len(log) < _MAX_LOG_SNAPSHOT_BYTES
    session = _LogSession(log)

    text, revision = await _open_whole_log(session)

    lines = text.split("\n")
    assert lines[0].startswith("[tongs] This job log is ")
    assert lines[1] == "\x1b[31mline\x1b[0m"
    assert text.endswith("\x1b[31mline\x1b[0m\n")
    assert revision["byte_count"] == len(text.encode())


@pytest.mark.asyncio
async def test_a_log_within_the_budget_opens_unchanged() -> None:
    session = _LogSession(b"one\ntwo\n")

    text, revision = await _open_whole_log(session)

    assert text == "one\ntwo\n"
    assert revision["byte_count"] == 8


def test_a_log_snapshot_always_fits_its_budget() -> None:
    for log in (
        JobLogTail("a\n" * 5000, 10_000, False),
        JobLogTail("\x00" * 9000, 50_000, True),
        JobLogTail("é" * 4000 + "\n" + "z" * 4000, 12_001, False),
    ):
        revision, entries = _log_snapshot(log, 4096)

        assert retained_size(revision, entries) <= 4096
        text = "".join(str(entry["text"]) for entry in entries)
        assert text.startswith("[tongs] This job log is ")
        assert revision["byte_count"] == len(text.encode())
