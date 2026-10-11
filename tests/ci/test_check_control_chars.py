"""Hold ``scripts/check_control_chars.py`` to its contract.

The lint and docs CI jobs and the pre-commit hook run the checker so that a
text file holding NUL or another stray control character fails before merge,
instead of turning into a binary blob in Git.  These cases cover what it
allows and refuses, how it reports, which files it reads, and that the
committed tree passes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.ci.verify_desktop_production_gate import ROOT

PROGRAM = ROOT / "scripts/check_control_chars.py"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_control_chars", PROGRAM
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    # dataclasses resolves annotations through the module's sys.modules entry.
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


check_control_chars = _load()


def _positions(data: bytes) -> list[tuple[int, int, int]]:
    return [
        (finding.line, finding.column, finding.byte)
        for finding in check_control_chars.find_control_characters(data)
    ]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"plain text\n",
        b"\tindented with a tab\n",
        b"windows line\r\nendings\r\n",
        "non-ASCII text: café → ü\n".encode(),
        # C1 controls and other non-ASCII bytes are not C0 controls.
        "\u0085\u009f\n".encode(),
    ],
)
def test_allowed_text_has_no_findings(data: bytes) -> None:
    assert _positions(data) == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"a\x00b\n", [(1, 2, 0x00)]),
        (
            b"\x07\x08\x0b\x0c\x0e\x1b\x1f\x7f",
            [
                (1, n, b)
                for n, b in enumerate(
                    (0x07, 0x08, 0x0B, 0x0C, 0x0E, 0x1B, 0x1F, 0x7F), start=1
                )
            ],
        ),
        # A carriage return is allowed only directly before a line feed.
        (b"old mac\rline\n", [(1, 8, 0x0D)]),
        (b"ends with a carriage return\r", [(1, 28, 0x0D)]),
        (b"\r\r\n", [(1, 1, 0x0D)]),
    ],
)
def test_disallowed_bytes_are_found_with_their_values(
    data: bytes, expected: list[tuple[int, int, int]]
) -> None:
    assert _positions(data) == expected


def test_findings_carry_one_based_line_and_byte_column() -> None:
    data = b"first\nsecond line \x00\n\nfourth\x01 and \x02\r\n"
    assert _positions(data) == [(2, 13, 0x00), (4, 7, 0x01), (4, 13, 0x02)]
    (first, *_) = check_control_chars.find_control_characters(data)
    assert first.describe() == "2:13: control character U+0000"


def test_binary_suffixes_and_symbolic_links_are_not_read(tmp_path: Path) -> None:
    image = tmp_path / "icon.PNG"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    source = tmp_path / "app.tsx"
    source.write_bytes(b"export {};\x00\n")
    link = tmp_path / "link.tsx"
    link.symlink_to(source)
    assert not check_control_chars.is_checked(image)
    assert not check_control_chars.is_checked(link)
    assert not check_control_chars.is_checked(tmp_path / "missing.ts")
    assert not check_control_chars.is_checked(tmp_path)
    assert check_control_chars.is_checked(source)
    found = list(check_control_chars.check_paths([image, link, source]))
    assert [(path.name, len(findings)) for path, findings in found] == [("app.tsx", 1)]


def test_every_binary_file_the_repository_tracks_has_a_binary_suffix() -> None:
    """A tracked file Git itself treats as binary must be skipped by suffix;
    otherwise the check would fail on it, or a new binary format would need
    its suffix added here."""

    eol = subprocess.run(
        ["git", "ls-files", "--eol", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    binary = [
        entry.split("\t", 1)[1]
        for entry in eol.split("\0")
        if entry and entry.split()[0] == "i/-text"
    ]
    assert binary, "the repository tracks images, so the listing is blind"
    unlisted = sorted(
        path
        for path in binary
        if Path(path).suffix.lower() not in check_control_chars.BINARY_SUFFIXES
    )
    assert unlisted == []


def _run(*arguments: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(PROGRAM), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def test_named_files_fail_with_each_location_reported(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("fine\n")
    dirty = tmp_path / "dirty.ts"
    dirty.write_bytes(b"ok\nbad\x00\n" + b"\x01" * 25)
    completed = _run(str(clean), str(dirty))
    assert completed.returncode == 1
    lines = completed.stdout.splitlines()
    assert lines[0] == f"{dirty}:2:4: control character U+0000"
    assert lines[1] == f"{dirty}:3:1: control character U+0001"
    assert len(lines) == check_control_chars.MAX_REPORTED_PER_FILE + 1
    assert lines[-1] == f"{dirty}: ... and 6 more"
    assert str(clean) not in completed.stdout
    assert "1 file(s) hold disallowed control characters" in completed.stderr


def test_clean_named_files_pass(tmp_path: Path) -> None:
    clean = tmp_path / "clean.css"
    clean.write_bytes(b"a {\r\n\tcolor: red;\r\n}\r\n")
    completed = _run(str(clean))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "No disallowed control characters in 1 text file(s).\n"


def test_the_committed_tree_passes() -> None:
    """With no arguments the checker reads every tracked file, from any
    working directory."""

    completed = _run(cwd=ROOT / "desktop")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    checked = int(completed.stdout.split(" in ")[1].split()[0])
    tracked = check_control_chars.tracked_files(ROOT)
    assert ROOT / "desktop/src/renderer/app.tsx" in tracked
    assert checked == sum(check_control_chars.is_checked(path) for path in tracked)
    assert checked > 100
