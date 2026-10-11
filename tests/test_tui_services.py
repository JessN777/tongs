"""The terminal adapter lists each discovered forge project once."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services.models import RepositoryRef, RepositorySnapshot
from tongs.services.session import ApplicationSession
from tongs.tui_services import TUIServiceAdapter, one_checkout_per_project


def _repo(
    path: str,
    project: str = "acme/widgets",
    *,
    worktree: bool = False,
    hostname: str = "github.com",
) -> Repo:
    remote = Remote(
        "origin",
        f"https://{hostname}/{project}.git",
        hostname,
        project,
        ForgeType.GITHUB,
    )
    return Repo(Path(path), (remote,), remote, worktree=worktree)


def test_main_checkout_wins_over_worktrees_in_any_order() -> None:
    first_worktree = _repo("/src/a-feature", worktree=True)
    main = _repo("/src/z-main")
    second_worktree = _repo("/src/b-fix", worktree=True)

    kept = one_checkout_per_project((first_worktree, main, second_worktree))

    assert kept == (main,)


def test_worktrees_alone_keep_the_first_by_path() -> None:
    later = _repo("/src/zz", worktree=True)
    earlier = _repo("/src/aa", worktree=True)

    assert one_checkout_per_project((later, earlier)) == (earlier,)


def test_two_main_checkouts_keep_the_first_by_path() -> None:
    later = _repo("/src/zz")
    earlier = _repo("/src/aa")

    assert one_checkout_per_project((later, earlier)) == (earlier,)


def test_other_projects_and_hosts_stay_in_order() -> None:
    widgets = _repo("/src/widgets")
    widgets_worktree = _repo("/src/widgets-wt", worktree=True)
    gadgets = _repo("/src/gadgets", "acme/gadgets")
    other_host = _repo("/src/widgets-gl", hostname="gitlab.com")

    kept = one_checkout_per_project((widgets_worktree, gadgets, widgets, other_host))

    assert kept == (widgets, gadgets, other_host)


def test_repositories_without_a_remote_are_kept() -> None:
    first = Repo(Path("/src/one"), ())
    second = Repo(Path("/src/two"), ())

    assert one_checkout_per_project((first, second)) == (first, second)


class _DiscoveringSession:
    def __init__(self, repositories: tuple[Repo, ...]) -> None:
        self.local_repositories = repositories

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        ref = RepositoryRef("github.com", "acme/widgets")
        return (RepositorySnapshot(ref, "acme/widgets", ForgeType.GITHUB),)


@pytest.mark.asyncio
async def test_discovery_publishes_one_row_but_the_session_keeps_all() -> None:
    worktree = _repo("/src/a-feature", worktree=True)
    main = _repo("/src/main")
    session = _DiscoveringSession((worktree, main))
    adapter = TUIServiceAdapter(cast(ApplicationSession, session))

    result = await adapter.discover_repositories()

    assert result.repositories == (main,)
    assert adapter.repositories == (main,)
    assert adapter.repository_ref(main) == RepositoryRef("github.com", "acme/widgets")
    assert session.local_repositories == (worktree, main)
