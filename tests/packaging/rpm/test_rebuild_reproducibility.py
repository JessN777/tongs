"""Payload compression and whole-file reproducibility of the RPM rebuilds.

The rebuild scripts run here against recording stand-ins for ``rpm``,
``rpmbuild`` and ``podman``, so the tests pin the exact ``rpmbuild`` defines
each published or previous build receives without a Fedora toolchain.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
DESKTOP = ROOT / "packaging" / "rpm" / "desktop"
COMPANIONS = ROOT / "packaging" / "rpm" / "python-dependencies"

#: The newest changelog date of every Tongs and companion spec, as rpm stores
#: it (noon UTC on Tue Sep 08 2026).
CHANGELOG_TIME = "1788868800"

#: Defines every rebuild passes so two rebuilds of one SRPM are byte-identical.
REPRODUCIBLE_DEFINES = {
    "source_date_epoch_from_changelog": "1",
    "clamp_mtime_to_source_date_epoch": "1",
    "use_source_date_epoch_as_buildtime": "1",
    "_buildhost": "tongs-rpm-builder",
}

FAKE_TOOL = """\
import itertools
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    record = {
        "tool": name,
        "argv": arguments,
        "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH"),
    }
    log.write(json.dumps(record) + "\\n")
if name == "rpm":
    if "%{CHANGELOGTIME}" in arguments:
        sys.stdout.write(os.environ["FAKE_CHANGELOG_TIME"])
    sys.exit(0)
if name == "rpmbuild":
    for flag, value in zip(arguments, arguments[1:]):
        if flag == "--define" and value.startswith("_topdir "):
            rpms = Path(value.removeprefix("_topdir ")) / "RPMS" / "noarch"
            rpms.mkdir(parents=True, exist_ok=True)
            (rpms / "fake-1-1.noarch.rpm").write_bytes(b"rpm")
    sys.exit(0)
if name == "podman":
    if arguments[:1] == ["run"]:
        for flag, value in zip(arguments, arguments[1:]):
            if flag == "--volume" and ":/output:" in value:
                output = Path(value.split(":", 1)[0])
                (output / "fake-1-1.noarch.rpm").write_bytes(b"rpm")
    elif arguments[:2] == ["image", "inspect"]:
        sys.stdout.write("[]\\n")
    sys.exit(0)
sys.exit(3)
"""


def _fake_tools(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    source = f"#!{sys.executable}\n{FAKE_TOOL}"
    for name in ("rpm", "rpmbuild", "podman"):
        tool = bin_dir / name
        tool.write_text(source)
        tool.chmod(0o755)
    temporary = tmp_path / "tmp"
    temporary.mkdir(exist_ok=True)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.jsonl"),
        "FAKE_CHANGELOG_TIME": CHANGELOG_TIME,
        "TMPDIR": str(temporary),
        "RUNNER_TEMP": str(temporary),
        # Any inherited epoch must be cleared so the changelog dates the build.
        "SOURCE_DATE_EPOCH": "1",
    }


def _calls(tmp_path: Path, tool: str) -> list[dict[str, object]]:
    log = tmp_path / "calls.jsonl"
    if not log.exists():
        return []
    records = [json.loads(line) for line in log.read_text().splitlines()]
    return [record for record in records if record["tool"] == tool]


def _defines(argv: list[str]) -> dict[str, str]:
    defines: dict[str, str] = {}
    for flag, value in itertools.pairwise(argv):
        if flag == "--define":
            name, _, body = value.partition(" ")
            assert name not in defines, name
            defines[name] = body
    return defines


def _run(
    script: Path, arguments: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _desktop_rebuild(
    tmp_path: Path, *extra: str, changelog_time: str = CHANGELOG_TIME
) -> subprocess.CompletedProcess[str]:
    env = _fake_tools(tmp_path)
    env["FAKE_CHANGELOG_TIME"] = changelog_time
    source_rpm = tmp_path / "tongs-desktop-1.0.0-1.fc44.src.rpm"
    source_rpm.write_bytes(b"srpm")
    output = tmp_path / "output"
    output.mkdir()
    return _run(
        DESKTOP / "rebuild_one_srpm.sh",
        ["--source-rpm", str(source_rpm), "--output-dir", str(output), *extra],
        env,
    )


def test_desktop_rebuild_defaults_to_multithreaded_level_19(tmp_path: Path) -> None:
    result = _desktop_rebuild(tmp_path)

    assert result.returncode == 0, result.stderr
    (rpmbuild,) = _calls(tmp_path, "rpmbuild")
    argv = rpmbuild["argv"]
    assert isinstance(argv, list)
    defines = _defines(argv)
    assert defines["_binary_payload"] == "w19T0.zstdio"
    assert defines["dist"] == ".fc44"
    for name, value in REPRODUCIBLE_DEFINES.items():
        assert defines[name] == value, name
    assert rpmbuild["source_date_epoch"] is None
    # A fixed build root keeps build paths identical between rebuilds.
    topdir = tmp_path / "tmp" / "tongs-desktop-rebuild"
    assert defines["_topdir"] == str(topdir)
    assert not topdir.exists()
    assert (tmp_path / "output" / "fake-1-1.noarch.rpm").is_file()


def test_desktop_rebuild_refuses_an_existing_build_root(tmp_path: Path) -> None:
    sentinel = tmp_path / "tmp" / "tongs-desktop-rebuild" / "keep"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("kept")

    result = _desktop_rebuild(tmp_path)

    assert result.returncode != 0
    assert _calls(tmp_path, "rpmbuild") == []
    assert sentinel.read_text() == "kept"


def test_desktop_rebuild_accepts_the_previous_payload_mode(tmp_path: Path) -> None:
    result = _desktop_rebuild(
        tmp_path,
        "--binary-payload",
        "w3T0.zstdio",
        "--dist",
        ".fc44~previous",
        "--desktop-version",
        "0.4.9",
    )

    assert result.returncode == 0, result.stderr
    (rpmbuild,) = _calls(tmp_path, "rpmbuild")
    argv = rpmbuild["argv"]
    assert isinstance(argv, list)
    defines = _defines(argv)
    assert defines["_binary_payload"] == "w3T0.zstdio"
    assert defines["tongs_desktop_version"] == "0.4.9"
    for name, value in REPRODUCIBLE_DEFINES.items():
        assert defines[name] == value, name


@pytest.mark.parametrize(
    "mode",
    [
        "w19.zstdio",
        "w19T4.zstdio",
        "w3.zstdio",
        "w19T0.xzdio",
        "w9.gzdio",
        "w.ufdio",
        "w19T0.zstdio extra",
        "",
    ],
)
def test_desktop_rebuild_refuses_any_other_payload_mode(
    tmp_path: Path, mode: str
) -> None:
    result = _desktop_rebuild(tmp_path, "--binary-payload", mode)

    assert result.returncode == 2
    assert "unsupported binary payload mode" in result.stderr
    assert _calls(tmp_path, "rpmbuild") == []


@pytest.mark.parametrize("changelog_time", ["(none)", "0", ""])
def test_desktop_rebuild_refuses_a_source_rpm_without_changelog(
    tmp_path: Path, changelog_time: str
) -> None:
    result = _desktop_rebuild(tmp_path, changelog_time=changelog_time)

    assert result.returncode == 1
    assert "no %changelog entry" in result.stderr
    assert _calls(tmp_path, "rpmbuild") == []


def test_companion_rebuild_publishes_level_19_reproducibly(tmp_path: Path) -> None:
    env = _fake_tools(tmp_path)
    source_rpm = tmp_path / "python-rfc8785-0.1.4-1.fc44.src.rpm"
    source_rpm.write_bytes(b"srpm")
    output = tmp_path / "output"
    output.mkdir()

    result = _run(
        COMPANIONS / "rebuild_one_srpm.sh",
        [
            "--distribution",
            "rfc8785",
            "--source-rpm",
            str(source_rpm),
            "--output-dir",
            str(output),
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    (rpmbuild,) = _calls(tmp_path, "rpmbuild")
    argv = rpmbuild["argv"]
    assert isinstance(argv, list)
    defines = _defines(argv)
    assert defines["_binary_payload"] == "w19T0.zstdio"
    for name, value in REPRODUCIBLE_DEFINES.items():
        assert defines[name] == value, name
    assert rpmbuild["source_date_epoch"] is None
    # Compiled companions embed their build paths, so the build root is fixed.
    topdir = tmp_path / "tmp" / "tongs-srpm-rebuild"
    assert defines["_topdir"] == str(topdir)
    assert not topdir.exists()
    assert (output / "fake-1-1.noarch.rpm").is_file()


def test_companion_rebuild_refuses_a_source_rpm_without_changelog(
    tmp_path: Path,
) -> None:
    env = _fake_tools(tmp_path)
    env["FAKE_CHANGELOG_TIME"] = "(none)"
    source_rpm = tmp_path / "python-rfc8785-0.1.4-1.fc44.src.rpm"
    source_rpm.write_bytes(b"srpm")
    output = tmp_path / "output"
    output.mkdir()

    result = _run(
        COMPANIONS / "rebuild_one_srpm.sh",
        [
            "--distribution",
            "rfc8785",
            "--source-rpm",
            str(source_rpm),
            "--output-dir",
            str(output),
        ],
        env,
    )

    assert result.returncode == 1
    assert "no %changelog entry" in result.stderr
    assert _calls(tmp_path, "rpmbuild") == []


def test_desktop_harness_passes_level_19_to_final_and_level_3_to_previous(
    tmp_path: Path,
) -> None:
    env = _fake_tools(tmp_path)
    srpms = tmp_path / "srpms"
    srpms.mkdir()
    for name in ("python-tongs-1.1.0-1.src.rpm", "tongs-desktop-1.1.0-1.src.rpm"):
        (srpms / name).write_bytes(b"srpm")
    companions = tmp_path / "companions"
    companions.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    output = tmp_path / "rpms"
    output.mkdir()

    result = _run(
        DESKTOP / "rebuild_srpms.sh",
        [
            "--base-image",
            "localhost/builder:test",
            "--checkout",
            str(checkout),
            "--source-sha",
            "0" * 40,
            "--srpm-dir",
            str(srpms),
            "--companion-rpm-dir",
            str(companions),
            "--output-dir",
            str(output),
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    rebuilds: dict[tuple[str, str], list[str]] = {}
    for call in _calls(tmp_path, "podman"):
        argv = call["argv"]
        assert isinstance(argv, list)
        if argv[:1] != ["run"]:
            continue
        script = "/checkout/packaging/rpm/desktop/rebuild_one_srpm.sh"
        arguments = argv[argv.index(script) + 1 :]
        image = argv[argv.index(script) - 1]
        source_name, variant = (
            image.removeprefix("localhost/tongs-").split(":")[0].rsplit("-", 1)
        )
        rebuilds[(source_name, variant)] = arguments

    assert set(rebuilds) == {
        (source, variant)
        for source in ("python-tongs", "tongs-desktop")
        for variant in ("previous", "final")
    }
    for (source_name, variant), arguments in rebuilds.items():
        options = dict(zip(arguments[::2], arguments[1::2]))
        expected_payload = "w3T0.zstdio" if variant == "previous" else "w19T0.zstdio"
        assert options["--binary-payload"] == expected_payload, (source_name, variant)
        expected_dist = ".fc44~previous" if variant == "previous" else ".fc44"
        assert options["--dist"] == expected_dist
        if source_name == "tongs-desktop" and variant == "previous":
            assert options["--desktop-version"] == "0.4.9"
        else:
            assert "--desktop-version" not in options


@pytest.mark.parametrize(
    "spec",
    [
        *sorted((DESKTOP / "templates").glob("*.spec.in")),
        *sorted((COMPANIONS / "specs").glob("*.spec")),
    ],
    ids=lambda path: path.name,
)
def test_every_spec_dates_its_build_from_a_changelog_entry(spec: Path) -> None:
    """The rebuilds refuse a source RPM without a changelog entry."""

    text = spec.read_text()
    _, separator, changelog = text.partition("\n%changelog\n")
    assert separator, spec.name
    assert changelog.startswith("* "), spec.name
