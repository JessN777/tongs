#!/usr/bin/env python3
"""Fail on NUL and other disallowed control characters in tracked text files.

A renderer source was once committed with raw NUL bytes, so Git and GitHub
treated it as binary and nothing caught it.  This check reads every file Git
tracks, or only the files named on the command line (as pre-commit passes
them), and reports every C0 control character except tab and line feed, every
DEL, and every carriage return that is not part of a CRLF line ending.

Binary files are recognised by their suffix, never by their content: content
sniffing would call exactly the file this check exists to catch binary.  A new
binary format therefore needs its suffix added to :data:`BINARY_SUFFIXES`.
Symbolic links are skipped, because Git stores the link text, not the target.

The lint and docs CI jobs run it on the runner's Python, so it is standard
library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Suffixes of the binary formats the repository tracks, or may reasonably
#: track.  Every other tracked file is checked as text.
BINARY_SUFFIXES = frozenset(
    {
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp4",
        ".otf",
        ".pdf",
        ".png",
        ".ttf",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".zip",
    }
)
#: Findings printed per file before the rest are only counted.
MAX_REPORTED_PER_FILE = 20

# Tab (0x09) and line feed (0x0a) are allowed anywhere; carriage return (0x0d)
# only directly before a line feed.
_DISALLOWED = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\r(?!\n)")


@dataclass(frozen=True, slots=True)
class Finding:
    """One disallowed byte: its 1-based line and byte column, and its value."""

    line: int
    column: int
    byte: int

    def describe(self) -> str:
        return f"{self.line}:{self.column}: control character U+{self.byte:04X}"


def find_control_characters(data: bytes) -> list[Finding]:
    """Return every disallowed control character in ``data``, in order."""

    findings = []
    line = 1
    line_start = 0
    scanned = 0
    for match in _DISALLOWED.finditer(data):
        offset = match.start()
        newlines = data.count(b"\n", scanned, offset)
        if newlines:
            line += newlines
            line_start = data.rindex(b"\n", scanned, offset) + 1
        scanned = offset
        findings.append(Finding(line, offset - line_start + 1, data[offset]))
    return findings


def is_checked(path: Path) -> bool:
    """Return whether ``path`` is read as text: an existing regular file whose
    suffix names no binary format."""

    return (
        not path.is_symlink()
        and path.is_file()
        and path.suffix.lower() not in BINARY_SUFFIXES
    )


def tracked_files(root: Path) -> list[Path]:
    """Return every path Git tracks under ``root``."""

    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [root / name.decode("utf-8") for name in listing.split(b"\0") if name]


def check_paths(paths: Sequence[Path]) -> Iterator[tuple[Path, list[Finding]]]:
    """Yield each path that holds a disallowed control character.

    Only paths :func:`is_checked` accepts are read.
    """

    for path in paths:
        if not is_checked(path):
            continue
        findings = find_control_characters(path.read_bytes())
        if findings:
            yield path, findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail on NUL and other control characters, except tab, line feed "
            "and CRLF line endings, in text files."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files to check (default: every file Git tracks in this repository)",
    )
    arguments = parser.parse_args(argv)
    paths = [
        path for path in arguments.paths or tracked_files(ROOT) if is_checked(path)
    ]
    failed = 0
    for path, findings in check_paths(paths):
        failed += 1
        for finding in findings[:MAX_REPORTED_PER_FILE]:
            print(f"{path}:{finding.describe()}")
        hidden = len(findings) - MAX_REPORTED_PER_FILE
        if hidden > 0:
            print(f"{path}: ... and {hidden} more")
    if failed:
        print(
            f"{failed} file(s) hold disallowed control characters; only tab, "
            "line feed and CRLF line endings are allowed in text files",
            file=sys.stderr,
        )
        return 1
    print(f"No disallowed control characters in {len(paths)} text file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
