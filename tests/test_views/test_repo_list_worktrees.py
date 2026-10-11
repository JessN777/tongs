"""A main checkout and its git worktree share one repository list row (#156)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from tests.test_scanner.test_discovery import _init_repo, add_worktree
from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    settle,
)
from tongs.forges.models import ForgeHost
from tongs.scanner.discovery import discover_repos
from tongs.scanner.repo import ForgeType, Repo
from tongs.views.repo_list import RepoListScreen


@pytest.mark.asyncio
async def test_main_checkout_and_worktree_show_as_one_row(tmp_path: Path) -> None:
    source = tmp_path / "src"
    main = source / "widgets"
    _init_repo(main, {"origin": "https://github.com/acme/widgets.git"})
    # "a-feature" sorts before "widgets", so path order alone would pick it.
    add_worktree(main, source / "a-feature")
    _init_repo(source / "gadgets", {"origin": "https://github.com/acme/gadgets.git"})

    def discoverer(_root: Path, **kwargs: object) -> list[Repo]:
        return discover_repos(source, **kwargs)  # type: ignore[arg-type]

    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    registry = MockForgeRegistry({"github.com": MockForgeClient(host, [])})
    app, session, _cache = make_app(tmp_path, [], registry, discoverer=discoverer)

    async with app.run_test(size=(120, 34)) as pilot:
        await settle(app)
        await pilot.press("r")
        await settle(app)
        screen = app.screen
        assert isinstance(screen, RepoListScreen)
        table = screen.query_one("#repo-table", DataTable)

        assert table.row_count == 2
        keys = sorted(str(row.value) for row in table.rows)
        assert keys == [
            str((source / "gadgets").resolve()),
            str(main.resolve()),
        ]
        # The session still keeps every checkout, the worktree included.
        names = sorted(repo.path.name for repo in session.local_repositories)
        assert names == ["a-feature", "gadgets", "widgets"]
