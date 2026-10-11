"""The Fedora provider audit tolerates only the companion-built Sigstore."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "desktop"
MANIFEST = json.loads((PACKAGING / "manifest.json").read_text())
SIGSTORE = next(
    requirement
    for requirement in MANIFEST["core_runtime_requirements"]
    if "sigstore" in requirement
)
PROVIDER = "python3-provider|0|1.0|1.fc44|noarch|fedora"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tongs_desktop_audit_providers", PACKAGING / "audit_providers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_providers = _load_script()


def _run_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unprovided: set[str],
    direct_mcp_cli: str = "",
) -> tuple[int | RuntimeError, dict[str, Any]]:
    """Run the audit CLI against faked ``dnf`` answers.

    ``unprovided`` names the provider queries that return no package.
    ``direct_mcp_cli`` is what ``python3-mcp+cli`` declares it provides.
    """

    def query(argument: str, *, whatprovides: bool) -> dict[str, Any]:
        packages = [] if argument in unprovided else [PROVIDER]
        return {"returncode": 0, "stdout": "", "stderr": "", "packages": packages}

    def package_provides(name: str) -> dict[str, Any]:
        stdout = direct_mcp_cli if name == "python3-mcp+cli" else ""
        return {"returncode": 0, "stdout": stdout, "stderr": ""}

    os_release = tmp_path / "os-release"
    os_release.write_text("ID=fedora\nVERSION_ID=44\n")

    def path(value: str) -> Path:
        return os_release if value == "/etc/os-release" else Path(value)

    monkeypatch.setattr(audit_providers, "_query", query)
    monkeypatch.setattr(audit_providers, "_package_provides", package_provides)
    monkeypatch.setattr(
        audit_providers, "_run", lambda command: {"command": command, "stdout": ""}
    )
    monkeypatch.setattr(audit_providers, "Path", path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(MANIFEST))
    output = tmp_path / "audit.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_providers.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output),
        ],
    )
    try:
        outcome: int | RuntimeError = audit_providers.main()
    except RuntimeError as error:
        outcome = error
    return outcome, json.loads(output.read_text())


def test_audit_tolerates_a_missing_sigstore_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, report = _run_audit(tmp_path, monkeypatch, unprovided={SIGSTORE})

    assert outcome == 0
    assert report["missing_requirements"] == []


def test_audit_fails_when_the_mcp_requirement_has_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, report = _run_audit(
        tmp_path,
        monkeypatch,
        unprovided={SIGSTORE, "python3dist(mcp[cli])", "python3-mcp+cli"},
    )

    assert isinstance(outcome, RuntimeError)
    assert str(outcome) == f"missing Fedora providers: {MANIFEST['mcp_requirement']}"
    assert report["missing_requirements"] == [MANIFEST["mcp_requirement"]]
    assert report["mcp_resolution"]["usable"] is False


def test_audit_accepts_mcp_through_the_direct_cli_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, report = _run_audit(
        tmp_path,
        monkeypatch,
        unprovided={"python3dist(mcp[cli])"},
        direct_mcp_cli="python3dist(mcp[cli]) = 1.27.0\n",
    )

    assert outcome == 0
    assert report["missing_requirements"] == []
    assert report["mcp_resolution"]["mode"] == "direct-package-provides"


@pytest.mark.parametrize(
    "requirement",
    [
        "python3dist(httpx) >= 0.27",
        "xorg-x11-server-Xwayland",
        "python3dist(hatch-vcs)",
    ],
)
def test_audit_fails_for_any_other_missing_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str
) -> None:
    outcome, report = _run_audit(
        tmp_path, monkeypatch, unprovided={SIGSTORE, requirement}
    )

    assert isinstance(outcome, RuntimeError)
    assert report["missing_requirements"] == [requirement]
