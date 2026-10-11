"""Tests for how the terminal app closes its session at exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tongs.app import TongsApp
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.forges.base import ForgeClient
from tongs.forges.models import ForgeHost
from tongs.plugins.registry import PluginRegistry
from tongs.services.errors import ServiceErrorCode
from tongs.services.session import ApplicationSession
from tongs.state.drafts import DraftStore


class NoHostRegistry:
    """Forge registry with no hosts, so nothing reaches the network."""

    def __init__(self) -> None:
        self.close_calls = 0

    def active_hostnames(self) -> list[str]:
        return []

    def get_host(self, hostname: str) -> ForgeHost | None:
        return None

    async def get_client(self, hostname: str) -> ForgeClient:
        raise AssertionError("no forge client is expected")

    async def close_all(self) -> None:
        self.close_calls += 1


class NoPlugins(PluginRegistry):
    def discover(self, plugin_config: dict[str, dict] | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_session_close_still_closes_stores_at_exit(
    tmp_path: Path,
) -> None:
    config = Config(scan_root=str(tmp_path))
    cache = CacheStore(tmp_path / "cache.db")
    drafts = DraftStore(tmp_path / "drafts.db")
    registry = NoHostRegistry()
    session = ApplicationSession(
        config=config,
        cache=cache,
        draft_store=drafts,
        forge_registry=registry,
        discoverer=lambda *args, **kwargs: [],
        shutdown_timeout=0.5,
    )
    app = TongsApp(config=config, session=session, plugin_registry=NoPlugins())

    async with app.run_test():
        submission_close = AsyncMock(side_effect=RuntimeError("owner still active"))
        session.review_submissions.close = submission_close

    assert app.shutdown_error is not None
    assert app.shutdown_error.code is ServiceErrorCode.SHUTDOWN_FAILED
    assert submission_close.await_count == 2
    assert cache._db is None
    assert drafts._db is None
    assert registry.close_calls == 1


# Runs the real app headless in its own interpreter: a TUI crash plus a
# failing review submission cleanup used to leave aiosqlite worker threads
# running, so the interpreter never exited.
CRASH_SCRIPT = """
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from tongs.app import TongsApp
from tongs.config import Config
from tongs.plugins.registry import PluginRegistry
from tongs.services.session import ApplicationSession

root = Path(sys.argv[1])


class NoHostRegistry:
    def active_hostnames(self) -> list[str]:
        return []

    def get_host(self, hostname: str) -> None:
        return None

    async def get_client(self, hostname: str) -> object:
        raise AssertionError("no forge client is expected")

    async def close_all(self) -> None:
        return None


class NoPlugins(PluginRegistry):
    def discover(self, plugin_config: object = None) -> None:
        return None


class CrashingApp(TongsApp):
    async def on_mount(self) -> None:
        await super().on_mount()

        async def failing_close() -> None:
            raise RuntimeError("owner still active")

        self.session.review_submissions.close = failing_close
        self.set_timer(0.2, self.crash)

    def crash(self) -> None:
        raise RuntimeError("simulated crash")


config = Config(scan_root=str(root))
session = ApplicationSession(
    config=config,
    cache_path=root / "cache.db",
    draft_db_path=root / "drafts.db",
    forge_registry=NoHostRegistry(),
    discoverer=lambda *args, **kwargs: [],
    shutdown_timeout=0.5,
)
app = CrashingApp(config=config, session=session, plugin_registry=NoPlugins())
app.run(headless=True)
workers = [
    thread
    for thread in threading.enumerate()
    if "_connection_worker_thread" in thread.name
]
for thread in workers:
    thread.join(5)
print(
    json.dumps(
        {
            "return_code": app.return_code,
            "shutdown_error": app.shutdown_error and app.shutdown_error.code.value,
            "live_sqlite_threads": sum(thread.is_alive() for thread in workers),
        }
    )
)
"""


def test_process_exits_after_tui_crash_with_failed_cleanup(tmp_path: Path) -> None:
    script = tmp_path / "crash.py"
    script.write_text(CRASH_SCRIPT, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
    }

    # A hang ends in TimeoutExpired, which fails the test.
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {
        "return_code": 1,
        "shutdown_error": "shutdown_failed",
        "live_sqlite_threads": 0,
    }
    assert "simulated crash" in result.stderr
