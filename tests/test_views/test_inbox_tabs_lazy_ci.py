"""Reviews and My MRs fill in CI per row on both forges (#385).

Both tabs list without CI, show the loading marker in each row's CI column,
and fill it in place from a lookup that goes through the shared session, the
CI cache, and the per-host request limit. GitHub CI combines check runs with
legacy commit statuses, so a pull request that only reports through a commit
status shows it. The request count of each tab refresh is pinned here so the
cost of the lookups stays visible.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from textual.pilot import Pilot

from tests.test_forges.github_ci_batch import (
    answer_ci_batch,
    check_run,
    is_ci_batch,
    pull_node,
    status_context,
)
from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.app import TongsApp
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.errors import NetworkError
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import CIStatus, ForgeHost, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.widgets.mr_table import MRTable, mr_row_key

GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
LOADING = ".."
TABLES = {"1": "#reviews-table", "2": "#my-mrs-table"}


def ci_cells(table: MRTable) -> dict[int, str]:
    return {
        mr.number: str(table.get_cell(mr_row_key(mr), "ci"))
        for mr in table._mr_data.values()
    }


async def wait_until(pilot: Pilot, condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await pilot.pause()

    await asyncio.wait_for(poll(), timeout=5)


def cached_app(
    tmp_path: Path,
    host: ForgeHost,
    inner: ForgeClient,
    project: str,
) -> TongsApp:
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    client = CachedForgeClient(inner, cache, hostname=host.hostname)
    client.host = host  # type: ignore[attr-defined]
    registry = MockForgeRegistry({host.hostname: client})  # type: ignore[dict-item]
    repo = make_repo(
        tmp_path, project, hostname=host.hostname, forge_type=host.forge_type
    )
    app, _session, _cache = make_app(tmp_path, [repo], registry, cache=cache)
    app.config.ascii_mode = True
    return app


async def wait_for_rows(pilot: Pilot, table: MRTable, count: int) -> None:
    await wait_until(
        pilot,
        lambda: table.row_count == count and LOADING not in ci_cells(table).values(),
    )


# -- GitHub ---------------------------------------------------------------


def github_pull(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"ref": f"feature-{number}", "sha": f"sha-{number}"},
        "base": {"ref": "main", "sha": "base"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": f"2026-09-0{number}T00:00:00Z",
        "html_url": f"https://github.com/acme/one/pull/{number}",
        "labels": [],
    }


# Each head's check runs and commit statuses. Pull request 2 reports only
# through a commit status (Jenkins), pull request 3 through nothing at all.
GITHUB_CHECK_RUNS = {
    "sha-1": [{"status": "completed", "conclusion": "success"}],
    "sha-2": [],
    "sha-3": [],
    "sha-4": [{"status": "completed", "conclusion": "success"}],
}
GITHUB_STATUSES = {
    "sha-1": [],
    "sha-2": [{"state": "failure", "context": "continuous-integration/jenkins"}],
    "sha-3": [],
    "sha-4": [{"state": "pending", "context": "tide"}],
}


class GitHubForge:
    """A mocked GitHub API that records the kind of every request."""

    def __init__(self, numbers: tuple[int, ...], *, graphql: bool = True) -> None:
        self.numbers = numbers
        # False answers GraphQL with 404, as GitHub Enterprise without it.
        self.graphql = graphql
        self.requests: list[str] = []
        self.searches: list[str] = []

    def counts(self) -> Counter[str]:
        return Counter(self.requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            self.requests.append("graphql")
            if not self.graphql:
                return httpx.Response(404, json={"message": "Not Found"})
            assert is_ci_batch(request)
            return answer_ci_batch(request, self._pull_node)
        if path == "/search/issues":
            self.requests.append("search")
            self.searches.append(request.url.params["q"])
            items = [
                {
                    "number": number,
                    "repository_url": "https://api.github.com/repos/acme/one",
                }
                for number in self.numbers
            ]
            return httpx.Response(200, json={"total_count": len(items), "items": items})
        prefix = "/repos/acme/one/"
        assert path.startswith(prefix), path
        rest = path[len(prefix) :]
        if rest.startswith("pulls/"):
            self.requests.append("pull")
            return httpx.Response(200, json=github_pull(int(rest.split("/")[1])))
        sha = rest.split("/")[1]
        if rest.endswith("/check-runs"):
            self.requests.append("check-runs")
            runs = GITHUB_CHECK_RUNS[sha]
            return httpx.Response(
                200, json={"total_count": len(runs), "check_runs": runs}
            )
        if rest.endswith("/status"):
            self.requests.append("status")
            statuses = GITHUB_STATUSES[sha]
            return httpx.Response(
                200,
                json={
                    "state": "pending",
                    "total_count": len(statuses),
                    "statuses": statuses,
                },
            )
        raise AssertionError(path)

    @staticmethod
    def _pull_node(_owner: str, _repo: str, number: int) -> dict:
        sha = f"sha-{number}"
        checks = [
            check_run(run["conclusion"], run["status"])
            for run in GITHUB_CHECK_RUNS[sha]
        ] + [status_context(status["state"]) for status in GITHUB_STATUSES[sha]]
        return pull_node(sha, checks or None)


@pytest.mark.asyncio
@pytest.mark.parametrize("graphql", [True, False], ids=["graphql", "rest"])
async def test_github_reviews_and_my_mrs_show_real_ci_with_pinned_requests(
    tmp_path: Path, graphql: bool
) -> None:
    forge = GitHubForge((1, 2, 3, 4), graphql=graphql)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handler), base_url=GITHUB.api_base
    )
    app = cached_app(tmp_path, GITHUB, GitHubClient(GITHUB, http), "acme/one")
    expected = {1: "OK", 2: "FAIL", 3: "?", 4: "PEND"}

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        reviews = app.screen.query_one("#reviews-table", MRTable)
        await wait_for_rows(pilot, reviews, 4)
        assert ci_cells(reviews) == expected
        assert {mr.number: mr.ci_status for mr in reviews._mr_data.values()} == {
            1: CIStatus.SUCCESS,
            2: CIStatus.FAILED,
            3: CIStatus.UNKNOWN,
            4: CIStatus.PENDING,
        }
        # One search page and one read per pull request, as before, then one
        # GraphQL query for every row's CI. Without GraphQL, two REST CI
        # requests per row: its check runs and its commit statuses.
        ci = {"graphql": 1} if graphql else {"graphql": 1, "check-runs": 4, "status": 4}
        assert forge.counts() == Counter({"search": 1, "pull": 4, **ci})

        # My MRs lists the same heads, so their CI comes from the cache.
        forge.requests.clear()
        await pilot.press("2")
        my_mrs = app.screen.query_one("#my-mrs-table", MRTable)
        await wait_for_rows(pilot, my_mrs, 4)
        await settle(app)
        assert ci_cells(my_mrs) == expected
        assert forge.counts() == Counter({"search": 1, "pull": 4})

        # A refresh reads the list and every row's CI past the cache. A host
        # that answered GraphQL with 404 is not asked again.
        forge.requests.clear()
        await pilot.press("ctrl+r")
        await settle(app)
        await wait_for_rows(pilot, my_mrs, 4)
        assert ci_cells(my_mrs) == expected
        ci = {"graphql": 1} if graphql else {"check-runs": 4, "status": 4}
        assert forge.counts() == Counter({"search": 1, "pull": 4, **ci})
        assert forge.searches == [
            "is:pr is:open review-requested:@me archived:false",
            "is:pr is:open author:@me archived:false",
            "is:pr is:open author:@me archived:false",
        ]
    await http.aclose()


# -- GitLab ---------------------------------------------------------------


def gitlab_merge_request(iid: int) -> dict[str, object]:
    # The global merge request list names the head commit but no pipeline.
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "state": "opened",
        "draft": False,
        "author": {"username": "alice", "name": "Alice"},
        "source_branch": f"feature-{iid}",
        "target_branch": "main",
        "sha": f"head-{iid}",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": f"2026-09-0{iid}T00:00:00Z",
        "web_url": f"https://gitlab.com/acme/one/-/merge_requests/{iid}",
        "references": {"full": f"acme/one!{iid}"},
        "labels": [],
    }


GITLAB_PIPELINES: dict[int, list[dict[str, object]]] = {
    1: [{"id": 11, "status": "success"}],
    2: [{"id": 12, "status": "failed"}],
    3: [],
}


class GitLabForge:
    """A mocked GitLab API that records the kind of every request."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.scopes: list[dict[str, str]] = []

    def counts(self) -> Counter[str]:
        return Counter(self.requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        if path == "/api/v4/user":
            self.requests.append("user")
            return httpx.Response(200, json={"username": "alice"})
        if path == "/api/v4/merge_requests":
            self.requests.append("list")
            params = dict(request.url.params)
            self.scopes.append(
                {key: params[key] for key in ("scope", "state") if key in params}
            )
            return httpx.Response(
                200, json=[gitlab_merge_request(iid) for iid in GITLAB_PIPELINES]
            )
        prefix = "/api/v4/projects/acme%2Fone/merge_requests/"
        if path.startswith(prefix) and path.endswith("/pipelines"):
            self.requests.append("pipelines")
            iid = int(path[len(prefix) :].split("/")[0])
            return httpx.Response(200, json=GITLAB_PIPELINES[iid])
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_gitlab_reviews_and_my_mrs_show_real_ci_with_pinned_requests(
    tmp_path: Path,
) -> None:
    forge = GitLabForge()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handler), base_url=GITLAB.api_base
    )
    app = cached_app(tmp_path, GITLAB, GitLabClient(GITLAB, http), "acme/one")
    expected = {1: "OK", 2: "FAIL", 3: "?"}

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        reviews = app.screen.query_one("#reviews-table", MRTable)
        await wait_for_rows(pilot, reviews, 3)
        assert ci_cells(reviews) == expected
        assert {mr.number: mr.head_commit_sha for mr in reviews._mr_data.values()} == {
            1: "head-1",
            2: "head-2",
            3: "head-3",
        }
        # The user, one list page, then one pipeline request per row.
        assert forge.counts() == Counter({"user": 1, "list": 1, "pipelines": 3})

        forge.requests.clear()
        await pilot.press("2")
        my_mrs = app.screen.query_one("#my-mrs-table", MRTable)
        await wait_for_rows(pilot, my_mrs, 3)
        await settle(app)
        assert ci_cells(my_mrs) == expected
        # The same heads were looked up a moment ago, so CI comes from cache.
        assert forge.counts() == Counter({"list": 1})

        forge.requests.clear()
        await pilot.press("ctrl+r")
        await settle(app)
        await wait_for_rows(pilot, my_mrs, 3)
        assert ci_cells(my_mrs) == expected
        assert forge.counts() == Counter({"list": 1, "pipelines": 3})
        assert forge.scopes == [
            {"scope": "all", "state": "opened"},
            {"scope": "created_by_me", "state": "opened"},
            {"scope": "created_by_me", "state": "opened"},
        ]
    await http.aclose()


# -- Lookup lifecycle -----------------------------------------------------


class PersonalLazyClient(MockForgeClient):
    """Personal lists without CI, and gated per-review CI lookups."""

    supports_lazy_ci = True

    def __init__(self, summaries: list[MRSummary]) -> None:
        super().__init__(GITHUB, summaries)
        self.ci_calls: list[tuple[int, str, bool]] = []
        self.gate: asyncio.Event | None = None
        self.cached_status: dict[str, CIStatus] = {}
        self.fresh_status: dict[str, CIStatus] = {}
        self.errors: dict[str, Exception] = {}

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup(number, head_sha, fresh=False)

    async def get_mr_ci_status_fresh(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup(number, head_sha, fresh=True)

    async def _lookup(self, number: int, head_sha: str, *, fresh: bool) -> CIStatus:
        self.ci_calls.append((number, head_sha, fresh))
        if self.gate is not None:
            await self.gate.wait()
        if head_sha in self.errors:
            raise self.errors[head_sha]
        statuses = self.fresh_status if fresh else self.cached_status
        return statuses.get(head_sha, CIStatus.SUCCESS)


def lazy_summary(number: int) -> MRSummary:
    return replace(
        make_summary(GITHUB, "acme/one", number),
        title=f"Review {number}",
        ci_status=CIStatus.UNKNOWN,
        head_commit_sha=f"sha-{number}",
    )


def lazy_app(tmp_path: Path, client: PersonalLazyClient) -> TongsApp:
    registry = MockForgeRegistry({GITHUB.hostname: client})
    app, _session, _cache = make_app(
        tmp_path, [make_repo(tmp_path, "acme/one")], registry
    )
    app.config.ascii_mode = True
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["1", "2"])
async def test_rows_show_the_loading_marker_until_their_own_lookup_lands(
    tmp_path: Path, key: str
) -> None:
    client = PersonalLazyClient([lazy_summary(1), lazy_summary(2)])
    client.cached_status = {"sha-1": CIStatus.FAILED}
    client.errors = {"sha-2": NetworkError("private detail")}
    client.gate = asyncio.Event()
    app = lazy_app(tmp_path, client)

    async with app.run_test(notifications=True) as pilot:
        await pilot.pause()
        await pilot.press(key)
        table = app.screen.query_one(TABLES[key], MRTable)
        await wait_until(pilot, lambda: table.row_count == 2)
        assert ci_cells(table) == {1: LOADING, 2: LOADING}

        client.gate.set()
        # A failed lookup shows unknown; the other row still fills in.
        await wait_until(pilot, lambda: ci_cells(table) == {1: "FAIL", 2: "?"})
        assert ("sha-1", False) in {(sha, fresh) for _n, sha, fresh in client.ci_calls}


@pytest.mark.asyncio
async def test_a_refresh_drops_old_lookups_and_reads_ci_past_the_cache(
    tmp_path: Path,
) -> None:
    client = PersonalLazyClient([lazy_summary(1), lazy_summary(2)])
    client.cached_status = {"sha-1": CIStatus.FAILED, "sha-2": CIStatus.FAILED}
    client.fresh_status = {"sha-1": CIStatus.SUCCESS, "sha-2": CIStatus.RUNNING}
    client.gate = asyncio.Event()
    app = lazy_app(tmp_path, client)

    async with app.run_test(notifications=True) as pilot:
        table = app.screen.query_one("#reviews-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 2)
        assert ci_cells(table) == {1: LOADING, 2: LOADING}

        await pilot.press("ctrl+r")
        await wait_until(
            pilot, lambda: sum(fresh for *_rest, fresh in client.ci_calls) == 2
        )
        client.gate.set()
        await wait_until(pilot, lambda: ci_cells(table) == {1: "OK", 2: "RUN"})
        await settle(app)
        # The lookups the refresh replaced never landed.
        assert ci_cells(table) == {1: "OK", 2: "RUN"}
        assert sorted(client.ci_calls) == [
            (1, "sha-1", False),
            (1, "sha-1", True),
            (2, "sha-2", False),
            (2, "sha-2", True),
        ]


@pytest.mark.asyncio
async def test_reviews_lookups_finish_after_switching_tabs(tmp_path: Path) -> None:
    client = PersonalLazyClient([lazy_summary(1)])
    client.cached_status = {"sha-1": CIStatus.FAILED}
    client.gate = asyncio.Event()
    app = lazy_app(tmp_path, client)

    async with app.run_test(notifications=True) as pilot:
        reviews = app.screen.query_one("#reviews-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)
        await pilot.press("2")
        my_mrs = app.screen.query_one("#my-mrs-table", MRTable)
        await wait_until(pilot, lambda: my_mrs.row_count == 1)

        client.gate.set()
        await wait_until(pilot, lambda: ci_cells(reviews) == {1: "FAIL"})
        await wait_until(pilot, lambda: ci_cells(my_mrs) == {1: "FAIL"})


@pytest.mark.asyncio
async def test_rows_that_arrive_with_ci_need_no_lookup(tmp_path: Path) -> None:
    listed = replace(lazy_summary(1), ci_status=CIStatus.RUNNING)
    client = PersonalLazyClient([listed])
    app = lazy_app(tmp_path, client)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = app.screen.query_one("#reviews-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)
        await settle(app)
        assert ci_cells(table) == {1: "RUN"}
        assert client.ci_calls == []


@pytest.mark.asyncio
async def test_rows_kept_after_a_failed_reload_still_get_their_ci(
    tmp_path: Path,
) -> None:
    client = PersonalLazyClient([lazy_summary(1)])
    client.cached_status = {"sha-1": CIStatus.FAILED}
    client.gate = asyncio.Event()
    app = lazy_app(tmp_path, client)

    async with app.run_test(notifications=True) as pilot:
        table = app.screen.query_one("#reviews-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)

        # A repository refresh stops every lookup and reloads the tab, whose
        # list read now fails, so the earlier row stays on screen.
        client.error = NetworkError("private detail")
        app.screen.on_repositories_refreshed(app.repository_generation + 1)
        await wait_until(pilot, lambda: len(client.ci_calls) == 2)
        assert ci_cells(table) == {1: LOADING}

        client.gate.set()
        await wait_until(pilot, lambda: ci_cells(table) == {1: "FAIL"})
