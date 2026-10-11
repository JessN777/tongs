"""Tests for the tongs-mcp tool logic, called as plain async functions."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from tests.test_mcp.forge_fakes import (
    BASE,
    HEAD_1,
    HEAD_2,
    REPO,
    FakeForge,
    make_session,
    pull,
)
from tongs.diff.conversion import convert_forge_changes
from tongs.mcp import tools
from tongs.mcp.tools import ToolFailure
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.session import ApplicationSession


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeForge]:
    """Route every forge request to a fresh fake, with a fixed token."""
    fake = FakeForge()

    async def handle_async_request(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await fake.handle(request)

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    with patch("tongs.forges.registry.resolve_token", return_value="tok"):
        yield fake


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[ApplicationSession]:
    started = await make_session(tmp_path).start()
    try:
        yield started
    finally:
        await started.close()


LIST_PATH = "/repos/acme/repo/pulls"
REVIEWS_PATH = "/repos/acme/repo/pulls/7/reviews"


def list_reads(forge: FakeForge, per_page: int) -> int:
    """Count pull request list reads of one page size."""
    return sum(
        1
        for request in forge.requests
        if request.url.path == LIST_PATH
        and request.url.params.get("per_page") == str(per_page)
    )


def ci_reads(forge: FakeForge) -> int:
    return sum(
        1
        for request in forge.requests
        if request.url.path.endswith(("/check-runs", "/status"))
        or request.url.path == "/graphql"
    )


async def opened(session: ApplicationSession, forge: FakeForge) -> None:
    """Issue the repository, then forget the requests that took."""
    await tools.admit_repository(session, tools.parse_repo_path(session, REPO))
    forge.requests.clear()


class TestGetMr:
    @pytest.mark.asyncio
    async def test_returns_head_and_base_sha(self, session, forge) -> None:
        result = await tools.get_mr(session, REPO, 7)
        assert result["number"] == 7
        assert result["head_sha"] == HEAD_1
        assert result["base_sha"] == BASE
        assert result["description"] == "Details"

    @pytest.mark.asyncio
    async def test_any_configured_host_repository_is_readable(
        self, session, forge
    ) -> None:
        """A repository needs no local clone: the session issues it on demand."""
        assert session.issued_repositories == frozenset()
        await tools.get_mr(session, REPO, 7)
        assert {ref.project_path for ref in session.issued_repositories} == {
            "acme/repo"
        }


class TestApproveMr:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("head_sha", ["", None])
    async def test_missing_head_sha_fails_before_any_request(
        self, session, forge, head_sha
    ) -> None:
        with pytest.raises(ToolFailure) as caught:
            await tools.approve_mr(session, REPO, 7, head_sha)
        assert caught.value.message == tools.HEAD_SHA_MISSING
        assert "get_mr" in caught.value.message
        assert forge.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "head_sha", ["abc1234", "z" * 40, HEAD_1 + "0", f" {HEAD_1}", 40]
    )
    async def test_malformed_head_sha_fails_before_any_request(
        self, session, forge, head_sha
    ) -> None:
        with pytest.raises(ToolFailure) as caught:
            await tools.approve_mr(session, REPO, 7, head_sha)
        assert caught.value.message == tools.HEAD_SHA_MALFORMED
        assert "get_mr" in caught.value.message
        assert forge.requests == []

    @pytest.mark.asyncio
    async def test_fails_when_head_moved_since_get_mr(self, session, forge) -> None:
        seen = (await tools.get_mr(session, REPO, 7))["head_sha"]
        forge.head = HEAD_2
        with pytest.raises(ToolFailure) as caught:
            await tools.approve_mr(session, REPO, 7, seen)
        assert caught.value.message == f"approve_mr failed: {tools.HEAD_SHA_STALE}"
        assert "Call get_mr again" in caught.value.message
        assert forge.reviews_posted == []

    @pytest.mark.asyncio
    async def test_fails_when_head_moves_during_the_approval(
        self, session, forge
    ) -> None:
        """The shared service checks the revision again right before writing."""
        seen = (await tools.get_mr(session, REPO, 7))["head_sha"]
        forge.next_heads = [HEAD_1, HEAD_2]
        with pytest.raises(ToolFailure) as caught:
            await tools.approve_mr(session, REPO, 7, seen)
        assert caught.value.message == f"approve_mr failed: {tools.HEAD_SHA_STALE}"
        assert forge.reviews_posted == []

    @pytest.mark.asyncio
    async def test_approves_the_current_head(self, session, forge) -> None:
        seen = (await tools.get_mr(session, REPO, 7))["head_sha"]
        result = await tools.approve_mr(session, REPO, 7, seen.upper())
        assert result == "MR approved successfully"
        assert forge.reviews_posted == [
            {"event": "APPROVE", "body": "", "commit_id": HEAD_1}
        ]
        assert forge.count("POST", REVIEWS_PATH) == 1

    @pytest.mark.asyncio
    async def test_unconfirmed_approval_says_to_check(self, session, forge) -> None:
        forge.errors[REVIEWS_PATH] = httpx.ReadTimeout("timed out zqSECRETzq")
        with pytest.raises(ToolFailure) as caught:
            await tools.approve_mr(session, REPO, 7, HEAD_1)
        assert "did not confirm the approval" in caught.value.message
        assert "SECRET" not in caught.value.message


def _github_file(name: str, **fields: object) -> dict:
    return {"filename": name, "status": "modified", **fields}


class TestGetMrDiff:
    @pytest.mark.asyncio
    async def test_every_file_appears_with_a_marker_when_it_has_no_patch(
        self, session, forge
    ) -> None:
        forge.files = [
            _github_file(
                "src/app.py",
                additions=1,
                deletions=1,
                changes=2,
                patch="@@ -1 +1 @@\n-old\n+new",
            ),
            _github_file(
                "logo.png", status="added", additions=0, deletions=0, changes=0
            ),
            _github_file(
                "src/new_name.py",
                status="renamed",
                previous_filename="src/old_name.py",
                additions=0,
                deletions=0,
                changes=0,
            ),
            _github_file(
                "data/huge.sql", additions=90_000, deletions=0, changes=90_000
            ),
        ]
        result = await tools.get_mr_diff(session, REPO, 7)
        blocks = result.split("--- a/")[1:]
        assert len(blocks) == 4
        assert blocks[0].startswith("src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@")
        assert blocks[1] == "logo.png\n+++ b/logo.png\n[binary]\n"
        assert blocks[2] == ("src/old_name.py\n+++ b/src/new_name.py\n[renamed]\n")
        assert blocks[3] == "data/huge.sql\n+++ b/data/huge.sql\n[truncated]"
        assert "left out" not in result

    @pytest.mark.asyncio
    async def test_stops_at_the_cap_with_a_note(self, session, forge) -> None:
        line = "+" + "x" * 1000
        patch = "@@ -0,0 +1,100 @@\n" + "\n".join([line] * 100)
        forge.files = [
            _github_file(
                f"gen/file_{index:02d}.txt",
                status="added",
                additions=100,
                deletions=0,
                changes=100,
                patch=patch,
            )
            for index in range(5)
        ]
        result = await tools.get_mr_diff(session, REPO, 7)
        assert len(result.encode()) <= tools.MAX_DIFF_BYTES
        shown = [
            name for name in range(5) if f"+++ b/gen/file_{name:02d}.txt" in result
        ]
        # Two whole files fit, the third is cut short, the last two are left out.
        assert shown == [0, 1, 2]
        assert result.endswith(
            "[truncated]\n[diff stopped at the 256 KiB cap: 2 of 5 files left out]"
        )

    @pytest.mark.asyncio
    async def test_forge_overflow_is_noted(self, session, forge) -> None:
        forge.changed_files = 3001
        result = await tools.get_mr_diff(session, REPO, 7)
        assert result.startswith("--- a/src/app.py")
        assert result.endswith(
            "[overflow] The forge capped the changed-file list, so files past "
            "its limit are missing from this diff."
        )

    def test_existing_headers_are_not_repeated(self) -> None:
        changes = [
            {"old_path": "old.py", "new_path": "new.py", "diff": "@@ -1 +1 @@\n-a\n+b"},
            {
                "old_path": "same.py",
                "new_path": "same.py",
                "diff": "--- a/same.py\n+++ b/same.py\n@@ -1 +1 @@\n-a\n+b",
            },
        ]
        result = tools.render_diff(changes, convert_forge_changes(changes))
        assert result.startswith("--- a/old.py\n+++ b/new.py\n@@")
        assert result.count("--- a/same.py") == 1

    def test_paths_stay_on_their_header_line(self) -> None:
        changes = [{"new_path": "a\n+++ b/evil", "old_path": "a", "diff": ""}]
        result = tools.render_diff(changes, convert_forge_changes(changes))
        assert result.splitlines()[1] == "+++ b/a?+++ b/evil"

    def test_a_file_too_big_for_the_cap_is_left_out(self) -> None:
        changes = [
            {"old_path": "a.txt", "new_path": "a.txt", "diff": "@@ -1 +1 @@\n-a\n+b"}
        ]
        result = tools.render_diff(
            changes, convert_forge_changes(changes), max_bytes=520
        )
        assert result == "[diff stopped at the 0 KiB cap: 1 of 1 file left out]"


class TestListMrs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["all", "OPEN", "opened", "", None])
    async def test_rejects_unknown_states(self, session, forge, state) -> None:
        with pytest.raises(ToolFailure) as caught:
            await tools.list_mrs(session, REPO, state)
        assert caught.value.message == "state must be one of: open, closed, merged."
        assert forge.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, 101, -1, True, 2.5, "10"])
    async def test_rejects_limits_out_of_range(self, session, forge, limit) -> None:
        with pytest.raises(ToolFailure) as caught:
            await tools.list_mrs(session, REPO, "open", limit)
        assert caught.value.message == "limit must be an integer from 1 to 100."
        assert forge.requests == []

    @pytest.mark.asyncio
    async def test_returns_at_most_limit_items_from_one_page(
        self, session, forge
    ) -> None:
        forge.pulls = [pull(number) for number in range(1, 51)]
        await opened(session, forge)
        result = await tools.list_mrs(session, REPO, "open", 5)
        assert [item["number"] for item in result] == [50, 49, 48, 47, 46]
        assert forge.count("GET", LIST_PATH) == 1
        assert list_reads(forge, 5) == 1

    @pytest.mark.asyncio
    async def test_default_limit_is_30(self, session, forge) -> None:
        forge.pulls = [pull(number) for number in range(1, 51)]
        await opened(session, forge)
        result = await tools.list_mrs(session, REPO)
        assert len(result) == 30
        assert forge.count("GET", LIST_PATH) == 1
        assert list_reads(forge, 30) == 1

    @pytest.mark.asyncio
    async def test_ci_is_read_for_the_returned_rows_only(self, session, forge) -> None:
        forge.pulls = [pull(number) for number in range(1, 51)]
        await opened(session, forge)
        result = await tools.list_mrs(session, REPO, "open", 5)
        assert [item["ci_status"] for item in result] == ["success"] * 5
        checked = {
            request.url.path.split("/")[5]
            for request in forge.requests
            if request.url.path.endswith("/check-runs")
        }
        assert checked == {f"{number:040x}" for number in range(46, 51)}
        # One GraphQL batch, then check runs and statuses for each row.
        assert ci_reads(forge) <= 1 + 2 * 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["closed", "merged"])
    async def test_closed_states_skip_ci(self, session, forge, state) -> None:
        forge.pulls = [
            pull(1, state="closed", merged=True),
            pull(2, state="closed"),
            pull(3),
        ]
        await opened(session, forge)
        result = await tools.list_mrs(session, REPO, state)
        assert {item["ci_status"] for item in result} == {"unknown"}
        assert ci_reads(forge) == 0

    @pytest.mark.asyncio
    async def test_github_merged_reads_closed_and_keeps_merged(
        self, session, forge
    ) -> None:
        forge.pulls = [pull(1, state="closed", merged=True), pull(2, state="closed")]
        await opened(session, forge)
        merged = await tools.list_mrs(session, REPO, "merged")
        closed = await tools.list_mrs(session, REPO, "closed")
        assert [item["number"] for item in merged] == [1]
        assert [item["number"] for item in closed] == [2, 1]
        states = {
            request.url.params["state"]
            for request in forge.requests
            if request.url.path == LIST_PATH
        }
        assert states == {"closed"}

    @pytest.mark.asyncio
    async def test_row_keys_are_unchanged(self, session, forge) -> None:
        result = await tools.list_mrs(session, REPO, "open", 1)
        assert set(result[0]) == {
            "number",
            "title",
            "author",
            "source_branch",
            "target_branch",
            "ci_status",
            "web_url",
        }


class TestErrors:
    @pytest.mark.asyncio
    async def test_forge_failure_is_the_shared_sanitised_message(
        self, session, forge
    ) -> None:
        forge.overrides["/repos/acme/repo/pulls/7"] = httpx.Response(
            500, text="internal zqSECRETzq https://user:zqSECRETzq@github.com"
        )
        await opened(session, forge)
        with pytest.raises(ToolFailure) as caught:
            await tools.get_mr(session, REPO, 7)
        assert caught.value.message == "get_mr failed: The operation failed."
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True

    @pytest.mark.asyncio
    async def test_list_failure_is_the_shared_sanitised_message(
        self, session, forge
    ) -> None:
        await opened(session, forge)
        forge.overrides[LIST_PATH] = httpx.Response(
            401, json={"message": "Bad credentials zqSECRETzq"}
        )
        with pytest.raises(ToolFailure) as caught:
            await tools.list_mrs(session, REPO, "closed")
        assert caught.value.message == (
            "list_mrs failed: Forge authentication is unavailable."
        )

    @pytest.mark.asyncio
    async def test_not_found_repository(self, session, forge) -> None:
        with pytest.raises(ToolFailure) as caught:
            await tools.get_mr(session, "github.com/acme/missing", 7)
        assert caught.value.message == (
            "get_mr failed: The requested forge resource was not found."
        )

    @pytest.mark.asyncio
    async def test_number_must_be_positive(self, session, forge) -> None:
        for number in (0, -3, True):
            with pytest.raises(ToolFailure, match="number must be a positive"):
                await tools.get_mr(session, REPO, number)
        assert forge.requests == []


class TestRequestBounds:
    @pytest.mark.asyncio
    async def test_concurrent_calls_share_the_host_limit(self, forge, tmp_path) -> None:
        """Every request, CI lookups included, waits for a per-host slot."""
        bounded = await make_session(tmp_path, max_parallel=4).start()
        forge.pulls = [pull(number) for number in range(1, 51)]
        forge.delay = 0.002
        try:
            first, second = await asyncio.gather(
                tools.list_mrs(bounded, REPO, "open", 30),
                tools.list_mrs(bounded, REPO, "open", 30),
            )
        finally:
            await bounded.close()
        assert [len(first), len(second)] == [30, 30]
        assert forge.peak == 4

    @pytest.mark.asyncio
    async def test_request_error_is_redacted(self, session, forge) -> None:
        forge.errors[LIST_PATH] = httpx.ConnectError("connect to zqSECRETzq failed")
        with pytest.raises(ToolFailure) as caught:
            await tools.list_mrs(session, REPO)
        assert "SECRET" not in caught.value.message
        assert caught.value.message == (
            "list_mrs failed: The forge could not be reached."
        )


class TestPostComment:
    @pytest.mark.asyncio
    async def test_posts_through_the_shared_service(self, session, forge) -> None:
        result = await tools.post_comment(session, REPO, 7, "Looks good")
        assert result == "Comment posted successfully"
        assert forge.comments_posted == [{"body": "Looks good"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", ["", "x" * 65_537])
    async def test_rejects_bodies_before_any_request(
        self, session, forge, body
    ) -> None:
        with pytest.raises(ToolFailure, match="body must be non-empty"):
            await tools.post_comment(session, REPO, 7, body)
        assert forge.requests == []


class TestListPipelines:
    @pytest.mark.asyncio
    async def test_reads_through_the_session(self, session, forge) -> None:
        result = await tools.list_pipelines(session, REPO, 7)
        assert result == [
            {
                "id": 77,
                "status": "success",
                "ref": "feature",
                "sha": HEAD_1[:7],
                "source": "pull_request",
                "duration_seconds": 300,
                "web_url": "https://github.com/acme/repo/actions/runs/77",
            }
        ]
        runs = [
            request
            for request in forge.requests
            if request.url.path == "/repos/acme/repo/actions/runs"
        ]
        assert [request.url.params["per_page"] for request in runs] == ["20"]


class TestSessionHolder:
    @pytest.mark.asyncio
    async def test_starts_once_and_closes(self, tmp_path) -> None:
        made: list[ApplicationSession] = []

        def factory() -> ApplicationSession:
            made.append(make_session(tmp_path))
            return made[-1]

        holder = tools.SessionHolder(factory)
        first = await holder.get()
        assert await holder.get() is first
        assert len(made) == 1
        await holder.close()
        assert holder.current is None
        with pytest.raises(ServiceError, match="closed"):
            first.issued_repositories  # noqa: B018

    @pytest.mark.asyncio
    async def test_close_after_failed_submission_cleanup_closes_every_store(
        self, tmp_path
    ) -> None:
        # A store left open keeps its aiosqlite worker thread, which is not a
        # daemon thread, so tongs-mcp would not exit after it stops (#442).
        before = set(threading.enumerate())
        holder = tools.SessionHolder(lambda: make_session(tmp_path))
        session = await holder.get()
        workers = [
            thread
            for thread in set(threading.enumerate()) - before
            if "_connection_worker_thread" in thread.name
        ]
        assert len(workers) == 2
        session.review_submissions.close = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("review submission tasks did not stop")
        )

        with pytest.raises(ServiceError) as raised:
            await holder.close()

        assert raised.value.code is ServiceErrorCode.SHUTDOWN_FAILED
        assert holder.current is None
        for thread in workers:
            await asyncio.to_thread(thread.join, 5)
        assert [thread.is_alive() for thread in workers] == [False, False]

    @pytest.mark.asyncio
    async def test_failed_start_is_retried(self, tmp_path) -> None:
        def broken() -> ApplicationSession:
            return ApplicationSession(
                config_loader=lambda _path: (_ for _ in ()).throw(
                    ValueError("bad zqSECRETzq")
                ),
                cache_path=tmp_path / "cache.db",
                draft_db_path=tmp_path / "drafts.db",
            )

        holder = tools.SessionHolder(broken)
        with pytest.raises(ToolFailure) as caught:
            await tools.open_session(holder, "get_mr")
        assert "SECRET" not in caught.value.message
        assert holder.current is None
        with pytest.raises(ServiceError):
            await holder.get()
