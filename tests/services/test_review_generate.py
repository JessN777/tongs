"""Generating a truncated file's diff locally through the shared service (#382)."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.diff.conversion import convert_forge_changes
from tongs.diff.generate import GenerationTooLargeError, generate_file_diff
from tongs.diff.models import DiffFile, FileStatus, LineType
from tongs.errors import AuthError, ContentTooLargeError, NotFoundError
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import CIStatus, ForgeHost, MRState, MRSummary, User
from tongs.scanner.repo import ForgeType
from tongs.services import (
    GENERATE_LIMITS,
    ApplicationSession,
    RepositoryRef,
    ReviewFileService,
    ReviewRef,
    ServiceError,
    ServiceErrorCode,
    review_files,
)
from tongs.services.models import ReviewRevision
from tongs.services.review_files import (
    GENERATED_CACHE_ENTRIES,
    GeneratedDiff,
    GenerationStatus,
)
from tongs.tui_services import TUIServiceAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "forge_files"
GITHUB_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
GITLAB_HOST = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
# nodejs/node PR 66601: the pull's base is the branch tip, three commits past
# the merge base the compare call reports.
BASE = "f9defa61fa95bcce4fa2e04b1204ca703d6604a2"
HEAD = "91c0ea6197a51df254ea569d4b5cf700526a9ee5"
MERGE_BASE = "7514ef8caff3feea55c60d628e003ccbda850485"
REVIEW = ReviewRef(RepositoryRef("github.com", "nodejs/node"), 66601)
REVISION = ReviewRevision(head_sha=HEAD, base_sha=BASE)

OLD_BODY = "".join(f"line {n}\n" for n in range(1, 41)).encode()
NEW_BODY = OLD_BODY.replace(b"line 5\n", b"line five\n").replace(
    b"line 30\n", b"line 30\nline 30.5\n"
)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def truncated(
    status: FileStatus = FileStatus.MODIFIED,
    *,
    old_path: str = "lib/app.js",
    new_path: str = "lib/app.js",
    **flags: object,
) -> DiffFile:
    values: dict[str, Any] = {
        "old_path": old_path,
        "new_path": new_path,
        "status": status,
        "hunks": (),
        "additions": 2,
        "deletions": 1,
        "language": "javascript",
        "is_truncated": True,
    }
    values.update(flags)
    return DiffFile(**values)


def apply(old: Sequence[str], file: DiffFile) -> list[str]:
    """Apply ``file``'s hunks to ``old``, checking each old and new number."""
    result: list[str] = []
    position = 1
    for hunk in file.hunks:
        start = hunk.old_start if hunk.old_count else hunk.old_start + 1
        result.extend(old[position - 1 : start - 1])
        position = start
        for line in hunk.lines:
            if line.line_type in (LineType.CONTEXT, LineType.DELETION):
                assert line.old_lineno == position
                assert old[position - 1] == line.content
                position += 1
            if line.line_type in (LineType.CONTEXT, LineType.ADDITION):
                result.append(line.content)
                assert line.new_lineno == len(result)
    result.extend(old[position - 1 :])
    return result


def lines(body: bytes) -> list[str]:
    return body.decode().split("\n")[:-1]


class GenClient:
    """A duck-typed forge client serving files and merge bases, counting calls."""

    def __init__(
        self,
        files: dict[tuple[str, str], bytes] | None = None,
        *,
        merge_base: object = MERGE_BASE,
        base_is_merge_base: bool = False,
    ) -> None:
        self.files = files or {}
        self.merge_base = merge_base
        self.revision_base_is_merge_base = base_is_merge_base
        self.reads: list[tuple[str, str, int]] = []
        self.merge_base_calls: list[tuple[str, str, str]] = []
        self.block: asyncio.Event | None = None
        self.started = 0
        self.all_started = asyncio.Event()

    async def get_file_at_ref(
        self, repo_path: str, path: str, ref: str, *, max_bytes: int
    ) -> bytes:
        self.reads.append((path, ref, max_bytes))
        if self.block is not None:
            self.started += 1
            if self.started == 2:
                self.all_started.set()
            await self.block.wait()
        try:
            body = self.files[(path, ref)]
        except KeyError:
            raise NotFoundError("Not found") from None
        if len(body) > max_bytes:
            raise ContentTooLargeError(max_bytes)
        return body

    async def get_merge_base(self, repo_path: str, base_sha: str, head_sha: str) -> str:
        self.merge_base_calls.append((repo_path, base_sha, head_sha))
        if isinstance(self.merge_base, Exception):
            raise self.merge_base
        return self.merge_base  # type: ignore[return-value]

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        return []

    async def close(self) -> None:
        return None


def service_for(client: object) -> ReviewFileService:
    async def get_client(_review: ReviewRef, _operation: str) -> ForgeClient:
        return client  # type: ignore[return-value]

    return ReviewFileService(get_client=get_client)


def both_sides(
    path: str = "lib/app.js", old: bytes = OLD_BODY, new: bytes = NEW_BODY
) -> GenClient:
    return GenClient({(path, MERGE_BASE): old, (path, HEAD): new})


async def generate(
    client: object,
    file: DiffFile | None = None,
    revision: ReviewRevision = REVISION,
) -> GeneratedDiff:
    return await service_for(client).generate_file_diff(
        REVIEW, revision, file or truncated()
    )


class TestGeneration:
    @pytest.mark.asyncio
    async def test_modified_file_is_generated_from_the_merge_base(self) -> None:
        client = both_sides()
        result = await generate(client)
        assert result.status is GenerationStatus.OK
        assert result.message == ""
        file = result.file
        assert file is not None
        assert file.generated_locally and not file.is_truncated
        assert (file.additions, file.deletions) == (2, 1)
        assert apply(lines(OLD_BODY), file) == lines(NEW_BODY)
        assert result.new_text is not None
        assert list(result.new_text.lines) == lines(NEW_BODY)
        assert client.merge_base_calls == [("nodejs/node", BASE, HEAD)]
        assert sorted(client.reads) == sorted(
            [
                ("lib/app.js", MERGE_BASE, GENERATE_LIMITS.max_bytes),
                ("lib/app.js", HEAD, GENERATE_LIMITS.max_bytes),
            ]
        )

    @pytest.mark.asyncio
    async def test_base_tip_is_used_only_when_it_is_the_merge_base(self) -> None:
        client = GenClient(
            {("lib/app.js", BASE): OLD_BODY, ("lib/app.js", HEAD): NEW_BODY},
            base_is_merge_base=True,
        )
        result = await generate(client)
        assert result.status is GenerationStatus.OK
        assert client.merge_base_calls == []
        assert ("lib/app.js", BASE, GENERATE_LIMITS.max_bytes) in client.reads

    @pytest.mark.asyncio
    async def test_rename_reads_each_side_at_its_own_path(self) -> None:
        client = GenClient(
            {("old/app.js", MERGE_BASE): OLD_BODY, ("new/app.js", HEAD): NEW_BODY}
        )
        source = truncated(
            FileStatus.RENAMED, old_path="old/app.js", new_path="new/app.js"
        )
        result = await generate(client, source)
        assert result.status is GenerationStatus.OK
        assert result.file is not None
        assert (result.file.old_path, result.file.new_path) == (
            "old/app.js",
            "new/app.js",
        )
        assert {(path, ref) for path, ref, _ in client.reads} == {
            ("old/app.js", MERGE_BASE),
            ("new/app.js", HEAD),
        }

    @pytest.mark.asyncio
    async def test_added_file_reads_only_the_head_and_needs_no_merge_base(
        self,
    ) -> None:
        client = GenClient({("lib/app.js", HEAD): NEW_BODY})
        result = await generate(client, truncated(FileStatus.ADDED))
        assert result.status is GenerationStatus.OK
        assert result.file is not None
        assert result.file.deletions == 0
        assert client.merge_base_calls == []
        assert [(path, ref) for path, ref, _ in client.reads] == [("lib/app.js", HEAD)]

    @pytest.mark.asyncio
    async def test_deleted_file_reads_only_the_merge_base(self) -> None:
        client = GenClient({("lib/app.js", MERGE_BASE): OLD_BODY})
        result = await generate(client, truncated(FileStatus.DELETED))
        assert result.status is GenerationStatus.OK
        assert result.new_text is None
        assert result.file is not None
        assert result.file.additions == 0
        assert [(path, ref) for path, ref, _ in client.reads] == [
            ("lib/app.js", MERGE_BASE)
        ]


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("file", "status", "message"),
        [
            (
                truncated(is_truncated=False),
                GenerationStatus.NOT_TRUNCATED,
                "The forge sent this file's diff, so there is nothing to generate.",
            ),
            (
                truncated(is_binary=True),
                GenerationStatus.BINARY,
                "This file is binary, so there is no text diff to generate.",
            ),
            (
                truncated(old_mode="120000", new_mode="120000"),
                GenerationStatus.UNSUPPORTED,
                "Symbolic links and submodules have no text diff to generate.",
            ),
            (
                truncated(old_mode="160000", new_mode="160000"),
                GenerationStatus.UNSUPPORTED,
                "Symbolic links and submodules have no text diff to generate.",
            ),
        ],
    )
    async def test_files_outside_the_scope_make_no_request(
        self, file: DiffFile, status: GenerationStatus, message: str
    ) -> None:
        client = both_sides()
        result = await generate(client, file)
        assert result == GeneratedDiff(status, None, None, message)
        assert client.reads == []
        assert client.merge_base_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "merge_base",
        [
            NotFoundError("Not found"),
            ContentTooLargeError(512),
            "7514ef8",
            None,
        ],
    )
    async def test_no_merge_base_never_falls_back_to_the_base_tip(
        self, merge_base: object
    ) -> None:
        client = GenClient(
            {("lib/app.js", BASE): OLD_BODY, ("lib/app.js", HEAD): NEW_BODY},
            merge_base=merge_base,
        )
        result = await generate(client)
        assert result.status is GenerationStatus.NO_MERGE_BASE
        assert result.message == (
            "Couldn't find where this branch started, so the diff can't be generated."
        )
        assert client.reads == []

    @pytest.mark.asyncio
    async def test_a_client_without_a_merge_base_read_has_no_merge_base(
        self,
    ) -> None:
        class FilesOnly:
            async def get_file_at_ref(self, *args: object, **kwargs: object) -> bytes:
                raise AssertionError("no read expected")

        result = await generate(FilesOnly())
        assert result.status is GenerationStatus.NO_MERGE_BASE

    @pytest.mark.asyncio
    async def test_an_over_cap_side_is_too_large(self) -> None:
        big = b"x\n" * (GENERATE_LIMITS.max_lines + 1)
        result = await generate(both_sides(new=big))
        assert result.status is GenerationStatus.TOO_LARGE
        assert result.message == (
            "This file is too large to generate locally "
            "(limit 5 MiB or 100,000 lines per version)."
        )
        assert result.file is None

    @pytest.mark.asyncio
    async def test_an_over_cap_body_is_too_large(self) -> None:
        big = b"x" * (GENERATE_LIMITS.max_bytes + 1)
        result = await generate(both_sides(old=big))
        assert result.status is GenerationStatus.TOO_LARGE

    @pytest.mark.asyncio
    async def test_a_binary_side_is_binary(self) -> None:
        result = await generate(both_sides(new=b"PK\x00\x01"))
        assert result.status is GenerationStatus.BINARY
        assert result.message == (
            "This file is binary, so there is no text diff to generate."
        )

    @pytest.mark.asyncio
    async def test_a_missing_side_is_not_found(self) -> None:
        client = GenClient({("lib/app.js", HEAD): NEW_BODY})
        result = await generate(client)
        assert result.status is GenerationStatus.NOT_FOUND
        assert result.file is None

    @pytest.mark.asyncio
    async def test_a_diff_over_the_output_cap_is_too_large(self) -> None:
        def too_long(*_args: object) -> DiffFile:
            raise GenerationTooLargeError(100_000)

        with patch("tongs.services.review_files.generate_file_diff", too_long):
            result = await generate(both_sides())
        assert result.status is GenerationStatus.TOO_LARGE
        assert result.message == (
            "The generated diff is too large to show (over 100,000 lines)."
        )

    @pytest.mark.asyncio
    async def test_invalid_revision_is_refused_before_any_request(self) -> None:
        client = both_sides()
        with pytest.raises(ServiceError) as caught:
            await generate(
                client, revision=ReviewRevision(head_sha="main", base_sha=BASE)
            )
        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert client.reads == [] and client.merge_base_calls == []

    @pytest.mark.asyncio
    async def test_invalid_path_is_refused_before_any_request(self) -> None:
        client = both_sides()
        with pytest.raises(ServiceError) as caught:
            await generate(client, truncated(new_path="../etc/passwd"))
        assert caught.value.code is ServiceErrorCode.INVALID_INPUT
        assert client.reads == [] and client.merge_base_calls == []

    @pytest.mark.asyncio
    async def test_auth_failure_raises(self) -> None:
        client = both_sides()
        client.merge_base = AuthError("Authentication failed")
        with pytest.raises(ServiceError) as caught:
            await generate(client)
        assert caught.value.code is ServiceErrorCode.AUTHENTICATION_FAILED

    @pytest.mark.asyncio
    async def test_a_failing_read_stops_the_other(self) -> None:
        client = both_sides()
        client.block = asyncio.Event()
        original = client.get_file_at_ref

        async def failing(
            repo_path: str, path: str, ref: str, *, max_bytes: int
        ) -> bytes:
            if ref == MERGE_BASE:
                client.reads.append((path, ref, max_bytes))
                await client.all_started.wait()
                raise AuthError("Authentication failed")
            client.started += 1
            client.all_started.set()
            return await original(repo_path, path, ref, max_bytes=max_bytes)

        client.get_file_at_ref = failing  # type: ignore[method-assign]
        with pytest.raises(ServiceError):
            await asyncio.wait_for(generate(client), 5)
        assert asyncio.all_tasks() == {asyncio.current_task()}


class TestCaching:
    @pytest.mark.asyncio
    async def test_second_generation_is_served_from_memory(self) -> None:
        client = both_sides()
        service = service_for(client)
        first = await service.generate_file_diff(REVIEW, REVISION, truncated())
        second = await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert second is first
        assert len(client.reads) == 2
        assert len(client.merge_base_calls) == 1

    @pytest.mark.asyncio
    async def test_merge_base_is_found_once_per_revision(self) -> None:
        client = GenClient(
            {
                (path, ref): body
                for path in ("a.js", "b.js")
                for ref, body in ((MERGE_BASE, OLD_BODY), (HEAD, NEW_BODY))
            }
        )
        service = service_for(client)
        for path in ("a.js", "b.js"):
            result = await service.generate_file_diff(
                REVIEW, REVISION, truncated(old_path=path, new_path=path)
            )
            assert result.status is GenerationStatus.OK
        assert len(client.merge_base_calls) == 1

    @pytest.mark.asyncio
    async def test_cache_keeps_the_latest_eight(self) -> None:
        paths = [f"f{n}.js" for n in range(GENERATED_CACHE_ENTRIES + 1)]
        client = GenClient(
            {
                (path, ref): body
                for path in paths
                for ref, body in ((MERGE_BASE, OLD_BODY), (HEAD, NEW_BODY))
            }
        )
        service = service_for(client)
        for path in paths:
            await service.generate_file_diff(
                REVIEW, REVISION, truncated(old_path=path, new_path=path)
            )
        service._memory.clear()
        reads = len(client.reads)
        await service.generate_file_diff(
            REVIEW, REVISION, truncated(old_path=paths[-1], new_path=paths[-1])
        )
        assert len(client.reads) == reads
        await service.generate_file_diff(
            REVIEW, REVISION, truncated(old_path=paths[0], new_path=paths[0])
        )
        assert len(client.reads) == reads + 2

    @pytest.mark.asyncio
    async def test_cache_keeps_generated_diffs_within_a_byte_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = ["a.js", "b.js"]
        client = GenClient(
            {
                (path, ref): body
                for path in paths
                for ref, body in ((MERGE_BASE, OLD_BODY), (HEAD, NEW_BODY))
            }
        )
        measured = service_for(client)
        await measured.generate_file_diff(
            REVIEW, REVISION, truncated(old_path="a.js", new_path="a.js")
        )
        one = measured._generated.size
        # The diff lines and the kept new version both count.
        assert one > len(NEW_BODY) + 3 * 192
        monkeypatch.setattr(review_files, "GENERATED_CACHE_BYTES", one + one // 2)
        service = service_for(client)
        for path in paths:
            await service.generate_file_diff(
                REVIEW, REVISION, truncated(old_path=path, new_path=path)
            )
        assert len(service._generated) == 1
        assert service._generated.size <= one + one // 2
        service._memory.clear()
        reads = len(client.reads)
        await service.generate_file_diff(
            REVIEW, REVISION, truncated(old_path="b.js", new_path="b.js")
        )
        assert len(client.reads) == reads
        await service.generate_file_diff(
            REVIEW, REVISION, truncated(old_path="a.js", new_path="a.js")
        )
        assert len(client.reads) == reads + 2

    @pytest.mark.asyncio
    async def test_a_diff_larger_than_the_budget_is_not_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(review_files, "GENERATED_CACHE_BYTES", 1024)
        client = both_sides()
        service = service_for(client)
        first = await service.generate_file_diff(REVIEW, REVISION, truncated())
        second = await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert first.status is second.status is GenerationStatus.OK
        assert len(service._generated) == 0
        assert service._generated.size == 0

    @pytest.mark.asyncio
    async def test_failures_are_not_cached(self) -> None:
        client = GenClient({("lib/app.js", HEAD): NEW_BODY})
        service = service_for(client)
        await service.generate_file_diff(REVIEW, REVISION, truncated())
        client.files[("lib/app.js", MERGE_BASE)] = OLD_BODY
        result = await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert result.status is GenerationStatus.OK

    @pytest.mark.asyncio
    async def test_clear_forgets_generated_diffs_and_merge_bases(self) -> None:
        client = both_sides()
        service = service_for(client)
        await service.generate_file_diff(REVIEW, REVISION, truncated())
        service.clear()
        await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert len(client.reads) == 4
        assert len(client.merge_base_calls) == 2

    @pytest.mark.asyncio
    async def test_cancellation_mid_download_keeps_nothing(self) -> None:
        client = both_sides()
        client.block = asyncio.Event()
        service = service_for(client)
        task = asyncio.create_task(
            service.generate_file_diff(REVIEW, REVISION, truncated())
        )
        await asyncio.wait_for(client.all_started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == {asyncio.current_task()}
        client.block = None
        result = await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert result.status is GenerationStatus.OK
        assert len(client.reads) == 4


class TestRunningGenerations:
    @pytest.mark.asyncio
    async def test_a_generation_still_running_after_a_cancel_is_reused(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        threads: list[str] = []

        def slow(source: DiffFile, old: object, new: object) -> DiffFile:
            threads.append(threading.current_thread().name)
            started.set()
            release.wait(5)
            return generate_file_diff(source, old, new)  # type: ignore[arg-type]

        client = both_sides()
        service = service_for(client)
        try:
            with patch("tongs.services.review_files.generate_file_diff", slow):
                first = asyncio.create_task(
                    service.generate_file_diff(REVIEW, REVISION, truncated())
                )
                assert await asyncio.to_thread(started.wait, 5)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                # The thread cannot stop; pressing e again waits for it.
                second = asyncio.create_task(
                    service.generate_file_diff(REVIEW, REVISION, truncated())
                )
                third = asyncio.create_task(
                    service.generate_file_diff(REVIEW, REVISION, truncated())
                )
                await asyncio.sleep(0.05)
                assert len(threads) == 1
                assert len(client.reads) == 2
                release.set()
                results = await asyncio.wait_for(asyncio.gather(second, third), 5)
        finally:
            release.set()
        assert [result.status for result in results] == [GenerationStatus.OK] * 2
        assert results[0].file is results[1].file
        assert results[0].file is not None
        assert apply(lines(OLD_BODY), results[0].file) == lines(NEW_BODY)
        assert len(threads) == 1
        assert len(client.reads) == 2
        assert service._running == {}
        again = await service.generate_file_diff(REVIEW, REVISION, truncated())
        assert again.file is results[0].file

    @pytest.mark.asyncio
    async def test_a_generation_left_running_finishes_quietly(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def too_long(*_args: object) -> DiffFile:
            started.set()
            release.wait(5)
            raise GenerationTooLargeError(100_000)

        loop = asyncio.get_running_loop()
        reported: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        service = service_for(both_sides())
        try:
            with patch("tongs.services.review_files.generate_file_diff", too_long):
                task = asyncio.create_task(
                    service.generate_file_diff(REVIEW, REVISION, truncated())
                )
                assert await asyncio.to_thread(started.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                release.set()
                for _ in range(100):
                    if not service._running:
                        break
                    await asyncio.sleep(0.01)
            assert service._running == {}
            del task
            gc.collect()
            assert reported == []
            assert len(service._generated) == 0
        finally:
            release.set()
            loop.set_exception_handler(None)


def _compare_response() -> httpx.Response:
    exchange = fixture("github_compare_merge_base")["exchanges"][0]
    return httpx.Response(200, json=exchange["response"]["body_json"])


def _http(handler: Callable[[httpx.Request], httpx.Response], base_url: str):
    return httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        follow_redirects=True,
    )


class ForgeServer:
    """Serve compare and raw file reads for one repository, recording requests."""

    def __init__(self, files: dict[tuple[str, str], bytes]) -> None:
        self.files = files
        self.requests: list[httpx.Request] = []

    def github(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/repos/nodejs/node/compare/{BASE}...{HEAD}":
            assert dict(request.url.params) == {"per_page": "1", "page": "2"}
            return _compare_response()
        prefix = "/repos/nodejs/node/contents/"
        if path.startswith(prefix):
            assert request.headers["Accept"] == "application/vnd.github.raw"
            return self._file(path.removeprefix(prefix), request)
        return httpx.Response(404, json={"message": "Not Found"})

    def gitlab(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.url.raw_path.decode().split("?", 1)[0]
        prefix = "/api/v4/projects/gitlab-org%2Fgitlab/repository/files/"
        if raw.startswith(prefix) and raw.endswith("/raw"):
            path = httpx.URL("https://x/" + raw.removeprefix(prefix)[:-4]).path[1:]
            return self._file(path, request)
        return httpx.Response(404, json={"message": "404 Not Found"})

    def _file(self, path: str, request: httpx.Request) -> httpx.Response:
        body = self.files.get((path, request.url.params["ref"]))
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, content=body)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def refs(self) -> set[str]:
        return {
            request.url.params["ref"]
            for request in self.requests
            if "ref" in request.url.params
        }


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[CacheStore]:
    cache = CacheStore(db_path=tmp_path / "cache.db", max_size_mb=10)
    await cache.open()
    yield cache
    await cache.close()


class TestRealForgeClients:
    @pytest.mark.asyncio
    async def test_github_file_without_a_patch(self, store: CacheStore) -> None:
        (file,) = convert_forge_changes(
            [
                {
                    "filename": "package-lock.json",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 1,
                    "changes": 3,
                }
            ]
        )
        assert file.is_truncated and not file.hunks
        server = ForgeServer(
            {
                ("package-lock.json", MERGE_BASE): OLD_BODY,
                ("package-lock.json", HEAD): NEW_BODY,
            }
        )
        async with _http(server.github, GITHUB_HOST.api_base) as http_client:
            forge = CachedForgeClient(
                GitHubClient(GITHUB_HOST, http_client), store, "github.com"
            )
            result = await service_for(forge).generate_file_diff(REVIEW, REVISION, file)
            assert result.status is GenerationStatus.OK
            assert result.file is not None
            assert apply(lines(OLD_BODY), result.file) == lines(NEW_BODY)
            assert result.file.language == file.language
            compares = [path for path in server.paths() if "/compare/" in path]
            assert len(compares) == 1
            assert server.refs() == {MERGE_BASE, HEAD}
            assert BASE not in server.refs()

            # A new session reads the merge base from the cache database.
            again = await service_for(forge).generate_file_diff(REVIEW, REVISION, file)
            assert again.status is GenerationStatus.OK
            assert len([p for p in server.paths() if "/compare/" in p]) == 1

    @pytest.mark.asyncio
    async def test_github_unknown_commit_has_no_merge_base(self) -> None:
        server = ForgeServer({})
        revision = ReviewRevision(head_sha="0" * 39 + "1", base_sha=BASE)
        async with _http(server.github, GITHUB_HOST.api_base) as http_client:
            result = await service_for(
                GitHubClient(GITHUB_HOST, http_client)
            ).generate_file_diff(REVIEW, revision, truncated())
        assert result.status is GenerationStatus.NO_MERGE_BASE
        assert all("/contents/" not in path for path in server.paths())

    @pytest.mark.asyncio
    async def test_gitlab_too_large_file(self) -> None:
        payload = fixture("gitlab_mr_diffs_too_large")["exchanges"][0]["response"]
        file = convert_forge_changes(payload["body_json"])[0]
        assert file.is_truncated and file.new_path == "locale/es/gitlab.po"
        base = "3bd0c31bfb2202f0ec4f80022b6db05c0a9af68e"
        server = ForgeServer(
            {(file.new_path, base): OLD_BODY, (file.new_path, HEAD): NEW_BODY}
        )
        review = ReviewRef(RepositoryRef("gitlab.com", "gitlab-org/gitlab"), 1)
        revision = ReviewRevision(head_sha=HEAD, base_sha=base)
        async with _http(server.gitlab, GITLAB_HOST.api_base) as http_client:
            result = await service_for(
                GitLabClient(GITLAB_HOST, http_client)
            ).generate_file_diff(review, revision, file)
        assert result.status is GenerationStatus.OK
        assert result.file is not None
        assert apply(lines(OLD_BODY), result.file) == lines(NEW_BODY)
        assert (result.file.old_mode, result.file.new_mode) == ("100644", "100644")
        assert all(
            "compare" not in path and "merge_base" not in path
            for path in server.paths()
        )
        assert server.refs() == {base, HEAD}
        assert len(server.requests) == 2


def _summary() -> MRSummary:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    return MRSummary(
        forge_host=GITHUB_HOST,
        repo_path="nodejs/node",
        local_path="",
        number=66601,
        title="Review",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=now,
        updated_at=now,
        web_url="https://github.com/nodejs/node/pull/66601",
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


class TestSessionWiring:
    @pytest.mark.asyncio
    async def test_tui_adapter_generates_through_the_session(
        self, tmp_path: Path
    ) -> None:
        client = both_sides()
        session = await ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_db_path=tmp_path / "drafts.db",
            forge_registry=FakeRegistry(client),
        ).start()
        try:
            repository = await session.open_repository("github.com", "nodejs/node")
            adapter = TUIServiceAdapter(session)
            adapter._review_refs[("github.com", "nodejs/node", 66601)] = ReviewRef(
                repository.ref, 66601
            )
            result = await adapter.generate_file_diff(_summary(), REVISION, truncated())
            assert result.status is GenerationStatus.OK
            await session.clear_cache()
            await adapter.generate_file_diff(_summary(), REVISION, truncated())
            assert len(client.merge_base_calls) == 2
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_unissued_repository_is_refused(self, tmp_path: Path) -> None:
        client = both_sides()
        session = await ApplicationSession(
            config=Config(),
            cache=FakeCache(),
            draft_db_path=tmp_path / "drafts.db",
            forge_registry=FakeRegistry(client),
        ).start()
        try:
            with pytest.raises(ServiceError) as caught:
                await session.review_files.generate_file_diff(
                    REVIEW, REVISION, truncated()
                )
            assert caught.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED
            assert client.reads == [] and client.merge_base_calls == []
        finally:
            await session.close()
