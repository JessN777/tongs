"""Explicit desktop lifecycle CLI orchestration tests."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import tongs.__main__ as main_module
import tongs.desktop.installer.activation as activation_module
from tongs.desktop.installer.activation import (
    BoundPythonEnvironment,
    DesktopInstallationPaths,
    DesktopInstallationStore,
    EnvironmentKind,
)
from tongs.desktop.installer.commands import (
    DesktopCommandContext,
    default_desktop_paths,
    run_desktop_cli,
)
from tongs.desktop.installer.extract import extract_verified_archive
from tongs.desktop.installer.models import (
    PERSISTENT_INSTALL_GUIDANCE,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
)

from .helpers import documents, verified_metadata

_ORPHAN = f".tongs-stage-{'a' * 32}"
_LINKED = f".tongs-stage-{'b' * 32}"
_FOREIGN = f".tongs-stage-{'c' * 32}"
_SHARED = f".tongs-stage-{'d' * 32}"
_UNRELATED = "keep-me"


class _Stage:
    def __init__(self, store: DesktopInstallationStore) -> None:
        self.store = store
        self.requests: list[InstallRequest] = []

    async def __call__(self, _transaction, request: InstallRequest):
        self.requests.append(request)
        return extract_verified_archive(
            documents()[1], verified_metadata(), self.store.paths.staging_root
        )


def _context(
    tmp_path: Path, *, kind: EnvironmentKind = EnvironmentKind.VENV
) -> tuple[DesktopCommandContext, _Stage, Mock]:
    store = DesktopInstallationStore(DesktopInstallationPaths.under(tmp_path / "data"))
    interpreter = tmp_path / "venv/bin/python"
    console = tmp_path / "venv/bin/tongs"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    console.write_text(f"#!{interpreter}\n")
    interpreter.chmod(0o755)
    console.chmod(0o755)
    environment = BoundPythonEnvironment(
        kind,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1.2.3",
    )
    stage = _Stage(store)
    launch = Mock()
    return DesktopCommandContext(store, environment, stage, launch), stage, launch


def test_install_status_launch_and_uninstall_are_explicit(tmp_path: Path) -> None:
    context, stage, launch = _context(tmp_path)
    output = io.StringIO()

    assert (
        run_desktop_cli(
            ["install", "--version", "1.2.3"],
            console_argv0="tongs",
            context=context,
            stdout=output,
        )
        == 0
    )
    assert stage.requests == [InstallRequest("1.2.3")]
    assert "Activated" in output.getvalue()
    assert (
        run_desktop_cli([], console_argv0="tongs", context=context, stdout=output) == 0
    )
    launch.assert_called_once()
    assert (
        run_desktop_cli(
            ["uninstall"], console_argv0="tongs", context=context, stdout=output
        )
        == 0
    )
    assert not context.store.paths.menu_path.exists()


def test_transient_uvx_install_is_rejected_with_persistent_guidance(
    tmp_path: Path,
) -> None:
    context, stage, _launch = _context(tmp_path, kind=EnvironmentKind.TRANSIENT_UVX)
    errors = io.StringIO()

    result = run_desktop_cli(
        ["install"], console_argv0="uvx", context=context, stderr=errors
    )

    assert result == 2
    assert PERSISTENT_INSTALL_GUIDANCE in errors.getvalue()
    assert stage.requests == []
    assert tuple(context.store.paths.staging_root.iterdir()) == ()


def test_install_translates_payload_rename_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, stage, _launch = _context(tmp_path)
    errors = io.StringIO()
    monkeypatch.setattr(
        activation_module.os,
        "rename",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected rename")),
    )

    result = run_desktop_cli(
        ["install"], console_argv0="tongs", context=context, stderr=errors
    )

    assert result == 2
    assert "could not be activated safely" in errors.getvalue()
    assert stage.requests == [InstallRequest()]
    with context.store.transaction() as transaction:
        assert transaction.read_state() is None
        assert transaction.read_journal() is None
    assert tuple(context.store.paths.staging_root.iterdir()) == ()


def test_update_from_different_environment_is_rejected_before_staging(
    tmp_path: Path,
) -> None:
    context, initial_stage, _launch = _context(tmp_path)
    assert run_desktop_cli(["install"], console_argv0="tongs", context=context) == 0
    other_context, other_stage, _other_launch = _context(tmp_path / "other")
    other_context = DesktopCommandContext(
        context.store,
        other_context.environment,
        other_stage,
        other_context.launch,
    )
    errors = io.StringIO()

    result = run_desktop_cli(
        ["update"], console_argv0="tongs", context=other_context, stderr=errors
    )

    assert result == 2
    assert "another Python environment" in errors.getvalue()
    assert initial_stage.requests == [InstallRequest()]
    assert other_stage.requests == []


def test_repair_download_fallback_is_opt_in_and_visible(tmp_path: Path) -> None:
    context, stage, _launch = _context(tmp_path)
    errors = io.StringIO()
    output = io.StringIO()

    without_download = run_desktop_cli(
        ["repair"],
        console_argv0="tongs",
        context=context,
        stdout=output,
        stderr=errors,
    )
    with_download = run_desktop_cli(
        ["repair", "--redownload"],
        console_argv0="tongs",
        context=context,
        stdout=output,
        stderr=errors,
    )

    assert without_download == 2
    assert with_download == 0
    assert stage.requests == [InstallRequest()]
    assert "downloading a verified replacement" in output.getvalue()


def test_redownload_replaces_corrupted_same_version_at_a_new_target(
    tmp_path: Path,
) -> None:
    context, stage, _launch = _context(tmp_path)
    assert run_desktop_cli(["install"], console_argv0="tongs", context=context) == 0
    with context.store.transaction() as transaction:
        installed = transaction.read_state()
    assert installed is not None and installed.active is not None
    old_target = installed.active.payload.target_path
    installed.active.payload.launcher_path.write_text("corrupted")
    errors = io.StringIO()

    assert (
        run_desktop_cli(
            ["repair"], console_argv0="tongs", context=context, stderr=errors
        )
        == 2
    )
    assert (
        run_desktop_cli(
            ["repair", "--redownload"], console_argv0="tongs", context=context
        )
        == 0
    )

    with context.store.transaction() as transaction:
        repaired = transaction.read_state()
    assert repaired is not None and repaired.active is not None
    assert repaired.active.payload.target_path != old_target
    assert repaired.active.payload.launcher_path.read_text() != "corrupted"
    assert stage.requests == [InstallRequest(), InstallRequest()]


def test_redownload_discards_an_unrecoverable_interrupted_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, stage, _launch = _context(tmp_path)
    assert run_desktop_cli(["install"], console_argv0="tongs", context=context) == 0
    with context.store.transaction() as transaction:
        installed = transaction.read_state()
    assert installed is not None and installed.active is not None

    def fail_menu(*_args: object, **_kwargs: object) -> str:
        raise InstallerError(InstallerErrorCode.STATE_CONFLICT, "injected menu failure")

    monkeypatch.setattr(activation_module, "install_user_menu", fail_menu)
    assert (
        run_desktop_cli(
            ["update"], console_argv0="tongs", context=context, stderr=io.StringIO()
        )
        == 2
    )
    monkeypatch.undo()
    with context.store.transaction() as transaction:
        journal = transaction.read_journal()
        assert transaction.read_state() == installed
    assert journal is not None
    candidate = journal.target.payload
    assert candidate.target_path.is_dir()
    candidate.launcher_path.write_text("corrupted")
    errors = io.StringIO()
    assert (
        run_desktop_cli(
            ["repair"], console_argv0="tongs", context=context, stderr=errors
        )
        == 2
    )
    # The plain repair fails because local recovery rejects the corrupted
    # candidate, not for an unrelated reason.
    assert "The installed desktop payload is incomplete or changed." in (
        errors.getvalue()
    )
    assert context.store.paths.journal_path.exists()

    output = io.StringIO()
    assert (
        run_desktop_cli(
            ["repair", "--redownload"],
            console_argv0="tongs",
            context=context,
            stdout=output,
            stderr=io.StringIO(),
        )
        == 0
    )

    assert "downloading a verified replacement" in output.getvalue()
    assert not context.store.paths.journal_path.exists()
    assert not candidate.target_path.exists()
    with context.store.transaction() as transaction:
        assert transaction.read_journal() is None
        repaired = transaction.read_state()
    assert repaired is not None and repaired.active is not None
    assert repaired.active.payload.target_path not in {
        candidate.target_path,
        installed.active.payload.target_path,
    }
    assert repaired.active.payload.launcher_path.read_text() != "corrupted"
    assert stage.requests == [InstallRequest(), InstallRequest(), InstallRequest()]


def test_relative_xdg_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("/home/tester")))
    paths = default_desktop_paths({"XDG_DATA_HOME": "relative/data"})

    assert paths.root == Path("/home/tester/.local/share/tongs/desktop")


def test_main_dispatches_alias_and_command_tree_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "tongs.desktop.installer.commands.run_desktop_cli",
        lambda argv, **_kwargs: calls.append(list(argv)) or 0,
    )

    assert main_module.main(["--install-desktop"]) == 0
    assert main_module.main(["desktop", "status"]) == 0
    assert calls == [["install"], ["status"]]


def test_importing_terminal_entrypoint_does_not_initialize_installer() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            (
                "import sys, tongs.__main__; "
                "assert 'sigstore' not in sys.modules; "
                "assert 'tongs.desktop.installer.commands' not in sys.modules"
            ),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode()


def test_default_context_failure_is_actionable_without_starting_install(
    tmp_path: Path,
) -> None:
    errors = io.StringIO()

    result = run_desktop_cli(
        ["install"],
        console_argv0=str(tmp_path / "missing-tongs"),
        stderr=errors,
    )

    assert result == 2
    assert "missing" in errors.getvalue().lower()
    assert not (tmp_path / "data").exists()


def _leave_staging(
    context: DesktopCommandContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Leave an orphan beside entries the sweep must not touch."""
    with context.store.transaction():
        pass
    root = context.store.paths.staging_root
    orphan = root / _ORPHAN
    (orphan / "runtime").mkdir(parents=True, mode=0o700)
    orphan.chmod(0o700)
    (orphan / "runtime" / "payload").write_text("leftover")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("outside")
    (root / _LINKED).symlink_to(outside, target_is_directory=True)
    foreign = root / _FOREIGN
    foreign.mkdir(mode=0o700)
    foreign.chmod(0o700)
    (foreign / "marker").write_text("foreign")
    shared = root / _SHARED
    shared.mkdir()
    shared.chmod(0o755)
    (root / _UNRELATED).mkdir(mode=0o700)
    foreign_inode = foreign.stat().st_ino
    real_fstat = os.fstat

    def fstat(descriptor: int) -> os.stat_result:
        details = real_fstat(descriptor)
        if details.st_ino != foreign_inode:
            return details
        values = list(details[:10])
        values[4] = details.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", fstat)
    return outside


def _staging_names(context: DesktopCommandContext) -> list[str]:
    return sorted(path.name for path in context.store.paths.staging_root.iterdir())


@pytest.mark.parametrize(
    ("argv", "installed_first"),
    [
        (["install"], False),
        (["update"], True),
        (["repair"], True),
        (["repair"], False),
        (["uninstall"], True),
        (["uninstall"], False),
    ],
)
def test_lifecycle_commands_sweep_only_owned_orphaned_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    installed_first: bool,
) -> None:
    context, _stage, _launch = _context(tmp_path)
    if installed_first:
        assert run_desktop_cli(["install"], console_argv0="tongs", context=context) == 0
    outside = _leave_staging(context, tmp_path, monkeypatch)

    run_desktop_cli(
        argv,
        console_argv0="tongs",
        context=context,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert _staging_names(context) == sorted([_LINKED, _FOREIGN, _SHARED, _UNRELATED])
    assert (outside / "marker").read_text() == "outside"
    root = context.store.paths.staging_root
    assert (root / _LINKED).is_symlink()
    assert (root / _FOREIGN / "marker").read_text() == "foreign"


def test_failed_repair_redownload_also_sweeps_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, stage, _launch = _context(tmp_path)
    _leave_staging(context, tmp_path, monkeypatch)

    assert (
        run_desktop_cli(
            ["repair", "--redownload"],
            console_argv0="tongs",
            context=context,
            stdout=io.StringIO(),
        )
        == 0
    )

    assert stage.requests == [InstallRequest()]
    assert _ORPHAN not in _staging_names(context)


@pytest.mark.parametrize("argv", [["status"], ["status", "--json"], []])
def test_status_and_launch_never_sweep_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    context, _stage, _launch = _context(tmp_path)
    assert run_desktop_cli(["install"], console_argv0="tongs", context=context) == 0
    _leave_staging(context, tmp_path, monkeypatch)

    assert run_desktop_cli(
        argv, console_argv0="tongs", context=context, stdout=io.StringIO()
    ) in {0, 1}

    assert _ORPHAN in _staging_names(context)
