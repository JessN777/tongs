"""Desktop gap rows, inline context reads and local generation (#383)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import cast
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.desktop.protocol import server as server_module
from tongs.desktop.protocol.diff_projection import (
    ConvertedDiffCache,
    DiffLayout,
    flatten_diff,
    flatten_file,
)
from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import (
    CONTEXT_MISMATCH_MESSAGE,
    DIFF_CONTEXT_CAPABILITY,
    GENERATE_NO_ROOM_MESSAGE,
    MAX_CONTEXT_LINES,
    SUPPORTED_CAPABILITIES,
    SUPPORTED_METHODS,
    DesktopSidecarServer,
)
from tongs.desktop.protocol.state import SnapshotStore, retained_size
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.base import ForgeClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import ForgeHost
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import ApplicationSession
from tongs.state.drafts import DraftStore

_HOST = ForgeHost(
    "gitlab.example.com", ForgeType.GITLAB, "https://gitlab.example.com/api/v4"
)
_PROJECT = "acme/widgets"
_ENCODED = "acme%2Fwidgets"
_PREFIX = f"/api/v4/projects/{_ENCODED}/merge_requests"
_FILES = f"/api/v4/projects/{_ENCODED}/repository/files"
_NUMBERS = (42, 43)
BASE = "a" * 40
START = "b" * 40
HEAD = "c" * 40

# The head version of app.py: line 10 became two lines and line 30 changed, so
# new numbers run one ahead of old numbers after the first hunk.
OLD_LINES = [f"line {n}" for n in range(1, 41)]
NEW_LINES = [*OLD_LINES[:9], "line ten", "line ten b", *OLD_LINES[10:]]
NEW_LINES[30] = "line thirty"
NEW_BODY = ("\n".join(NEW_LINES) + "\n").encode()
OLD_BODY = ("\n".join(OLD_LINES) + "\n").encode()
APP_PATCH = (
    "@@ -7,7 +7,8 @@\n"
    " line 7\n line 8\n line 9\n-line 10\n+line ten\n+line ten b\n"
    " line 11\n line 12\n line 13\n"
    "@@ -27,7 +28,7 @@\n"
    " line 27\n line 28\n line 29\n-line 30\n+line thirty\n"
    " line 31\n line 32\n line 33\n"
)


def _merge_request(number: int) -> dict[str, object]:
    return {
        "iid": number,
        "title": f"Change {number}",
        "state": "opened",
        "draft": False,
        "source_branch": f"feature/{number}",
        "target_branch": "main",
        "web_url": f"https://gitlab.example.com/{_PROJECT}/-/merge_requests/{number}",
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-02T14:30:00Z",
        "has_conflicts": False,
        "user_notes_count": 0,
        "labels": [],
        "author": {"username": "alice", "name": "Alice Dev"},
        "head_pipeline": {"id": 9001, "status": "success"},
        "description": "",
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable",
        "changes_count": "3",
        "diff_refs": {"base_sha": BASE, "start_sha": START, "head_sha": HEAD},
    }


def _changes() -> list[dict[str, object]]:
    return [
        {"old_path": "app.py", "new_path": "app.py", "diff": APP_PATCH},
        {
            "old_path": "big.py",
            "new_path": "big.py",
            "diff": "",
            "too_large": True,
            "additions": 1,
            "deletions": 1,
        },
        {
            "old_path": "added.py",
            "new_path": "added.py",
            "new_file": True,
            "diff": "@@ -0,0 +1,2 @@\n+one\n+two\n",
        },
    ]


@dataclass
class _GitLab:
    """A mocked GitLab serving two merge requests and raw files at commits."""

    files: dict[tuple[str, str], httpx.Response] = field(default_factory=dict)
    changes: list[dict[str, object]] = field(default_factory=_changes)
    requests: list[str] = field(default_factory=list)
    file_reads: list[tuple[str, str]] = field(default_factory=list)
    block: asyncio.Event | None = None
    read_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        self.requests.append(raw_path)
        if raw_path == _PREFIX:
            return httpx.Response(
                200, json=[_merge_request(number) for number in _NUMBERS]
            )
        for number in _NUMBERS:
            if raw_path == f"{_PREFIX}/{number}":
                return httpx.Response(200, json=_merge_request(number))
            if raw_path == f"{_PREFIX}/{number}/diffs":
                return httpx.Response(200, json=self.changes)
        if raw_path.startswith(f"{_FILES}/") and raw_path.endswith("/raw"):
            path = unquote(raw_path[len(_FILES) + 1 : -len("/raw")])
            ref = request.url.params["ref"]
            self.file_reads.append((path, ref))
            self.read_started.set()
            if self.block is not None:
                await self.block.wait()
            response = self.files.get((path, ref))
            if response is None:
                return httpx.Response(404, json={"message": "404 File Not Found"})
            return response
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


def _body(data: bytes) -> httpx.Response:
    return httpx.Response(200, content=data)


class _Registry:
    def __init__(self, client: GitLabClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [_HOST.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return _HOST if hostname == _HOST.hostname else None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return self.client

    async def close_all(self) -> None:
        await self.client.close()


class _Writer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def response(self, request_id: str) -> dict[str, object]:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), 5)
            if frame.get("type") == "response" and frame.get("id") == request_id:
                return frame


class _Wire:
    def __init__(self, reader: asyncio.StreamReader, writer: _Writer) -> None:
        self.reader = reader
        self.writer = writer
        self.count = 0

    def send(self, method: str, params: JsonObject) -> str:
        self.count += 1
        request_id = f"request-{self.count}"
        frame = {
            "v": 1,
            "type": "request",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self.reader.feed_data(json.dumps(frame).encode() + b"\n")
        return request_id

    def cancel(self, request_id: str) -> None:
        frame = {"v": 1, "type": "cancel", "id": request_id}
        self.reader.feed_data(json.dumps(frame).encode() + b"\n")

    async def request(self, method: str, params: JsonObject) -> dict[str, object]:
        return await self.writer.response(self.send(method, params))

    async def call(self, method: str, params: JsonObject) -> JsonObject:
        response = await self.request(method, params)
        assert "error" not in response, response.get("error")
        return cast(JsonObject, response["result"])

    async def error(self, method: str, params: JsonObject) -> JsonObject:
        response = await self.request(method, params)
        assert "result" not in response, response.get("result")
        return cast(JsonObject, response["error"])


@dataclass(frozen=True)
class _Wired:
    wire: _Wire
    forge: _GitLab
    server: DesktopSidecarServer
    reviews: tuple[str, ...]
    handshake: JsonObject

    @property
    def review(self) -> str:
        return self.reviews[0]

    async def open(self, **params: object) -> JsonObject:
        return await self.wire.call(
            "diff.open", cast(JsonObject, {"review": self.review, **params})
        )

    async def context(self, snapshot: object, **params: object) -> JsonObject:
        values: dict[str, object] = {
            "review": self.review,
            "snapshot": snapshot,
            "file_index": 0,
            "new_start": 15,
            "count": MAX_CONTEXT_LINES,
        }
        values.update(params)
        return await self.wire.call("diff.context", cast(JsonObject, values))


Configure = Callable[[_GitLab, DesktopSidecarServer], None]


async def _start(tmp_path: Path, configure: Configure | None) -> AsyncIterator[_Wired]:
    forge = _GitLab()
    forge.files[("app.py", HEAD)] = _body(NEW_BODY)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handle), base_url=_HOST.api_base
    )
    session = ApplicationSession(
        config=Config(),
        cache=CacheStore(db_path=tmp_path / "cache.db"),
        draft_store=DraftStore(tmp_path / "drafts.db"),
        forge_registry=cast(object, _Registry(GitLabClient(_HOST, http))),  # type: ignore[arg-type]
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    if configure is not None:
        configure(forge, server)
    reader = asyncio.StreamReader()
    writer = _Writer()
    running = asyncio.create_task(server.run(reader, writer))
    wire = _Wire(reader, writer)
    try:
        handshake = await wire.call(
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [DIFF_CONTEXT_CAPABILITY],
            },
        )
        repository = await wire.call(
            "repositories.open",
            {"hostname": _HOST.hostname, "project_path": _PROJECT},
        )
        listed = await wire.call(
            "reviews.list",
            {"scope": "all_open", "repository": cast(str, repository["handle"])},
        )
        items = cast(list[dict[str, object]], listed["items"])
        handles = {
            cast(dict[str, object], item["summary"])["number"]: cast(
                str, item["handle"]
            )
            for item in items
        }
        yield _Wired(
            wire,
            forge,
            server,
            tuple(handles[number] for number in _NUMBERS),
            handshake,
        )
        await wire.call("shutdown", {})
        await asyncio.wait_for(running, 5)
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 5)


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> AsyncIterator[_Wired]:
    async for value in _start(tmp_path, None):
        yield value


def _entries(page: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], page["entries"])


def _gaps(page: JsonObject) -> list[JsonObject]:
    return [entry for entry in _entries(page) if entry["kind"] == "gap"]


def _file_reads(forge: _GitLab) -> list[str]:
    return [path for path in forge.requests if path.startswith(_FILES)]


# -- diff.open gaps ---------------------------------------------------------


@pytest.mark.asyncio
async def test_handshake_offers_the_context_capability_and_methods(
    wired: _Wired,
) -> None:
    assert DIFF_CONTEXT_CAPABILITY in SUPPORTED_CAPABILITIES
    assert {"diff.context", "diff.generate"} <= set(SUPPORTED_METHODS)
    assert DIFF_CONTEXT_CAPABILITY in cast(list[str], wired.handshake["capabilities"])
    methods = cast(list[str], wired.handshake["methods"])
    assert {"diff.context", "diff.generate"} <= set(methods)


@pytest.mark.asyncio
async def test_open_without_gaps_is_unchanged_and_reads_no_file(
    wired: _Wired,
) -> None:
    for layout in ("unified", "split"):
        plain = await wired.open(layout=layout)
        explicit = await wired.open(layout=layout, gaps=False)
        gapped = await wired.open(layout=layout, gaps=True)

        assert _entries(plain) == _entries(explicit)
        assert _gaps(plain) == []
        assert [e for e in _entries(gapped) if e["kind"] != "gap"] == _entries(plain)
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["unified", "split"])
async def test_gap_rows_sit_around_the_hunks_in_both_layouts(
    wired: _Wired, layout: str
) -> None:
    page = await wired.open(layout=layout, gaps=True)
    entries = _entries(page)

    app_rows = [entry for entry in entries if entry["file_index"] == 0]
    kinds = [entry["kind"] for entry in app_rows]
    body = "split" if layout == "split" else "line"
    assert kinds[:3] == ["file", "gap", "hunk"]
    assert kinds[-1] == "gap"
    assert kinds.count("gap") == 3
    assert body in kinds
    second_hunk = [i for i, kind in enumerate(kinds) if kind == "hunk"][1]
    assert kinds[second_hunk - 1] == "gap"
    assert _gaps(page) == [
        {
            "kind": "gap",
            "file_index": 0,
            "gap_index": 0,
            "position": "before",
            "old_start": 1,
            "new_start": 1,
            "count": 6,
        },
        {
            "kind": "gap",
            "file_index": 0,
            "gap_index": 1,
            "position": "between",
            "old_start": 14,
            "new_start": 15,
            "count": 13,
        },
        {
            "kind": "gap",
            "file_index": 0,
            "gap_index": 2,
            "position": "after",
            "old_start": 34,
            "new_start": 35,
            "count": None,
        },
    ]


def _line_file(**flags: object) -> DiffFile:
    lines = tuple(
        DiffLine(n, n, f"line {n}", LineType.CONTEXT) for n in range(10, 13)
    ) + (DiffLine(13, None, "gone", LineType.DELETION),)
    hunk = DiffHunk("@@ -10,4 +10,3 @@", 10, 4, 10, 3, lines)
    values: dict[str, object] = {
        "old_path": "a.py",
        "new_path": "a.py",
        "status": FileStatus.MODIFIED,
        "hunks": (hunk,),
    }
    values.update(flags)
    return DiffFile(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "flags",
    [
        {"status": FileStatus.ADDED},
        {"status": FileStatus.DELETED},
        {"is_binary": True},
        {"is_truncated": True},
        {"new_mode": "120000"},
    ],
)
def test_files_that_cannot_expand_get_no_gap_rows(flags: dict[str, object]) -> None:
    file = _line_file(**flags)
    for layout in DiffLayout:
        assert flatten_diff((file,), layout, gaps=True) == flatten_diff((file,), layout)


def test_flatten_file_matches_the_file_in_a_whole_diff() -> None:
    files = (_line_file(status=FileStatus.ADDED), _line_file())
    for layout in DiffLayout:
        whole = flatten_diff(files, layout, gaps=True)
        single = flatten_file(1, files[1], layout, gaps=True)
        assert single == tuple(entry for entry in whole if entry["file_index"] == 1)
        assert [entry["kind"] for entry in single][:2] == ["file", "gap"]


@pytest.mark.asyncio
async def test_gap_rows_carry_line_numbers_past_any_read_cap(tmp_path: Path) -> None:
    """A small patch deep in a long file still opens and refuses to expand."""

    def configure(forge: _GitLab, _server: DesktopSidecarServer) -> None:
        forge.changes = [
            {
                "old_path": "app.py",
                "new_path": "app.py",
                "diff": "@@ -1400000,5 +1400000,5 @@\n a\n-b\n+B\n c\n d\n e\n",
            }
        ]
        forge.files[("app.py", HEAD)] = _body(b"x\n" * 20_001)

    async for wired in _start(tmp_path, configure):
        page = await wired.open(gaps=True)
        assert _gaps(page) == [
            {
                "kind": "gap",
                "file_index": 0,
                "gap_index": 0,
                "position": "before",
                "old_start": 1,
                "new_start": 1,
                "count": 1_399_999,
            },
            {
                "kind": "gap",
                "file_index": 0,
                "gap_index": 1,
                "position": "after",
                "old_start": 1_400_005,
                "new_start": 1_400_005,
                "count": None,
            },
        ]
        result = await wired.context(page["snapshot_id"], new_start=1_400_005)
        assert result["status"] == "too_large"
        assert result["rows"] == []


# -- diff.context -----------------------------------------------------------


@pytest.mark.asyncio
async def test_context_returns_a_middle_gap_with_both_line_numbers(
    wired: _Wired,
) -> None:
    page = await wired.open(gaps=True)

    result = await wired.context(page["snapshot_id"], new_start=15)

    assert result["status"] == "ok"
    assert result["message"] is None
    assert result["line_count"] == 41
    rows = cast(list[JsonObject], result["rows"])
    assert len(rows) == 13
    assert rows[0] == {
        "kind": "context",
        "file_index": 0,
        "old_line": 14,
        "new_line": 15,
        "content": "line 14",
    }
    assert rows[-1]["new_line"] == 27
    assert rows[-1]["old_line"] == 26
    assert wired.forge.file_reads == [("app.py", HEAD)]


@pytest.mark.asyncio
async def test_context_honours_count_and_starts_inside_a_gap(
    wired: _Wired,
) -> None:
    page = await wired.open(layout="split", gaps=True)

    result = await wired.context(page["snapshot_id"], new_start=17, count=2)

    rows = cast(list[JsonObject], result["rows"])
    assert [(row["old_line"], row["new_line"]) for row in rows] == [(16, 17), (17, 18)]
    assert [row["content"] for row in rows] == ["line 16", "line 17"]


@pytest.mark.asyncio
async def test_context_after_the_last_hunk_runs_to_the_end_of_the_file(
    wired: _Wired,
) -> None:
    page = await wired.open(gaps=True)

    result = await wired.context(page["snapshot_id"], new_start=35)
    past_end = await wired.context(page["snapshot_id"], new_start=60)

    rows = cast(list[JsonObject], result["rows"])
    assert [row["new_line"] for row in rows] == list(range(35, 42))
    assert rows[-1] == {
        "kind": "context",
        "file_index": 0,
        "old_line": 40,
        "new_line": 41,
        "content": "line 40",
    }
    assert past_end == {"status": "ok", "message": None, "line_count": 41, "rows": []}


def _set_app(response: httpx.Response) -> Configure:
    def configure(forge: _GitLab, _server: DesktopSidecarServer) -> None:
        forge.files[("app.py", HEAD)] = response

    return configure


_MISMATCHED = NEW_BODY.replace(b"line 12\n", b"line twelve\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status", "message"),
    [
        (
            _body(b"x\n" * 20_001),
            "too_large",
            "File too large to expand (over 2 MiB or 20,000 lines).",
        ),
        (_body(b"line 1\0\n"), "binary", "This file is binary at this commit."),
        (
            httpx.Response(404, json={"message": "404 File Not Found"}),
            "not_found",
            "The forge has no copy of this file at this commit. Refresh the review.",
        ),
        (
            httpx.Response(302, headers={"Location": "https://lfs.example.com/x"}),
            "unsupported",
            "This forge can't read files at a commit.",
        ),
        (_body(_MISMATCHED), "mismatch", CONTEXT_MISMATCH_MESSAGE),
    ],
)
async def test_context_reports_each_read_outcome(
    tmp_path: Path, response: httpx.Response, status: str, message: str
) -> None:
    async for wired in _start(tmp_path, _set_app(response)):
        page = await wired.open(gaps=True)
        result = await wired.context(page["snapshot_id"])
        assert result == {
            "status": status,
            "message": message,
            "line_count": None,
            "rows": [],
        }


@pytest.mark.asyncio
async def test_context_stops_before_the_byte_cap(
    wired: _Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await wired.open(gaps=True)
    one_row = len(
        json.dumps(
            {
                "kind": "context",
                "file_index": 0,
                "old_line": 14,
                "new_line": 15,
                "content": "line 14",
            },
            separators=(",", ":"),
        ).encode()
    )
    monkeypatch.setattr(server_module, "MAX_CONTEXT_BYTES", one_row * 3 + 1)

    result = await wired.context(page["snapshot_id"], new_start=15)

    rows = cast(list[JsonObject], result["rows"])
    assert result["status"] == "ok"
    assert [row["new_line"] for row in rows] == [15, 16, 17]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"count": 0},
        {"count": MAX_CONTEXT_LINES + 1},
        {"count": True},
        {"count": 1.5},
        {"file_index": -1},
        {"file_index": "0"},
        {"new_start": 0},
        {"new_start": -3},
        {"new_start": 2**53},
        {"surprise": 1},
        {"snapshot": ""},
        {"snapshot": 7},
        # Lines inside a hunk or outside the file are not a gap.
        {"new_start": 10},
        {"new_start": 12, "file_index": 0},
        # A file index past the snapshot's files.
        {"file_index": 3},
        # The added file has no gaps at all.
        {"file_index": 2, "new_start": 1},
    ],
)
async def test_context_refuses_bad_parameters_before_any_read(
    wired: _Wired, change: dict[str, object]
) -> None:
    page = await wired.open(gaps=True)
    params: dict[str, object] = {
        "review": wired.review,
        "snapshot": page["snapshot_id"],
        "file_index": 0,
        "new_start": 15,
        "count": 10,
    }
    params.update(change)

    error = await wired.wire.error("diff.context", cast(JsonObject, params))

    assert error["code"] == "invalid_params"
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
async def test_context_requires_every_parameter(wired: _Wired) -> None:
    page = await wired.open(gaps=True)
    full: dict[str, object] = {
        "review": wired.review,
        "snapshot": page["snapshot_id"],
        "file_index": 0,
        "new_start": 15,
        "count": 10,
    }
    for key in full:
        params = {name: value for name, value in full.items() if name != key}
        error = await wired.wire.error("diff.context", cast(JsonObject, params))
        assert error["code"] == "invalid_params", key
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
async def test_context_for_an_unknown_or_replaced_snapshot_is_expired(
    wired: _Wired,
) -> None:
    first = await wired.open(gaps=True)
    unknown = await wired.context("not-a-snapshot")
    # A fresh open, as Refresh sends, replaces the read the first one showed.
    await wired.open(gaps=True)
    replaced = await wired.context(first["snapshot_id"])

    for result in (unknown, replaced):
        assert result["status"] == "expired"
        assert result["rows"] == []
        assert isinstance(result["message"], str)
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
async def test_context_refuses_another_reviews_snapshot(wired: _Wired) -> None:
    other = await wired.wire.call(
        "diff.open", {"review": wired.reviews[1], "gaps": True}
    )

    error = await wired.wire.error(
        "diff.context",
        {
            "review": wired.review,
            "snapshot": other["snapshot_id"],
            "file_index": 0,
            "new_start": 15,
            "count": 10,
        },
    )

    assert error["code"] == "invalid_params"
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
async def test_context_creates_no_snapshot(wired: _Wired) -> None:
    page = await wired.open(gaps=True)
    snapshots = wired.server._snapshots
    before = (snapshots._retained_rows, snapshots._retained_bytes)

    await wired.context(page["snapshot_id"])

    assert (snapshots._retained_rows, snapshots._retained_bytes) == before


# -- diff.generate ----------------------------------------------------------

# A big.py whose every fifth line changed, so its generated diff spans pages.
BIG_OLD = "".join(f"value {n}\n" for n in range(1, 2001)).encode()
BIG_NEW = "".join(
    f"value {n}{' changed' if n % 5 == 0 else ''}\n" for n in range(1, 2001)
).encode()


def _serve_big(old: bytes = BIG_OLD, new: bytes = BIG_NEW) -> Configure:
    def configure(forge: _GitLab, _server: DesktopSidecarServer) -> None:
        forge.files[("big.py", BASE)] = _body(old)
        forge.files[("big.py", HEAD)] = _body(new)

    return configure


async def _generate(wired: _Wired, snapshot: object, file_index: int = 1) -> JsonObject:
    return await wired.wire.call(
        "diff.generate",
        {"review": wired.review, "snapshot": snapshot, "file_index": file_index},
    )


@pytest.mark.asyncio
async def test_generate_pages_one_file_and_releases_it_after_the_last_page(
    tmp_path: Path,
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        page = await wired.open(layout="split")
        generated = await _generate(wired, page["snapshot_id"])

        assert generated["status"] == "ok"
        assert generated["message"] is None
        assert generated["generated"] is True
        assert generated["revision"] == page["revision"]
        assert generated["cursor"] == 0
        assert sorted(wired.forge.file_reads) == [("big.py", BASE), ("big.py", HEAD)]
        entries = list(_entries(generated))
        first = entries[0]
        assert first["kind"] == "file"
        assert first["file_index"] == 1
        assert first["is_truncated"] is False
        assert set(first) == set(_entries(page)[0])
        snapshot = cast(str, generated["snapshot_id"])
        cursor = generated["next_cursor"]
        assert cursor is not None
        while cursor is not None:
            more = await wired.wire.call(
                "diff.page",
                {
                    "snapshot": snapshot,
                    "resource": wired.review,
                    "cursor": cursor,
                },
            )
            entries.extend(_entries(more))
            cursor = more["next_cursor"]

        assert {entry["file_index"] for entry in entries} == {1}
        assert {entry["kind"] for entry in entries} == {"file", "hunk", "split"}
        assert sum(1 for entry in entries if entry["kind"] == "split") >= 400
        released = await wired.wire.error(
            "diff.page", {"snapshot": snapshot, "resource": wired.review, "cursor": 0}
        )
        assert released["code"] == "snapshot_expired"
        assert wired.server._generated_snapshots == {}
        # The review's own diff snapshot is still there to page.
        await wired.wire.call(
            "diff.page",
            {"snapshot": page["snapshot_id"], "resource": wired.review, "cursor": 0},
        )


@pytest.mark.asyncio
async def test_a_single_page_generation_is_released_at_once(tmp_path: Path) -> None:
    small_new = BIG_OLD.replace(b"value 7\n", b"value seven\n")
    async for wired in _start(tmp_path, _serve_big(new=small_new)):
        page = await wired.open()
        generated = await _generate(wired, page["snapshot_id"])

        assert generated["status"] == "ok"
        assert generated["next_cursor"] is None
        lines = [e for e in _entries(generated) if e["kind"] == "line"]
        assert [
            line["content"] for line in lines if line["line_type"] != "context"
        ] == [
            "value 7",
            "value seven",
        ]
        error = await wired.wire.error(
            "diff.page",
            {
                "snapshot": generated["snapshot_id"],
                "resource": wired.review,
                "cursor": 0,
            },
        )
        assert error["code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_generate_refuses_without_headroom_for_the_snapshot_count(
    tmp_path: Path,
) -> None:
    def configure(forge: _GitLab, server: DesktopSidecarServer) -> None:
        _serve_big()(forge, server)
        server._snapshots = SnapshotStore(max_snapshots=1)

    async for wired in _start(tmp_path, configure):
        page = await wired.open()
        generated = await _generate(wired, page["snapshot_id"])

        assert generated == {
            "status": "too_large",
            "message": GENERATE_NO_ROOM_MESSAGE,
            "generated": False,
            "snapshot_id": None,
            "resource": None,
            "revision": None,
            "cursor": None,
            "next_cursor": None,
            "entries": [],
        }
        # The diff being read was not evicted to make room.
        await wired.wire.call(
            "diff.page",
            {"snapshot": page["snapshot_id"], "resource": wired.review, "cursor": 0},
        )


@pytest.mark.asyncio
async def test_generate_refuses_without_headroom_for_rows(tmp_path: Path) -> None:
    def configure(forge: _GitLab, server: DesktopSidecarServer) -> None:
        _serve_big()(forge, server)
        server._snapshots = SnapshotStore(max_retained_rows=500)

    async for wired in _start(tmp_path, configure):
        page = await wired.open()
        generated = await _generate(wired, page["snapshot_id"])

        assert generated["status"] == "too_large"
        assert generated["message"] == GENERATE_NO_ROOM_MESSAGE
        # Desktop-only wording in the terminal's style: the cause, then the fix.
        assert generated["message"] == (
            "The generated diff is too large to show with the other diffs "
            "already open. Close one and try again."
        )
        await wired.wire.call(
            "diff.page",
            {"snapshot": page["snapshot_id"], "resource": wired.review, "cursor": 0},
        )


@pytest.mark.asyncio
async def test_generate_reports_service_outcomes(tmp_path: Path) -> None:
    async for wired in _start(tmp_path, None):
        page = await wired.open()
        not_truncated = await _generate(wired, page["snapshot_id"], file_index=0)
        missing = await _generate(wired, page["snapshot_id"], file_index=1)
        expired = await _generate(wired, "gone-snapshot")

        assert not_truncated["status"] == "not_truncated"
        assert not_truncated["message"] == (
            "The forge sent this file's diff, so there is nothing to generate."
        )
        assert missing["status"] == "not_found"
        assert expired["status"] == "expired"
        for result in (not_truncated, missing, expired):
            assert result["generated"] is False
            assert result["entries"] == []
            assert result["snapshot_id"] is None
        assert wired.server._generated_snapshots == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"file_index": -1},
        {"file_index": 9},
        {"file_index": None},
        {"count": 3},
        {"snapshot": ""},
    ],
)
async def test_generate_refuses_bad_parameters(
    wired: _Wired, change: dict[str, object]
) -> None:
    page = await wired.open()
    params: dict[str, object] = {
        "review": wired.review,
        "snapshot": page["snapshot_id"],
        "file_index": 1,
    }
    params.update(change)

    error = await wired.wire.error("diff.generate", cast(JsonObject, params))

    assert error["code"] == "invalid_params"
    assert _file_reads(wired.forge) == []


@pytest.mark.asyncio
async def test_a_cancel_mid_generate_keeps_nothing(tmp_path: Path) -> None:
    def configure(forge: _GitLab, server: DesktopSidecarServer) -> None:
        _serve_big()(forge, server)
        forge.block = asyncio.Event()

    async for wired in _start(tmp_path, configure):
        page = await wired.open()
        snapshots = wired.server._snapshots
        before = (snapshots._retained_rows, snapshots._retained_bytes)
        request_id = wired.wire.send(
            "diff.generate",
            {"review": wired.review, "snapshot": page["snapshot_id"], "file_index": 1},
        )
        await asyncio.wait_for(wired.forge.read_started.wait(), 5)

        wired.wire.cancel(request_id)
        response = await wired.wire.writer.response(request_id)
        assert wired.forge.block is not None
        wired.forge.block.set()

        error = cast(JsonObject, response["error"])
        assert error["code"] == "request_cancelled"
        assert error["retryable"] is True
        assert (snapshots._retained_rows, snapshots._retained_bytes) == before
        assert wired.server._generated_snapshots == {}


# -- releasing generated diffs the renderer stopped reading (#431) -----------


async def _page(wired: _Wired, snapshot: object, cursor: int = 0) -> JsonObject:
    return await wired.wire.call(
        "diff.page", {"snapshot": snapshot, "resource": wired.review, "cursor": cursor}
    )


async def _page_error(wired: _Wired, snapshot: object) -> JsonObject:
    return await wired.wire.error(
        "diff.page", {"snapshot": snapshot, "resource": wired.review, "cursor": 0}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", ["generate", "page"])
async def test_a_cancel_for_an_answered_read_releases_the_generated_diff(
    tmp_path: Path, cancelled: str
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        page = await wired.open(layout="split")
        generate_id = wired.wire.send(
            "diff.generate",
            {"review": wired.review, "snapshot": page["snapshot_id"], "file_index": 1},
        )
        generated = cast(
            JsonObject, (await wired.wire.writer.response(generate_id))["result"]
        )
        snapshot = cast(str, generated["snapshot_id"])
        assert generated["next_cursor"] is not None
        page_id = wired.wire.send(
            "diff.page",
            {
                "snapshot": snapshot,
                "resource": wired.review,
                "cursor": generated["next_cursor"],
            },
        )
        more = await wired.wire.writer.response(page_id)
        assert "result" in more
        retained = wired.server._snapshots._retained_rows

        # The renderer gave up between pages: its cancel arrives after the reply.
        wired.wire.cancel(generate_id if cancelled == "generate" else page_id)

        error = await _page_error(wired, snapshot)
        assert error["code"] == "snapshot_expired"
        assert wired.server._generated_snapshots == {}
        assert wired.server._snapshots._retained_rows < retained
        # The review's own diff is untouched.
        await _page(wired, page["snapshot_id"])


@pytest.mark.asyncio
async def test_a_cancel_for_another_request_keeps_the_generated_diff(
    tmp_path: Path,
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        open_id = wired.wire.send(
            "diff.open", {"review": wired.review, "layout": "split"}
        )
        page = cast(JsonObject, (await wired.wire.writer.response(open_id))["result"])
        generated = await _generate(wired, page["snapshot_id"])
        snapshot = cast(str, generated["snapshot_id"])

        wired.wire.cancel(open_id)
        wired.wire.cancel("request-unknown")

        second = await _page(wired, snapshot)
        assert second["next_cursor"] is not None
        assert list(wired.server._generated_snapshots) == [snapshot]


@pytest.mark.asyncio
async def test_a_reused_request_id_does_not_release_an_earlier_generation(
    tmp_path: Path,
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        page = await wired.open(layout="split")
        generate_id = wired.wire.send(
            "diff.generate",
            {"review": wired.review, "snapshot": page["snapshot_id"], "file_index": 1},
        )
        generated = cast(
            JsonObject, (await wired.wire.writer.response(generate_id))["result"]
        )
        snapshot = cast(str, generated["snapshot_id"])
        assert generated["next_cursor"] is not None

        # A new request reuses the answered generate's id, then is cancelled.
        while generate_id in wired.server._pending:
            await asyncio.sleep(0)
        frame = {
            "v": 1,
            "type": "request",
            "id": generate_id,
            "method": "diff.page",
            "params": {
                "snapshot": page["snapshot_id"],
                "resource": wired.review,
                "cursor": 0,
            },
        }
        wired.wire.reader.feed_data(json.dumps(frame).encode() + b"\n")
        assert "result" in await wired.wire.writer.response(generate_id)
        wired.wire.cancel(generate_id)

        assert (await _page(wired, snapshot, generated["next_cursor"]))
        assert list(wired.server._generated_snapshots) == [snapshot]


@pytest.mark.asyncio
async def test_a_newer_diff_releases_generations_of_diffs_no_longer_on_screen(
    tmp_path: Path,
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        page = await wired.open(layout="split")
        generated = await _generate(wired, page["snapshot_id"])
        snapshot = cast(str, generated["snapshot_id"])
        assert generated["next_cursor"] is not None

        # A layout switch names the diff on screen, which may still be
        # reading the generated file while the new layout loads.
        switched = await wired.open(layout="unified", from_snapshot=page["snapshot_id"])
        assert (await _page(wired, snapshot))["next_cursor"] is not None
        # Another review's diff leaves this review's generation alone.
        await wired.wire.call("diff.open", {"review": wired.reviews[1]})
        assert list(wired.server._generated_snapshots) == [snapshot]

        # The next open of the review comes after the switched diff took over.
        await wired.open(layout="split", from_snapshot=switched["snapshot_id"])

        error = await _page_error(wired, snapshot)
        assert error["code"] == "snapshot_expired"
        assert wired.server._generated_snapshots == {}


@pytest.mark.asyncio
async def test_a_refresh_keeps_the_generation_on_screen_until_the_next_one(
    tmp_path: Path,
) -> None:
    async for wired in _start(tmp_path, _serve_big()):
        page = await wired.open(layout="split")
        generated = await _generate(wired, page["snapshot_id"])
        snapshot = cast(str, generated["snapshot_id"])

        # A Refresh names no snapshot: the last diff opened is the one on
        # screen, so its generation may still be paging.
        await wired.open(layout="split")
        assert (await _page(wired, snapshot))["next_cursor"] is not None

        await wired.open(layout="split")

        error = await _page_error(wired, snapshot)
        assert error["code"] == "snapshot_expired"
        assert wired.server._generated_snapshots == {}


# -- state and cache --------------------------------------------------------


def test_can_retain_counts_rows_bytes_and_snapshots() -> None:
    store = SnapshotStore(max_snapshots=2, max_retained_bytes=400, max_retained_rows=5)
    revision: JsonObject = {"head_sha": "h"}
    entries: list[JsonObject] = [{"n": 1}, {"n": 2}]
    size = retained_size(revision, entries)

    assert store.can_retain(2, size)
    store.create("review", revision, entries)
    assert store.can_retain(3, 400 - size)
    assert not store.can_retain(4, 10)
    assert not store.can_retain(1, 400 - size + 1)
    store.create("review", revision, [{"n": 3}])
    assert not store.can_retain(0, 0)
    assert not store.can_retain(-1, 0)


def test_retained_size_matches_what_create_retains() -> None:
    store = SnapshotStore(max_retained_bytes=1000)
    revision: JsonObject = {"head_sha": "h", "base_sha": "b"}
    entries: list[JsonObject] = [{"content": "é and  "}, {"n": 2}]
    size = retained_size(revision, entries)

    store.create("review", revision, entries)

    assert store.can_retain(0, 1000 - size)
    assert not store.can_retain(0, 1000 - size + 1)


def test_cache_records_layouts_and_owners() -> None:
    cache = ConvertedDiffCache()
    revision: JsonObject = {"head_sha": "h", "base_sha": "b", "start_sha": None}
    cache.store("a", "a1", revision, (_line_file(),), layout=DiffLayout.SPLIT)
    cache.remember("a", "a2")

    assert cache.layout("a", "a1") is DiffLayout.SPLIT
    assert cache.layout("a", "a2") is DiffLayout.UNIFIED
    assert cache.layout("b", "a1") is None
    assert cache.owner("a1") == "a"
    assert cache.owner("zz") is None
