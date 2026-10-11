"""Discover git repositories by walking the filesystem."""

from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path

from tongs.scanner.remote import parse_remote_url
from tongs.scanner.repo import Remote, Repo

# Bytes read from the first line of a ``.git`` file; only its prefix matters.
_GITDIR_LINE_LIMIT = 4096


def discover_repos(
    root: Path,
    max_depth: int = 5,
    extra_gitlab_hosts: frozenset[str] = frozenset(),
    extra_github_hosts: frozenset[str] = frozenset(),
) -> list[Repo]:
    """Walk root directory and discover git repositories.

    Scans up to max_depth levels deep for a ``.git`` folder, or a ``.git``
    file that points at one, as in a linked worktree. Symlinked directories
    are followed, even outside root, and count as one level. Each real
    directory is walked once, so a link cycle ends and a repository reached by
    two paths is reported once, at its resolved path. Dot-folders are not
    scanned. Parses remotes from each repo to determine forge type.
    Skips nested repos (a repository inside another one's working tree).
    A repository that contains root, reached through a link back up, is not
    reported and does not hide the repositories under root.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []

    ancestors = set(root.parents)
    checkouts = {
        path: worktree
        for path, worktree in _find_checkouts(root, max_depth).items()
        if path not in ancestors
    }
    found = set(checkouts)
    repos: list[Repo] = []

    for repo_path, worktree in checkouts.items():
        if _is_nested_in(repo_path, found):
            continue

        remotes = _read_remotes(repo_path, extra_gitlab_hosts, extra_github_hosts)
        if not remotes:
            continue

        primary = _pick_primary_remote(remotes)
        repos.append(
            Repo(
                path=repo_path,
                remotes=tuple(remotes),
                primary_remote=primary,
                worktree=worktree,
            )
        )

    repos.sort(key=lambda r: r.display_name.lower())
    return repos


def _find_checkouts(root: Path, max_depth: int) -> dict[Path, bool]:
    """Map each checkout under root, by real path, to whether it is a worktree.

    The walk is breadth first, so each real directory is visited once and at
    its shallowest depth, however many links lead to it. A checkout's own
    working tree is not walked.
    """
    checkouts: dict[Path, bool] = {}
    if max_depth < 0:
        return checkouts
    visited: set[Path] = {root}
    queue: deque[tuple[Path, int]] = deque([(root, 0)])

    while queue:
        directory, depth = queue.popleft()
        worktree = _checkout_kind(directory)
        if worktree is not None:
            checkouts[directory] = worktree
            continue
        if depth >= max_depth:
            continue

        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue

        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
                # The walk starts from a resolved root and only ever moves to
                # resolved directories, so only a link needs resolving here.
                real = entry.resolve(strict=True) if entry.is_symlink() else entry
            except (OSError, RuntimeError):
                # A broken or unreadable link, or a link loop.
                continue
            if real in visited:
                continue
            visited.add(real)
            queue.append((real, depth + 1))

    return checkouts


def _checkout_kind(directory: Path) -> bool | None:
    """Return whether directory is a worktree checkout, or None for no checkout.

    A ``.git`` folder marks a main checkout. A ``.git`` file whose first line
    is ``gitdir: <path>`` marks a linked worktree, or a submodule, which the
    nested check drops. Only that first line is read; git itself resolves the
    pointer when the remotes are read.
    """
    marker = directory / ".git"
    try:
        if marker.is_dir():
            return False
        if not marker.is_file():
            return None
        with marker.open("rb") as handle:
            first_line = handle.readline(_GITDIR_LINE_LIMIT)
    except OSError:
        return None
    if first_line.startswith(b"gitdir:") and first_line[7:].strip():
        return True
    return None


def _is_nested_in(path: Path, known_repos: set[Path]) -> bool:
    """Check if path is inside another discovered repo, comparing real paths."""
    for parent in path.parents:
        if parent in known_repos:
            return True
    return False


def _read_remotes(
    repo_path: Path,
    extra_gitlab_hosts: frozenset[str],
    extra_github_hosts: frozenset[str],
) -> list[Remote]:
    """Read git remotes from a repository."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    remotes: dict[str, Remote] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue

        name, url = parts[0], parts[1]
        if name in remotes:
            continue

        remote = parse_remote_url(name, url, extra_gitlab_hosts, extra_github_hosts)
        if remote is not None:
            remotes[name] = remote

    return list(remotes.values())


def _pick_primary_remote(remotes: list[Remote]) -> Remote | None:
    """Pick the primary remote. Prefer 'origin', then 'upstream', then first."""
    by_name = {r.name: r for r in remotes}
    return (
        by_name.get("origin")
        or by_name.get("upstream")
        or (remotes[0] if remotes else None)
    )
