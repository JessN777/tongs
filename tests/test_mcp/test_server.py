"""Tests for the registered tongs-mcp tools and the server lifecycle.

This file runs in the Fedora installed-wheel smoke subset, so it imports only
tongs and the MCP SDK. The tool logic, with a fake forge, is tested through
the plain functions in ``test_tools.py``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NoReturn
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tongs.config import Config, HostConfig
from tongs.errors import AuthError
from tongs.forges.models import MRPage
from tongs.mcp import tools
from tongs.mcp.tools import SessionHolder
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.session import ApplicationSession

mcp_available = True
try:
    from mcp.shared.memory import create_connected_server_and_client_session

    from tongs.mcp import server
except ImportError:
    mcp_available = False

pytestmark = pytest.mark.skipif(not mcp_available, reason="mcp not installed")

UNCONFIGURED_HOST = "evil-gitlab.example"
REPO = "github.com/acme/repo"
HEAD_1 = "1" * 40


def make_session(tmp_path: Path) -> ApplicationSession:
    """A session with one extra host per forge and storage under ``tmp_path``."""
    return ApplicationSession(
        config=Config(
            extra_hosts={
                "lab": HostConfig("git.lab.corp", "gitlab"),
                "hub": HostConfig("git.hub.corp", "github"),
            }
        ),
        cache_path=tmp_path / "cache.db",
        draft_db_path=tmp_path / "drafts.db",
    )


@pytest_asyncio.fixture(autouse=True)
async def holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[SessionHolder]:
    """Give the server a session with one extra host per forge."""
    sessions = SessionHolder(lambda: make_session(tmp_path))
    monkeypatch.setattr(server, "sessions", sessions)
    yield sessions
    await sessions.close()


@pytest.fixture
def forbid_token_lookup():
    with patch(
        "tongs.forges.registry.resolve_token",
        side_effect=AssertionError("token lookup must not run"),
    ) as resolve:
        yield resolve


class TestHostAdmission:
    @pytest.mark.asyncio
    async def test_valid_paths(self, holder) -> None:
        session = await holder.get()
        parse = tools.parse_repo_path
        assert parse(session, "github.com/owner/repo").project_path == "owner/repo"
        nested = parse(session, "gitlab.com/group/subgroup/repo")
        assert (nested.hostname, nested.project_path) == (
            "gitlab.com",
            "group/subgroup/repo",
        )
        assert parse(session, "git.lab.corp/a/b/c").hostname == "git.lab.corp"
        assert parse(session, "git.hub.corp/a/b").project_path == "a/b"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("repo_path", "message"),
        [
            ("github.com/owner/repo/extra", "GitHub host"),
            ("git.hub.corp/owner/repo/extra", "GitHub host"),
            ("noslash", "repo_path must be"),
            ("", "repo_path must be"),
            ("../foo/bar", "repo_path must be"),
            ("github.com/a/../b", "repo_path must be"),
            ("github.com/a/.", "repo_path must be"),
            ("github.com/оwner/repo", "repo_path must be"),
            ("github.com/owner/repo\n", "repo_path must be"),
            ("GitHub.com/owner/repo", "not configured"),
            (f"{UNCONFIGURED_HOST}/owner/repo", "not configured"),
            ("github.com.evil.example/owner/repo", "not configured"),
        ],
    )
    async def test_rejected_paths(self, holder, repo_path, message) -> None:
        session = await holder.get()
        with pytest.raises(tools.ToolFailure, match=message):
            tools.parse_repo_path(session, repo_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_mrs", ()),
            ("get_mr", (1,)),
            ("get_mr_diff", (1,)),
            ("post_comment", (1, "hi")),
            ("approve_mr", (1, HEAD_1)),
            ("list_pipelines", (1,)),
        ],
    )
    async def test_rejects_before_token_lookup(
        self, forbid_token_lookup, holder, tool, args
    ) -> None:
        """Every tool, including the write tools, rejects before credentials."""
        with (
            patch(
                "tongs.forges.registry.create_client",
                side_effect=AssertionError("no HTTP client may be built"),
            ),
            pytest.raises(server.ToolError, match="not configured"),
        ):
            await getattr(server, tool)(f"{UNCONFIGURED_HOST}/owner/repo", *args)
        forbid_token_lookup.assert_not_called()
        session = holder.current
        assert session is not None
        assert session.forge_registry._clients == {}

    @pytest.mark.asyncio
    async def test_configured_extra_host_reaches_client(self) -> None:
        """A configured self-hosted GitLab still gets a client and a request."""
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.gitlab.GitLabClient.list_mrs_page",
                return_value=MRPage((), has_next=False),
            ) as list_page,
        ):
            result = await server.list_mrs("git.lab.corp/group/sub/app")
        assert result == []
        assert {call.args[0] for call in list_page.await_args_list} == {"group/sub/app"}


TOOL_CALLS = [
    ("list_mrs", {}),
    ("get_mr", {"number": 1}),
    ("get_mr_diff", {"number": 1}),
    ("post_comment", {"number": 1, "body": "hi"}),
    ("approve_mr", {"number": 1, "head_sha": HEAD_1}),
    ("list_pipelines", {"number": 1}),
]


class TestErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("tool", "args"), TOOL_CALLS)
    async def test_forge_error_text_never_reaches_client(self, tool, args) -> None:
        """Every tool replaces forge error text with the shared message."""
        secret = "https://user:zqSECRETzq@git.lab.corp token zqSECRETzq"
        with (
            patch(
                "tongs.forges.registry.resolve_token",
                side_effect=AuthError(secret),
            ),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await getattr(server, tool)("git.lab.corp/group/app", **args)
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert message == f"{tool} failed: Forge authentication is unavailable."
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    @pytest.mark.asyncio
    async def test_validation_failures_are_tool_errors(
        self, forbid_token_lookup
    ) -> None:
        with pytest.raises(server.ToolError) as excinfo:
            await server.mcp.call_tool("list_mrs", {"repo_path": REPO, "state": "all"})
        assert "state must be one of: open, closed, merged." in str(excinfo.value)
        # The schema requires head_sha, so the SDK refuses a call without it.
        with pytest.raises(server.ToolError) as excinfo:
            await server.mcp.call_tool("approve_mr", {"repo_path": REPO, "number": 7})
        assert "head_sha" in str(excinfo.value)
        with pytest.raises(server.ToolError) as excinfo:
            await server.mcp.call_tool(
                "approve_mr", {"repo_path": REPO, "number": 7, "head_sha": ""}
            )
        assert tools.HEAD_SHA_MISSING in str(excinfo.value)
        forbid_token_lookup.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_netrc_through_mcp_call(self, tmp_path) -> None:
        """A malformed .netrc reaches an MCP client without its contents."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine git.lab.corp login x zqSECRETzq-value\n")
        netrc_file.chmod(0o600)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_keyring", return_value=None),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await server.mcp.call_tool(
                "list_mrs", {"repo_path": "git.lab.corp/group/app"}
            )
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "Forge authentication is unavailable" in message

    @pytest.mark.asyncio
    async def test_session_start_failure_is_sanitised(
        self, monkeypatch, tmp_path
    ) -> None:
        def broken() -> NoReturn:
            raise ServiceError(
                ServiceErrorCode.CONFIGURATION_INVALID,
                "The Tongs configuration is invalid.",
            )

        monkeypatch.setattr(server, "sessions", SessionHolder(broken))
        with pytest.raises(server.ToolError) as excinfo:
            await server.get_mr(REPO, 7)
        assert (
            str(excinfo.value) == "get_mr failed: The Tongs configuration is invalid."
        )


class TestRegisteredTools:
    @pytest.mark.asyncio
    async def test_tool_names_and_arguments(self) -> None:
        listed = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert sorted(listed) == [
            "approve_mr",
            "get_mr",
            "get_mr_diff",
            "list_mrs",
            "list_pipelines",
            "post_comment",
        ]
        assert list(listed["list_mrs"].inputSchema["properties"]) == [
            "repo_path",
            "state",
            "limit",
        ]
        assert list(listed["approve_mr"].inputSchema["properties"]) == [
            "repo_path",
            "number",
            "head_sha",
        ]
        assert listed["approve_mr"].inputSchema["required"] == [
            "repo_path",
            "number",
            "head_sha",
        ]
        assert "get_mr" in (listed["approve_mr"].description or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_mrs", (REPO, "closed", 7)),
            ("get_mr", (REPO, 7)),
            ("get_mr_diff", (REPO, 7)),
            ("post_comment", (REPO, 7, "hi")),
            ("approve_mr", (REPO, 7, HEAD_1)),
            ("list_pipelines", (REPO, 7)),
        ],
    )
    async def test_tools_run_the_plain_functions_on_the_server_session(
        self, monkeypatch, holder, tool, args
    ) -> None:
        calls: list[tuple[object, ...]] = []

        async def recorded(session: ApplicationSession, *rest: object) -> str:
            calls.append((session, *rest))
            return "result"

        monkeypatch.setattr(tools, tool, recorded)
        assert await getattr(server, tool)(*args) == "result"
        assert calls == [(holder.current, *args)]

    @pytest.mark.asyncio
    async def test_list_mrs_defaults(self, monkeypatch, holder) -> None:
        calls: list[tuple[object, ...]] = []

        async def recorded(session: ApplicationSession, *rest: object) -> list:
            calls.append(rest)
            return []

        monkeypatch.setattr(tools, "list_mrs", recorded)
        await server.mcp.call_tool("list_mrs", {"repo_path": REPO})
        assert calls == [(REPO, "open", 30)]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_one_session_for_every_call(self, holder) -> None:
        with pytest.raises(server.ToolError):
            await server.list_mrs(REPO, "all")
        session = holder.current
        assert session is not None
        with pytest.raises(server.ToolError):
            await server.get_mr(REPO, 0)
        assert holder.current is session

    @pytest.mark.asyncio
    async def test_session_closed_on_shutdown(self, holder) -> None:
        async with create_connected_server_and_client_session(server.mcp) as client:
            result = await client.call_tool(
                "list_mrs", {"repo_path": REPO, "state": "all"}
            )
            assert result.isError is True
            session = holder.current
            assert session is not None
        assert holder.current is None
        with pytest.raises(ServiceError) as caught:
            session.issued_repositories  # noqa: B018
        assert caught.value.code is ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_shutdown_after_failed_cleanup_closes_every_store(
        self, holder, caplog
    ) -> None:
        # A store left open keeps its aiosqlite worker thread, which is not a
        # daemon thread, so tongs-mcp would not exit after it stops (#442).
        before = set(threading.enumerate())
        async with create_connected_server_and_client_session(server.mcp):
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

        assert holder.current is None
        for thread in workers:
            await asyncio.to_thread(thread.join, 5)
        assert [thread.is_alive() for thread in workers] == [False, False]
        assert "did not close cleanly" in caplog.text

    @pytest.mark.asyncio
    async def test_client_reads_tool_errors(self, holder, forbid_token_lookup) -> None:
        async with create_connected_server_and_client_session(server.mcp) as client:
            result = await client.call_tool(
                "approve_mr",
                {"repo_path": REPO, "number": 7, "head_sha": "abc1234"},
            )
        assert result.isError is True
        assert tools.HEAD_SHA_MALFORMED in result.content[0].text
        forbid_token_lookup.assert_not_called()
