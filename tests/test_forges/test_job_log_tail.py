"""Job logs are streamed into a bounded tail instead of read in full (#314)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tongs.errors import NetworkError, NotFoundError
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.http import request_log_tail
from tongs.forges.models import ForgeHost, JobLogTail
from tongs.scanner.repo import ForgeType

_GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
_GITLAB = ForgeHost(
    "gitlab.example.com", ForgeType.GITLAB, "https://gitlab.example.com/api/v4"
)


class _ChunkedBody(httpx.AsyncByteStream):
    """A response body sent in many chunks, counting what was read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.sent += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _log_lines(count: int) -> bytes:
    return b"".join(f"line {index:05d}\n".encode() for index in range(count))


# JobLogTail.from_bytes


def test_a_log_within_the_budget_is_kept_whole() -> None:
    tail = JobLogTail.from_bytes(b"one\ntwo\n", 8, 100)

    assert tail == JobLogTail("one\ntwo\n", 8, False)


def test_a_cut_log_keeps_its_end_from_a_line_boundary() -> None:
    data = b"first line\nsecond line\nthird line\n"

    tail = JobLogTail.from_bytes(data, len(data), 20)

    assert tail.text == "third line\n"
    assert tail.truncated
    assert tail.total_bytes == len(data)


def test_a_single_long_line_is_cut_on_a_character_boundary() -> None:
    data = ("é" * 50).encode()

    tail = JobLogTail.from_bytes(data, len(data), 15)

    assert tail.truncated
    assert set(tail.text) == {"é"}
    assert len(tail.text.encode()) <= 15


def test_replacement_characters_never_take_the_text_past_the_budget() -> None:
    # Each invalid byte decodes to U+FFFD, three UTF-8 bytes.
    data = b"\xff" * 30

    tail = JobLogTail.from_bytes(data, len(data), 30)

    assert tail.truncated
    assert len(tail.text.encode()) <= 30


def test_a_tail_of_a_longer_stream_is_marked_truncated() -> None:
    tail = JobLogTail.from_bytes(b"abc\nlast\n", 1000, 9)

    assert tail.truncated
    assert tail.total_bytes == 1000
    assert tail.text == "last\n"


# request_log_tail


@pytest.mark.asyncio
async def test_a_large_log_streams_into_a_bounded_tail() -> None:
    data = _log_lines(20_000)
    body = _ChunkedBody([data[i : i + 997] for i in range(0, len(data), 997)])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://forge.test"
    ) as client:
        tail = await request_log_tail(client, "/log", max_bytes=1000)

    assert body.sent == len(data)
    assert body.closed
    assert tail.truncated
    assert tail.total_bytes == len(data)
    assert len(tail.text.encode()) <= 1000
    assert tail.text.endswith("line 19999\n")
    assert tail.text.startswith("line ")


@pytest.mark.asyncio
async def test_an_error_status_maps_from_a_bounded_body() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://forge.test"
    ) as client:
        with pytest.raises(NotFoundError):
            await request_log_tail(client, "/log", max_bytes=1000)


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_network_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://forge.test"
    ) as client:
        with pytest.raises(NetworkError):
            await request_log_tail(client, "/log", max_bytes=1000)


@pytest.mark.asyncio
async def test_a_declared_charset_decodes_the_tail() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="café\n".encode("latin-1"),
            headers={"Content-Type": "text/plain; charset=latin-1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://forge.test"
    ) as client:
        tail = await request_log_tail(client, "/log", max_bytes=1000)

    assert tail == JobLogTail("café\n", 5, False)


# Forge clients


@pytest.mark.asyncio
async def test_github_streams_the_job_log_after_its_redirect() -> None:
    data = _log_lines(5_000)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": "https://logs.example.com/blob"}
            )
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_GITHUB.api_base,
        follow_redirects=True,
    ) as http:
        tail = await GitHubClient(_GITHUB, http).get_job_log_tail(
            "acme/widgets", 42, 200
        )

    assert paths == [
        "api.github.com/repos/acme/widgets/actions/jobs/42/logs",
        "logs.example.com/blob",
    ]
    assert tail.truncated
    assert tail.total_bytes == len(data)
    assert tail.text.endswith("line 04999\n")
    assert len(tail.text.encode()) <= 200


@pytest.mark.asyncio
async def test_gitlab_streams_the_job_trace() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, content=b"short trace\n")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=_GITLAB.api_base
    ) as http:
        tail = await GitLabClient(_GITLAB, http).get_job_log_tail(
            "group/project", 7, 1000
        )

    assert paths == ["/api/v4/projects/group%2Fproject/jobs/7/trace"]
    assert tail == JobLogTail("short trace\n", 12, False)


class _WholeLogClient(GitLabClient):
    """A client that only reads whole logs, as a third-party one may."""

    get_job_log_tail = ForgeClient.get_job_log_tail

    def __init__(self, log: str) -> None:
        self._log = log

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        return self._log


@pytest.mark.asyncio
async def test_the_default_tail_cuts_a_whole_log_to_the_budget() -> None:
    tail = await _WholeLogClient("a\nb\nc\n").get_job_log_tail("group/project", 1, 4)

    assert tail == JobLogTail("b\nc\n", 6, True)


def test_a_cut_on_a_line_start_keeps_that_line() -> None:
    data = b"aaaa\nbbbb\ncccc\n"

    tail = JobLogTail.from_bytes(data, len(data), 10)

    assert tail == JobLogTail("bbbb\ncccc\n", 15, True)


@pytest.mark.asyncio
async def test_a_body_that_fails_to_decode_is_a_network_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"not gzip at all"
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://forge.test"
    ) as client:
        with pytest.raises(NetworkError):
            await request_log_tail(client, "/log", max_bytes=1000)
