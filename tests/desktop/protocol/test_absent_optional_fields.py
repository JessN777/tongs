"""Wire coverage for values a forge legitimately leaves absent.

The desktop client validates every read against an exact DTO, and one row that
breaks the contract makes it reject the whole response.  The core models spell
an absent optional text as the empty string, so the sidecar has to translate
that to null on the wire.  These tests pin both known instances: the language of
a file with no detected lexer, and the inline position of a review-level note.
The CI and commit projections follow the same rule for the fields a forge may
omit (#184): a pipeline's link and source, a job's stage and link, a commit's
link and a review's merge status.

The same shapes cover the metadata GitHub's files endpoint withholds.  The
sidecar derives the shapes that endpoint's remaining fields determine and leaves
only the genuinely undetermined one unavailable, so these tests also pin the
derived per-file state and keep the captured renderer fixture equal to what the
sidecar projects today.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import (
    DesktopSidecarServer,
    RequestContext,
    _detail_wire,
    _log_entries,
)
from tongs.desktop.protocol.state import HandleKind
from tongs.forges.github import GitHubClient
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    InlineComment,
    MRDetail,
    MRState,
    Pipeline,
    PipelineJob,
    User,
)
from tongs.plugins.desktop import DesktopCancellation
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ForgeCapabilities,
    PipelineRef,
    RawDiffSnapshot,
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "diff-shapes.json"
_METADATA_FLAGS = (
    "is_binary",
    "is_truncated",
    "is_empty",
    "is_mode_only",
    "is_rename_only",
    "is_unavailable",
)
# The state each forge's payload actually determines for the four files with no
# patch.  GitHub reports the rename, the added empty file and, through its path,
# the binary file; it exposes no way to tell a mode-only change from binary
# content, so ``src/tool.sh`` stays unavailable there instead of guessing.
_EXPECTED_METADATA_STATE = {
    "github": {
        "assets/icon.bin": ("is_binary",),
        "src/empty_placeholder.py": ("is_empty",),
        "src/renamed_module.py": ("is_rename_only",),
        "src/tool.sh": ("is_unavailable",),
    },
    "gitlab": {
        "assets/icon.bin": ("is_binary",),
        "src/empty_placeholder.py": ("is_empty",),
        "src/renamed_module.py": ("is_rename_only",),
        "src/tool.sh": ("is_mode_only",),
    },
}


def _fixture() -> JsonObject:
    return cast(JsonObject, json.loads(_FIXTURE.read_text()))


class _DiffSession:
    def __init__(self, raw: RawDiffSnapshot) -> None:
        self.raw = raw

    async def get_raw_diff(self, review: ReviewRef) -> RawDiffSnapshot:
        assert review == self.raw.review
        return self.raw


class _DiscussionSession:
    def __init__(self, discussions: tuple[Discussion, ...]) -> None:
        self.discussions = discussions

    async def get_discussions(self, _review: ReviewRef) -> tuple[Discussion, ...]:
        return self.discussions


def _server_for_diff(changes: object) -> tuple[DesktopSidecarServer, str]:
    review = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
    raw = RawDiffSnapshot(
        review,
        ReviewRevision("head", "base", None),
        tuple(cast(list[JsonObject], changes)),
    )
    server = DesktopSidecarServer(session=cast(object, _DiffSession(raw)))
    return server, server._handles.issue(HandleKind.REVIEW, review)


def _comment(identifier: str, file_path: str, line: int | None) -> InlineComment:
    return InlineComment(
        id=identifier,
        author=User("reviewer", ""),
        body="",
        created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        file_path=file_path,
        old_line=None,
        new_line=line,
    )


async def _file_rows(forge: str) -> list[JsonObject]:
    server, handle = _server_for_diff(_fixture()[f"{forge}_changes"])
    page = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle}, RequestContext("diff", DesktopCancellation())
        ),
    )
    return [
        cast(JsonObject, entry)
        for entry in cast(list[object], page["entries"])
        if cast(JsonObject, entry)["kind"] == "file"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_withheld_patch_projects_the_state_the_forge_determines(
    forge: str,
) -> None:
    """Each hard shape reaches the client as one honest, exclusive state.

    A generic unavailable badge on GitHub told the reviewer nothing about the
    file, so a shape the payload determines must arrive as that shape and the
    one it does not must arrive as unavailable rather than as a guess.
    """
    rows = await _file_rows(forge)

    expected = _EXPECTED_METADATA_STATE[forge]
    observed = {
        cast(str, row["new_path"]): tuple(
            flag for flag in _METADATA_FLAGS if row[flag] is True
        )
        for row in rows
        if cast(str, row["new_path"]) in expected
    }
    assert observed == expected
    for row in rows:
        for flag in _METADATA_FLAGS:
            assert isinstance(row[flag], bool), f"{flag} must be a wire boolean"


@pytest.mark.asyncio
async def test_file_modes_reach_the_wire_where_the_forge_reports_them() -> None:
    """GitLab's chmod arrives with both modes; GitHub reports none (#397).

    GitLab's ``"0"`` for the absent side of an added or deleted file is not a
    mode, so it projects as null like every mode GitHub withholds.
    """
    gitlab = {cast(str, row["new_path"]): row for row in await _file_rows("gitlab")}
    github = await _file_rows("github")

    assert (gitlab["src/tool.sh"]["old_mode"], gitlab["src/tool.sh"]["new_mode"]) == (
        "100644",
        "100755",
    )
    added = gitlab["src/empty_placeholder.py"]
    deleted = gitlab["src/obsolete.py"]
    assert (added["old_mode"], added["new_mode"]) == (None, "100644")
    assert (deleted["old_mode"], deleted["new_mode"]) == ("100644", None)
    assert all(row["old_mode"] is None and row["new_mode"] is None for row in github)


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_captured_renderer_page_equals_what_the_sidecar_projects(
    forge: str,
) -> None:
    """The fixture the renderer tests read must not drift from the sidecar.

    Those tests assert badges against a captured wire page.  If the projection
    changes and the capture does not, they would keep proving the old contract.
    """
    server, handle = _server_for_diff(_fixture()[f"{forge}_changes"])
    page = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle}, RequestContext("diff", DesktopCancellation())
        ),
    )
    captured = cast(JsonObject, _fixture()[f"{forge}_unified_page"])

    assert page["entries"] == captured["entries"]


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_file_with_no_detected_lexer_projects_a_null_language(
    forge: str,
) -> None:
    """``assets/icon.bin`` has no lexer, and null is its wire form.

    An empty string here made the desktop client reject the entire diff page,
    so a review whose files were all readable showed one generic read failure.
    """
    server, handle = _server_for_diff(_fixture()[f"{forge}_changes"])

    page = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle}, RequestContext("diff", DesktopCancellation())
        ),
    )

    binary = cast(JsonObject, cast(list[object], page["entries"])[0])
    assert binary["new_path"] == "assets/icon.bin"
    assert binary["language"] is None


@pytest.mark.asyncio
async def test_review_level_note_projects_a_null_inline_path() -> None:
    """A note with no position keeps the thread readable, path absent."""
    review = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
    threads = (
        Discussion(
            "53f505e47aca",
            False,
            replace(_comment("1", "", None), replies=(_comment("4", "", None),)),
        ),
        Discussion(
            "b689d5db45fc",
            True,
            InlineComment(
                id="2",
                author=User("reviewer", "Reviewer"),
                body="inline",
                created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
                file_path="src/calc.py",
                new_line=26,
                replies=(_comment("3", "src/calc.py", 26),),
            ),
        ),
    )
    server = DesktopSidecarServer(session=cast(object, _DiscussionSession(threads)))
    handle = server._handles.issue(HandleKind.REVIEW, review)

    result = cast(
        JsonObject,
        await server._discussions_list(
            {"review": handle}, RequestContext("threads", DesktopCancellation())
        ),
    )

    discussions = [
        cast(JsonObject, item) for item in cast(list[object], result["discussions"])
    ]
    unpositioned = cast(JsonObject, discussions[0]["root_comment"])
    positioned = cast(JsonObject, discussions[1]["root_comment"])
    assert unpositioned["file_path"] is None
    assert unpositioned["old_line"] is None
    assert unpositioned["new_line"] is None
    unpositioned_reply = cast(
        JsonObject, cast(list[object], unpositioned["replies"])[0]
    )
    assert unpositioned_reply["file_path"] is None
    assert positioned["file_path"] == "src/calc.py"
    reply = cast(JsonObject, cast(list[object], positioned["replies"])[0])
    assert reply["file_path"] == "src/calc.py"


def test_empty_job_log_projects_one_empty_row() -> None:
    """An empty log is one empty row, which the client must accept as content."""
    assert _log_entries(b"") == ({"text": ""},)


_GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
_REVIEW = ReviewRef(RepositoryRef("github.com", "acme/repo"), 7)


def _github_client() -> GitHubClient:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    return GitHubClient(_GITHUB, httpx.AsyncClient(transport=transport))


class _CISession:
    """Answers the CI and commit reads with fixed core values."""

    def __init__(
        self,
        pipelines: tuple[Pipeline, ...] = (),
        jobs: tuple[PipelineJob, ...] = (),
        commits: tuple[Commit, ...] = (),
    ) -> None:
        self.pipelines = pipelines
        self.jobs = jobs
        self.commits = commits

    async def list_pipelines(
        self, _repository: RepositoryRef, *, per_page: int = 20
    ) -> tuple[Pipeline, ...]:
        return self.pipelines

    async def list_review_pipelines(
        self, _review: ReviewRef, *, per_page: int = 20
    ) -> tuple[Pipeline, ...]:
        return self.pipelines

    async def get_pipeline_jobs(
        self, _pipeline: PipelineRef
    ) -> tuple[PipelineJob, ...]:
        return self.jobs

    async def get_commits(self, _review: ReviewRef) -> tuple[Commit, ...]:
        return self.commits


def _context() -> RequestContext:
    return RequestContext("ci", DesktopCancellation())


def _values(result: object, key: str) -> list[JsonObject]:
    items = cast(list[object], cast(JsonObject, result)[key])
    return [cast(JsonObject, cast(JsonObject, item)["value"]) for item in items]


@pytest.mark.asyncio
async def test_github_job_without_a_workflow_name_projects_a_null_stage() -> None:
    """GitHub names no stage, so the job list carries null rather than "".

    An empty stage made the client reject every job of the pipeline, so the
    Pipelines view showed a read failure for a run whose jobs were all fine.
    """
    client = _github_client()
    jobs = (
        client._parse_job({"id": 2, "name": "build", "status": "queued"}),
        client._parse_job(
            {
                "id": 3,
                "name": "lint",
                "status": "queued",
                "workflow_name": "CI",
                "html_url": "https://github.com/acme/repo/actions/runs/1/job/3",
            }
        ),
    )
    server = DesktopSidecarServer(session=cast(object, _CISession(jobs=jobs)))
    pipeline = server._handles.issue(
        HandleKind.PIPELINE, PipelineRef(_REVIEW.repository, 1)
    )

    result = await server._jobs_list({"pipeline": pipeline}, _context())

    bare, named = _values(result, "jobs")
    assert (bare["name"], bare["stage"], bare["web_url"]) == ("build", None, None)
    assert (named["stage"], named["web_url"]) == (
        "CI",
        "https://github.com/acme/repo/actions/runs/1/job/3",
    )


@pytest.mark.asyncio
async def test_pipeline_without_a_source_or_link_projects_nulls() -> None:
    """A run with no event or link reads, with only those two fields absent."""
    client = _github_client()
    run = client._parse_workflow_run(
        {"id": 1, "status": "completed", "head_branch": "main", "head_sha": "abc123"}
    )
    server = DesktopSidecarServer(session=cast(object, _CISession(pipelines=(run,))))
    review = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    repository = server._handles.issue(HandleKind.REPOSITORY, _REVIEW.repository)

    for result in (
        await server._review_pipelines_list({"review": review}, _context()),
        await server._pipelines_list({"repository": repository}, _context()),
    ):
        (value,) = _values(result, "pipelines")
        assert (value["source"], value["web_url"]) == (None, None)
        assert (value["ref"], value["sha"]) == ("main", "abc123")


@pytest.mark.asyncio
async def test_required_ci_text_is_never_turned_into_null() -> None:
    """Only the declared optional fields change; a broken required value stays.

    Mapping every empty text to null would hide a contract break the client is
    meant to catch, so an empty job name or pipeline ref reaches it unchanged.
    """
    session = _CISession(
        pipelines=(Pipeline(1, CIStatus.PENDING, "", "", "", ""),),
        jobs=(PipelineJob(2, "", "", CIStatus.PENDING),),
    )
    server = DesktopSidecarServer(session=cast(object, session))
    review = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    pipeline = server._handles.issue(
        HandleKind.PIPELINE, PipelineRef(_REVIEW.repository, 1)
    )

    (run,) = _values(
        await server._review_pipelines_list({"review": review}, _context()),
        "pipelines",
    )
    (job,) = _values(
        await server._jobs_list({"pipeline": pipeline}, _context()), "jobs"
    )

    assert (run["ref"], run["sha"], run["web_url"], run["source"]) == (
        "",
        "",
        None,
        None,
    )
    assert (job["name"], job["stage"], job["web_url"]) == ("", None, None)


@pytest.mark.asyncio
async def test_commit_without_a_link_projects_a_null_link() -> None:
    commits = (
        Commit("abc123def456", "abc123de", "Fix", "Fix", User("dev", "")),
        Commit(
            "def456abc123",
            "def456ab",
            "Docs",
            "Docs",
            User("dev", ""),
            web_url="https://github.com/acme/repo/commit/def456abc123",
        ),
    )
    server = DesktopSidecarServer(session=cast(object, _CISession(commits=commits)))
    review = server._handles.issue(HandleKind.REVIEW, _REVIEW)

    result = cast(
        JsonObject, await server._commits_list({"review": review}, _context())
    )

    links = [
        cast(JsonObject, commit)["web_url"]
        for commit in cast(list[object], result["commits"])
    ]
    assert links == [None, "https://github.com/acme/repo/commit/def456abc123"]


@pytest.mark.parametrize(("merge_status", "expected"), [("", None), ("clean", "clean")])
def test_review_detail_without_a_merge_status_projects_null(
    merge_status: str, expected: str | None
) -> None:
    created = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    detail = MRDetail(
        forge_host=_GITHUB,
        repo_path="acme/repo",
        local_path="/work/repo",
        number=7,
        title="Title",
        author=User("dev", ""),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=created,
        updated_at=created,
        web_url="https://github.com/acme/repo/pull/7",
        merge_status=merge_status,
    )
    snapshot = ReviewSnapshot(
        _REVIEW,
        detail,
        ReviewRevision("head", "base", None),
        ForgeCapabilities(False, False, False, False, False),
    )

    assert _detail_wire(snapshot)["merge_status"] == expected
