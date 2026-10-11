"""Tests for the terminal entry point's handling of config.toml."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

import tongs.app
import tongs.config
from tongs.__main__ import main
from tongs.config import Config
from tongs.services.errors import ServiceError, ServiceErrorCode


class _FakeApp:
    """Records the config the entry point starts the app with."""

    started: ClassVar[list[Config]] = []
    shutdown_error: ServiceError | None = None

    def __init__(self, config: Config) -> None:
        self._config = config

    def run(self) -> None:
        _FakeApp.started.append(self._config)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tongs.config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(tongs.app, "TongsApp", _FakeApp)
    _FakeApp.started = []
    return tmp_path


def test_unquoted_value_prints_one_line_and_exits_2(
    config_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = config_home / "config.toml"
    config_file.write_text("[general]\nscan_root = ~/git\n", encoding="utf-8")

    assert main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("tongs: ")
    assert str(config_file) in lines[0]
    assert "line 2, column 13" in lines[0]
    assert "Traceback" not in captured.err
    assert _FakeApp.started == []


@pytest.mark.parametrize(
    ("body", "named"),
    [
        ('[cache]\nmr_list_ttl = "60"\n', "[cache] mr_list_ttl"),
        ('hosts = ["x"]\n', "[hosts]"),
    ],
)
def test_type_errors_exit_2_naming_the_key(
    config_home: Path, capsys: pytest.CaptureFixture[str], body: str, named: str
) -> None:
    (config_home / "config.toml").write_text(body, encoding="utf-8")

    assert main([]) == 2

    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert named in err
    assert _FakeApp.started == []


def test_valid_config_starts_the_app_with_the_scan_root_override(
    config_home: Path,
) -> None:
    (config_home / "config.toml").write_text(
        '[general]\nscan_root = "/work"\n', encoding="utf-8"
    )

    assert main(["--scan-root", "/other"]) == 0

    assert [config.scan_root for config in _FakeApp.started] == ["/other"]


def test_shutdown_failure_prints_one_line_and_exits_1(
    config_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ServiceError(
        ServiceErrorCode.SHUTDOWN_FAILED,
        "One or more application resources did not close cleanly.",
    )
    monkeypatch.setattr(_FakeApp, "shutdown_error", error)

    assert main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"tongs: {error.message}\n"
    assert len(_FakeApp.started) == 1
