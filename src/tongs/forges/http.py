"""Async HTTP transport layer for forge API calls."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from email.utils import parsedate_to_datetime

import httpx

from tongs.errors import (
    AuthError,
    ConflictError,
    ContentTooLargeError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    UnsupportedFileReadError,
    ValidationError,
    redact_credentials,
)
from tongs.forges.models import JobLogTail

log = logging.getLogger(__name__)

#: The longest rate-limit wait a request sleeps through before its one retry.
#: A forge that asks for a longer wait gets a RateLimitError straight away.
MAX_RETRY_AFTER_SECONDS = 60

#: The wait before the one retry when a rate limit names no reset time.
DEFAULT_RETRY_AFTER_SECONDS = 5

_MAX_HEADER_DIGITS = 12

# Body text of a 403 that is a rate limit even without rate-limit headers.
_RATE_LIMIT_403_MARKERS = ("secondary rate limit", "abuse detection")

# Retry sleeps go through this name so tests can observe them without waiting.
_sleep = asyncio.sleep
#: Methods safe to send twice. A rate-limited write fails at once instead of
#: sleeping and resending, so callers with their own deadline (review
#: submission) report the rate limit rather than an unknown outcome.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HostRequestLimiter:
    """Bound the forge requests in flight to one host.

    Every transport wrapped by one limiter shares its slots. A slot is taken
    when a request is sent and given back when its response stream closes, so
    a streamed download keeps its slot until it is read or closed. The wait
    for a slot has no timeout of its own; the request timeout starts once the
    slot is held.
    """

    def __init__(self, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._max_parallel = max_parallel
        self._semaphore = asyncio.Semaphore(max_parallel)

    @property
    def max_parallel(self) -> int:
        """The most requests this limiter lets run at once."""
        return self._max_parallel

    def wrap(self, transport: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        """Return ``transport`` bounded by this limiter's slots."""
        return _LimitedTransport(transport, self._semaphore)

    def install(self, client: httpx.AsyncClient) -> None:
        """Bound every transport ``client`` can send through.

        Passing ``transport=`` to the client constructor would turn off the
        proxies httpx reads from the environment, so the client is built as
        usual and its transport and proxy mounts are wrapped afterwards.
        """
        client._transport = self.wrap(client._transport)
        client._mounts = {
            pattern: None if transport is None else self.wrap(transport)
            for pattern, transport in client._mounts.items()
        }


class _LimitedTransport(httpx.AsyncBaseTransport):
    """Hold one limiter slot from send until the response stream closes."""

    def __init__(
        self, transport: httpx.AsyncBaseTransport, semaphore: asyncio.Semaphore
    ) -> None:
        self._transport = transport
        self._semaphore = semaphore

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._semaphore.acquire()
        slot = _Slot(self._semaphore)
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException:
            slot.release()
            raise
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            slot.release()
            raise TypeError("The forge transport returned a synchronous stream")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_SlotStream(stream, slot),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class _Slot:
    """Release a limiter slot exactly once."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore: asyncio.Semaphore | None = semaphore

    def release(self) -> None:
        semaphore, self._semaphore = self._semaphore, None
        if semaphore is not None:
            semaphore.release()


class _SlotStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, slot: _Slot) -> None:
        self._stream = stream
        self._slot = slot

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._slot.release()


class RefreshingTokenAuth(httpx.Auth):
    """Bearer auth that re-resolves the token once when a request gets a 401.

    Short-lived credentials (such as glab OAuth tokens) expire during long
    sessions. ``refresh`` is a blocking callable returning a new token, or
    ``None`` when no better credential is available; it runs in a worker thread.
    """

    def __init__(self, token: str, refresh: Callable[[], str | None]):
        self._token = token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    def sync_auth_flow(self, request: httpx.Request):
        raise RuntimeError("RefreshingTokenAuth only supports async clients")

    async def async_auth_flow(self, request: httpx.Request):
        sent_token = self._token
        request.headers["Authorization"] = f"Bearer {sent_token}"
        response = yield request
        if response.status_code != 401:
            return

        # Read and close the rejected response first, so it gives back its
        # host request slot before the refresh runs.
        await response.aread()
        async with self._lock:
            # Another request may already have refreshed the token.
            if self._token == sent_token:
                try:
                    new_token = await asyncio.to_thread(self._refresh)
                except ForgeError as e:
                    log.debug("Token refresh failed: %s", e)
                    new_token = None
                if not new_token or new_token == sent_token:
                    return
                log.info("Retrying request with refreshed credentials")
                self._token = new_token

        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def create_client(
    base_url: str,
    token: str,
    timeout: float = 30.0,
    refresh: Callable[[], str | None] | None = None,
    *,
    limiter: HostRequestLimiter | None = None,
) -> httpx.AsyncClient:
    """Create an authenticated async HTTP client for a forge API.

    When ``refresh`` is given, a 401 triggers one token refresh and retry.
    When ``limiter`` is given, every request the client sends, including
    redirects and the retry after a refresh, takes one of its slots.
    """
    headers = {"Accept": "application/json"}
    auth = None
    if refresh is None:
        headers["Authorization"] = f"Bearer {token}"
    else:
        auth = RefreshingTokenAuth(token, refresh)
    client = httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    )
    if limiter is not None:
        limiter.install(client)
    return client


def map_http_error(
    response: httpx.Response, *, now: Callable[[], float] = time.time
) -> ForgeError:
    """Map an HTTP error response to the appropriate ForgeError subclass.

    A 429, and a 403 that carries ``Retry-After`` or ``x-ratelimit-remaining:
    0`` or names a secondary rate limit in its body, map to RateLimitError. ``now`` returns the current Unix time and
    turns an HTTP-date ``Retry-After`` or an ``x-ratelimit-reset`` epoch into
    a wait in seconds.
    """
    status = response.status_code
    body = _safe_body(response)
    headers = httpx.Headers(response.headers)

    if status == 401:
        return AuthError(f"Authentication failed: {body}")
    if status == 403 and _is_rate_limited_403(headers, body):
        return RateLimitError(
            f"Rate limited: {body}", retry_after=_retry_after(headers, now)
        )
    if status == 403:
        return ForgePermissionError(f"Insufficient permissions: {body}")
    if status == 404:
        return NotFoundError(f"Not found: {body}")
    if status == 405:
        # GitHub's PR merge endpoint answers a merge attempt it will not
        # perform (merge conflicts, required checks unmet, etc.) with 405
        # and a reason in the body, e.g. {"message": "Pull Request has merge
        # conflicts"}. GitLab's merge endpoint uses the same status when
        # merge_request.mergeable? is false (conflicts, draft, unresolved
        # threads, a failing or pending pipeline), decided before any merge
        # is attempted. Both are a definitive, known-state rejection rather
        # than an ambiguous failure.
        return ConflictError(f"Conflict: {body}")
    if status == 406:
        # Legacy GitLab releases returned 406 on the merge endpoint itself
        # for "branch cannot be merged"; current GitLab reserves 406 for
        # cancel_merge_when_pipeline_succeeds (no auto-merge to cancel).
        # Neither case runs a mutation before returning 406, so it stays a
        # definitive, known-state rejection like 405.
        return ConflictError(f"Conflict: {body}")
    if status == 409:
        return ConflictError(f"Conflict: {body}")
    if status in {400, 422}:
        return ValidationError(f"Validation failed: {body}")
    if status == 429:
        return RateLimitError(
            f"Rate limited: {body}", retry_after=_retry_after(headers, now)
        )

    return ForgeError(f"HTTP {status}: {body}")


def _is_rate_limited_403(headers: httpx.Headers, body: str) -> bool:
    """Tell a rate-limit 403 from a permission 403.

    GitHub answers both an exhausted primary rate limit and a secondary rate
    limit with 403; the first sets ``x-ratelimit-remaining: 0`` and the second
    usually sets ``Retry-After``. A secondary limit without ``Retry-After`` is
    still named in the body ("You have exceeded a secondary rate limit").
    """
    if headers.get("Retry-After") is not None:
        return True
    remaining = headers.get("x-ratelimit-remaining")
    if remaining is not None and remaining.strip() == "0":
        return True
    text = body.lower()
    return any(marker in text for marker in _RATE_LIMIT_403_MARKERS)


def graphql_rate_limit_error(
    response: httpx.Response,
    errors: object,
    *,
    now: Callable[[], float] = time.time,
) -> RateLimitError | None:
    """Return the rate limit a GraphQL error list reports, or ``None``.

    GitHub answers a query past the GraphQL rate limit with 200 and an error
    of type ``RATE_LIMITED``; the reset is in the usual rate-limit headers.
    """
    if not isinstance(errors, list):
        return None
    for error in errors:
        if isinstance(error, dict) and error.get("type") == "RATE_LIMITED":
            return RateLimitError(
                "Rate limited: GraphQL rate limit exceeded",
                retry_after=_retry_after(httpx.Headers(response.headers), now),
            )
    return None


def _retry_after(headers: httpx.Headers, now: Callable[[], float]) -> int | None:
    """Return the wait the forge asked for in whole seconds, or ``None``.

    ``Retry-After`` is read as delta-seconds or as an HTTP-date. Without it, a
    GitHub ``x-ratelimit-reset`` epoch is used.
    """
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        value = retry_after.strip()
        if _is_bounded_digits(value):
            return int(value)
        return _seconds_until_date(value, now)
    reset = headers.get("x-ratelimit-reset")
    if reset is not None and _is_bounded_digits(reset.strip()):
        return max(0, math.ceil(int(reset.strip()) - now()))
    return None


def _seconds_until_date(value: str, now: Callable[[], float]) -> int | None:
    if not value or not value.isascii() or len(value) > 64:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return max(0, math.ceil(parsed.timestamp() - now()))


def _is_bounded_digits(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isdigit()
        and len(value) <= _MAX_HEADER_DIGITS
    )


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    _retried: bool = False,
    **kwargs,
) -> dict | list:
    """Make an authenticated API request with error mapping.

    Returns parsed JSON response body.
    Raises appropriate ForgeError subclass on failure.
    On a rate limit, a GET, HEAD or OPTIONS request waits ``retry_after``
    seconds and retries once, unless the wait is longer than
    ``MAX_RETRY_AFTER_SECONDS``. A rate-limited write raises at once. A timeout
    is not retried.
    """
    response = await _send(client, method, path, _retried=_retried, **kwargs)
    if response.status_code == 204:
        return {}
    return response.json()


async def request_page(
    client: httpx.AsyncClient,
    path: str,
    *,
    page: int,
    per_page: int,
    params: dict[str, str | int] | None = None,
) -> tuple[list[dict], bool]:
    """Read exactly one page of a list endpoint.

    Returns the page's items and whether the forge reports another page. The
    ``Link`` header (GitHub and GitLab) and GitLab's ``X-Next-Page`` header are
    authoritative when present; without either, a full page is taken to mean
    another page may follow, the same rule ``paginate`` uses.
    """
    query: dict[str, str | int] = dict(params or {})
    query["per_page"] = per_page
    query["page"] = page
    response = await _send(client, "GET", path, params=query)
    data = [] if response.status_code == 204 else response.json()
    if not isinstance(data, list):
        raise ValidationError("Validation failed: expected a list response")
    return data, _has_next_page(response, len(data), per_page)


def _has_next_page(response: httpx.Response, count: int, per_page: int) -> bool:
    next_page = response.headers.get("X-Next-Page")
    if next_page is not None:
        return bool(next_page.strip())
    link = response.headers.get("Link")
    if link is not None:
        return any(
            'rel="next"' in part.replace(" ", "") or "rel=next" in part
            for part in link.split(",")
        )
    return count >= per_page


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    _retried: bool = False,
    **kwargs,
) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.TransportError as e:
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e

    if response.status_code >= 400:
        err = map_http_error(response)
        if not _retried and method.upper() in _RETRYABLE_METHODS:
            delay = _rate_limit_retry_delay(err, method, path)
            if delay is not None:
                # The response is already read and closed, so no host request
                # slot is held while sleeping.
                await _sleep(delay)
                return await _send(client, method, path, _retried=True, **kwargs)
        raise err

    return response


def _rate_limit_retry_delay(err: ForgeError, method: str, path: str) -> int | None:
    """Return how long to wait before retrying ``err`` once, or ``None``.

    Only a rate limit is retried, and only when the forge asks for a wait of
    at most ``MAX_RETRY_AFTER_SECONDS``.
    """
    if not isinstance(err, RateLimitError):
        return None
    delay = (
        err.retry_after if err.retry_after is not None else DEFAULT_RETRY_AFTER_SECONDS
    )
    if delay > MAX_RETRY_AFTER_SECONDS:
        log.warning("Rate limited on %s %s for %ds; not retrying", method, path, delay)
        return None
    log.warning("Rate limited on %s %s, retrying in %ds", method, path, delay)
    return delay


#: The most bytes of an error body ``request_bytes`` reads before mapping it.
ERROR_BODY_LIMIT = 64 * 1024

# Headers that describe the wire body, dropped when an error body that was
# already decoded is rebuilt for ``map_http_error``.
_WIRE_BODY_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)


async def request_bytes(
    client: httpx.AsyncClient,
    path: str,
    *,
    max_bytes: int,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """GET ``path`` and return its body, refusing bodies over ``max_bytes``.

    The response is streamed, so its host request slot is given back on every
    exit: a body past the limit, a cancellation and an error status. The read
    asks for ``Accept-Encoding: identity``, so the wire bytes are the counted
    bytes: a ``Content-Length`` over the limit is refused before reading, and
    the limit counts the bytes as they arrive. A forge that compresses the body
    anyway is still capped on decoded bytes, but its ``Content-Length`` is not
    trusted for the early refusal, and one wire chunk may briefly decode past
    the limit before the read is refused. An error status
    is mapped with ``map_http_error`` from at most ``ERROR_BODY_LIMIT`` bytes
    of its body, and a rate limit is retried once, as ``request`` does.

    The client's own redirect handling is turned off for this read. A forge
    answers a read of a renamed or transferred repository with a redirect to
    the same API, so up to ``MAX_READ_REDIRECTS`` such redirects are followed
    by hand (see ``_redirect_target``); any other redirect raises
    UnsupportedFileReadError, because it may lead the forge token elsewhere.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    headers = {**(headers or {}), "Accept-Encoding": "identity"}
    target: str | httpx.URL = path
    request_params = params
    hops = 0
    retried = False
    while True:
        result = await _stream_bytes(
            client, target, max_bytes, request_params, headers, params
        )
        if isinstance(result, bytes):
            return result
        if isinstance(result, httpx.URL):
            hops += 1
            if hops > MAX_READ_REDIRECTS:
                raise UnsupportedFileReadError(
                    "The forge redirected the read too many times"
                )
            # The redirect target carries the query, checked against params.
            target = result
            request_params = None
            continue
        delay = None if retried else _rate_limit_retry_delay(result, "GET", path)
        if delay is None:
            raise result
        retried = True
        # The response is closed, so no host request slot is held here.
        await _sleep(delay)


#: The most same-API redirects one bounded read follows.
MAX_READ_REDIRECTS = 2
_FOLLOWED_REDIRECTS = frozenset({301, 302, 307, 308})


def _redirect_target(
    client: httpx.AsyncClient,
    response: httpx.Response,
    params: dict[str, str] | None,
) -> httpx.URL:
    """Return where a redirect may be followed, or raise UnsupportedFileReadError.

    Only a 301, 302, 307 or 308 is followed, and only to a Location that,
    resolved against the request, has the scheme, host and port of the
    client's base URL, stays under its path (``/`` for api.github.com,
    ``/api/v3/`` for GitHub Enterprise, ``/api/v4/`` for GitLab), carries no
    user info or dot segment, and keeps every query parameter of the first
    request, so a read never moves to another commit.
    """
    status = response.status_code
    refused = UnsupportedFileReadError(
        f"The forge answered the file read with a redirect ({status})"
    )
    location = response.headers.get("Location")
    if status not in _FOLLOWED_REDIRECTS or not location:
        raise refused
    try:
        target = response.request.url.join(location)
    except (httpx.InvalidURL, ValueError):
        raise refused from None
    base = client.base_url
    if (
        target.scheme != base.scheme
        or target.host != base.host
        or target.port != base.port
        or target.userinfo
        or not target.path.startswith(base.path)
        or any(segment in {".", ".."} for segment in target.path.split("/"))
    ):
        raise refused
    for name, value in (params or {}).items():
        if target.params.get_list(name) != [value]:
            raise refused
    return target.copy_with(fragment=None)


async def _stream_bytes(
    client: httpx.AsyncClient,
    target: str | httpx.URL,
    max_bytes: int,
    params: dict[str, str] | None,
    headers: dict[str, str],
    first_params: dict[str, str] | None,
) -> bytes | httpx.URL | ForgeError:
    """Read one response.

    Return its body, the URL of a redirect that may be followed, or the error
    its status maps to. ``first_params`` are the first request's query
    parameters, which a followed redirect must keep.
    """
    try:
        async with client.stream(
            "GET", target, params=params, headers=headers, follow_redirects=False
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                return _redirect_target(client, response, first_params)
            if status >= 400:
                body = await _read_prefix(response, ERROR_BODY_LIMIT)
                return map_http_error(_error_response(response, body))
            length = response.headers.get("Content-Length")
            encoding = response.headers.get("Content-Encoding", "identity")
            if (
                length is not None
                and encoding.strip().lower() == "identity"
                and _is_bounded_digits(length.strip())
                and int(length.strip()) > max_bytes
            ):
                raise ContentTooLargeError(max_bytes)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ContentTooLargeError(max_bytes)
                data.extend(chunk)
            return bytes(data)
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.RequestError as e:
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e


async def request_log_tail(
    client: httpx.AsyncClient, path: str, *, max_bytes: int
) -> JobLogTail:
    """GET a log at ``path`` and keep only its last ``max_bytes`` bytes.

    The body is streamed into a tail buffer that never holds more than twice
    the budget plus one chunk, so a log of any size is read in bounded memory
    and a log over the budget yields its end instead of an error. Bytes are
    counted after any content decoding, which is the size of the text. The
    client's redirect policy applies as it does to ``client.get``, so a GitHub
    log follows its redirect to blob storage. An error status is mapped with
    ``map_http_error`` from at most ``ERROR_BODY_LIMIT`` bytes of its body.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    try:
        async with client.stream("GET", path) as response:
            if response.status_code >= 400:
                body = await _read_prefix(response, ERROR_BODY_LIMIT)
                raise map_http_error(_error_response(response, body))
            tail = bytearray()
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                tail.extend(chunk)
                if len(tail) > 2 * max_bytes + 1:
                    # One byte before the budget shows whether the cut
                    # falls on a line start.
                    del tail[: len(tail) - max_bytes - 1]
            encoding = response.charset_encoding or "utf-8"
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.RequestError as e:
        # Also a body that fails to decode partway through the stream.
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e
    return JobLogTail.from_bytes(bytes(tail), total, max_bytes, encoding)


async def _read_prefix(response: httpx.Response, limit: int) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk[: limit - len(data)])
        if len(data) >= limit:
            break
    return bytes(data)


def _error_response(response: httpx.Response, body: bytes) -> httpx.Response:
    """Rebuild a streamed error response around its already decoded prefix."""
    headers = [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.lower() not in _WIRE_BODY_HEADERS
    ]
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=body,
        request=response.request,
    )


async def paginate(
    client: httpx.AsyncClient,
    path: str,
    per_page: int = 100,
    max_pages: int | None = None,
    **kwargs,
) -> list[dict]:
    """Paginate a GET request, collecting all results."""
    params = dict(kwargs.pop("params", {}))
    params["per_page"] = per_page

    results: list[dict] = []
    page = 1

    while True:
        params["page"] = page
        data = await request(client, "GET", path, params=params, **kwargs)

        if isinstance(data, list):
            results.extend(data)
            if len(data) < per_page:
                break
        else:
            results.append(data)
            break

        page += 1
        if max_pages and page > max_pages:
            break

    return results


def _safe_body(response: httpx.Response) -> str:
    """Extract a safe, redacted body string from a response."""
    try:
        data = response.json()
        msg = data.get("message", data.get("error", str(data)))
    except (ValueError, AttributeError):
        msg = response.text[:200]
    return redact_credentials(str(msg))
