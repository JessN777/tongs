"""Pipeline panel with three-level drill-down: pipelines -> jobs -> log."""

from __future__ import annotations

import re
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from tongs.config import app_config
from tongs.forges.models import CIStatus, Pipeline, PipelineJob
from tongs.helpers import (
    CI_ASCII_WIDTH,
    ci_icon_markup,
    ci_icon_text,
    format_duration,
    relative_time,
)
from tongs.widgets.external_editor import (
    FALLBACK_EDITORS,
    EditorUnavailableError,
    editor_argv,
    view_text,
)
from tongs.widgets.two_press import TwoPressConfirm

LOG_MAX_LINES = 50000
"""Job log lines kept in the widget; older lines are dropped past this."""


_UNSAFE_FILE_LABEL = re.compile(r"[^A-Za-z0-9._-]")


def _safe_file_label(name: str) -> str:
    """Return a short file name part that cannot leave the temp folder."""
    return _UNSAFE_FILE_LABEL.sub("_", name)[:40] or "job"


class CancelPipelineRequested(Message):
    def __init__(self, pipeline_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id


class RetryPipelineRequested(Message):
    def __init__(self, pipeline_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id


class CancelJobRequested(Message):
    def __init__(self, pipeline_id: int, job_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id
        self.job_id = job_id


class RetryJobRequested(Message):
    def __init__(self, pipeline_id: int, job_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id
        self.job_id = job_id


class LoadJobsRequested(Message):
    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self.pipeline = pipeline


class LoadJobLogRequested(Message):
    def __init__(self, job: PipelineJob, pipeline: Pipeline) -> None:
        super().__init__()
        self.job = job
        self.pipeline = pipeline


class PipelineCard(Static):
    """A single pipeline rendered as a card."""

    DEFAULT_CSS = """
    PipelineCard {
        margin: 0 0 1 0;
        padding: 1 2;
        border: solid $accent-darken-2;
        background: $surface;
        height: auto;
    }
    PipelineCard.focused {
        border: solid $accent;
        background: $foreground 8%;
    }
    PipelineCard.failed {
        border: solid $error;
    }
    PipelineCard.running {
        border: solid $warning;
    }
    """

    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self.pipeline = pipeline

    def on_mount(self) -> None:
        p = self.pipeline
        icon_char, icon_style = ci_icon_text(p.status, app_config(self.app).ascii_mode)
        content = Text()
        content.append(f"{icon_char} ", icon_style)
        content.append(f"Pipeline #{p.id}", Style(bold=True))
        content.append(f"  {relative_time(p.created_at)}", Style(dim=True))
        content.append("\n")
        content.append(f"  {p.sha[:7]}  {p.ref}", Style(dim=True))
        if p.source:
            content.append(f"  {p.source}", Style(dim=True))
        dur = format_duration(p.duration_seconds)
        if dur:
            content.append(f"  {dur}", Style(dim=True))

        self.update(content)

        if p.status == CIStatus.FAILED:
            self.add_class("failed")
        elif p.status == CIStatus.RUNNING:
            self.add_class("running")

    def on_click(self) -> None:
        self.post_message(LoadJobsRequested(self.pipeline))


class JobCard(Static):
    """A single job rendered as a line."""

    DEFAULT_CSS = """
    JobCard {
        padding: 0 2;
        height: auto;
    }
    JobCard.focused {
        background: $foreground 8%;
    }
    """

    def __init__(self, job: PipelineJob) -> None:
        super().__init__()
        self.job = job

    def on_mount(self) -> None:
        j = self.job
        ascii_mode = app_config(self.app).ascii_mode
        icon_char, icon_style = ci_icon_text(j.status, ascii_mode)
        if ascii_mode:
            # Labels differ in length; pad them so the job names line up.
            icon_char = icon_char.ljust(CI_ASCII_WIDTH)
        content = Text()
        content.append(f"  {icon_char} ", icon_style)
        content.append(f"{j.name:<30}", Style(bold=j.status == CIStatus.FAILED))
        dur = format_duration(j.duration_seconds)
        if dur:
            content.append(f"  {dur}", Style(dim=True))
        if j.status == CIStatus.FAILED:
            content.append("  FAILED", Style(color="red", bold=True))
        if j.allow_failure:
            content.append("  allow failure", Style(dim=True))
        self.update(content)

    def on_click(self) -> None:
        if self.parent and hasattr(self.parent, "parent"):
            panel = self.parent.parent
            if isinstance(panel, PipelinePanel) and panel._current_pipeline:
                self.post_message(
                    LoadJobLogRequested(self.job, panel._current_pipeline)
                )


class PipelinePanel(Widget, can_focus=True):
    """Three-level pipeline viewer: pipelines -> jobs -> log."""

    DEFAULT_CSS = """
    PipelinePanel {
        height: 1fr;
    }
    PipelinePanel VerticalScroll {
        height: 1fr;
    }
    PipelinePanel #job-log-container {
        height: 1fr;
    }
    PipelinePanel RichLog {
        height: 1fr;
    }
    PipelinePanel #log-search-input {
        display: none;
        height: 1;
    }
    PipelinePanel #log-search-status {
        dock: bottom;
        display: none;
        height: 1;
        padding: 0 1;
        background: $accent-darken-2;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("j", "next_item", "Down", show=False),
        Binding("k", "prev_item", "Up", show=False),
        Binding("down", "next_item", "Down", show=False),
        Binding("up", "prev_item", "Up", show=False),
        Binding("enter", "drill_in", "Open", show=True),
        Binding("escape", "drill_out", "Back", show=True),
        Binding("C", "cancel", "Cancel", show=True, key_display="C"),
        Binding("R", "retry", "Retry", show=True, key_display="R"),
        Binding("o", "open_browser", "Browser", show=True, key_display="o"),
        Binding("f2", "open_in_editor", "Editor", show=False),
        Binding("slash", "search_log", "Search", show=False, key_display="/"),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Previous match", show=False, key_display="N"),
    ]

    _focused_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._view_level: int = 0
        self._pipelines: list[Pipeline] = []
        self._jobs: list[PipelineJob] = []
        self._current_pipeline: Pipeline | None = None
        self._current_job: PipelineJob | None = None
        self._job_log_text: str = ""
        # One confirm for cancel and retry, keyed by (action, level, id), so a
        # pipeline and a job with the same number never confirm each other.
        self._two_press = TwoPressConfirm()
        self._search_matches: list[int] = []
        self._search_index: int = 0
        self._search_active: bool = False
        self._search_query: str = ""
        self._search_saved_scroll: int | None = None
        self._log_row_offset: int = 0
        self._log_plain_lines: list[str] = []
        self._log_search_lines: list[str] = []
        self._render_gen: int = 0
        self._saved_pipeline_idx: int = 0
        self._saved_job_idx: int = 0
        self._job_card_map: dict[int, int] = {}

    @property
    def view_level(self) -> int:
        """How far the user has drilled in: 0 pipelines, 1 jobs, 2 one log."""
        return self._view_level

    @property
    def current_pipeline(self) -> Pipeline | None:
        """The pipeline the jobs list is showing, when drilled in."""
        return self._current_pipeline

    @property
    def current_job(self) -> PipelineJob | None:
        """The job whose log is showing, when one is open."""
        return self._current_job

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="pipeline-list-scroll")
        yield VerticalScroll(id="job-list-scroll")
        with Vertical(id="job-log-container"):
            yield Static("", id="job-log-header")
            yield RichLog(id="job-log-content", max_lines=LOG_MAX_LINES, wrap=False)
            yield Input(id="log-search-input", placeholder="Search log...")
            yield Static("", id="log-search-status")

    def on_mount(self) -> None:
        self.query_one("#job-list-scroll").display = False
        self.query_one("#job-log-container").display = False

    def set_pipelines(self, pipelines: list[Pipeline]) -> None:
        self._pipelines = pipelines
        self._two_press.reset()
        self._view_level = 0
        self._focused_index = 0
        self._render_pipeline_list()
        self.focus()

    def _render_pipeline_list(self) -> None:
        self._render_gen += 1
        scroll = self.query_one("#pipeline-list-scroll", VerticalScroll)
        scroll.display = True
        self.query_one("#job-list-scroll").display = False
        self.query_one("#job-log-container").display = False
        scroll.remove_children()

        if not self._pipelines:
            scroll.mount(Static("[dim]No pipelines for this MR[/]"))
            return

        g = self._render_gen
        for i, p in enumerate(self._pipelines):
            card = PipelineCard(p)
            card.id = f"pipeline-card-{g}-{i}"
            if i == self._focused_index:
                card.add_class("focused")
            scroll.mount(card)

    def set_jobs(self, jobs: list[PipelineJob], pipeline: Pipeline) -> None:
        self._jobs = jobs
        self._current_pipeline = pipeline
        self._view_level = 1
        self._focused_index = 0
        self._render_job_list()
        self.focus()

    def _render_job_list(self) -> None:
        self._render_gen += 1
        self.query_one("#pipeline-list-scroll").display = False
        scroll = self.query_one("#job-list-scroll", VerticalScroll)
        scroll.display = True
        self.query_one("#job-log-container").display = False
        scroll.remove_children()

        if self._current_pipeline:
            p = self._current_pipeline
            header = Static(
                f"Pipeline #{p.id}  "
                f"{ci_icon_markup(p.status, app_config(self.app).ascii_mode)} "
                f"{p.status.value}  "
                f"{format_duration(p.duration_seconds)}"
            )
            scroll.mount(header)

        stages: dict[str, list[tuple[int, PipelineJob]]] = {}
        for job_index, j in enumerate(self._jobs):
            stage = j.stage or "default"
            stages.setdefault(stage, []).append((job_index, j))

        g = self._render_gen
        card_idx = 0
        self._job_card_map: dict[int, int] = {}
        for stage_name, stage_jobs in stages.items():
            scroll.mount(
                Static(Content.from_markup("\n  [bold]$stage[/]", stage=stage_name))
            )
            for job_idx, job in stage_jobs:
                card = JobCard(job)
                card.id = f"job-card-{g}-{card_idx}"
                if card_idx == self._focused_index:
                    card.add_class("focused")
                scroll.mount(card)
                self._job_card_map[card_idx] = job_idx
                card_idx += 1

    def set_job_log(self, log_text: str, job: PipelineJob, pipeline: Pipeline) -> None:
        self._reset_search()
        self._current_job = job
        self._current_pipeline = pipeline
        self._job_log_text = log_text
        self._view_level = 2
        self._render_job_log()
        self.focus()

    def _render_job_log(self) -> None:
        self.query_one("#pipeline-list-scroll").display = False
        self.query_one("#job-list-scroll").display = False
        container = self.query_one("#job-log-container")
        container.display = True

        job = self._current_job
        if job:
            header = self.query_one("#job-log-header", Static)
            icon_char, icon_style = ci_icon_text(
                job.status, app_config(self.app).ascii_mode
            )
            # Built as Text, not markup: the job name and stage come from the
            # forge and brackets in them would otherwise be parsed as tags.
            header_text = Text()
            header_text.append("Job: ")
            header_text.append(job.name)
            header_text.append("  ")
            header_text.append(icon_char, icon_style)
            header_text.append(f" {job.status.value}  ")
            duration = format_duration(job.duration_seconds)
            if duration:
                header_text.append(f"{duration}  ")
            header_text.append("Stage: ")
            header_text.append(job.stage or "")
            header_text.append(
                "  F2 open in editor  / search  n/N next-previous match",
                Style(dim=True),
            )
            header.update(header_text)

        log_widget = self.query_one("#job-log-content", RichLog)
        log_widget.clear()

        lines = self._job_log_text.split("\n")
        # The widget keeps only the last LOG_MAX_LINES rows, so a log line's
        # rendered row is its index minus the number of dropped lines. Each
        # line renders as exactly one row because the RichLog does not wrap.
        self._log_row_offset = max(0, len(lines) - LOG_MAX_LINES)
        self._log_plain_lines = []
        self._log_search_lines = []
        for i, line in enumerate(lines):
            line_num = Text(f"{i + 1:>6} ", style=Style(dim=True))
            content = Text.from_ansi(line)
            plain = content.plain
            self._log_plain_lines.append(plain)
            self._log_search_lines.append(plain.lower())
            rendered = Text()
            rendered.append_text(line_num)
            rendered.append_text(content)
            # The gutter keeps every write non-empty, so each log line renders
            # as exactly one row and the row mapping above stays exact.
            log_widget.write(rendered)

    def _get_focused_pipeline(self) -> Pipeline | None:
        if self._view_level != 0:
            return None
        if 0 <= self._focused_index < len(self._pipelines):
            return self._pipelines[self._focused_index]
        return None

    def _get_focused_job(self) -> PipelineJob | None:
        if self._view_level != 1:
            return None
        job_idx = self._job_card_map.get(self._focused_index)
        if job_idx is not None and 0 <= job_idx < len(self._jobs):
            return self._jobs[job_idx]
        return None

    def watch__focused_index(self, old: int, new: int) -> None:
        g = self._render_gen
        if self._view_level == 0:
            self._update_card_focus(f"pipeline-card-{g}", old, new)
        elif self._view_level == 1:
            self._update_card_focus(f"job-card-{g}", old, new)

    def _update_card_focus(self, prefix: str, old: int, new: int) -> None:
        try:
            old_card = self.query_one(f"#{prefix}-{old}")
            old_card.remove_class("focused")
        except NoMatches:
            pass
        try:
            new_card = self.query_one(f"#{prefix}-{new}")
            new_card.add_class("focused")
            new_card.scroll_visible()
        except NoMatches:
            pass

    def _max_index(self) -> int:
        if self._view_level == 0:
            return max(0, len(self._pipelines) - 1)
        elif self._view_level == 1:
            return max(0, len(self._job_card_map) - 1)
        return 0

    def action_next_item(self) -> None:
        if self._focused_index < self._max_index():
            self._focused_index += 1

    def action_prev_item(self) -> None:
        if self._focused_index > 0:
            self._focused_index -= 1

    def action_drill_in(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if p:
                self._saved_pipeline_idx = self._focused_index
                self.post_message(LoadJobsRequested(p))
        elif self._view_level == 1:
            j = self._get_focused_job()
            if j and self._current_pipeline:
                self._saved_job_idx = self._focused_index
                self.post_message(LoadJobLogRequested(j, self._current_pipeline))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "drill_out":
            return self._view_level > 0 or self._search_active
        if action in ("next_match", "prev_match"):
            return bool(self._search_matches)
        return True

    def action_drill_out(self) -> None:
        self._two_press.reset()
        if self._search_active:
            self._close_search(restore_scroll=True)
            return
        if self._view_level == 2:
            self._view_level = 1
            self._focused_index = self._saved_job_idx
            self._reset_search()
            self._render_job_list()
            self.focus()
        elif self._view_level == 1:
            self._view_level = 0
            self._focused_index = self._saved_pipeline_idx
            self._render_pipeline_list()
            self.focus()

    def action_cancel(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if not p or p.status not in (CIStatus.RUNNING, CIStatus.PENDING):
                self.app.notify("Can only cancel running or pending pipelines")
                return
            if self._two_press.arm(("cancel", "pipeline", p.id)):
                self.post_message(CancelPipelineRequested(p.id))
            else:
                self.app.notify(f"Cancel pipeline #{p.id}? Press C again.")
        elif self._view_level == 1:
            j = self._get_focused_job()
            if not j or j.status not in (CIStatus.RUNNING, CIStatus.PENDING):
                self.app.notify("Can only cancel running or pending jobs")
                return
            if self._two_press.arm(("cancel", "job", j.id)):
                assert self._current_pipeline is not None
                self.post_message(CancelJobRequested(self._current_pipeline.id, j.id))
            else:
                self.app.notify(f"Cancel job {j.name}? Press C again.", markup=False)

    def action_retry(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if not p or p.status not in (CIStatus.FAILED, CIStatus.CANCELED):
                self.app.notify("Can only retry failed or canceled pipelines")
                return
            if self._two_press.arm(("retry", "pipeline", p.id)):
                self.post_message(RetryPipelineRequested(p.id))
            else:
                self.app.notify(f"Retry pipeline #{p.id}? Press R again.")
        elif self._view_level == 1:
            j = self._get_focused_job()
            if not j or j.status not in (CIStatus.FAILED, CIStatus.CANCELED):
                self.app.notify("Can only retry failed or canceled jobs")
                return
            if self._two_press.arm(("retry", "job", j.id)):
                assert self._current_pipeline is not None
                self.post_message(RetryJobRequested(self._current_pipeline.id, j.id))
            else:
                self.app.notify(f"Retry job {j.name}? Press R again.", markup=False)

    def action_open_browser(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if p and p.web_url:
                self.app.open_url(p.web_url)
        elif self._view_level == 1:
            j = self._get_focused_job()
            if j and j.web_url:
                self.app.open_url(j.web_url)

    def action_open_in_editor(self) -> None:
        if self._view_level != 2 or not self._job_log_text:
            return
        try:
            # A log is only read, so a pager will do when no editor is found.
            argv = editor_argv(
                app_config(self.app), fallbacks=(*FALLBACK_EDITORS, "less")
            )
        except EditorUnavailableError as error:
            self.app.notify(error.message, markup=False)
            return
        try:
            job_name = self._current_job.name if self._current_job else "job"
            view_text(
                self.app,
                argv,
                Text.from_ansi(self._job_log_text).plain,
                suffix=".log",
                prefix=f"tongs-joblog-{_safe_file_label(job_name)}-",
            )
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.app.notify(f"Editor failed: {exc}", markup=False)

    def action_search_log(self) -> None:
        if self._view_level != 2:
            self.app.notify("Open a job log first, then press / to search it")
            return
        search_input = self.query_one("#log-search-input", Input)
        if not self._search_active:
            log_widget = self.query_one("#job-log-content", RichLog)
            self._search_saved_scroll = int(log_widget.scroll_offset.y)
        self._search_active = True
        self._search_query = ""
        self._search_matches = []
        self._search_index = 0
        search_input.display = True
        with search_input.prevent(Input.Changed):
            search_input.value = ""
        self._set_search_status(self._search_hint())
        search_input.focus()

    def action_next_match(self) -> None:
        self._step_match(1)

    def action_prev_match(self) -> None:
        self._step_match(-1)

    def _step_match(self, delta: int) -> None:
        if not self._search_matches:
            return
        self._search_index = (self._search_index + delta) % len(self._search_matches)
        self._jump_to_match()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "log-search-input":
            return
        event.stop()
        self._do_search(event.value.strip())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "log-search-input":
            return
        event.stop()
        query = event.value.strip()
        if not query:
            self._close_search(restore_scroll=True)
            return
        event.input.display = False
        if query != self._search_query:
            self._do_search(query)
        elif self._search_matches:
            self._jump_to_match()
        self.focus()

    def _reset_search(self) -> None:
        self._search_active = False
        self._search_query = ""
        self._search_matches = []
        self._search_index = 0
        self._search_saved_scroll = None
        search_input = self.query_one("#log-search-input", Input)
        search_input.display = False
        with search_input.prevent(Input.Changed):
            search_input.value = ""
        self.query_one("#job-log-content", RichLog).auto_scroll = True
        self._set_search_status(Text())

    def _close_search(self, *, restore_scroll: bool) -> None:
        saved = self._search_saved_scroll
        self._reset_search()
        if restore_scroll and saved is not None:
            log_widget = self.query_one("#job-log-content", RichLog)
            log_widget.scroll_to(y=saved, animate=False)
        self.focus()

    @staticmethod
    def _search_hint() -> Text:
        return Text(
            "Search log: type a term, enter to keep, escape to close",
            style=Style(dim=True),
        )

    def _set_search_status(self, content: Text) -> None:
        """Render the status as Text so log content is never parsed as markup."""
        status = self.query_one("#log-search-status", Static)
        status.update(content)
        status.display = bool(content.plain)

    def _do_search(self, query: str) -> None:
        self._search_query = query
        self._search_matches = []
        self._search_index = 0
        if not query:
            self._set_search_status(self._search_hint())
            return

        plain_query = query.lower()
        self._search_matches = [
            i for i, line in enumerate(self._log_search_lines) if plain_query in line
        ]

        if not self._search_matches:
            status = Text()
            status.append("No matches for ", Style(dim=True))
            status.append(query, Style(bold=True))
            self._set_search_status(status)
            return

        self._jump_to_match()

    def _jump_to_match(self) -> None:
        if not self._search_matches:
            return
        target = self._search_matches[self._search_index]
        log_widget = self.query_one("#job-log-content", RichLog)
        log_widget.auto_scroll = False
        log_widget.scroll_to(y=self._rendered_row(target), animate=False)
        total = len(self._search_matches)
        status = Text()
        status.append(f"Match {self._search_index + 1}/{total}", Style(bold=True))
        status.append(f"  line {target + 1}  ", Style(dim=True))
        status.append(self._match_preview(target))
        self._set_search_status(status)

    def _rendered_row(self, line_index: int) -> int:
        """Rendered row of a log line, offset by any lines the widget dropped."""
        return max(0, line_index - self._log_row_offset)

    def _match_preview(self, line_index: int) -> str:
        if not 0 <= line_index < len(self._log_plain_lines):
            return ""
        return self._log_plain_lines[line_index].strip()[:60]
