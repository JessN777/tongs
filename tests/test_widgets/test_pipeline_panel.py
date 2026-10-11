"""Tests for tongs.widgets.pipeline_panel pure functions and messages."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import Input, RichLog, Static

from tongs.forges.models import CIStatus, Pipeline, PipelineJob
from tongs.widgets.pipeline_panel import (
    CancelJobRequested,
    CancelPipelineRequested,
    LoadJobLogRequested,
    LoadJobsRequested,
    PipelinePanel,
    RetryJobRequested,
    RetryPipelineRequested,
)
from tongs.widgets.two_press import TwoPressConfirm


class PipelinePanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.posted: list[Message] = []

    def compose(self) -> ComposeResult:
        yield PipelinePanel(id="pipeline-panel")

    def on_cancel_pipeline_requested(self, message: CancelPipelineRequested) -> None:
        self.posted.append(message)

    def on_retry_pipeline_requested(self, message: RetryPipelineRequested) -> None:
        self.posted.append(message)

    def on_cancel_job_requested(self, message: CancelJobRequested) -> None:
        self.posted.append(message)

    def on_retry_job_requested(self, message: RetryJobRequested) -> None:
        self.posted.append(message)

    def on_load_jobs_requested(self, message: LoadJobsRequested) -> None:
        self.posted.append(message)

    def on_load_job_log_requested(self, message: LoadJobLogRequested) -> None:
        self.posted.append(message)


@pytest.mark.asyncio
async def test_editor_export_strips_ansi_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PipelinePanelApp()
    exported: list[str] = []
    paths: list[Path] = []

    def capture(command: list[str], *, check: bool) -> None:
        assert check is False
        path = Path(command[-1])
        paths.append(path)
        exported.append(path.read_text())

    monkeypatch.setenv("EDITOR", "editor")
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    monkeypatch.setattr("tongs.widgets.external_editor.subprocess.run", capture)

    async with app.run_test():
        panel = app.query_one("#pipeline-panel", PipelinePanel)
        panel._view_level = 2
        panel._job_log_text = "\x1b[31mfailed\x1b[0m\nplain"
        panel.action_open_in_editor()

    assert exported == ["failed\nplain"]
    assert "\x1b" not in exported[0]
    # Job logs can hold secrets, so the exported copy must not outlive the editor.
    assert paths
    assert not paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_name", ["rspec 1/3", "caller / callee", "../../etc"])
async def test_editor_export_works_for_any_job_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_name: str
) -> None:
    app = PipelinePanelApp()
    exported: list[tuple[Path, str]] = []

    def capture(command: list[str], *, check: bool) -> None:
        path = Path(command[-1])
        exported.append((path, path.read_text()))

    monkeypatch.setenv("EDITOR", "editor")
    monkeypatch.setattr("tongs.widgets.external_editor.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    monkeypatch.setattr("tongs.widgets.external_editor.subprocess.run", capture)
    notices: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_: notices.append(message))

    async with app.run_test():
        panel = app.query_one("#pipeline-panel", PipelinePanel)
        panel._view_level = 2
        panel._current_job = PipelineJob(
            id=31, name=job_name, stage="test", status=CIStatus.FAILED
        )
        panel._job_log_text = "log line"
        panel.action_open_in_editor()

    assert notices == []
    assert len(exported) == 1
    path, content = exported[0]
    assert content == "log line"
    assert path.parent == tmp_path
    assert path.name.startswith("tongs-joblog-")
    assert not path.exists()


# ===================================================================
# Messages posted by the panel actions
# ===================================================================


def _pipeline(status: CIStatus) -> Pipeline:
    return Pipeline(
        id=7,
        status=status,
        ref="main",
        sha="abc1234",
        web_url="https://example.com/pipelines/7",
    )


def _job(status: CIStatus) -> PipelineJob:
    return PipelineJob(id=31, name="build", stage="build", status=status)


def _ids(messages: list[Message]) -> list[tuple]:
    return [
        (
            type(m).__name__,
            getattr(m, "pipeline_id", None),
            getattr(m, "job_id", None),
        )
        for m in messages
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        ("C", CIStatus.RUNNING, "CancelPipelineRequested"),
        ("R", CIStatus.FAILED, "RetryPipelineRequested"),
    ],
)
async def test_pipeline_mutation_posts_only_after_the_confirm_press(
    key: str, status: CIStatus, expected: str
) -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([_pipeline(status)])
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app.posted == []
        await pilot.press(key)
        await pilot.pause()

    assert _ids(app.posted) == [(expected, 7, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        ("C", CIStatus.RUNNING, "CancelJobRequested"),
        ("R", CIStatus.FAILED, "RetryJobRequested"),
    ],
)
async def test_job_mutation_posts_only_after_the_confirm_press(
    key: str, status: CIStatus, expected: str
) -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_jobs([_job(status)], _pipeline(CIStatus.RUNNING))
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app.posted == []
        await pilot.press(key)
        await pilot.pause()

    assert _ids(app.posted) == [(expected, 7, 31)]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_late_confirm_press_rearms_instead_of_cancelling() -> None:
    app = PipelinePanelApp()
    clock = _Clock()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel._two_press = TwoPressConfirm(clock=clock)
        panel.set_pipelines([_pipeline(CIStatus.RUNNING)])
        await pilot.pause()
        await pilot.press("C")
        clock.now += 6.0
        await pilot.press("C")
        await pilot.pause()
        assert app.posted == []
        clock.now += 1.0
        await pilot.press("C")
        await pilot.pause()

    assert _ids(app.posted) == [("CancelPipelineRequested", 7, None)]


@pytest.mark.asyncio
async def test_pipeline_and_job_with_the_same_id_do_not_cross_confirm() -> None:
    app = PipelinePanelApp()
    pipeline = _pipeline(CIStatus.RUNNING)
    job = PipelineJob(id=7, name="build", stage="build", status=CIStatus.RUNNING)
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([pipeline])
        await pilot.pause()
        await pilot.press("C")
        panel.set_jobs([job], pipeline)
        await pilot.pause()
        await pilot.press("C")
        await pilot.pause()
        assert app.posted == []
        await pilot.press("C")
        await pilot.pause()

    assert _ids(app.posted) == [("CancelJobRequested", 7, 7)]


@pytest.mark.asyncio
async def test_cancel_then_retry_on_one_target_does_not_cross_confirm() -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([_pipeline(CIStatus.RUNNING)])
        await pilot.pause()
        await pilot.press("C")
        panel._pipelines = [_pipeline(CIStatus.FAILED)]
        await pilot.press("R")
        await pilot.pause()

    assert app.posted == []


@pytest.mark.asyncio
async def test_drill_out_clears_an_armed_cancel() -> None:
    app = PipelinePanelApp()
    pipeline = _pipeline(CIStatus.RUNNING)
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_jobs([_job(CIStatus.RUNNING)], pipeline)
        await pilot.pause()
        await pilot.press("C")
        assert panel._two_press.armed
        await pilot.press("escape")
        await pilot.pause()
        assert not panel._two_press.armed
        panel.set_jobs([_job(CIStatus.RUNNING)], pipeline)
        await pilot.pause()
        await pilot.press("C")
        await pilot.pause()

    assert app.posted == []


@pytest.mark.asyncio
async def test_new_pipeline_list_clears_an_armed_cancel() -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([_pipeline(CIStatus.RUNNING)])
        await pilot.pause()
        await pilot.press("C")
        panel.set_pipelines([_pipeline(CIStatus.RUNNING)])
        await pilot.pause()
        assert not panel._two_press.armed
        await pilot.press("C")
        await pilot.pause()

    assert app.posted == []


@pytest.mark.asyncio
async def test_enter_loads_the_focused_pipeline_then_the_focused_job_log() -> None:
    app = PipelinePanelApp()
    pipeline = _pipeline(CIStatus.SUCCESS)
    job = _job(CIStatus.SUCCESS)
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([pipeline])
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        panel.set_jobs([job], pipeline)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    load_jobs, load_log = app.posted
    assert isinstance(load_jobs, LoadJobsRequested)
    assert load_jobs.pipeline is pipeline
    assert isinstance(load_log, LoadJobLogRequested)
    assert (load_log.job, load_log.pipeline) == (job, pipeline)


@pytest.mark.asyncio
async def test_job_log_search_is_case_insensitive() -> None:
    app = PipelinePanelApp()
    async with app.run_test():
        panel = app.query_one(PipelinePanel)
        panel._view_level = 2
        panel._log_search_lines = ["build ok", "error: failed"]
        panel._log_plain_lines = ["build ok", "error: failed"]
        panel._log_row_offset = 0
        panel._do_search("ERROR")

        assert panel._search_matches == [1]


def _log_status(panel: PipelinePanel) -> str:
    return panel.query_one("#log-search-status", Static).render().plain


@pytest.mark.asyncio
async def test_n_scrolls_to_the_rendered_row_of_a_truncated_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The widget keeps only the last LOG_MAX_LINES rows, so a match's raw line
    # index is ahead of its rendered row by the number of dropped lines.
    monkeypatch.setattr("tongs.widgets.pipeline_panel.LOG_MAX_LINES", 200)
    log_lines = [f"line {i:03d}" for i in range(300)]
    log_lines[150] = "line 150 needle one"
    log_lines[160] = "line 160 needle two"
    app = PipelinePanelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_job_log(
            "\n".join(log_lines), _job(CIStatus.FAILED), _pipeline(CIStatus.FAILED)
        )
        await pilot.pause()
        log_widget = panel.query_one("#job-log-content", RichLog)
        assert len(log_widget.lines) == 200

        await pilot.press("slash")
        for key in "needle":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is panel
        assert "Match 1/2" in _log_status(panel)
        assert log_widget.scroll_offset.y == 50
        assert log_widget.lines[50].text.endswith("line 150 needle one")

        await pilot.press("n")
        await pilot.pause()
        status = _log_status(panel)
        assert "Match 2/2" in status
        assert "line 161" in status
        assert log_widget.scroll_offset.y == 60
        assert log_widget.lines[60].text.endswith("line 160 needle two")

        await pilot.press("N")
        await pilot.pause()
        assert "Match 1/2" in _log_status(panel)
        assert log_widget.scroll_offset.y == 50


@pytest.mark.asyncio
async def test_job_log_search_matches_the_text_without_ansi_codes() -> None:
    log = "setup done\n\x1b[1;31mERR\x1b[0mOR: \x1b[33mboom\x1b[0m\n\x1b[32mok\x1b[0m"
    app = PipelinePanelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_job_log(log, _job(CIStatus.FAILED), _pipeline(CIStatus.FAILED))
        await pilot.pause()
        await pilot.press("slash")
        search = panel.query_one("#log-search-input", Input)

        # The query spans two colour changes in the raw line.
        search.value = "error: boom"
        await pilot.pause()
        assert panel._search_matches == [1]
        assert "Match 1/1" in _log_status(panel)
        assert "ERROR: boom" in _log_status(panel)

        for escape_text in ("\x1b[", "[31m", "31m", "[0m"):
            search.value = escape_text
            await pilot.pause()
            assert panel._search_matches == [], escape_text
            assert "No matches for" in _log_status(panel)
