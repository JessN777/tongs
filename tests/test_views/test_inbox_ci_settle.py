"""Inbox CI lookups settle before they start and respect the host's limit.

While the first All Open load streams in, each repository re-sorts the list,
so lookups wait until the stream ends or pauses for longer than the debounce
and then cover one window. Lookups in flight per host are bounded by
``max_parallel``, except while GitHub batches them into GraphQL queries, when
one batch may be in flight since it becomes one request.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

import tongs.views.inbox as inbox_module
from tests.test_tui_session import (
    NOW,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tests.test_views.test_inbox_all_open_lazy_ci import (
    HOST,
    LOADING,
    LazyForgeClient,
    ci_cells,
    wait_until,
)
from tests.test_views.test_inbox_visible_ci import AllOpenOnlyClient, open_all_open
from tongs.app import TongsApp
from tongs.forges.github import GitHubClient
from tongs.forges.models import CIStatus, MRPage, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.widgets.mr_table import MRTable

GITLAB = replace(HOST, hostname="gitlab.com", forge_type=ForgeType.GITLAB)


class GatedListClient(AllOpenOnlyClient):
    """A lazy forge whose repository list reads wait for the test to release."""

    def __init__(self, summaries: list[MRSummary], page_size: int) -> None:
        super().__init__(summaries, page_size=page_size)
        self.arrive: dict[str, asyncio.Event] = {}

    async def list_mrs_page(self, repo_path: str, *args, **kwargs) -> MRPage:
        await self.arrive.setdefault(repo_path, asyncio.Event()).wait()
        return await super().list_mrs_page(repo_path, *args, **kwargs)


class ConcurrencyClient(AllOpenOnlyClient):
    """A lazy forge that counts its lookups in flight and holds them on a gate."""

    def __init__(self, summaries: list[MRSummary], page_size: int) -> None:
        super().__init__(summaries, page_size=page_size)
        self.batched_ci_lookups = 1
        self.gate = asyncio.Event()
        self.in_flight = 0
        self.peak = 0

    async def _lookup(
        self, repo_path: str, number: int, head_sha: str, *, fresh: bool
    ) -> CIStatus:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.gate.wait()
            return await super()._lookup(repo_path, number, head_sha, fresh=fresh)
        finally:
            self.in_flight -= 1


def stacked(projects: list[str], per_project: int) -> list[MRSummary]:
    """Return reviews where each later project is newer than every earlier one.

    Projects that land in list order then put their rows above every row
    already shown, as repositories arriving out of recency order do.
    """
    rows: list[MRSummary] = []
    for index, project in enumerate(projects):
        newest = (len(projects) - index) * per_project
        for offset in range(per_project):
            number = index * 1000 + offset + 1
            rows.append(
                replace(
                    make_summary(HOST, project, number),
                    title=f"Review {project} {number}",
                    updated_at=NOW - timedelta(hours=newest + offset),
                    head_commit_sha=f"sha-{number}",
                )
            )
    return rows


def build(
    tmp_path: Path,
    client: LazyForgeClient,
    projects: list[str],
    *,
    max_parallel: int = 2,
) -> TongsApp:
    registry = MockForgeRegistry({HOST.hostname: client})
    repos = [make_repo(tmp_path, project) for project in projects]
    app, _session, _cache = make_app(tmp_path, repos, registry)
    app.config.max_parallel = max_parallel
    return app


def window_keys(table: MRTable) -> set[str]:
    keys = table.keys_near_viewport()
    assert keys is not None
    return set(keys)


def filled(table: MRTable, keys: set[str]) -> bool:
    """Return whether every row of ``keys`` shows its CI."""
    cells = ci_cells(table)
    return all(
        (mr := table.get_mr(key)) is not None and cells[mr.number] != LOADING
        for key in keys
    )


def looked_up_keys(client: LazyForgeClient) -> list[str]:
    return [f"{HOST.hostname}:{call[0]}:{call[1]}" for call in client.ci_calls]


@pytest.mark.asyncio
async def test_a_streaming_first_load_looks_up_one_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # While the test feeds the stream, a debounce far longer than any gap
    # between arrivals means a slow test machine never looks like a pause.
    monkeypatch.setattr(inbox_module, "_CI_WINDOW_DEBOUNCE_SECONDS", 30.0)
    projects = [f"acme/r{index:02d}" for index in range(12)]
    client = GatedListClient(stacked(projects, 60), page_size=60)
    # Every repository read may start at once, so the test alone sets the
    # order they land in.
    app = build(tmp_path, client, projects, max_parallel=len(projects))

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        # Oldest first, each well within the debounce after the last, so
        # every repository puts its rows on top of the list.
        for count, project in enumerate(projects, start=1):
            last = count == len(projects)
            if last:
                # The last arrival ends the stream; its lookup may now run.
                monkeypatch.setattr(inbox_module, "_CI_WINDOW_DEBOUNCE_SECONDS", 0.1)
            client.arrive.setdefault(project, asyncio.Event()).set()
            await wait_until(pilot, lambda count=count: table.row_count == count * 60)
            if not last:
                await pilot.pause(0.02)
                assert not client.ci_calls
        await wait_until(pilot, lambda: not app.screen.loading_all_open)
        await wait_until(
            pilot, lambda: bool(client.ci_calls) and filled(table, window_keys(table))
        )
        await settle(app)
        await pilot.pause(0.6)
        window = window_keys(table)

        # One window was looked up, each row once, and nothing outside it.
        looked_up = looked_up_keys(client)
        assert len(looked_up) == len(window)
        assert set(looked_up) == window
        assert Counter(looked_up).most_common(1)[0][1] == 1


@pytest.mark.asyncio
async def test_a_pause_in_the_stream_looks_up_the_rows_already_shown(
    tmp_path: Path,
) -> None:
    projects = ["acme/old", "acme/new"]
    client = GatedListClient(stacked(projects, 60), page_size=60)
    app = build(tmp_path, client, projects, max_parallel=2)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        client.arrive.setdefault("acme/old", asyncio.Event()).set()
        await wait_until(pilot, lambda: table.row_count == 60)
        # The other repository is slow, so the rows shown get their CI
        # without waiting for it.
        await wait_until(pilot, lambda: len(client.ci_calls) > 0)
        assert app.screen.loading_all_open
        assert {call[0] for call in client.ci_calls} == {"acme/old"}

        client.arrive.setdefault("acme/new", asyncio.Event()).set()
        await wait_until(pilot, lambda: not app.screen.loading_all_open)
        await wait_until(pilot, lambda: filled(table, window_keys(table)))


@pytest.mark.asyncio
async def test_lookups_in_flight_follow_the_hosts_limit(tmp_path: Path) -> None:
    client = ConcurrencyClient(stacked(["acme/a"], 300), page_size=300)
    app = build(tmp_path, client, ["acme/a"], max_parallel=3)

    async with app.run_test(size=(100, 80), notifications=True) as pilot:
        await settle(app)
        table = await open_all_open(pilot, app, 300)
        window = window_keys(table)
        assert len(window) > 60

        # Each lookup is its own read: max_parallel of them at a time.
        await wait_until(pilot, lambda: client.in_flight == 3)
        await pilot.pause(0.05)
        assert client.peak == 3

        # Batched lookups become one request, so one batch may be in flight.
        client.batched_ci_lookups = 50
        client.gate.set()
        client.gate = asyncio.Event()
        await wait_until(pilot, lambda: client.in_flight == 50)
        await pilot.pause(0.05)
        assert client.peak == 50

        # Batching stopped (a REST fallback): back to max_parallel.
        client.batched_ci_lookups = 1
        client.gate.set()
        client.gate = asyncio.Event()
        await wait_until(pilot, lambda: client.in_flight == 3)
        await pilot.pause(0.05)
        assert client.in_flight == 3

        client.gate.set()
        await wait_until(pilot, lambda: filled(table, window))
        await settle(app)
        assert Counter(looked_up_keys(client)).most_common(1)[0][1] == 1


@pytest.mark.asyncio
async def test_each_host_has_its_own_lookup_limit(tmp_path: Path) -> None:
    github = ConcurrencyClient(stacked(["acme/a"], 100), page_size=100)
    github.batched_ci_lookups = 50
    gitlab = ConcurrencyClient(
        [
            replace(item, forge_host=GITLAB, head_commit_sha=f"lab-{item.number}")
            for item in stacked(["group/b"], 100)
        ],
        page_size=100,
    )
    gitlab.host = GITLAB
    registry = MockForgeRegistry(
        {HOST.hostname: github, GITLAB.hostname: gitlab}  # type: ignore[dict-item]
    )
    repos = [
        make_repo(tmp_path, "acme/a"),
        make_repo(
            tmp_path, "group/b", hostname=GITLAB.hostname, forge_type=ForgeType.GITLAB
        ),
    ]
    app, _session, _cache = make_app(tmp_path, repos, registry)

    async with app.run_test(size=(100, 80), notifications=True) as pilot:
        await settle(app)
        await open_all_open(pilot, app, 200)
        await wait_until(pilot, lambda: github.in_flight == 50)
        await wait_until(pilot, lambda: gitlab.in_flight == 2)
        await pilot.pause(0.05)
        assert (github.peak, gitlab.peak) == (50, 2)
        github.gate.set()
        gitlab.gate.set()


@pytest.mark.parametrize(
    ("max_parallel", "graphql", "expected"),
    [
        (8, True, 60),
        (8, False, 8),
        (64, True, 64),
        (100, True, 100),
    ],
)
@pytest.mark.asyncio
async def test_the_github_lookup_limit_follows_its_batching(
    tmp_path: Path, max_parallel: int, graphql: bool, expected: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url.path)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=HOST.api_base
    )
    client = GitHubClient(HOST, http)
    client.host = HOST  # type: ignore[attr-defined]
    client._ci_graphql = graphql
    gitlab = LazyForgeClient([])
    gitlab.host = GITLAB
    registry = MockForgeRegistry(
        {HOST.hostname: client, GITLAB.hostname: gitlab}  # type: ignore[dict-item]
    )
    app, _session, _cache = make_app(tmp_path, [], registry)
    app.config.max_parallel = max_parallel

    async with app.run_test() as pilot:
        await settle(app)
        services = app.services
        assert await services.ci_lookup_limit(HOST.hostname) == expected
        # Every GitLab lookup is its own read.
        assert await services.ci_lookup_limit(GITLAB.hostname) == max_parallel
        # A host without a client yet keeps the request limit.
        assert await services.ci_lookup_limit("unknown.example") == max_parallel
        await pilot.pause()
    await http.aclose()
