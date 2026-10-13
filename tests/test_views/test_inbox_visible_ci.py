"""Inbox CI lookups follow the rows in view (#396 follow-up).

One All Open load lists every repository's newest page, but only the rows in
view, plus about one screen above and below, get a CI lookup. Scrolling looks
up the rows it brings into view and drops queued lookups for rows that left
before they started. On GitHub the lookups that run together become one
GraphQL query per 15 rows. A rate limit stops new lookups for its host until
the reset and says so once.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from textual.pilot import Pilot
from textual.widgets import TabbedContent

import tongs.services.session as session_module
import tongs.views.inbox as inbox_module
from tests.test_forges.github_ci_batch import (
    answer_ci_batch,
    batch_pulls,
    check_run,
    is_ci_batch,
    pull_node,
)
from tests.test_tui_session import MockForgeRegistry, make_app, make_repo, settle
from tests.test_views.test_inbox_all_open_lazy_ci import (
    HOST,
    LOADING,
    LazyForgeClient,
    build,
    ci_cells,
    notices,
    numbers,
    status_text,
    summary,
    wait_until,
)
from tongs.app import TongsApp
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.errors import NetworkError, RateLimitError
from tongs.forges.github import GitHubClient
from tongs.forges.models import CIStatus, MRPage, MRSummary
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.widgets.mr_table import MRTable, mr_row_key, sort_mrs


class AllOpenOnlyClient(LazyForgeClient):
    """A lazy forge whose Reviews and My MRs lists are empty.

    Only All Open rows then ask for CI, so a test counts its lookups alone.
    """

    async def list_my_reviews(self) -> list[MRSummary]:
        return []

    async def list_my_mrs(self) -> list[MRSummary]:
        return []

    async def list_mrs_page(self, repo_path: str, *args, **kwargs) -> MRPage:
        if repo_path in self.repo_errors:
            raise self.repo_errors[repo_path]
        return await super().list_mrs_page(repo_path, *args, **kwargs)


def many(projects: list[str], per_project: int) -> list[MRSummary]:
    """Return ``per_project`` reviews per project, newest first overall."""
    rows: list[MRSummary] = []
    for index, project in enumerate(projects):
        for offset in range(per_project):
            number = index * 1000 + offset + 1
            rows.append(summary(project, number, len(projects) * offset + index))
    return rows


def rows_in_view(table: MRTable) -> int:
    return table.scrollable_content_region.height - table.header_height


def looked_up(client: LazyForgeClient) -> list[int]:
    return [call[1] for call in client.ci_calls]


async def open_all_open(pilot: Pilot, app: TongsApp, rows: int) -> MRTable:
    await pilot.press("3")
    table = app.screen.query_one("#all-open-table", MRTable)
    await wait_until(pilot, lambda: table.row_count == rows)
    await wait_until(pilot, lambda: not app.screen.loading_all_open)
    return table


@pytest.mark.asyncio
async def test_only_rows_near_the_view_are_looked_up(tmp_path: Path) -> None:
    # One repository lands in one step, so the first view is final.
    client = AllOpenOnlyClient(many(["acme/a"], 300), page_size=300)
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 300)
        view = rows_in_view(table)
        assert 5 < view < 50
        # The rows in view and one screen below them (nothing is above).
        window = numbers(table)[: 2 * view]
        await wait_until(pilot, lambda: len(client.ci_calls) == len(window))
        await settle(app)
        assert sorted(looked_up(client)) == sorted(window)
        assert all(ci_cells(table)[number] == "●" for number in window)
        assert ci_cells(table)[numbers(table)[-1]] == LOADING

        # Scrolling far down looks up only the rows brought into view.
        client.ci_calls.clear()
        table.focus()
        table.move_cursor(row=200)
        await wait_until(pilot, lambda: ci_cells(table)[numbers(table)[200]] == "●")
        await settle(app)
        top = int(table.scroll_y)
        window = numbers(table)[top - view : top + 2 * view]
        assert sorted(looked_up(client)) == sorted(window)
        # Rows between the two windows were never shown and never looked up.
        assert ci_cells(table)[numbers(table)[120]] == LOADING


@pytest.mark.asyncio
async def test_queued_lookups_that_scroll_away_are_dropped(tmp_path: Path) -> None:
    # Lookups in flight are bounded by max_parallel, which is 2 here.
    client = AllOpenOnlyClient(many(["acme/a"], 300), page_size=300)
    gate = asyncio.Event()
    first_rows = {f"sha-{number}" for number in range(1, 3)}
    client.ci_gates = {sha: gate for sha in first_rows}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 300)
        await wait_until(pilot, lambda: len(client.ci_calls) == 2)
        # Two lookups wait; the rest of the window is queued behind them.
        assert len(app.screen._ci["all-open"].queue) == 2 * rows_in_view(table) - 2

        table.focus()
        table.move_cursor(row=250)
        row_250 = mr_row_key(table.get_selected_mr())  # type: ignore[arg-type]
        await wait_until(pilot, lambda: row_250 in app.screen._ci["all-open"].queue)
        gate.set()
        await wait_until(pilot, lambda: ci_cells(table)[numbers(table)[250]] == "●")
        await wait_until(pilot, lambda: not app.screen._ci["all-open"].queue)
        await settle(app)

        # The queued rows near the top scrolled away before their lookup
        # started, so they were dropped and still wait for CI.
        assert ci_cells(table)[numbers(table)[10]] == LOADING
        assert numbers(table)[10] not in looked_up(client)
        assert Counter(looked_up(client)).most_common(1)[0][1] == 1


@pytest.mark.asyncio
async def test_ci_sort_with_pending_rows_looks_up_only_rows_in_view(
    tmp_path: Path,
) -> None:
    client = AllOpenOnlyClient(many(["acme/a"], 200), page_size=200)
    client.ci_status = {f"sha-{number}": CIStatus.FAILED for number in (150, 160)}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 200)
        view = rows_in_view(table)
        await wait_until(pilot, lambda: len(client.ci_calls) == 2 * view)
        await settle(app)
        before = len(client.ci_calls)

        table.focus()
        await pilot.press("s", "s")
        assert table._sort_key == "ci"
        await settle(app)
        await pilot.pause(0.3)
        await settle(app)
        # Known rows sort first and the rows still waiting come after them,
        # so the sort brings no new row into view and looks nothing up.
        assert len(client.ci_calls) == before
        order = numbers(table)
        known = [n for n in order if ci_cells(table)[n] != LOADING]
        assert order[: len(known)] == known
        assert all(ci_cells(table)[n] == LOADING for n in order[len(known) :])


@pytest.mark.asyncio
async def test_ci_sort_fills_rows_in_view_after_a_scroll_mid_list(
    tmp_path: Path,
) -> None:
    client = AllOpenOnlyClient(many(["acme/a"], 300), page_size=300)
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 300)
        view = rows_in_view(table)
        await wait_until(pilot, lambda: len(client.ci_calls) == 2 * view)
        await settle(app)

        table.focus()
        await pilot.press("s", "s")
        assert table.sort_key == "ci"
        await settle(app)
        cursor = table.cursor_row
        # A mouse wheel scroll: the view moves, the cursor does not.
        table.scroll_to(y=200, animate=False)
        await settle(app)
        top = int(table.scroll_y)
        assert top == 200
        assert table.cursor_row == cursor

        def in_view() -> list[int]:
            return numbers(table)[top : top + view]

        # Each landing moves its row up into the known rows and shifts a
        # waiting row into view, so the view is looked at again until every
        # row on screen shows its CI.
        await wait_until(
            pilot, lambda: all(ci_cells(table)[n] != LOADING for n in in_view())
        )
        await wait_until(pilot, lambda: not app.screen._ci["all-open"].workers)
        await settle(app)
        assert all(ci_cells(table)[n] == "●" for n in in_view())
        assert int(table.scroll_y) == top
        # Only rows that sat in the window were looked up, each once.
        assert len(client.ci_calls) <= top + 2 * view
        assert Counter(looked_up(client)).most_common(1)[0][1] == 1
        assert ci_cells(table)[numbers(table)[-1]] == LOADING


@pytest.mark.asyncio
async def test_github_all_open_cost_is_one_query_per_fifty_rows_in_view(
    tmp_path: Path,
) -> None:
    """End to end: list pages, then GraphQL batches for the rows in view only."""
    projects = [f"acme/r{index}" for index in range(3)]
    paths: list[str] = []
    batches: list[list[tuple[str, str, int]]] = []

    def pull(project: str, number: int) -> dict[str, object]:
        return {
            "number": number,
            "title": f"PR {number}",
            "state": "open",
            "draft": False,
            "user": {"login": "alice"},
            "head": {"ref": f"feature-{number}", "sha": f"sha-{number}"},
            "base": {"ref": "main", "sha": "base"},
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": (
                datetime(2026, 9, 30, tzinfo=UTC) - timedelta(minutes=number)
            ).isoformat(),
            "html_url": f"https://github.com/{project}/pull/{number}",
            "labels": [],
        }

    shown: list[MRTable] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith("/pulls") and path != f"/repos/{projects[0]}/pulls":
            # The newest repository lands first, so the older ones' rows
            # arrive below it and never push looked-up rows out of view.
            while not shown or shown[0].row_count < 100:
                await asyncio.sleep(0.01)
        if path == "/search/issues":
            return httpx.Response(200, json={"items": [], "total_count": 0})
        if is_ci_batch(request):
            batches.append(batch_pulls(request))
            return answer_ci_batch(
                request,
                lambda _owner, _repo, number: pull_node(
                    f"sha-{number}", [check_run("success")]
                ),
            )
        for index, project in enumerate(projects):
            if path == f"/repos/{project}/pulls":
                pulls = [
                    pull(project, index * 1000 + offset + 1) for offset in range(100)
                ]
                return httpx.Response(200, json=pulls)
        raise AssertionError(path)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=HOST.api_base
    )
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    client = CachedForgeClient(GitHubClient(HOST, http), cache, hostname=HOST.hostname)
    client.host = HOST  # type: ignore[attr-defined]
    registry = MockForgeRegistry({HOST.hostname: client})  # type: ignore[dict-item]
    repos = [make_repo(tmp_path, project) for project in projects]
    app, _session, _cache = make_app(tmp_path, repos, registry, cache=cache)

    async with app.run_test(size=(100, 80), notifications=True) as pilot:
        await settle(app)
        paths.clear()
        shown.append(app.screen.query_one("#all-open-table", MRTable))
        table = await open_all_open(pilot, app, 300)
        view = rows_in_view(table)
        window = numbers(table)[: 2 * view]
        await wait_until(pilot, lambda: all(ci_cells(table)[n] == "●" for n in window))
        await settle(app)

        # First paint: one list page per repository. Then one screen of CI
        # (the rows in view and one screen below): one GraphQL query per 15
        # rows, give or take a batch split by timing, and no REST CI request.
        # Lookups that start more than a batch window apart on a loaded
        # runner can split a batch, so allow up to twice the ideal count;
        # that is still far below one request per row.
        queries = paths.count("/graphql")
        ideal = math.ceil(len(window) / 15)
        assert ideal <= queries <= 2 * ideal
        assert sorted(path for path in paths if path != "/graphql") == sorted(
            f"/repos/{project}/pulls" for project in projects
        )
        assert sorted(number for batch in batches for _o, _r, number in batch) == (
            sorted(window)
        )
        assert all(len(batch) <= 15 for batch in batches)
        assert not any(path.endswith(("/check-runs", "/status")) for path in paths)
    await http.aclose()


@pytest.mark.asyncio
async def test_a_ci_rate_limit_holds_lookups_and_says_when_it_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AllOpenOnlyClient([summary("acme/a", n, n) for n in range(1, 6)])
    client.ci_errors = {
        f"sha-{n}": RateLimitError("Rate limited", retry_after=600) for n in (1, 2)
    }
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 5)
        await wait_until(pilot, lambda: "rate limit" in status_text(app))
        await settle(app)
        limit = app.services.rate_limit("github.com")
        assert limit is not None and limit.resets_at is not None
        resets = f"{limit.resets_at.astimezone():%H:%M}"
        assert status_text(app) == (
            f"5 open MRs across 1 repository. "
            f"GitHub rate limit reached, resets at {resets}"
        )
        # One notice for the tab load, not one per row.
        assert notices(app).count(f"GitHub rate limit reached, resets at {resets}") == 1
        # Rate-limited rows keep their loading marker.
        assert ci_cells(table)[1] == LOADING and ci_cells(table)[2] == LOADING

        # Nothing more is sent to the host while it is limited.
        calls = len(client.ci_calls)
        client.ci_errors = {}
        app.screen._update_ci_window("all-open")
        await settle(app)
        assert len(client.ci_calls) == calls

        # Once the reset passes, the waiting rows are looked up again.
        later = datetime.now(UTC) + timedelta(minutes=11)
        monkeypatch.setattr(session_module, "_utcnow", lambda: later)
        app.screen._update_ci_window("all-open")
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())
        app.screen._render_all_open_status()
        assert status_text(app) == "5 open MRs across 1 repository"


@pytest.mark.asyncio
async def test_a_list_rate_limit_is_one_notice_with_the_wait(tmp_path: Path) -> None:
    client = AllOpenOnlyClient(
        [summary("acme/a", 1, 1), summary("acme/b", 2, 2), summary("acme/c", 3, 3)]
    )
    client.repo_errors = {
        "acme/b": RateLimitError("Rate limited"),
        "acme/c": RateLimitError("Rate limited"),
    }
    app = build(tmp_path, client, ["acme/a", "acme/b", "acme/c"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        text = "GitHub rate limit reached, try again in a few minutes"
        assert notices(app).count(text) == 1
        assert not any("skipped" in notice for notice in notices(app))
        assert status_text(app).endswith(text)


@pytest.mark.asyncio
async def test_rows_left_waiting_after_a_failed_reload_are_looked_up(
    tmp_path: Path,
) -> None:
    client = AllOpenOnlyClient([summary("acme/a", 1, 1)])
    gate = asyncio.Event()
    client.ci_gates = {"sha-1": gate}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)

        async def failing(*_args: object, **_kwargs: object):
            raise ServiceError(ServiceErrorCode.NETWORK_UNAVAILABLE, "Offline.")
            yield  # pragma: no cover - makes this an async generator

        app.services.stream_all_open = failing  # type: ignore[method-assign]
        # The reload stops the lookup in flight, then fails before any row.
        await pilot.press("ctrl+r")
        await wait_until(pilot, lambda: client.ci_cancelled == ["sha-1"])
        await wait_until(pilot, lambda: len(client.ci_calls) == 2)
        gate.set()
        await wait_until(pilot, lambda: ci_cells(table) == {1: "●"})


class PagedClient(AllOpenOnlyClient):
    """Gate later pages so a test can act while a load more runs."""

    def __init__(self, summaries: list[MRSummary], page_size: int) -> None:
        super().__init__(summaries, page_size)
        self.later_pages = asyncio.Event()

    async def list_mrs_page(self, repo_path: str, *args, **kwargs) -> MRPage:
        page = kwargs.get("page", args[1] if len(args) > 1 else 1)
        if page > 1:
            await self.later_pages.wait()
        return await super().list_mrs_page(repo_path, *args, **kwargs)


@pytest.mark.asyncio
async def test_switching_tabs_during_a_load_more_keeps_the_loaded_pages(
    tmp_path: Path,
) -> None:
    client = PagedClient(
        [summary("acme/a", n, n) for n in range(1, 7)]
        + [summary("acme/b", n, n) for n in range(11, 17)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/a", "acme/b"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 4)
        client.later_pages.set()
        await pilot.press("m")
        await wait_until(pilot, lambda: table.row_count == 8)
        await wait_until(pilot, lambda: not app.screen.loading_all_open)

        # The next load more is stopped by leaving the tab mid-read.
        client.later_pages.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: app.screen.loading_all_open)
        await pilot.press("1")
        await settle(app)
        assert table.row_count == 8
        assert "all-open" in app.screen._loaded_tabs
        assert len(app.screen._all_open_cursors) == 2

        # Coming back shows the same rows without a reload, and m goes on.
        reads = len(client.page_reads)
        await pilot.press("3")
        await settle(app)
        assert table.row_count == 8
        assert len(client.page_reads) == reads
        assert status_text(app) == (
            "8 open MRs loaded across 2 repositories, more to load (m)"
        )
        client.later_pages.set()
        await pilot.press("m")
        await wait_until(pilot, lambda: table.row_count == 12)


@pytest.mark.asyncio
async def test_a_reorder_that_moves_the_cursor_last_loads_nothing(
    tmp_path: Path,
) -> None:
    client = AllOpenOnlyClient(
        [
            summary("acme/a", 1, 1, title="Alpha"),
            summary("acme/a", 2, 2, title="Bravo"),
            summary("acme/a", 3, 3, title="Charlie"),
            summary("acme/a", 4, 4, title="Delta"),
        ],
        page_size=3,
    )
    gates = {f"sha-{n}": asyncio.Event() for n in (1, 2, 3)}
    client.ci_gates = dict(gates)
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 3)
        table.focus()
        await pilot.press("s", "s")
        assert table._sort_key == "ci"
        selected = table.get_selected_mr()
        assert selected is not None and selected.number == 1
        reads = list(client.page_reads)

        # The other rows' CI arrives first, so they move ahead of Alpha and
        # the cursor follows Alpha to the last row.
        gates["sha-2"].set()
        gates["sha-3"].set()
        await wait_until(pilot, lambda: numbers(table) == [2, 3, 1])
        await pilot.pause(0.2)
        assert table.cursor_row == 2
        assert client.page_reads == reads

        # A real move to the last row still loads more.
        gates["sha-1"].set()
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())
        assert numbers(table) == [1, 2, 3]
        await pilot.press("down", "down")
        await wait_until(pilot, lambda: table.row_count == 4)


@pytest.mark.asyncio
async def test_a_load_more_from_the_middle_leaves_the_cursor(tmp_path: Path) -> None:
    client = AllOpenOnlyClient(
        [summary("acme/a", n, n) for n in range(1, 5)], page_size=2
    )
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 2)
        table.focus()
        assert table.cursor_row == 0
        await pilot.press("m")
        await wait_until(pilot, lambda: table.row_count == 4)
        await settle(app)
        selected = table.get_selected_mr()
        assert selected is not None and selected.number == 1


@pytest.mark.asyncio
async def test_the_added_count_leaves_the_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inbox_module, "_LOAD_MORE_STATUS_SECONDS", 0.2)
    client = AllOpenOnlyClient(
        [summary("acme/a", n, n) for n in range(1, 6)], page_size=2
    )
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 2)
        await pilot.press("m")
        await wait_until(pilot, lambda: table.row_count == 4)
        await wait_until(
            pilot,
            lambda: (
                status_text(app)
                == "4 open MRs loaded across 1 repository, more to load (m)"
            ),
        )


@pytest.mark.asyncio
async def test_a_repository_refresh_reloads_the_all_open_tab_in_place(
    tmp_path: Path,
) -> None:
    client = AllOpenOnlyClient(
        [summary("acme/a", n, n) for n in range(1, 4)], page_size=2
    )
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await open_all_open(pilot, app, 2)
        assert status_text(app).endswith("more to load (m)")
        reads = len(client.page_reads)

        # The background repository scan finished. The tab the user is
        # reading stays active and reloads, so the offer is recomputed from
        # the fresh read rather than cleared by a jump back to My Reviews.
        app.screen.on_repositories_refreshed(app.repository_generation + 1)
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: len(client.page_reads) == reads + 1)
        await settle(app)
        assert app.screen.query_one(TabbedContent).active == "all-open"
        assert table.row_count == 2
        assert status_text(app).endswith("more to load (m)")
        assert app.screen.check_action("load_more", ()) is True


@pytest.mark.asyncio
async def test_a_failed_lookup_still_shows_unknown(tmp_path: Path) -> None:
    client = AllOpenOnlyClient([summary("acme/a", 1, 1)])
    client.ci_errors = {"sha-1": NetworkError("private detail")}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 1)
        await wait_until(pilot, lambda: ci_cells(table) == {1: "?"})
        assert not any("rate limit" in notice for notice in notices(app))


def test_pending_rows_sort_after_every_known_status() -> None:
    unknown = replace(summary("acme/a", 1, 1, title="A"), ci_status=CIStatus.UNKNOWN)
    pending = replace(summary("acme/a", 2, 2, title="B"), ci_status=CIStatus.UNKNOWN)
    failed = replace(summary("acme/a", 3, 3, title="C"), ci_status=CIStatus.FAILED)
    ordered = sort_mrs([pending, unknown, failed], "ci", {mr_row_key(pending)})
    assert [mr.number for mr in ordered] == [3, 1, 2]
