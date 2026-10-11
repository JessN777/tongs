"""The sidecar closes every store as it exits, even after a failed cleanup (#442).

A store left open keeps its aiosqlite worker thread, which is not a daemon
thread, so the sidecar process would stay alive after its shutdown.
"""

from __future__ import annotations

import asyncio
import json
import threading
from importlib.metadata import version
from pathlib import Path

import pytest

from tongs.config import Config
from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import ApplicationSession
from tongs.state.drafts import DraftStore

# The server's and the session's shutdown timeout, in seconds.
_TIMEOUT = 1.0
# How long the failing submission cleanup and the running draft write each
# take: within one timeout apiece, but longer than one timeout together.
_STEP = 0.6


class _QueueWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    document = {
        "v": 1,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(document).encode() + b"\n"


def _sqlite_workers(before: set[threading.Thread]) -> list[threading.Thread]:
    return [
        thread
        for thread in set(threading.enumerate()) - before
        if "_connection_worker_thread" in thread.name
    ]


@pytest.mark.asyncio
async def test_exit_after_a_failed_submission_cleanup_closes_every_store(
    tmp_path: Path,
) -> None:
    before = set(threading.enumerate())
    drafts = DraftStore(tmp_path / "drafts.db")
    session = ApplicationSession(
        config=Config(),
        cache_path=tmp_path / "cache.db",
        draft_store=drafts,
        shutdown_timeout=_TIMEOUT,
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
        shutdown_timeout=_TIMEOUT,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))
    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
    )
    handshake = await asyncio.wait_for(writer.frames.get(), 5)
    assert "result" in handshake
    workers = _sqlite_workers(before)
    assert len(workers) == 2

    async def failing_cleanup() -> None:
        await asyncio.sleep(_STEP)
        raise RuntimeError("review submission tasks did not stop")

    session.review_submissions.close = failing_cleanup  # type: ignore[method-assign]
    # A draft store write is still running when the sidecar stops, and the
    # final close lets it finish before it closes the store.
    await drafts._operation_lock.acquire()
    asyncio.get_running_loop().call_later(2 * _STEP, drafts._operation_lock.release)

    reader.feed_eof()
    await asyncio.wait_for(task, 10 * _TIMEOUT)

    # Closed by the time the server returns, not by a close it left running.
    assert drafts._db is None
    for thread in workers:
        await asyncio.to_thread(thread.join, 5)
    assert [thread.is_alive() for thread in workers] == [False, False]
    assert session.shutdown_error is not None
