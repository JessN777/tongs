"""The shared review file read service (#381)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from tongs.config import Config
from tongs.errors import (
    AuthError,
    ContentTooLargeError,
    NotFoundError,
    RateLimitError,
    UnsupportedFileReadError,
)
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import CIStatus, ForgeHost, MRState, MRSummary, User
from tongs.scanner.repo import ForgeType
from tongs.services import (
    EXPAND_LIMITS,
    GENERATE_LIMITS,
    ApplicationSession,
    FileRead,
    FileReadLimits,
    FileReadStatus,
    FileText,
    RepositoryRef,
    ReviewFileService,
    ReviewRef,
    ServiceError,
    ServiceErrorCode,
)
from tongs.services.review_files import decode_file_text
from tongs.tui_services import TUIServiceAdapter

SHA = "0123456789abcdef0123456789abcdef01234567"
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
REVIEW = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 7)


class FileClient:
    """A duck-typed forge client that serves files and counts reads."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}
        self.calls: list[tuple[str, str, str, int]] = []
        self.error: Exception | None = None

    async def get_file_at_ref(
        self, repo_path: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes:
        self.calls.append((repo_path, path, ref, max_bytes))
        if self.error is not None:
            raise self.error
        try:
            body = self.files[path]
        except KeyError:
            raise NotFoundError("Not found") from None
        if len(body) > max_bytes:
            raise ContentTooLargeError(max_bytes)
        return body

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        return []

    async def close(self) -> None:
        return None


def service_for(
    client: object, *, memory_cache_bytes: int = 32 * 1024 * 1024
) -> ReviewFileService:
    async def get_client(_review: ReviewRef, _operation: str) -> ForgeClient:
        return client  # type: ignore[return-value]

    return ReviewFileService(
        get_client=get_client, memory_cache_bytes=memory_cache_bytes
    )


async def read(
    client: object, path: str = "a.py", ref: str = SHA, **kwargs: FileReadLimits
) -> FileRead:
    return await service_for(client).read_file(REVIEW, path, ref, **kwargs)


class TestInputValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ref",
        ["main", "abc", SHA.upper(), SHA[:-1], SHA + "0", "g" * 40, "", None],
    )
    async def test_invalid_commit_makes_no_request(self, ref: object) -> None:
        client = FileClient({"a.py": b"x"})
        with pytest.raises(ServiceError) as caught:
            await read(client, ref=ref)  # type: ignore[arg-type]
        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert client.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/abs",
            "../x",
            "a/../b",
            "a/./b",
            "./a",
            "a//b",
            "a/",
            ".",
            "..",
            "a\x00b",
            "a\nb",
            "a\x1fb",
            "a\x7fb",
            "a\ud800b",
            "x" * 4097,
            "ü" * 2049,
            None,
        ],
    )
    async def test_invalid_path_makes_no_request(self, path: object) -> None:
        client = FileClient({"a.py": b"x"})
        with pytest.raises(ServiceError) as caught:
            await read(client, path=path)  # type: ignore[arg-type]
        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert client.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        ["a.py", "src/a b/ü#?%.py", ".github/workflows/ci.yml", "a..b", "x" * 4096],
    )
    async def test_valid_paths_are_read(self, path: str) -> None:
        client = FileClient({path: b"x\n"})
        result = await read(client, path=path)
        assert result.status is FileReadStatus.OK
        assert client.calls == [("acme/widgets", path, SHA, EXPAND_LIMITS.max_bytes)]

    @pytest.mark.asyncio
    async def test_sha256_commits_are_accepted(self) -> None:
        client = FileClient({"a.py": b"x\n"})
        result = await read(client, ref="ab" * 32)
        assert result.status is FileReadStatus.OK


class TestDecoding:
    @pytest.mark.asyncio
    async def test_lines_and_final_newline(self) -> None:
        result = await read(FileClient({"a.py": b"one\ntwo\n"}))
        assert result == FileRead(
            FileReadStatus.OK, FileText(("one", "two"), True, 8), ""
        )

    def test_missing_final_newline_is_recorded(self) -> None:
        assert decode_file_text(b"one\ntwo") == FileText(("one", "two"), False, 7)

    def test_empty_file_has_no_lines(self) -> None:
        assert decode_file_text(b"") == FileText((), True, 0)

    def test_blank_last_line_is_kept(self) -> None:
        assert decode_file_text(b"a\n\n") == FileText(("a", ""), True, 3)

    def test_carriage_returns_stay_in_the_content(self) -> None:
        assert decode_file_text(b"a\r\nb\r\n").lines == ("a\r", "b\r")
        assert decode_file_text(b"a\rb\n").lines == ("a\rb",)

    def test_unicode_line_separators_do_not_split(self) -> None:
        text = "a b c\x85d\x0ce\n".encode()
        assert decode_file_text(text).lines == ("a b c\x85d\x0ce",)

    def test_invalid_utf8_is_replaced(self) -> None:
        assert decode_file_text(b"caf\xe9\n").lines == ("caf�",)

    @pytest.mark.asyncio
    async def test_nul_near_the_start_is_binary(self) -> None:
        result = await read(FileClient({"a.bin": b"PK\x03\x04\x00rest"}), "a.bin")
        assert result == FileRead(
            FileReadStatus.BINARY, None, "This file is binary at this commit."
        )

    @pytest.mark.asyncio
    async def test_nul_after_the_sniff_window_is_text(self) -> None:
        body = b"a" * 8000 + b"\x00\n"
        result = await read(FileClient({"a.txt": body}), "a.txt")
        assert result.status is FileReadStatus.OK

    @pytest.mark.asyncio
    async def test_nul_at_the_end_of_the_sniff_window_is_binary(self) -> None:
        body = b"a" * 7999 + b"\x00\n"
        result = await read(FileClient({"a.txt": body}), "a.txt")
        assert result.status is FileReadStatus.BINARY


class TestOutcomes:
    @pytest.mark.asyncio
    async def test_too_many_bytes(self) -> None:
        body = b"x" * (EXPAND_LIMITS.max_bytes + 1)
        result = await read(FileClient({"a.py": body}))
        assert result == FileRead(
            FileReadStatus.TOO_LARGE,
            None,
            "File too large to expand (over 2 MiB or 20,000 lines).",
        )

    @pytest.mark.asyncio
    async def test_too_many_lines(self) -> None:
        body = b"\n" * (EXPAND_LIMITS.max_lines + 1)
        result = await read(FileClient({"a.py": body}))
        assert result.status is FileReadStatus.TOO_LARGE
        assert result.text is None

    @pytest.mark.asyncio
    async def test_line_limit_is_inclusive(self) -> None:
        body = b"\n" * EXPAND_LIMITS.max_lines
        result = await read(FileClient({"a.py": body}))
        assert result.status is FileReadStatus.OK

    @pytest.mark.asyncio
    async def test_generation_limits_reach_the_forge(self) -> None:
        client = FileClient({"a.py": b"x" * (3 * 1024 * 1024)})
        result = await read(client, limits=GENERATE_LIMITS)
        assert result.status is FileReadStatus.OK
        assert client.calls[0][3] == GENERATE_LIMITS.max_bytes

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        result = await read(FileClient())
        assert result == FileRead(
            FileReadStatus.NOT_FOUND,
            None,
            "The forge has no copy of this file at this commit. Refresh the review.",
        )

    @pytest.mark.asyncio
    async def test_client_reporting_unsupported(self) -> None:
        client = FileClient()
        client.error = UnsupportedFileReadError()
        result = await read(client)
        assert result == FileRead(
            FileReadStatus.UNSUPPORTED, None, "This forge can't read files at a commit."
        )

    @pytest.mark.asyncio
    async def test_duck_typed_client_without_reads_is_unsupported(self) -> None:
        result = await read(object())
        assert result.status is FileReadStatus.UNSUPPORTED

    @pytest.mark.asyncio
    async def test_other_failures_raise_a_service_error(self) -> None:
        client = FileClient()
        client.error = AuthError("Authentication failed: ghp_secret123")
        with pytest.raises(ServiceError) as caught:
            await read(client)
        assert caught.value.code is ServiceErrorCode.AUTHENTICATION_FAILED
        assert "ghp_secret123" not in str(caught.value)

    @pytest.mark.asyncio
    async def test_non_bytes_body_is_an_invalid_response(self) -> None:
        class TextClient:
            async def get_file_at_ref(
                self, repo_path: str, path: str, ref: str, *, max_bytes: int
            ) -> str:
                return "not bytes"

        with pytest.raises(ServiceError) as caught:
            await read(TextClient())
        assert caught.value.code is ServiceErrorCode.INVALID_RESPONSE

    def test_limits_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            FileReadLimits(0, 10)
        with pytest.raises(ValueError):
            FileReadLimits(10, 0)


class TestMemoryCache:
    @pytest.mark.asyncio
    async def test_second_read_is_served_from_memory(self) -> None:
        client = FileClient({"a.py": b"x\n"})
        service = service_for(client)
        first = await service.read_file(REVIEW, "a.py", SHA)
        second = await service.read_file(REVIEW, "a.py", SHA)
        assert first == second
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_cached_file_is_checked_against_smaller_limits(self) -> None:
        client = FileClient({"a.py": b"x" * (3 * 1024 * 1024)})
        service = service_for(client)
        generated = await service.read_file(REVIEW, "a.py", SHA, limits=GENERATE_LIMITS)
        assert generated.status is FileReadStatus.OK
        expanded = await service.read_file(REVIEW, "a.py", SHA)
        assert expanded.status is FileReadStatus.TOO_LARGE
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_failures_are_not_kept(self) -> None:
        client = FileClient()
        service = service_for(client)
        for _ in range(2):
            result = await service.read_file(REVIEW, "a.py", SHA)
            assert result.status is FileReadStatus.NOT_FOUND
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_least_recently_used_file_is_evicted_past_the_budget(self) -> None:
        client = FileClient({"a": b"a" * 600, "b": b"b" * 600, "c": b"c" * 600})
        service = service_for(client, memory_cache_bytes=2000)
        await service.read_file(REVIEW, "a", SHA)
        await service.read_file(REVIEW, "b", SHA)
        await service.read_file(REVIEW, "a", SHA)  # a is now the most recent
        await service.read_file(REVIEW, "c", SHA)  # evicts b
        await service.read_file(REVIEW, "a", SHA)
        await service.read_file(REVIEW, "b", SHA)
        assert [call[1] for call in client.calls] == ["a", "b", "c", "b"]

    @pytest.mark.asyncio
    async def test_each_line_counts_against_the_budget(self) -> None:
        # Both bodies are 1,000 bytes; the second is 1,000 lines, whose line
        # strings cost far more memory than the body.
        client = FileClient({"one": b"x" * 1000, "many": b"\n" * 1000})
        service = service_for(client, memory_cache_bytes=10_000)
        for path in ("one", "many", "one", "many"):
            result = await service.read_file(REVIEW, path, SHA)
            assert result.status is FileReadStatus.OK
        assert [call[1] for call in client.calls] == ["one", "many", "many"]

    @pytest.mark.asyncio
    async def test_clear_forgets_files(self) -> None:
        client = FileClient({"a.py": b"x\n"})
        service = service_for(client)
        await service.read_file(REVIEW, "a.py", SHA)
        service.clear()
        await service.read_file(REVIEW, "a.py", SHA)
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_files_are_keyed_by_repository_commit_and_path(self) -> None:
        client = FileClient({"a.py": b"x\n", "A.py": b"y\n"})
        service = service_for(client)
        other_review = ReviewRef(RepositoryRef("github.com", "acme/other"), 7)
        await service.read_file(REVIEW, "a.py", SHA)
        await service.read_file(REVIEW, "A.py", SHA)
        await service.read_file(REVIEW, "a.py", "f" * 40)
        await service.read_file(other_review, "a.py", SHA)
        assert len(client.calls) == 4


def _http(handler, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


class TestRealForgeClients:
    @pytest.mark.asyncio
    async def test_github_redirect_to_another_host_is_unsupported(self) -> None:
        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            return httpx.Response(
                302, headers={"Location": "https://objects.example.net/blob"}
            )

        async with _http(handler, GITHUB_HOST.api_base) as http_client:
            result = await read(GitHubClient(GITHUB_HOST, http_client))
        assert result.status is FileReadStatus.UNSUPPORTED
        assert hosts == ["api.github.com"]

    @pytest.mark.asyncio
    async def test_gitlab_404_is_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "404 File Not Found"})

        async with _http(handler, GITLAB_HOST.api_base) as http_client:
            result = await read(GitLabClient(GITLAB_HOST, http_client))
        assert result.status is FileReadStatus.NOT_FOUND

    @pytest.mark.asyncio
    async def test_github_content_length_over_the_limit_is_too_large(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": str(EXPAND_LIMITS.max_bytes + 1)}
            )

        async with _http(handler, GITHUB_HOST.api_base) as http_client:
            result = await read(GitHubClient(GITHUB_HOST, http_client))
        assert result.status is FileReadStatus.TOO_LARGE

    @pytest.mark.asyncio
    async def test_gitlab_crlf_file_round_trips(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"one\r\ntwo")

        async with _http(handler, GITLAB_HOST.api_base) as http_client:
            result = await read(GitLabClient(GITLAB_HOST, http_client))
        assert result.text == FileText(("one\r", "two"), False, 8)


def _summary() -> MRSummary:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    return MRSummary(
        forge_host=GITHUB_HOST,
        repo_path="acme/widgets",
        local_path="",
        number=7,
        title="Review widgets",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=now,
        updated_at=now,
        web_url="https://github.com/acme/widgets/pull/7",
    )


class FakeRegistry:
    def __init__(self, client: object) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return ["github.com"]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return GITHUB_HOST if hostname == "github.com" else None

    async def get_client(self, hostname: str) -> ForgeClient:
        return self.client  # type: ignore[return-value]

    async def close_all(self) -> None:
        return None


class FakeCache:
    async def open(self) -> None:
        return None

    async def clear(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def _session(tmp_path: Path, client: object) -> ApplicationSession:
    return await ApplicationSession(
        config=Config(),
        cache=FakeCache(),
        draft_db_path=tmp_path / "drafts.db",
        forge_registry=FakeRegistry(client),
    ).start()


class TestSessionWiring:
    @pytest.mark.asyncio
    async def test_session_reads_files_for_issued_reviews(self, tmp_path: Path) -> None:
        client = FileClient({"a.py": b"x\n"})
        session = await _session(tmp_path, client)
        try:
            repository = await session.open_repository("github.com", "acme/widgets")
            result = await session.review_files.read_file(
                ReviewRef(repository.ref, 7), "a.py", SHA
            )
            assert result.status is FileReadStatus.OK
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_unissued_repository_is_refused(self, tmp_path: Path) -> None:
        client = FileClient({"a.py": b"x\n"})
        session = await _session(tmp_path, client)
        try:
            with pytest.raises(ServiceError) as caught:
                await session.review_files.read_file(REVIEW, "a.py", SHA)
            assert caught.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED
            assert client.calls == []
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_rate_limit_is_recorded_for_the_host(self, tmp_path: Path) -> None:
        client = FileClient()
        client.error = RateLimitError("Rate limited", retry_after=30)
        session = await _session(tmp_path, client)
        try:
            repository = await session.open_repository("github.com", "acme/widgets")
            with pytest.raises(ServiceError) as caught:
                await session.review_files.read_file(
                    ReviewRef(repository.ref, 7), "a.py", SHA
                )
            assert caught.value.code is ServiceErrorCode.RATE_LIMITED
            assert session.host_rate_limit("github.com") is not None
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_clear_cache_and_close_forget_files(self, tmp_path: Path) -> None:
        client = FileClient({"a.py": b"x\n"})
        session = await _session(tmp_path, client)
        repository = await session.open_repository("github.com", "acme/widgets")
        review = ReviewRef(repository.ref, 7)
        await session.review_files.read_file(review, "a.py", SHA)
        await session.clear_cache()
        await session.review_files.read_file(review, "a.py", SHA)
        assert len(client.calls) == 2
        await session.close()
        with pytest.raises(ServiceError) as caught:
            await session.review_files.read_file(review, "a.py", SHA)
        assert caught.value.code is ServiceErrorCode.CLOSED

    @pytest.mark.asyncio
    async def test_tui_adapter_reads_with_expansion_limits(
        self, tmp_path: Path
    ) -> None:
        client = FileClient({"src/a.py": b"one\ntwo\n"})
        session = await _session(tmp_path, client)
        try:
            repository = await session.open_repository("github.com", "acme/widgets")
            adapter = TUIServiceAdapter(session)
            adapter._review_refs[("github.com", "acme/widgets", 7)] = ReviewRef(
                repository.ref, 7
            )
            result = await adapter.read_review_file(_summary(), "src/a.py", SHA)
            assert result.text == FileText(("one", "two"), True, 8)
            assert client.calls == [
                ("acme/widgets", "src/a.py", SHA, EXPAND_LIMITS.max_bytes)
            ]
        finally:
            await session.close()


class TestFileContentTtlWiring:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ttl", "seconds"),
        [
            (timedelta(hours=2), 7_200),
            (timedelta(days=1), 86_400),
            # A Config built in code can hold a value load_config never makes.
            (timedelta(0), 86_400),
            (timedelta(days=400), 86_400),
        ],
    )
    async def test_configured_ttl_reaches_the_registry(
        self, tmp_path: Path, ttl: timedelta, seconds: int
    ) -> None:
        captured: dict[str, object] = {}

        def make_registry(**kwargs: object) -> FakeRegistry:
            captured.update(kwargs)
            return FakeRegistry(FileClient())

        with patch("tongs.services.session.ForgeRegistry", make_registry):
            session = await ApplicationSession(
                config=Config(file_content_ttl=ttl),
                cache=FakeCache(),
                draft_db_path=tmp_path / "drafts.db",
            ).start()
        try:
            assert captured["file_content_ttl"] == seconds
        finally:
            await session.close()
