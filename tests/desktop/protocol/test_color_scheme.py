"""Tests that the desktop handshake carries the configured colour scheme (#226)."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import ApplicationSession


class _QueueWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _ConfigSession:
    """A session whose config is any object, or that has no config at all."""

    recovery_warnings: tuple[object, ...] = ()

    def __init__(self, config: object | None) -> None:
        if config is not None:
            self.config = config

    async def start(self) -> _ConfigSession:
        return self

    async def close(self, *, final: bool = False) -> None:
        return None


def _registry() -> DesktopPluginRegistry:
    return DesktopPluginRegistry(
        entry_point_source=lambda _group: (), host_version="1.0"
    )


async def _handshake(session: object) -> JsonObject:
    server = DesktopSidecarServer(
        session=session, plugin_registry=_registry(), shutdown_timeout=0.2
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))
    params = {
        "protocol_major": 1,
        "core_version": version("tongs"),
        "capabilities": [],
    }
    document = {
        "v": 1,
        "type": "request",
        "id": "handshake",
        "method": "handshake",
        "params": params,
    }
    reader.feed_data(json.dumps(document).encode() + b"\n")
    response = await asyncio.wait_for(writer.frames.get(), 2)
    reader.feed_eof()
    await asyncio.wait_for(task, 2)
    assert "error" not in response
    return cast(JsonObject, response["result"])


def _session(tmp_path: Path, body: str | None) -> ApplicationSession:
    config_file = tmp_path / "config.toml"
    if body is not None:
        config_file.write_text(body, encoding="utf-8")
    return ApplicationSession(
        config_path=config_file,
        cache_path=tmp_path / "cache.db",
        draft_db_path=tmp_path / "drafts.db",
    )


@pytest.mark.parametrize("scheme", ["system", "light", "dark"])
@pytest.mark.asyncio
async def test_handshake_reports_the_configured_scheme(
    tmp_path: Path, scheme: str
) -> None:
    session = _session(tmp_path, f'[ui]\ncolor_scheme = "{scheme}"\n')
    result = await _handshake(session)
    assert result["color_scheme"] == scheme


@pytest.mark.asyncio
async def test_handshake_follows_the_system_without_a_config_file(
    tmp_path: Path,
) -> None:
    result = await _handshake(_session(tmp_path, None))
    assert result["color_scheme"] == "system"


@pytest.mark.asyncio
async def test_handshake_reports_system_for_an_unknown_configured_name(
    tmp_path: Path,
) -> None:
    result = await _handshake(_session(tmp_path, '[ui]\ncolor_scheme = "Dark"\n'))
    assert result["color_scheme"] == "system"


@pytest.mark.parametrize(
    "config",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(color_scheme="neon"),
        SimpleNamespace(color_scheme=3),
    ],
)
@pytest.mark.asyncio
async def test_handshake_reports_system_when_the_session_has_no_valid_scheme(
    config: object | None,
) -> None:
    result = await _handshake(_ConfigSession(config))
    assert result["color_scheme"] == "system"
