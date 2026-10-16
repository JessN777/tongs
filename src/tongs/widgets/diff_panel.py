"""Split-pane diff viewer: file tree on left, diff content on right."""

from __future__ import annotations

import asyncio
import difflib
import re
from collections import OrderedDict
from collections.abc import Callable
from enum import Enum
from typing import ClassVar

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Span, Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Markdown, OptionList, Static, Tree
from textual.widgets.option_list import Option

from tongs.config import DEFAULT_SYNTAX_THEME, app_config
from tongs.diff.context import (
    DiffGap,
    ExpandedBlock,
    FileContextModel,
    FileExpansion,
    GapPosition,
    GapRow,
    file_gaps,
    inside_gap,
    is_expandable,
    verify_new_side,
)
from tongs.diff.context import segments as file_segments
from tongs.diff.generate import FileLines
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion
from tongs.helpers import markdown_lines, relative_time
from tongs.state.drafts import DiffSide
from tongs.widgets.ascii import TREE_ICONS, TREE_LINES, TongsMarkdown
from tongs.widgets.review_draft import DraftMarker
from tongs.widgets.split_diff import (
    FOLD_READ_ONLY_MESSAGE,
    GENERATED_COMMENT_MESSAGE,
    RANGE_CROSSES_GAP_MESSAGE,
    READ_ONLY_LINE_MESSAGE,
    TRUNCATED_NOTICE,
    DiffModeChanged,
    DiffModeState,
    DiffSelection,
    DiffSelectionChanged,
    DiffViewMode,
    FileLayout,
    FoldMarker,
    HighlightMap,
    SplitCommentRequested,
    SplitDiffView,
    SplitReplyRequested,
    SplitResolveRequested,
    gap_message,
    is_actionable,
    placeholder_message,
)
from tongs.widgets.tinted_option_list import TintedOptionList
from tongs.widgets.two_press import TwoPressConfirm

_TOKEN_SPLIT = re.compile(r"(\s+)")
"""Word diff tokens: runs of non-space text and the whitespace between them."""

_TAB_SIZE = 4
"""Tab stop the lexers expand tabs to: Rich's ``Syntax`` default."""

MOVE_TO_GAP_MESSAGE = (
    'Move to a "... unchanged lines ..." row to expand it, '
    "or to an expanded line to collapse it."
)
"""Shown when ``e`` is pressed on a row it can neither expand nor collapse."""

NO_MORE_LINES_MESSAGE = "No more lines below."
"""Shown when the file turns out to end where the last hunk ends."""

CONTEXT_LOADING_MESSAGE = "Still loading this file."
"""Shown when ``e`` is pressed again while the file is being read."""

FULL_FILE_MAX_ROWS = 20_000
"""Most code rows the whole-file view draws; one option per row (provisional)."""

FULL_FILE_TOO_LARGE_MESSAGE = (
    "File too large to show in full ({rows:,} lines). Press e on a gap to expand it."
)
"""Shown when ``F`` would draw more than :data:`FULL_FILE_MAX_ROWS` rows."""

EXPANDED_FALLBACK_MAX_LINES = 500
"""Most expanded lines lexed one by one while the whole file is being lexed.

Lexing a line on its own costs a Pygments call on the UI thread, so a large
expansion shows plain text for the moment the worker thread needs instead.
"""

FULL_FILE_LABEL = "Full file"
"""Shown in the file header while the whole-file view is on."""

GENERATED_LABEL = "Generated locally"
"""Shown in the file header of a diff tongs computed from the two versions."""

GENERATING_NOTICE = (
    "Generating diff locally (downloading {count}). Press e again to cancel."
)
"""Replaces the truncation notice while a generation runs."""

GENERATION_CANCELLED_MESSAGE = "Cancelled. Press e to try again."
"""Shown under the truncation notice after a generation is cancelled."""

GENERATION_FAILED_MESSAGE = "Could not generate the diff."
"""Shown under the truncation notice when a generation fails with no reason."""

_NOTICE_STYLE = Style(dim=True, italic=True)
"""Style of the truncation notice rows."""

_GAP_INDENT = "         "
"""Gap rows line up with the in-hunk fold markers."""


class CommentMode(Enum):
    """Mode for inline comment creation."""

    COMMENT = "comment"
    SUGGEST = "suggest"


class CommentRequested(Message):
    """Posted when the user wants to comment from the diff view."""

    def __init__(
        self,
        file: DiffFile | None = None,
        line: DiffLine | None = None,
        mode: CommentMode = CommentMode.COMMENT,
        context_lines: list[DiffLine] | None = None,
        side: DiffSide | None = None,
        selection: DiffSelection | None = None,
    ) -> None:
        super().__init__()
        self.file = file
        self.line = line
        self.mode = mode
        self.context_lines = context_lines
        self.side = side
        self.selection = selection


class DiffFileTree(Tree):
    """File tree showing changed files with status and stats."""

    def on_mount(self) -> None:
        if app_config(self.app).ascii_mode:
            self.ICON_NODE, self.ICON_NODE_EXPANDED = TREE_ICONS
            self.LINES = dict(TREE_LINES)

    def set_files(
        self,
        files: list[DiffFile],
        discussions_by_file: dict[str, list[Discussion]] | None = None,
    ) -> None:
        self.clear()
        self.root.set_label(f"Changed files ({len(files)})")
        disc_map = discussions_by_file or {}
        for i, f in enumerate(files):
            icon = {
                "modified": "[yellow]M[/]",
                "added": "[green]A[/]",
                "deleted": "[red]D[/]",
                "renamed": "[blue]R[/]",
            }.get(f.status.value, "?")
            basename = f.new_path.rsplit("/", 1)[-1]
            # The file name is appended as plain Text so Tree never parses it
            # as markup; a path such as "pages/[id].tsx" must stay intact.
            label = Text.assemble(Text.from_markup(icon), " ", basename)
            stats = f"[green]+{f.additions}[/] [red]-{f.deletions}[/]"
            file_discs = disc_map.get(f.new_path, [])
            unresolved = sum(1 for d in file_discs if not d.is_resolved)
            node = self.root.add(label, data=i)
            if unresolved:
                node.add_leaf(
                    f"{stats}  [yellow]{unresolved} comment{'s' if unresolved != 1 else ''}[/]"
                )
            elif file_discs:
                node.add_leaf(f"{stats}  [dim]{len(file_discs)} resolved[/]")
            else:
                node.add_leaf(stats)
            node.expand()
        self.root.expand()


class ReplyRequested(Message):
    """Posted when the user wants to reply to an existing discussion."""

    def __init__(
        self, discussion_id: str, file: DiffFile, line: DiffLine, author: str = ""
    ) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.file = file
        self.line = line
        self.author = author


class ResolveRequested(Message):
    """Posted when the user wants to resolve/unresolve a discussion."""

    def __init__(self, discussion_id: str, resolved: bool) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.resolved = resolved


class _ThreadToggled(Message):
    """Internal message: discussion expansion state changed, re-render needed."""


class ContextRequested(Message):
    """Posted when a file's unchanged lines are needed to fill its gaps.

    ``gap_index`` names the gap the reader asked for; None asks for the whole
    file. The screen reads the file and answers with
    :meth:`DiffPanel.apply_file_context` or :meth:`DiffPanel.file_context_failed`.
    """

    def __init__(self, file: DiffFile, gap_index: int | None) -> None:
        super().__init__()
        self.file = file
        self.gap_index = gap_index


class GenerateRequested(Message):
    """Posted when the reader asks to generate a truncated file's diff locally.

    The screen downloads both versions and answers with
    :meth:`DiffPanel.replace_file` or :meth:`DiffPanel.generation_failed`.
    """

    def __init__(self, file: DiffFile) -> None:
        super().__init__()
        self.file = file


class GenerateCancelRequested(Message):
    """Posted when the reader cancels a running generation with a second ``e``."""

    def __init__(self, file: DiffFile) -> None:
        super().__init__()
        self.file = file


def generating_notice(file: DiffFile) -> str:
    """Return the notice shown while a file's diff is being generated."""
    one_side = file.status in (FileStatus.ADDED, FileStatus.DELETED)
    return GENERATING_NOTICE.format(count="1 file" if one_side else "2 files")


def gap_label(gap: DiffGap, *, loading: bool = False) -> str:
    """Return the ASCII text of a collapsed gap row."""
    return f"{_GAP_INDENT}{gap_message(gap, loading=loading)}"


def _expanded_rows(layout: FileLayout) -> int:
    return sum(
        len(segment.lines)
        for segment in layout.segments
        if isinstance(segment, ExpandedBlock)
    )


def full_file_refusal(file: DiffFile) -> str | None:
    """Say why ``F`` cannot show this file in full, or None when it can."""
    if is_expandable(file):
        return None
    if file.status is FileStatus.ADDED:
        return "The diff already shows every line of an added file."
    if file.status is FileStatus.DELETED:
        return "The diff already shows every line of a deleted file."
    if file.is_binary:
        return "Binary file, there are no lines to show."
    if file.is_truncated:
        return "The forge did not send this whole diff. Press e to generate it locally."
    if not file.hunks or file.is_metadata_only:
        return "This file has no changed lines to show around."
    if file.old_mode in _LINK_MODES or file.new_mode in _LINK_MODES:
        return "Symlinks and submodules have no lines to show."
    return "The diff hunks do not line up with the file, so it cannot be shown in full."


_LINK_MODES = frozenset({"120000", "160000"})
"""Git modes of symlinks and submodules."""


class DiffOptionList(TintedOptionList):
    """Per-line interactive diff list with cursor navigation."""

    DEFAULT_CSS = """
    DiffOptionList {
        height: 1fr;
        border: none;
        padding: 0;
        & > .option-list--option {
            padding: 0;
        }
        & > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        &:focus > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        & > .option-list--option-disabled {
            color: $text-disabled;
        }
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("J", "extend_down", "Sel down", show=False),
        Binding("K", "extend_up", "Sel up", show=False),
        Binding("escape", "clear_selection", "Clear sel", show=False),
        Binding(
            "right_square_bracket",
            "next_comment",
            "Next comment",
            show=True,
            key_display="]",
        ),
        Binding(
            "left_square_bracket",
            "prev_comment",
            "Prev comment",
            show=True,
            key_display="[",
        ),
        Binding(
            "d", "toggle_discussion", "Show/Hide thread", show=True, key_display="d"
        ),
        Binding("r", "reply_discussion", "Reply", show=True, key_display="r"),
        Binding("R", "resolve_discussion", "Resolve", show=True, key_display="R"),
        Binding("c", "comment", "Comment", show=True),
        Binding("f3", "suggest", "Suggest", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(compact=True, markup=False, **kwargs)
        self._line_map: dict[int, DiffLine] = {}
        self._current_file: DiffFile | None = None
        self._selection_anchor: int | None = None
        self._comment_map: dict[int, list[Discussion]] = {}
        self._expanded_threads: set[str] = set()
        self._comment_indices: list[int] = []
        self._resolve_confirm = TwoPressConfirm()
        self._explicit_selection_side: DiffSide | None = None
        # Rows that show unchanged lines outside the hunks: collapsed gaps and
        # expanded lines. They take the cursor but never enter _line_map, so
        # they can never form a DiffSelection or a comment anchor.
        self._gap_rows: dict[int, DiffGap] = {}
        self._readonly_rows: dict[int, DiffLine] = {}

    def _on_read_only_row(self) -> bool:
        """Whether the cursor is on a gap row or an expanded line."""
        highlighted = self.highlighted
        return highlighted is not None and (
            highlighted in self._gap_rows or highlighted in self._readonly_rows
        )

    def _range_crosses_read_only_row(self) -> bool:
        """Whether the J/K range includes a gap row or an expanded line."""
        if self._selection_anchor is None or self.highlighted is None:
            return False
        lo = min(self._selection_anchor, self.highlighted)
        hi = max(self._selection_anchor, self.highlighted)
        # A fold inside a hunk hides lines of that hunk, so a range may span it.
        return any(
            lo <= index <= hi and gap.position is not GapPosition.INSIDE
            for index, gap in self._gap_rows.items()
        ) or any(lo <= index <= hi for index in self._readonly_rows)

    def _refuse_read_only(self, *, commenting: bool = False) -> bool:
        """Notify and return True when the cursor or range cannot take a comment."""
        if self._on_read_only_row():
            # A fold inside a hunk hides lines that take a comment once shown.
            gap = self.cursor_gap()
            fold = gap is not None and gap.position is GapPosition.INSIDE
            self.app.notify(FOLD_READ_ONLY_MESSAGE if fold else READ_ONLY_LINE_MESSAGE)
            return True
        if commenting and self._range_crosses_read_only_row():
            self.app.notify(RANGE_CROSSES_GAP_MESSAGE)
            return True
        return False

    def _refuse_generated(self) -> bool:
        """Notify and return True when the file's diff was generated locally."""
        if self._current_file is not None and self._current_file.generated_locally:
            self.app.notify(GENERATED_COMMENT_MESSAGE)
            return True
        return False

    def cursor_gap(self) -> DiffGap | None:
        """Return the gap under the cursor, if the cursor is on a gap row."""
        if self.highlighted is None:
            return None
        return self._gap_rows.get(self.highlighted)

    def _cursor_mark(self) -> tuple[str, object] | None:
        """Identify a gap row or expanded line under the cursor across redraws."""
        highlighted = self.highlighted
        if highlighted is None:
            return None
        if (gap := self._gap_rows.get(highlighted)) is not None:
            return ("gap", gap.key)
        if (line := self._readonly_rows.get(highlighted)) is not None:
            return ("line", line)
        return None

    def cursor_new_line(self) -> int | None:
        """Return the new-side line number under the cursor, if any.

        A gap row answers with its first line, so a redraw that opens or
        closes it can keep the cursor on the same stretch of the file.
        """
        highlighted = self.highlighted
        if highlighted is None:
            return None
        if (gap := self._gap_rows.get(highlighted)) is not None:
            return gap.new_start
        line = self._readonly_rows.get(highlighted) or self._line_map.get(highlighted)
        return line.new_lineno if line is not None else None

    def cursor_line(self) -> DiffLine | None:
        """Return the expanded or hunk line under the cursor, if any."""
        highlighted = self.highlighted
        if highlighted is None:
            return None
        return self._readonly_rows.get(highlighted) or self._line_map.get(highlighted)

    def land_on(self, new_line: int) -> bool:
        """Move the cursor to the row that shows a new-side line number."""
        rows = {**self._line_map, **self._readonly_rows}
        index = next(
            (i for i, line in sorted(rows.items()) if line.new_lineno == new_line),
            None,
        )
        if index is None:
            index = next(
                (
                    i
                    for i, gap in sorted(self._gap_rows.items())
                    if gap.covers(new_line)
                ),
                None,
            )
        if index is None:
            return False
        self.highlighted = index
        self.scroll_to_highlight()
        return True

    def _restore_cursor_mark(self, mark: tuple[str, object]) -> bool:
        kind, value = mark
        if kind == "gap":
            index = next(
                (i for i, gap in self._gap_rows.items() if gap.key == value), None
            )
        else:
            index = self._find_read_only(value)
        if index is None:
            return False
        self.highlighted = index
        self.scroll_to_highlight()
        return True

    def _find_read_only(self, target: object) -> int | None:
        return next(
            (index for index, line in self._readonly_rows.items() if line is target),
            None,
        )

    def _reset_rows(self) -> None:
        """Forget every per-row map before the options are rebuilt."""
        self._line_map.clear()
        self._line_types.clear()
        self._comment_map.clear()
        self._comment_indices.clear()
        self._gap_rows.clear()
        self._readonly_rows.clear()

    @property
    def selection(self) -> DiffSelection | None:
        """Return the unified cursor as an original-coordinate selection."""
        if (
            self._current_file is None
            or self.highlighted is None
            or (line := self._line_map.get(self.highlighted)) is None
        ):
            return None
        inferred_side = (
            DiffSide.OLD if line.line_type is LineType.DELETION else DiffSide.NEW
        )
        side = (
            self._explicit_selection_side
            if self._explicit_selection_side is not None
            and is_actionable(line, self._explicit_selection_side)
            else inferred_side
        )
        selected_lines = self._get_selection_lines() or [line]
        actionable = tuple(
            candidate for candidate in selected_lines if is_actionable(candidate, side)
        )
        if line not in actionable:
            actionable = (line,)
        return DiffSelection(self._current_file, side, line, actionable)

    def restore_selection(self, selection: DiffSelection) -> bool:
        """Restore a selection by its original parsed line objects."""
        if self._current_file != selection.file:
            return False
        selected = self._find_line(selection.line)
        if selected is None:
            return False
        self.highlighted = selected
        self._explicit_selection_side = selection.side
        range_indices = [
            index
            for line in selection.lines
            if (index := self._find_line(line)) is not None
        ]
        self._selection_anchor = None
        if len(range_indices) > 1:
            self._selection_anchor = (
                min(range_indices)
                if selected == max(range_indices)
                else max(range_indices)
            )
        self.scroll_to_highlight()
        return True

    def _find_line(self, target: DiffLine) -> int | None:
        return next(
            (
                index
                for index, line in self._line_map.items()
                if line is target or line == target
            ),
            None,
        )

    def _in_selection_range(self, option_index: int) -> bool:
        """Check whether an option index falls within the active visual selection."""
        if self._selection_anchor is None:
            return False
        cursor = self.highlighted
        if cursor is None:
            return False
        lo = min(self._selection_anchor, cursor)
        hi = max(self._selection_anchor, cursor)
        return lo <= option_index <= hi

    def _get_selection_lines(self) -> list[DiffLine]:
        """Return DiffLine objects in the current selection range."""
        if self._selection_anchor is None or self.highlighted is None:
            return []
        lo = min(self._selection_anchor, self.highlighted)
        hi = max(self._selection_anchor, self.highlighted)
        return [self._line_map[i] for i in range(lo, hi + 1) if i in self._line_map]

    # -- key handlers -------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "clear_selection":
            return self._selection_anchor is not None
        return True

    def action_clear_selection(self) -> None:
        self._selection_anchor = None
        self.refresh()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_toggle_discussion(self) -> None:
        """Expand or collapse discussion threads on the current line."""
        if self.highlighted is None:
            return
        discussions = self._comment_map.get(self.highlighted)
        if not discussions:
            # A read-only row without a thread explains itself; one with a
            # thread, such as an expanded line carrying an outdated thread,
            # can still show and hide it.
            self._refuse_read_only()
            return
        disc_ids = {d.id for d in discussions}
        if disc_ids & self._expanded_threads:
            self._expanded_threads -= disc_ids
        else:
            self._expanded_threads |= disc_ids
        self.post_message(_ThreadToggled())

    def _get_target_discussion(self) -> Discussion | None:
        """Get the most relevant discussion on the current line (first unresolved, or first)."""
        if self.highlighted is None:
            return None
        discussions = self._comment_map.get(self.highlighted)
        if not discussions:
            return None
        for d in discussions:
            if not d.is_resolved:
                return d
        return discussions[0]

    def action_reply_discussion(self) -> None:
        """Open reply editor for the discussion on the current line."""
        if self._refuse_generated() or self._refuse_read_only():
            return
        disc = self._get_target_discussion()
        if disc is None:
            self.app.notify("No discussion on this line")
            return
        dl = self._line_map.get(self.highlighted)
        if dl is None:
            return
        self.post_message(
            ReplyRequested(
                discussion_id=disc.id,
                file=self._current_file,
                line=dl,
                author=disc.root_comment.author.username,
            )
        )

    def action_resolve_discussion(self) -> None:
        """Resolve or unresolve the discussion on the current line."""
        if self._refuse_read_only():
            return
        disc = self._get_target_discussion()
        if disc is None:
            self.app.notify("No discussion on this line")
            return
        if not disc.resolvable:
            self.app.notify("Thread resolution not supported")
            return
        if self._resolve_confirm.arm(disc.id):
            self.post_message(ResolveRequested(disc.id, resolved=not disc.is_resolved))
        else:
            action = "Unresolve" if disc.is_resolved else "Resolve"
            self.app.notify(
                f"{action} thread by @{disc.root_comment.author.username}? Press R again.",
                markup=False,
            )

    def action_comment(self) -> None:
        """Post a CommentRequested message for the highlighted line."""
        if self.highlighted is None or self._refuse_generated():
            return
        if self._refuse_read_only(commenting=True):
            return
        dl = self._line_map.get(self.highlighted)
        if dl is None:
            self.app.notify("Move to a code line to comment")
            return

        context = (
            self._get_selection_lines() if self._selection_anchor is not None else None
        )
        selection = self.selection
        self.post_message(
            CommentRequested(
                file=self._current_file,
                line=dl,
                mode=CommentMode.COMMENT,
                context_lines=context,
                side=selection.side if selection is not None else None,
                selection=selection,
            )
        )
        self._selection_anchor = None

    def action_suggest(self) -> None:
        """Post a CommentRequested message with SUGGEST mode."""
        if self.highlighted is None or self._refuse_generated():
            return
        if self._refuse_read_only(commenting=True):
            return
        dl = self._line_map.get(self.highlighted)
        if dl is None:
            self.app.notify("Move to a code line to suggest")
            return
        if dl.line_type == LineType.DELETION:
            self.app.notify("Cannot suggest on deletion lines")
            return
        if self._explicit_selection_side is DiffSide.OLD:
            self.app.notify("Suggestions are available on the new side only")
            return

        context = (
            self._get_selection_lines() if self._selection_anchor is not None else None
        )
        selection = self.selection
        self.post_message(
            CommentRequested(
                file=self._current_file,
                line=dl,
                mode=CommentMode.SUGGEST,
                context_lines=context,
                side=selection.side if selection is not None else None,
                selection=selection,
            )
        )
        self._selection_anchor = None

    def _move_cursor_down(self) -> None:
        """Move cursor down via OptionList (no selection logic)."""
        OptionList.action_cursor_down(self)

    def _move_cursor_up(self) -> None:
        """Move cursor up via OptionList (no selection logic)."""
        OptionList.action_cursor_up(self)

    def action_cursor_down(self) -> None:
        self._selection_anchor = None
        self._explicit_selection_side = None
        self._move_cursor_down()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_cursor_up(self) -> None:
        self._selection_anchor = None
        self._explicit_selection_side = None
        self._move_cursor_up()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_extend_down(self) -> None:
        """Shift+J: extend selection downward."""
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        self._move_cursor_down()
        self.refresh()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_extend_up(self) -> None:
        """Shift+K: extend selection upward."""
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        self._move_cursor_up()
        self.refresh()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_next_comment(self) -> None:
        """Jump to the next line with a discussion."""
        if not self._comment_indices:
            self.app.notify("No comments in this file")
            return
        import bisect

        current = self.highlighted or 0
        pos = bisect.bisect_right(self._comment_indices, current)
        if pos >= len(self._comment_indices):
            pos = 0
        self.highlighted = self._comment_indices[pos]
        self.scroll_to_highlight()

    def action_prev_comment(self) -> None:
        """Jump to the previous line with a discussion."""
        if not self._comment_indices:
            self.app.notify("No comments in this file")
            return
        import bisect

        current = self.highlighted or 0
        pos = bisect.bisect_left(self._comment_indices, current) - 1
        if pos < 0:
            pos = len(self._comment_indices) - 1
        self.highlighted = self._comment_indices[pos]
        self.scroll_to_highlight()

    async def _on_click(self, event: events.Click) -> None:
        """Handle click: Ctrl+Click extends selection, plain click clears."""
        clicked_option: int | None = event.style.meta.get("option")
        if clicked_option is None or self._options[clicked_option].disabled:
            return
        if event.ctrl:
            if self._selection_anchor is None:
                self._selection_anchor = self.highlighted
            self.highlighted = clicked_option
            self.refresh()
        else:
            self._selection_anchor = None
            self._explicit_selection_side = None
            self.highlighted = clicked_option
        self.post_message(DiffSelectionChanged(self.selection))


class DiffContent(Widget):
    """Container coordinating unified, split, and Markdown diff views."""

    MIN_SPLIT_WIDTH = 88
    HIGHLIGHT_CACHE_FILES = 16
    """Highlighted files kept for revisits; the least recently shown goes."""
    CONTEXT_CACHE_FILES = 16
    """Read files kept for expanding context; the least recently used goes.

    A file that goes loses its open gaps and whole-file colours with its
    lines, so it reads as before its first expansion.
    """

    DEFAULT_CSS = """
    DiffContent {
        height: 1fr;
    }
    #diff-markdown-preview {
        height: 1fr;
        overflow-y: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._showing_preview: bool = False
        self._current_file: DiffFile | None = None
        self._file_discussions: list[Discussion] = []
        self._requested_mode = DiffViewMode.UNIFIED
        self._effective_mode = DiffViewMode.UNIFIED
        # The Pygments style for code, from ``[ui] syntax_theme`` once mounted.
        self._syntax_theme = DEFAULT_SYNTAX_THEME
        self._preview_selection: DiffSelection | None = None
        self._draft_markers: tuple[DraftMarker, ...] = ()
        # Highlight maps by id(file), holding the file so a reused id misses,
        # least recently shown first.
        self._highlight_cache: OrderedDict[int, tuple[DiffFile, HighlightMap]] = (
            OrderedDict()
        )
        # Per-file expand-in-place state by id(file), holding the file so a
        # reused id misses: the new file's lines once read (least recently
        # used first), which gaps are open, and a read in flight with the gap
        # that asked for it.
        self._context_models: OrderedDict[int, tuple[DiffFile, FileContextModel]] = (
            OrderedDict()
        )
        self._expansions: dict[int, tuple[DiffFile, FileExpansion]] = {}
        self._context_pending: dict[int, tuple[DiffFile, int | None]] = {}
        # Per-file highlight maps with the whole-file colours of expanded
        # lines merged over the per-hunk ones, once the lexer has run. Only a
        # file with lines in ``_context_models`` has one.
        self._context_highlights: dict[int, tuple[DiffFile, HighlightMap]] = {}
        # Local generation state of truncated files by id(file), holding the
        # file so a reused id misses: None while it runs, otherwise the line
        # shown under the truncation notice (a cancel or a failure).
        self._generation: dict[int, tuple[DiffFile, str | None]] = {}

    def compose(self) -> ComposeResult:
        yield DiffOptionList(id="diff-option-list")
        yield SplitDiffView(id="split-diff-view")
        yield TongsMarkdown(id="diff-markdown-preview")

    def on_mount(self) -> None:
        self.query_one(SplitDiffView).display = False
        self.query_one("#diff-markdown-preview", Markdown).display = False
        config = app_config(self.app)
        self._syntax_theme = config.syntax_theme
        # ``[ui] diff_style`` picks the layout a new diff view starts in; the
        # config loader already turned an invalid value into "unified".
        if config.diff_style == DiffViewMode.SPLIT.value:
            self._requested_mode = DiffViewMode.SPLIT

    @property
    def mode_state(self) -> DiffModeState:
        """Return the user preference and current responsive layout."""
        return DiffModeState(self._requested_mode, self._effective_mode)

    @property
    def selection(self) -> DiffSelection | None:
        """Return a source-backed selection independent of visual layout."""
        if self._showing_preview:
            return self._preview_selection
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).selection
        return self.query_one(DiffOptionList).selection

    def request_mode(self, mode: DiffViewMode) -> DiffModeState:
        """Select a preferred layout, applying narrow fallback if necessary."""
        old_state = self.mode_state
        selection = self.selection
        self._requested_mode = mode
        effective = self._effective_for_width(self.size.width)
        self._set_effective_mode(effective, selection)
        state = self.mode_state
        if state != old_state:
            self.post_message(DiffModeChanged(state))
        return state

    def toggle_mode(self) -> DiffModeState:
        """Toggle the persistent user preference."""
        requested = (
            DiffViewMode.UNIFIED
            if self._requested_mode is DiffViewMode.SPLIT
            else DiffViewMode.SPLIT
        )
        return self.request_mode(requested)

    def on_resize(self, event: events.Resize) -> None:
        old_state = self.mode_state
        selection = self.selection
        self._set_effective_mode(self._effective_for_width(event.size.width), selection)
        if self.mode_state != old_state:
            self.post_message(DiffModeChanged(self.mode_state))

    def _effective_for_width(self, width: int) -> DiffViewMode:
        if self._requested_mode is DiffViewMode.SPLIT and width >= self.MIN_SPLIT_WIDTH:
            return DiffViewMode.SPLIT
        return DiffViewMode.UNIFIED

    def _set_effective_mode(
        self,
        mode: DiffViewMode,
        selection: DiffSelection | None,
    ) -> None:
        if mode is self._effective_mode:
            return
        self._copy_expanded_threads(self._effective_mode, mode)
        self._effective_mode = mode
        if self._current_file is not None:
            self._render_current_file(selection)
        elif not self._showing_preview:
            self._show_active_diff()
        self.post_message(DiffSelectionChanged(self.selection))

    def _copy_expanded_threads(
        self, source: DiffViewMode, target: DiffViewMode
    ) -> None:
        unified = self.query_one(DiffOptionList)
        split = self.query_one(SplitDiffView)
        if source is DiffViewMode.UNIFIED and target is DiffViewMode.SPLIT:
            split._expanded_threads.clear()
            split._expanded_threads.update(unified._expanded_threads)
        elif source is DiffViewMode.SPLIT and target is DiffViewMode.UNIFIED:
            unified._expanded_threads.clear()
            unified._expanded_threads.update(split.expanded_threads)

    def show_file(
        self,
        file: DiffFile,
        discussions: list[Discussion] | None = None,
        draft_markers: tuple[DraftMarker, ...] | None = None,
    ) -> None:
        """Render one file, with its draft markers when given, in one pass.

        The whole-file view is per file: moving to another file turns it off.
        """
        self._reset_full_file(keep=file)
        self._showing_preview = False
        self._current_file = file
        self._file_discussions = discussions or []
        if draft_markers is not None:
            self._draft_markers = draft_markers
        self._render_current_file()

    def set_draft_markers(self, markers: tuple[DraftMarker, ...]) -> None:
        """Replace durable marker projections and preserve the current selection.

        Unchanged markers leave the diff as it is, so saving or refreshing a
        draft that moves no marker does not redraw the file.
        """
        if markers == self._draft_markers:
            return
        self._draft_markers = markers
        if self._current_file is not None:
            self._render_current_file(self.selection)

    def clear_highlight_cache(self) -> None:
        """Forget highlighted files, for example when a new diff is loaded."""
        self._highlight_cache.clear()

    # -- expand in place ----------------------------------------------------

    def clear_file_context(self) -> None:
        """Forget read files, open gaps, pending reads and generations."""
        self._context_models.clear()
        self._expansions.clear()
        self._context_pending.clear()
        self._context_highlights.clear()
        self._generation.clear()

    # -- local generation ---------------------------------------------------

    def generation_running(self, file: DiffFile) -> bool:
        """Whether this file's diff is being generated locally."""
        entry = self._generation.get(id(file))
        return entry is not None and entry[0] is file and entry[1] is None

    def set_generation(self, file: DiffFile, status: str | None) -> None:
        """Mark a generation as running (None) or record the line it left."""
        self._generation[id(file)] = (file, status)
        self._rerender_if_current(file)

    def clear_generation(self, file: DiffFile, *, rerender: bool = True) -> None:
        """Forget a file's generation state so its notice reads as at first."""
        entry = self._generation.get(id(file))
        if entry is not None and entry[0] is file:
            del self._generation[id(file)]
            if rerender:
                self._rerender_if_current(file)

    def truncation_notice(self, file: DiffFile) -> tuple[str, ...]:
        """Return the notice rows of a truncated file, or ``()`` for any other."""
        if not file.is_truncated or file.is_binary:
            return ()
        entry = self._generation.get(id(file))
        if entry is None or entry[0] is not file:
            return (TRUNCATED_NOTICE,)
        note = entry[1]
        if note is None:
            return (generating_notice(file),)
        return (TRUNCATED_NOTICE, note)

    def seed_file_context(
        self, file: DiffFile, lines: tuple[str, ...], ends_with_newline: bool
    ) -> None:
        """Store a file's new version, already in hand, without opening a gap.

        A generated diff comes with its new version, so its gaps show their
        lengths at once and open with no further read.
        """
        model = FileContextModel(file, lines, ends_with_newline)
        self._store_context_model(file, model)
        self._highlight_context(file, model)

    def context_model(self, file: DiffFile) -> FileContextModel | None:
        """Return the file's read lines, when they have been loaded."""
        entry = self._context_models.get(id(file))
        if entry is None or entry[0] is not file:
            return None
        self._context_models.move_to_end(id(file))
        return entry[1]

    def _store_context_model(self, file: DiffFile, model: FileContextModel) -> None:
        """Keep a file's read lines, letting the least recently used file go."""
        self._context_models[id(file)] = (file, model)
        self._context_models.move_to_end(id(file))
        while len(self._context_models) > self.CONTEXT_CACHE_FILES:
            key, _entry = self._context_models.popitem(last=False)
            # Without its lines a file shows no open gap, so it drops them too.
            self._expansions.pop(key, None)
            self._context_highlights.pop(key, None)

    def file_expansion(self, file: DiffFile) -> FileExpansion:
        """Return which of the file's gaps are open."""
        entry = self._expansions.get(id(file))
        return entry[1] if entry is not None and entry[0] is file else FileExpansion()

    def _set_expansion(self, file: DiffFile, expansion: FileExpansion) -> None:
        self._expansions[id(file)] = (file, expansion)

    def context_pending(self, file: DiffFile) -> bool:
        """Whether a read of this file is in flight."""
        entry = self._context_pending.get(id(file))
        return entry is not None and entry[0] is file

    def _pending_gap(self, file: DiffFile) -> int | None:
        entry = self._context_pending.get(id(file))
        return entry[1] if entry is not None and entry[0] is file else None

    def cursor_gap(self) -> DiffGap | None:
        """Return the gap row under the cursor in the active layout, if any."""
        if self._showing_preview or self._current_file is None:
            return None
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).cursor_gap()
        return self.query_one(DiffOptionList).cursor_gap()

    def _cursor_line(self) -> DiffLine | None:
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).cursor_line()
        return self.query_one(DiffOptionList).cursor_line()

    def _cursor_new_line(self) -> int | None:
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).cursor_new_line()
        return self.query_one(DiffOptionList).cursor_new_line()

    def _land_on(self, new_line: int) -> bool:
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).land_on(new_line)
        return self.query_one(DiffOptionList).land_on(new_line)

    def mark_context_pending(self, file: DiffFile, gap_index: int | None) -> None:
        """Record a read in flight and show its gap row, or all of them, as loading."""
        self._context_pending[id(file)] = (file, gap_index)
        self._rerender_if_current(file)

    def expand_gap(self, file: DiffFile, gap_index: int) -> bool:
        """Open one gap of a file whose lines are loaded.

        The cursor lands on the first newly shown line, in either layout, if
        it is still on that gap's row. A reader who moved on while the file
        was read keeps their cursor and selection. Returns False when the gap
        has no lines, which happens when the file ends at the last hunk.
        """
        model = self.context_model(file)
        if model is None:
            return False
        lines = model.expanded_lines(gap_index)
        if not lines:
            self._rerender_if_current(file)
            return False
        self._set_expansion(file, self.file_expansion(file).expand(gap_index))
        if self._current_file is file and not self._showing_preview:
            gap = self.cursor_gap()
            on_gap = (
                gap is not None
                and gap.position is not GapPosition.INSIDE
                and gap.index == gap_index
            )
            self._render_current_file(self.selection)
            if on_gap:
                self._land_on_read_only(lines[0])
            self.post_message(DiffSelectionChanged(self.selection))
        return True

    def _land_on_read_only(self, line: DiffLine) -> None:
        if self._effective_mode is DiffViewMode.SPLIT:
            split = self.query_one(SplitDiffView)
            index = split.find_read_only(line)
            if index is not None:
                split.highlight_row(index)
            return
        option_list = self.query_one(DiffOptionList)
        index = option_list._find_read_only(line)
        if index is not None:
            option_list.highlighted = index
            option_list.scroll_to_highlight()

    def reveal_fold(self, file: DiffFile, gap: DiffGap) -> None:
        """Show the context lines a fold inside a hunk hides; no read is needed."""
        self._set_expansion(file, self.file_expansion(file).reveal_fold(gap))
        if self._current_file is file and not self._showing_preview:
            self._render_current_file()
            self._land_on(gap.new_start)
            self.post_message(DiffSelectionChanged(self.selection))

    def collapse_at_cursor(self, file: DiffFile) -> bool:
        """Hide again the expanded line or opened fold under the cursor.

        An expanded line collapses its whole gap back to its row, and a line
        shown from a fold inside a hunk folds again; the cursor lands on that
        row. In the whole-file view either one also turns the view off,
        keeping what was open before it. The lines stay loaded, so ``e``
        opens them again with no read. Returns False, changing nothing, when
        the cursor is on neither, including a context line the hunk always
        showed.
        """
        if self._current_file is not file or self._showing_preview:
            return False
        line = self._cursor_line()
        if line is None or line.new_lineno is None:
            return False
        expansion = self.file_expansion(file)
        if line.line_type is LineType.EXPANDED:
            gap = next(
                (
                    segment.gap
                    for segment in self._layout(file).segments
                    if isinstance(segment, ExpandedBlock)
                    and any(shown is line for shown in segment.lines)
                ),
                None,
            )
            if gap is None:
                return False
            self._set_expansion(file, expansion.collapse(gap.index))
            self._render_current_file()
            self._land_on(gap.new_start)
            self.post_message(DiffSelectionChanged(self.selection))
            return True
        if line.line_type is not LineType.CONTEXT:
            return False
        refolded = expansion.refold(line.new_lineno)
        if refolded == expansion or not self._folds_line(file, line, refolded):
            # A thread or a draft keeps the line on screen, or it sits at the
            # edge of a run where the hunk always shows it: nothing folds.
            return False
        self._set_expansion(file, refolded)
        self._render_current_file()
        self._land_on(line.new_lineno)
        self.post_message(DiffSelectionChanged(self.selection))
        return True

    def _folds_line(
        self, file: DiffFile, line: DiffLine, expansion: FileExpansion
    ) -> bool:
        """Whether a hunk line would sit inside a fold under ``expansion``.

        The active layout runs its own fold on the line's hunk, with the
        threads and drafts that keep lines on screen, without drawing it.
        """
        hunk = next(
            (hunk for hunk in file.hunks if any(shown is line for shown in hunk.lines)),
            None,
        )
        if hunk is None:
            return False
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).folds_line(file, hunk, line, expansion)
        renderer = DiffRenderer(
            file.language,
            comment_lines=_build_comment_lines(self._file_discussions),
            protected_lines=self._draft_line_keys(file),
            revealed=expansion.reveals,
            syntax_theme=self._syntax_theme,
        )
        return renderer.folds_line(hunk, line)

    def set_full_file(self, file: DiffFile, full: bool) -> None:
        """Switch the whole-file view of one file, keeping the reader's place.

        Switching it off restores the gaps and folds that were open before.
        """
        expansion = self.file_expansion(file)
        if expansion.full is full:
            return
        current = self._current_file is file and not self._showing_preview
        selection = self.selection if current else None
        target = self._cursor_new_line() if current else None
        self._set_expansion(file, expansion.with_full(full))
        if not current:
            return
        self._render_current_file(selection)
        restored = self.selection
        if target is not None and (
            selection is None or restored is None or restored.line is not selection.line
        ):
            # The cursor was on a gap row or an expanded line, or on a line
            # that now folds away: stay on the same line, or on the gap row
            # or fold that now holds it.
            self._land_on(target)
        self.post_message(DiffSelectionChanged(self.selection))

    def _reset_full_file(self, keep: DiffFile) -> None:
        """Turn the whole-file view off for every file but ``keep``."""
        for key, (file, expansion) in list(self._expansions.items()):
            if file is not keep and expansion.full:
                self._expansions[key] = (file, expansion.with_full(False))

    def apply_file_context(
        self,
        file: DiffFile,
        lines: tuple[str, ...],
        ends_with_newline: bool,
        gap_index: int | None,
    ) -> bool:
        """Store a file's lines and open the gap that asked for them.

        With no gap, the lines are stored for the whole-file view, which the
        caller then switches on. Returns False when the requested gap turned
        out to hold no lines.
        """
        self._context_pending.pop(id(file), None)
        model = FileContextModel(file, lines, ends_with_newline)
        self._store_context_model(file, model)
        self._highlight_context(file, model)
        if gap_index is None:
            self._rerender_if_current(file)
            return True
        return self.expand_gap(file, gap_index)

    def file_context_failed(self, file: DiffFile) -> None:
        """Drop a failed read so the gap row reads as before."""
        if self.context_pending(file):
            del self._context_pending[id(file)]
        self._rerender_if_current(file)

    def _rerender_if_current(self, file: DiffFile) -> None:
        if self._current_file is file and not self._showing_preview:
            self._render_current_file(self.selection)

    # -- whole-file highlighting --------------------------------------------

    @work(group="diff-context-highlight", exit_on_error=False)
    async def _highlight_context(self, file: DiffFile, model: FileContextModel) -> None:
        """Colour the expanded lines by lexing the whole new file once.

        Lexing runs in a thread. Until it ends, and if the lexer fails, a few
        expanded lines are lexed one by one and a large expansion stays plain
        (see :data:`EXPANDED_FALLBACK_MAX_LINES`).
        """
        language = file.language or "text"
        if language == "text" or not model.gaps:
            return
        highlighted = await asyncio.to_thread(
            _highlight_new_file, language, model, self._syntax_theme
        )
        if not highlighted or self.context_model(file) is not model:
            return
        # Unchanged lines read the same on both sides, so both get the colours.
        base = self._highlight_map(file)
        merged = HighlightMap(
            old={**base.old, **highlighted}, new={**base.new, **highlighted}
        )
        self._context_highlights[id(file)] = (file, merged)
        self._rerender_if_current(file)

    def _file_highlight(self, file: DiffFile) -> HighlightMap:
        """Return the file's highlight map with its expanded lines merged in."""
        entry = self._context_highlights.get(id(file))
        if entry is not None and entry[0] is file:
            return entry[1]
        return self._highlight_map(file)

    def _layout(self, file: DiffFile) -> FileLayout:
        """What both layouts draw around and inside the file's hunks."""
        model = self.context_model(file)
        expansion = self.file_expansion(file)
        gaps = model.gaps if model is not None else file_gaps(file)
        loading: frozenset[int] = frozenset()
        if self.context_pending(file):
            pending_gap = self._pending_gap(file)
            loading = frozenset(
                gap.index
                for gap in gaps
                if pending_gap is None or gap.index == pending_gap
            )
        return FileLayout(
            file_segments(file, model, expansion),
            loading,
            expansion,
            self.truncation_notice(file),
        )

    def _highlight_map(self, file: DiffFile) -> HighlightMap:
        """Return the file's highlight map, running Pygments only once per file."""
        cached = self._highlight_cache.get(id(file))
        if cached is not None and cached[0] is file:
            self._highlight_cache.move_to_end(id(file))
            return cached[1]
        highlight_map = _build_highlight_map(file, self._syntax_theme)
        self._highlight_cache[id(file)] = (file, highlight_map)
        self._highlight_cache.move_to_end(id(file))
        while len(self._highlight_cache) > self.HIGHLIGHT_CACHE_FILES:
            self._highlight_cache.popitem(last=False)
        return highlight_map

    def _render_current_file(self, selection: DiffSelection | None = None) -> None:
        file = self._current_file
        if file is None:
            return
        if self._effective_mode is DiffViewMode.SPLIT:
            split = self.query_one(SplitDiffView)
            split.show_file(
                file,
                self._file_discussions,
                self._file_highlight(file),
                selection,
                self._draft_markers,
                self._layout(file),
            )
        else:
            self._show_diff(file, selection)
        self._show_active_diff()

    def _show_active_diff(self) -> None:
        unified = self.query_one(DiffOptionList)
        split = self.query_one(SplitDiffView)
        markdown = self.query_one("#diff-markdown-preview", Markdown)
        unified.display = (
            not self._showing_preview and self._effective_mode is DiffViewMode.UNIFIED
        )
        split.display = (
            not self._showing_preview and self._effective_mode is DiffViewMode.SPLIT
        )
        markdown.display = self._showing_preview

    def _show_diff(
        self, file: DiffFile, selection: DiffSelection | None = None
    ) -> None:
        option_list = self.query_one(DiffOptionList)
        ascii_mode = app_config(self.app).ascii_mode
        cursor_mark = (
            option_list._cursor_mark() if option_list._current_file is file else None
        )
        option_list.clear_options()
        option_list._reset_rows()
        option_list._current_file = file
        option_list._selection_anchor = None
        option_list._explicit_selection_side = None
        option_list._resolve_confirm.reset()

        if file.is_binary or not file.hunks:
            # One shared line with the split view, so a binary, empty, mode
            # only, rename only or forge-withheld file reads the same in both
            # layouts instead of claiming the diff was too large for the API.
            messages = self.truncation_notice(file) or (placeholder_message(file),)
            option_list.add_options(
                [
                    Option(Text(message, style=Style(dim=True)), disabled=True)
                    for message in messages
                ]
            )
            return

        disc_index = _build_discussion_index(self._file_discussions)
        comment_lines = _build_comment_lines(self._file_discussions)
        expanded = option_list._expanded_threads
        layout = self._layout(file)

        renderer = DiffRenderer(
            file.language,
            comment_lines=comment_lines,
            highlight_map=self._file_highlight(file),
            protected_lines=self._draft_line_keys(file),
            revealed=layout.expansion.reveals,
            expanded_fallback=_expanded_rows(layout) <= EXPANDED_FALLBACK_MAX_LINES,
            syntax_theme=self._syntax_theme,
        )

        # A file the forge sent only in part says so above its hunks.
        all_options: list[Option] = [
            Option(Text(f"  {message}", style=_NOTICE_STYLE), disabled=True)
            for message in layout.notice
        ]
        option_idx = len(all_options)
        first_changed_idx: int | None = None

        for segment in layout.segments:
            if isinstance(segment, GapRow):
                gap = segment.gap
                loading = gap.index in layout.loading
                all_options.append(
                    Option(Text(gap_label(gap, loading=loading), style=Style(dim=True)))
                )
                option_list._gap_rows[option_idx] = gap
                option_idx += 1
                continue
            if isinstance(segment, ExpandedBlock):
                # A thread can point at a line outside the current hunks, for
                # example one left on an earlier revision: it is drawn under
                # its expanded line like a hunk line's thread, while the line
                # itself stays read-only and never becomes a comment anchor.
                for dl in segment.lines:
                    all_options.append(Option(renderer._render_line(dl)))
                    option_list._readonly_rows[option_idx] = dl
                    option_list._line_types[option_idx] = dl.line_type
                    line_discs = _match_discussions(dl, disc_index)
                    if line_discs:
                        option_list._comment_map[option_idx] = line_discs
                        option_list._comment_indices.append(option_idx)
                    option_idx += 1
                    expanded_discs = [d for d in line_discs if d.id in expanded]
                    for block_line in _render_thread_block(
                        expanded_discs,
                        ascii_mode=ascii_mode,
                        code_theme=self._syntax_theme,
                    ):
                        all_options.append(Option(block_line, disabled=True))
                        option_idx += 1
                continue
            hunk = segment.hunk
            header_text = Text(f"  {hunk.header}", style=Style(bold=True, dim=True))
            all_options.append(Option(header_text, disabled=True))
            option_idx += 1

            for dl, text in renderer.render_lines(hunk, hunk_index=segment.hunk_index):
                if isinstance(dl, DiffGap):
                    all_options.append(Option(text))
                    option_list._gap_rows[option_idx] = dl
                elif dl is None:
                    all_options.append(Option(text, disabled=True))
                elif dl.line_type == LineType.NO_NEWLINE:
                    all_options.append(Option(text, disabled=True))
                    option_list._line_types[option_idx] = dl.line_type
                else:
                    all_options.append(Option(text))
                    option_list._line_map[option_idx] = dl
                    option_list._line_types[option_idx] = dl.line_type
                    line_discs = _match_discussions(dl, disc_index)
                    if line_discs:
                        option_list._comment_map[option_idx] = line_discs
                        option_list._comment_indices.append(option_idx)
                    if first_changed_idx is None and dl.line_type in (
                        LineType.ADDITION,
                        LineType.DELETION,
                    ):
                        first_changed_idx = option_idx
                    option_idx += 1
                    expanded_discs = [d for d in line_discs if d.id in expanded]
                    for block_line in _render_thread_block(
                        expanded_discs,
                        ascii_mode=ascii_mode,
                        code_theme=self._syntax_theme,
                    ):
                        all_options.append(Option(block_line, disabled=True))
                        option_idx += 1
                    for marker in self._markers_for_line(file, dl):
                        all_options.append(
                            Option(_render_draft_marker(marker), disabled=True)
                        )
                        option_idx += 1
                    continue
                option_idx += 1

        option_list.add_options(all_options)

        if first_changed_idx is not None:
            option_list.highlighted = first_changed_idx
        restored = selection is not None and option_list.restore_selection(selection)
        if not restored and cursor_mark is not None:
            option_list._restore_cursor_mark(cursor_mark)

    def on__thread_toggled(self, event: _ThreadToggled) -> None:
        """Re-render the diff when a thread is expanded or collapsed."""
        if self._current_file:
            ol = self.query_one(DiffOptionList)
            selection = ol.selection
            self._show_diff(self._current_file, selection)

    def on_split_comment_requested(self, event: SplitCommentRequested) -> None:
        """Translate split interactions to the established screen contract."""
        event.stop()
        selection = event.selection
        self.post_message(
            CommentRequested(
                file=selection.file,
                line=selection.line,
                mode=CommentMode.SUGGEST if event.suggest else CommentMode.COMMENT,
                context_lines=list(selection.lines),
                side=selection.side,
                selection=selection,
            )
        )

    def on_split_reply_requested(self, event: SplitReplyRequested) -> None:
        event.stop()
        selection = event.selection
        self.post_message(
            ReplyRequested(
                event.discussion.id,
                selection.file,
                selection.line,
                event.discussion.root_comment.author.username,
            )
        )

    def on_split_resolve_requested(self, event: SplitResolveRequested) -> None:
        event.stop()
        self.post_message(ResolveRequested(event.discussion_id, event.resolved))

    def toggle_markdown_preview(self, file: DiffFile) -> None:
        """Toggle between diff view and rendered markdown preview."""
        if not _is_markdown_file(file):
            self.app.notify("Not a markdown file")
            return

        md = self.query_one("#diff-markdown-preview", Markdown)

        if self._showing_preview:
            self._showing_preview = False
            self._render_current_file(self._preview_selection)
            self._preview_selection = None
        else:
            self._preview_selection = self.selection
            self._showing_preview = True
            new_content = _reconstruct_new_content(file)
            md.update(new_content if new_content else "*No content to preview*")
            self._show_active_diff()

    def show_placeholder(self, message: str) -> None:
        option_list = self.query_one(DiffOptionList)
        option_list.clear_options()
        option_list._reset_rows()
        option_list._expanded_threads.clear()
        option_list._current_file = None
        option_list._selection_anchor = None
        option_list._resolve_confirm.reset()
        option_list._explicit_selection_side = None
        option_list.add_option(
            Option(Text(message, style=Style(dim=True)), disabled=True)
        )
        self.query_one(SplitDiffView).show_placeholder(message)
        self._showing_preview = False
        self._preview_selection = None
        self._current_file = None
        self._file_discussions = []
        self._draft_markers = ()
        self._highlight_cache.clear()
        self.clear_file_context()
        self._show_active_diff()
        self.post_message(DiffSelectionChanged(None))

    def _markers_for_line(
        self, file: DiffFile, line: DiffLine
    ) -> tuple[DraftMarker, ...]:
        return tuple(
            marker
            for marker in self._draft_markers
            if marker.matches(file, line, marker.side)
        )

    def _draft_line_keys(self, file: DiffFile) -> set[tuple[int | None, int | None]]:
        return {
            (marker.old_line, marker.new_line)
            for marker in self._draft_markers
            if marker.old_path == file.old_path and marker.new_path == file.new_path
        }

    def jump_to(
        self,
        line: int | None,
        side: DiffSide,
        discussion_id: str = "",
    ) -> bool:
        """Jump to one original-coordinate line in the active layout."""
        if self._current_file is None:
            return False
        if self._effective_mode is DiffViewMode.SPLIT:
            return self.query_one(SplitDiffView).jump_to(line or 0, side, discussion_id)

        option_list = self.query_one(DiffOptionList)
        if discussion_id:
            option_list._expanded_threads.add(discussion_id)
            self._show_diff(self._current_file, option_list.selection)
        if line is None:
            return False
        for index, candidate in option_list._line_map.items():
            coordinate = (
                candidate.old_lineno if side is DiffSide.OLD else candidate.new_lineno
            )
            if coordinate == line:
                option_list.highlighted = index
                option_list.scroll_to_highlight()
                return True
        return False


class DiffRenderer:
    """Renders diff hunks with syntax highlighting and word-level diffs."""

    def __init__(
        self,
        language: str = "",
        comment_lines: dict[tuple[int | None, int | None], bool] | None = None,
        highlight_map: HighlightMap | None = None,
        protected_lines: set[tuple[int | None, int | None]] | None = None,
        revealed: Callable[[DiffLine], bool] | None = None,
        expanded_fallback: bool = True,
        syntax_theme: str = DEFAULT_SYNTAX_THEME,
    ):
        self._language = language or "text"
        # The Pygments style for code; only token colours and weights are used.
        self._syntax_theme = syntax_theme
        self._comment_lines = comment_lines or {}
        self._highlight_map = highlight_map or HighlightMap()
        self._protected_lines = protected_lines or set()
        # Context lines the reader opened, or every line in the whole-file
        # view: they never fold.
        self._revealed = revealed
        self._expanded_fallback = expanded_fallback

    CONTEXT_LINES = 3

    def render_lines(
        self, hunk: DiffHunk, *, hunk_index: int = 0
    ) -> list[tuple[DiffLine | DiffGap | None, Text]]:
        """Render a hunk into per-line items.

        Returns a list of ``(DiffLine | DiffGap | None, Text)`` tuples:
        - ``(DiffLine, Text)`` for selectable diff lines.
        - ``(DiffGap, Text)`` for a fold, an INSIDE gap ``e`` can reveal.
        - ``(None, Text)`` for any other marker (non-selectable).
        """
        result: list[tuple[DiffLine | DiffGap | None, Text]] = []
        folded = self._fold_context(list(hunk.lines), hunk_index=hunk_index)
        i = 0
        while i < len(folded):
            item = folded[i]

            if isinstance(item, str):
                # Fold marker
                gap = item.gap if isinstance(item, FoldMarker) else None
                result.append(
                    (gap, Text(f"{_GAP_INDENT}{item}", style=Style(dim=True)))
                )
                i += 1
                continue

            dl = item
            if dl.line_type == LineType.DELETION:
                old_lines, new_lines, consumed = _collect_change_block(folded, i)
                if new_lines:
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append(
                            (
                                old_dl,
                                self._render_word_diff_line(
                                    old_dl, new_dl, is_old=True
                                ),
                            )
                        )
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append(
                            (
                                new_dl,
                                self._render_word_diff_line(
                                    old_dl, new_dl, is_old=False
                                ),
                            )
                        )
                    for extra in old_lines[len(new_lines) :]:
                        result.append((extra, self._render_line(extra)))
                    for extra in new_lines[len(old_lines) :]:
                        result.append((extra, self._render_line(extra)))
                else:
                    for old_dl in old_lines:
                        result.append((old_dl, self._render_line(old_dl)))
                i += consumed
                continue

            result.append((dl, self._render_line(dl)))
            i += 1

        return result

    # -- kept for backward compatibility if anything calls render_hunk ------

    def render_hunk(self, hunk: DiffHunk) -> Text:
        result = Text()
        for _dl, text in self.render_lines(hunk):
            result.append_text(text)
            result.append("\n")
        result.rstrip()
        return result

    def folds_line(self, hunk: DiffHunk, line: DiffLine) -> bool:
        """Whether :meth:`render_lines` would hide a line of ``hunk`` in a fold."""
        return not any(item is line for item in self._fold_context(list(hunk.lines)))

    def _fold_context(
        self, lines: list[DiffLine], *, hunk_index: int = 0
    ) -> list[DiffLine | str]:
        """Replace long runs of context lines with fold markers.

        Each marker is a :class:`FoldMarker` carrying the INSIDE gap it hides.
        Lines with a thread, a draft marker or opened by the reader never fold.
        """
        n = self.CONTEXT_LINES
        result: list[DiffLine | str] = []
        ctx_start = None
        revealed = self._revealed

        def flush(start: int, stop: int) -> None:
            if stop - start > n * 2:
                hidden = lines[start + n : stop - n]
                result.extend(lines[start : start + n])
                result.append(
                    FoldMarker(inside_gap(hunk_index, hidden[0], len(hidden)))
                )
                result.extend(lines[stop - n : stop])
            else:
                result.extend(lines[start:stop])

        for i, dl in enumerate(lines):
            key = (dl.old_lineno, dl.new_lineno)
            if (
                dl.line_type == LineType.CONTEXT
                and key not in self._comment_lines
                and key not in self._protected_lines
                and not (revealed is not None and revealed(dl))
            ):
                if ctx_start is None:
                    ctx_start = i
            else:
                if ctx_start is not None:
                    flush(ctx_start, i)
                    ctx_start = None
                result.append(dl)

        if ctx_start is not None:
            flush(ctx_start, len(lines))

        return result

    def _render_line(self, dl: DiffLine) -> Text:
        """Render a single diff line using foreground-only styling.

        Background colors are NOT set here; they are applied by
        TintedOptionList.render_line() so the OptionList cursor highlight
        does not conflict.
        """
        gutter = self._gutter(dl)

        line = Text()
        line.append_text(gutter)

        if dl.line_type == LineType.ADDITION:
            line.append("+", Style(color="green"))
            line.append_text(self._content_text(dl))
        elif dl.line_type == LineType.DELETION:
            line.append("-", Style(color="red"))
            line.append_text(self._content_text(dl))
        elif dl.line_type == LineType.NO_NEWLINE:
            line.append(dl.content, Style(dim=True))
        else:
            line.append(" ")
            line.append_text(self._content_text(dl))

        return line

    def _render_word_diff_line(
        self, old_dl: DiffLine, new_dl: DiffLine, is_old: bool
    ) -> Text:
        """Render one side of a word-diff pair.

        The line keeps its syntax colours and its whitespace exactly; changed
        tokens, including changed runs of indentation, are bold and
        underlined. No bgcolor is applied; line-level backgrounds come from
        TintedOptionList.render_line().
        """
        dl = old_dl if is_old else new_dl
        line = Text()
        line.append_text(self._gutter(dl))
        if is_old:
            line.append("-", Style(color="red"))
        else:
            line.append("+", Style(color="green"))

        content = self._content_text(dl).copy()
        highlight = Style(bold=True, underline=True)
        if not content.spans:
            # Without syntax colours, colour the change like the line marker.
            highlight += Style(color="red" if is_old else "green")

        # Compare the raw contents, so a tab replaced by the spaces the lexer
        # expands it to still shows as a change, then map the changed ranges
        # onto the highlighted text.
        ranges = _changed_ranges(old_dl.content, new_dl.content, is_old)
        offsets = _raw_offsets(dl.content, content.plain)
        if offsets is None:
            ranges = _changed_ranges(
                self._content_text(old_dl).plain,
                self._content_text(new_dl).plain,
                is_old,
            )
        for start, end in ranges:
            if offsets is not None:
                start, end = offsets[start], offsets[end]
            if start < end:
                content.stylize(highlight, start, end)

        line.append_text(content)
        return line

    def _content_text(self, dl: DiffLine) -> Text:
        """Return a line's highlighted content, from the map when it has it.

        An expanded line the map lacks is lexed on its own only while few are
        shown; past that it stays plain until the whole-file colours arrive.
        """
        cached = self._highlight_map.get(dl)
        if cached is not None:
            return cached
        if dl.line_type is LineType.EXPANDED and not self._expanded_fallback:
            return Text(dl.content)
        return self._highlight_content(dl.content)

    def _highlight_content(self, content: str) -> Text:
        if not content or self._language == "text":
            return Text(content)
        try:
            syntax = Syntax(
                content,
                self._language,
                theme=self._syntax_theme,
                line_numbers=False,
                word_wrap=False,
            )
            highlighted = syntax.highlight(content)
            highlighted.rstrip()
            return _without_backgrounds(highlighted)
        except Exception:  # noqa: BLE001 - Third-party lexer failures must leave the diff readable.
            return Text(content)

    def _gutter(self, dl: DiffLine) -> Text:
        old = f"{dl.old_lineno:>4}" if dl.old_lineno is not None else "    "
        new = f"{dl.new_lineno:>4}" if dl.new_lineno is not None else "    "
        gutter = Text(f"{old} {new} ", style=Style(dim=True))
        key = (dl.old_lineno, dl.new_lineno)
        if key in self._comment_lines:
            all_resolved = self._comment_lines[key]
            if all_resolved:
                gutter.append("* ", Style(dim=True))
            else:
                gutter.append("* ", Style(color="yellow", bold=True))
        else:
            gutter.append("  ")
        return gutter


def _collect_change_block(
    lines: list[DiffLine | str], start: int
) -> tuple[list[DiffLine], list[DiffLine], int]:
    """Collect consecutive deletion+addition lines as a change block."""
    old_lines: list[DiffLine] = []
    new_lines: list[DiffLine] = []
    i = start

    while (
        i < len(lines)
        and isinstance(lines[i], DiffLine)
        and lines[i].line_type == LineType.DELETION
    ):
        old_lines.append(lines[i])
        i += 1

    while (
        i < len(lines)
        and isinstance(lines[i], DiffLine)
        and lines[i].line_type == LineType.ADDITION
    ):
        new_lines.append(lines[i])
        i += 1

    return old_lines, new_lines, i - start


def _is_markdown_file(file: DiffFile) -> bool:
    """Check whether a diff file is a Markdown document."""
    return file.language == "markdown" or file.new_path.endswith(".md")


def _reconstruct_new_content(file: DiffFile) -> str:
    """Reconstruct the new-side file content from diff hunks.

    Collects CONTEXT and ADDITION lines (which together represent the
    post-change file) and joins them into a single string.
    """
    lines: list[str] = []
    for hunk in file.hunks:
        for dl in hunk.lines:
            if dl.line_type in (LineType.CONTEXT, LineType.ADDITION):
                lines.append(dl.content)
    return "\n".join(lines)


_GUTTER_PREFIX = "            | "


def _render_thread_block(
    discussions: list[Discussion],
    *,
    ascii_mode: bool = False,
    code_theme: str = DEFAULT_SYNTAX_THEME,
) -> list[Text]:
    """Render expanded discussion threads as Text lines for disabled Options."""
    if not discussions:
        return []

    def _render_body_as_markdown(body: str, indent: str = "") -> list[Text]:
        if not body:
            return []
        lines: list[Text] = []
        for segments in markdown_lines(
            body, 60, ascii_mode=ascii_mode, code_theme=code_theme
        ):
            line = Text()
            line.append(_GUTTER_PREFIX, Style(dim=True))
            if indent:
                line.append(indent)
            for seg in segments:
                if seg.text and seg.text != "\n":
                    line.append(seg.text, seg.style)
            lines.append(line)
        return lines

    def _render_comment(comment, is_reply: bool = False) -> list[Text]:
        lines: list[Text] = []
        indent = "  " if is_reply else ""
        author_text = Text()
        author_text.append(_GUTTER_PREFIX, Style(dim=True))
        author_text.append(
            f"{indent}@{comment.author.username}  {relative_time(comment.created_at)}",
            Style(bold=not is_reply, dim=is_reply),
        )
        lines.append(author_text)
        lines.extend(_render_body_as_markdown(comment.body or "", indent))
        return lines

    result: list[Text] = []
    for i, disc in enumerate(discussions):
        if i > 0:
            sep = Text()
            sep.append("            +-- -- -- --", Style(dim=True))
            result.append(sep)

        header = Text()
        header.append("            ", Style(dim=True))
        if disc.is_resolved:
            header.append("| [resolved] ", Style(dim=True))
        else:
            header.append("| ", Style(color="yellow"))

        header.append(
            f"@{disc.root_comment.author.username}",
            Style(bold=True),
        )
        result.append(header)

        result.extend(_render_body_as_markdown(disc.root_comment.body or ""))

        for reply in disc.root_comment.replies:
            blank = Text()
            blank.append(_GUTTER_PREFIX, Style(dim=True))
            result.append(blank)
            result.extend(_render_comment(reply, is_reply=True))

    return result


def _tokenize(content: str) -> list[str]:
    """Split a line into word and whitespace tokens that join back exactly."""
    return [token for token in _TOKEN_SPLIT.split(content) if token]


def _changed_ranges(old: str, new: str, is_old: bool) -> list[tuple[int, int]]:
    """Return the character ranges of one side that a word diff changed."""
    old_tokens = _tokenize(old)
    new_tokens = _tokenize(new)
    tokens = old_tokens if is_old else new_tokens
    offsets = [0]
    for token in tokens:
        offsets.append(offsets[-1] + len(token))
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    ranges: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        start, end = (i1, i2) if is_old else (j1, j2)
        if tag != "equal" and start < end:
            ranges.append((offsets[start], offsets[end]))
    return ranges


def _raw_offsets(raw: str, plain: str) -> list[int] | None:
    """Map every offset of a line's raw content onto its highlighted text.

    The lexer expands a tab to the next tab stop and reads a lone carriage
    return as a line break, and a line highlighted on its own loses its
    trailing whitespace. The result has ``len(raw) + 1`` entries, or is None
    when the two texts do not line up.
    """
    offsets: list[int] = []
    j = 0
    for index, char in enumerate(raw):
        offsets.append(j)
        if j < len(plain) and (plain[j] == char or (char == "\r" and plain[j] == "\n")):
            j += 1
            continue
        if char == "\t":
            column = j - plain.rfind("\n", 0, j) - 1
            width = _TAB_SIZE - column % _TAB_SIZE
            if plain[j : j + width] == " " * width:
                j += width
                continue
        if j == len(plain) and raw[index:].isspace():
            offsets.extend([j] * (len(raw) - index - 1))
            break
        return None
    offsets.append(j)
    return offsets


def _without_background(style: Style) -> Style:
    """Keep a token's colour and weight but drop its background."""
    if style.bgcolor is None:
        return style
    return Style(
        color=style.color,
        bold=style.bold,
        dim=style.dim,
        italic=style.italic,
        underline=style.underline,
        strike=style.strike,
    )


def _without_backgrounds(text: Text) -> Text:
    """Drop theme backgrounds so the diff and cursor rows show through code.

    Pygments themes such as monokai give every token the theme background,
    which would otherwise paint over the addition, deletion, cursor and
    selection backgrounds the option lists apply to whole rows.
    """
    if isinstance(text.style, Style):
        text.style = _without_background(text.style)
    text.spans = [
        Span(span.start, span.end, _without_background(span.style))
        if isinstance(span.style, Style)
        else span
        for span in text.spans
    ]
    return text


def _highlight_stream(
    syntax: Syntax, lines: list[DiffLine], into: dict[int, Text]
) -> None:
    """Lex consecutive lines of one side as one stream, keyed by ``id(line)``.

    Pygments reads a lone carriage return as a line break. A line with one
    before its last character is therefore lexed on its own from the start,
    because a stream ending in an empty line would hide the extra break and
    shift every later line's colours by one. A line count that still does
    not match also falls back to lexing each line on its own.
    """
    if not lines:
        return
    parts: list[Text] = []
    if not any("\r" in dl.content[:-1] for dl in lines):
        highlighted = syntax.highlight("\n".join(dl.content for dl in lines))
        parts = list(highlighted.split("\n", allow_blank=True))
        if len(parts) == len(lines) + 1 and not parts[-1].plain:
            parts.pop()
    if len(parts) != len(lines):
        parts = []
        for dl in lines:
            single = syntax.highlight(dl.content)
            single.rstrip()
            parts.append(single)
    for dl, part in zip(lines, parts):
        into[id(dl)] = _without_backgrounds(part)


def _highlight_new_file(
    language: str,
    model: FileContextModel,
    syntax_theme: str = DEFAULT_SYNTAX_THEME,
) -> dict[int, Text]:
    """Lex the whole new file once and return the colours of its expanded lines.

    One stream over every line lets a string or comment that opens in a hunk
    colour the unchanged lines after it. Lines the hunks already show are
    lexed only for that context; their colours stay in the per-hunk maps.
    Returns ``{}`` when the lexer fails, leaving the lines plain.
    """
    expanded = model.all_expanded_lines()
    if not expanded:
        return {}
    by_number = {line.new_lineno: line for line in expanded}
    stream = [
        by_number.get(number) or DiffLine(number, number, content, LineType.CONTEXT)
        for number, content in enumerate(model.lines, start=1)
    ]
    colours: dict[int, Text] = {}
    try:
        _highlight_stream(_syntax_for(language, syntax_theme), stream, colours)
    except Exception:  # noqa: BLE001 - Third-party lexer failures must leave the diff readable.
        return {}
    return {id(line): colours[id(line)] for line in expanded if id(line) in colours}


def _syntax_for(language: str, syntax_theme: str = DEFAULT_SYNTAX_THEME) -> Syntax:
    """Return a Syntax with its lexer resolved once, for many streams."""
    syntax = Syntax(
        "", language, theme=syntax_theme, line_numbers=False, word_wrap=False
    )
    lexer = syntax.lexer
    if lexer is not None:
        syntax = Syntax(
            "", lexer, theme=syntax_theme, line_numbers=False, word_wrap=False
        )
    return syntax


def _build_highlight_map(
    file: DiffFile, syntax_theme: str = DEFAULT_SYNTAX_THEME
) -> HighlightMap:
    """Highlight a file once, lexing each hunk's old and new side separately.

    The old stream is context plus deletions and the new stream is context
    plus additions, so an unterminated string or comment on one side cannot
    colour lines of the other side or of a later hunk.
    """
    language = file.language or "text"
    if language == "text" or not file.hunks:
        return HighlightMap()

    old: dict[int, Text] = {}
    new: dict[int, Text] = {}
    try:
        syntax = _syntax_for(language, syntax_theme)
        for hunk in file.hunks:
            _highlight_stream(
                syntax,
                [
                    dl
                    for dl in hunk.lines
                    if dl.line_type in (LineType.CONTEXT, LineType.DELETION)
                ],
                old,
            )
            _highlight_stream(
                syntax,
                [
                    dl
                    for dl in hunk.lines
                    if dl.line_type in (LineType.CONTEXT, LineType.ADDITION)
                ],
                new,
            )
    except Exception:  # noqa: BLE001 - Third-party lexer failures must leave the diff readable.
        return HighlightMap()
    return HighlightMap(old=old, new=new)


def _build_discussion_index(
    discussions: list[Discussion],
) -> dict[tuple[int | None, int | None], list[Discussion]]:
    """Index inline discussions by (old_line, new_line) for fast lookup."""
    index: dict[tuple[int | None, int | None], list[Discussion]] = {}
    for d in discussions:
        if not d.is_inline:
            continue
        rc = d.root_comment
        key = (rc.old_line, rc.new_line)
        index.setdefault(key, []).append(d)
    return index


def _build_comment_lines(
    discussions: list[Discussion],
) -> dict[tuple[int | None, int | None], bool]:
    """Build a map of (old_line, new_line) -> all_resolved for gutter markers."""
    lines: dict[tuple[int | None, int | None], list[bool]] = {}
    for d in discussions:
        if not d.is_inline:
            continue
        rc = d.root_comment
        key = (rc.old_line, rc.new_line)
        lines.setdefault(key, []).append(d.is_resolved)
    return {k: all(v) for k, v in lines.items()}


def _match_discussions(
    dl: DiffLine,
    index: dict[tuple[int | None, int | None], list[Discussion]],
) -> list[Discussion]:
    """Find discussions matching a DiffLine by line numbers."""
    key = (dl.old_lineno, dl.new_lineno)
    result = index.get(key, [])
    if not result and dl.new_lineno is not None:
        result = index.get((None, dl.new_lineno), [])
    if not result and dl.old_lineno is not None:
        result = index.get((dl.old_lineno, None), [])
    return result


def _render_draft_marker(marker: DraftMarker) -> Text:
    state = " stale" if marker.stale else ""
    text = Text()
    text.append(
        f"    DRAFT {marker.side.value}{state}: ", Style(color="yellow", bold=True)
    )
    text.append(marker.body, Style(color="yellow"))
    return text


# Shown literally above the diff while the forge's changed file list is
# truncated. It stays ASCII so it reads the same in ASCII mode.
CHANGES_OVERFLOW_BANNER = (
    "The forge returned only part of the changed files, so this list is "
    "incomplete. Press o to open the review in the browser."
)


class DiffPanel(Widget):
    """Split-pane diff viewer with file tree and content."""

    DEFAULT_CSS = """
    DiffPanel {
        height: 1fr;
    }

    DiffPanel #diff-overflow-banner {
        height: auto;
        padding: 0 1;
        color: $warning;
        display: none;
    }

    DiffPanel DiffFileTree {
        width: 35;
        min-width: 20;
        border-right: solid $accent;
    }

    DiffPanel #diff-right-pane {
        width: 1fr;
    }

    DiffPanel #diff-file-header {
        height: auto;
        padding: 0 1;
    }

    DiffPanel DiffContent {
        width: 1fr;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("n", "next_file", "Next file", show=True),
        Binding("N", "prev_file", "Prev file", show=False, key_display="N"),
        Binding("m", "preview_markdown", "Preview MD", show=False),
        Binding("v", "toggle_diff_mode", "Unified/Split", show=True),
        Binding("e", "expand_context", "Expand/Collapse", show=True),
        Binding("F", "toggle_full_file", "Full file", show=True, key_display="F"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._files: list[DiffFile] = []
        self._current_index: int = 0
        self._discussions_by_file: dict[str, list[Discussion]] = {}
        self._draft_markers: tuple[DraftMarker, ...] = ()
        self._changes_overflow = False
        # The truncated file whose diff is being generated; one at a time.
        self._generating: DiffFile | None = None

    def compose(self) -> ComposeResult:
        yield Static(CHANGES_OVERFLOW_BANNER, id="diff-overflow-banner", markup=False)
        with Horizontal():
            yield DiffFileTree("Files", id="diff-file-tree")
            with Vertical(id="diff-right-pane"):
                yield Static(id="diff-file-header")
                yield DiffContent(id="diff-content")

    @property
    def mode_state(self) -> DiffModeState:
        """Expose the stable requested/effective mode seam."""
        return self.query_one("#diff-content", DiffContent).mode_state

    @property
    def selection(self) -> DiffSelection | None:
        """Expose the current source-coordinate selection."""
        return self.query_one("#diff-content", DiffContent).selection

    def request_mode(self, mode: DiffViewMode) -> DiffModeState:
        """Request a unified or split layout."""
        return self.query_one("#diff-content", DiffContent).request_mode(mode)

    def set_files(
        self,
        files: list[DiffFile],
        discussions: list[Discussion] | None = None,
        *,
        changes_overflow: bool = False,
    ) -> None:
        if not files:
            self.show_placeholder("No changes")
            return
        self._files = files
        self._generating = None
        content = self.query_one("#diff-content", DiffContent)
        content.clear_highlight_cache()
        content.clear_file_context()
        self.set_changes_overflow(changes_overflow)
        self._discussions_by_file = {}
        for d in discussions or []:
            if d.is_inline and d.root_comment.file_path:
                fp = d.root_comment.file_path
                display_path = next(
                    (
                        file.new_path
                        for file in files
                        if fp in {file.old_path, file.new_path}
                    ),
                    fp,
                )
                self._discussions_by_file.setdefault(display_path, []).append(d)

        tree = self.query_one("#diff-file-tree", DiffFileTree)
        tree.set_files(files, self._discussions_by_file)
        self._show_file(0)

    def set_draft_markers(self, markers: tuple[DraftMarker, ...]) -> None:
        """Show durable comments without adding selectable source rows."""
        self._draft_markers = markers
        self.query_one("#diff-content", DiffContent).set_draft_markers(markers)

    @property
    def changes_overflow_shown(self) -> bool:
        """Whether the truncated file list banner is on screen."""
        return bool(self.query_one("#diff-overflow-banner", Static).display)

    def set_changes_overflow(self, overflow: bool) -> None:
        """Record whether the forge truncated the changed file list.

        The banner stays up for as long as files are shown and the review says
        the list is partial, whichever of the two arrives first.
        """
        self._changes_overflow = overflow
        self._sync_overflow_banner()

    def _sync_overflow_banner(self) -> None:
        banner = self.query_one("#diff-overflow-banner", Static)
        banner.display = self._changes_overflow and bool(self._files)

    def show_placeholder(self, message: str) -> None:
        """Replace all source-backed panel state with an authoritative message."""
        self._files = []
        self._current_index = 0
        self._generating = None
        self._discussions_by_file = {}
        self._sync_overflow_banner()
        self.query_one("#diff-file-tree", DiffFileTree).set_files([])
        self.query_one("#diff-file-header", Static).update("")
        self.query_one("#diff-content", DiffContent).show_placeholder(message)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is not None and isinstance(event.node.data, int):
            self._show_file(event.node.data)

    def _show_file(self, index: int) -> None:
        if 0 <= index < len(self._files):
            self._current_index = index
            file = self._files[index]
            file_discs = self._discussions_by_file.get(file.new_path, [])

            content = self.query_one("#diff-content", DiffContent)
            content.show_file(file, file_discs, self._draft_markers)
            self._update_header(file)

    def _update_header(self, file: DiffFile) -> None:
        """Show the file's path, counts, mode change and view state."""
        header = self.query_one("#diff-file-header", Static)
        mode_change = file.mode_change
        full = self.query_one("#diff-content", DiffContent).file_expansion(file).full
        header.update(
            Content.from_markup(
                "[bold]$path[/]  [green]+$additions[/] [red]-$deletions[/]  "
                + ("[yellow]$mode[/]  " if mode_change else "")
                + ("[reverse] $generated [/]  " if file.generated_locally else "")
                + ("[reverse] $full [/]  " if full else "")
                + "[dim]$language[/]",
                path=file.new_path,
                additions=file.additions,
                deletions=file.deletions,
                mode=mode_change,
                generated=GENERATED_LABEL,
                full=FULL_FILE_LABEL,
                language=file.language or "",
            )
        )

    def action_next_file(self) -> None:
        if self._files:
            self._show_file((self._current_index + 1) % len(self._files))

    def action_prev_file(self) -> None:
        if self._files:
            self._show_file((self._current_index - 1) % len(self._files))

    def action_toggle_diff_mode(self) -> None:
        state = self.query_one("#diff-content", DiffContent).toggle_mode()
        if state.narrow_fallback:
            self.app.notify("Split view selected; using unified at this width")

    def action_comment(self) -> None:
        """Fallback comment action when DiffOptionList does not have focus."""
        if self._files and 0 <= self._current_index < len(self._files):
            file = self._files[self._current_index]
            if file.generated_locally:
                self.app.notify(GENERATED_COMMENT_MESSAGE)
                return
            line = self._find_first_changed_line(file)
            if line:
                side = (
                    DiffSide.OLD
                    if line.line_type is LineType.DELETION
                    else DiffSide.NEW
                )
                selection = DiffSelection(file, side, line, (line,))
                self.post_message(
                    CommentRequested(
                        file=file,
                        line=line,
                        side=side,
                        selection=selection,
                    )
                )
                return
        self.post_message(CommentRequested())

    def _find_first_changed_line(self, file: DiffFile) -> DiffLine | None:
        for hunk in file.hunks:
            for dl in hunk.lines:
                if dl.line_type in (LineType.ADDITION, LineType.DELETION):
                    return dl
        return None

    def jump_to_discussion(
        self, file_path: str, line: int | None, discussion_id: str = ""
    ) -> None:
        """Navigate to a specific file and line, expanding the discussion."""
        for i, f in enumerate(self._files):
            if f.new_path == file_path or f.old_path == file_path:
                self._show_file(i)
                file_discussions = self._discussions_by_file.get(f.new_path, [])
                target = next(
                    (
                        discussion
                        for discussion in file_discussions
                        if discussion.id == discussion_id
                    ),
                    None,
                )
                side = (
                    DiffSide.OLD
                    if target is not None
                    and target.root_comment.old_line is not None
                    and target.root_comment.new_line is None
                    else DiffSide.NEW
                )
                content = self.query_one("#diff-content", DiffContent)
                content.jump_to(line, side, discussion_id)
                return

    def _shown_file(self) -> DiffFile | None:
        if self._files and 0 <= self._current_index < len(self._files):
            return self._files[self._current_index]
        return None

    def _owns(self, file: DiffFile) -> bool:
        """Whether a file object belongs to the files on screen now."""
        return self.index_of(file) is not None

    def index_of(self, file: DiffFile) -> int | None:
        """Return where a file object sits in the files on screen, or None."""
        return next(
            (index for index, shown in enumerate(self._files) if shown is file), None
        )

    def action_expand_context(self) -> None:
        """Open the gap under the cursor, or close the one the cursor is in.

        The file is read the first time a gap opens. A fold inside a hunk
        opens at once: the forge already sent its lines. On an expanded line,
        or a line shown from a fold, ``e`` hides those lines again with no
        read. On a file the forge truncated, ``e`` on any other row generates
        the diff locally. While that runs, ``e`` on any row cancels it.
        """
        file = self._shown_file()
        if file is None:
            return
        content = self.query_one("#diff-content", DiffContent)
        if content.generation_running(file):
            # The notice says "Press e again to cancel", wherever the cursor is.
            self._toggle_generation(file)
            return
        gap = content.cursor_gap()
        if gap is None and content.collapse_at_cursor(file):
            # Collapsing in the whole-file view turns it off.
            self._update_header(file)
            return
        if file.is_truncated and not file.is_binary:
            # A partly sent file still folds long context inside its hunks,
            # and those rows say e opens them.
            if gap is not None and gap.position is GapPosition.INSIDE:
                content.reveal_fold(file, gap)
            else:
                self._toggle_generation(file)
            return
        if gap is None:
            self.app.notify(MOVE_TO_GAP_MESSAGE)
            return
        if gap.position is GapPosition.INSIDE:
            content.reveal_fold(file, gap)
            return
        if content.context_model(file) is not None:
            if not content.expand_gap(file, gap.index):
                self.app.notify(NO_MORE_LINES_MESSAGE)
            return
        if content.context_pending(file):
            self.app.notify(CONTEXT_LOADING_MESSAGE)
            return
        content.mark_context_pending(file, gap.index)
        self.post_message(ContextRequested(file, gap.index))

    def action_toggle_full_file(self) -> None:
        """Show the whole current file, or go back to what was open before.

        The first time it reads the file at the review's head, unless the
        hunks and their folds already hold every line there is to show.
        """
        file = self._shown_file()
        if file is None:
            return
        content = self.query_one("#diff-content", DiffContent)
        if content.file_expansion(file).full:
            content.set_full_file(file, False)
            self._update_header(file)
            return
        reason = full_file_refusal(file)
        if reason is not None:
            self.app.notify(reason, markup=False)
            return
        if not file_gaps(file):
            # Nothing lies outside the hunks: unfolding them is the whole file.
            self._show_full_file(file, None)
            return
        model = content.context_model(file)
        if model is not None:
            self._show_full_file(file, model)
            return
        if content.context_pending(file):
            self.app.notify(CONTEXT_LOADING_MESSAGE)
            return
        content.mark_context_pending(file, None)
        self.post_message(ContextRequested(file, None))

    def _show_full_file(self, file: DiffFile, model: FileContextModel | None) -> None:
        # With no read, the hunks hold the whole file: count their lines.
        rows = (
            model.full_view_rows()
            if model is not None
            else sum(len(hunk.lines) for hunk in file.hunks)
        )
        if rows > FULL_FILE_MAX_ROWS:
            self.app.notify(FULL_FILE_TOO_LARGE_MESSAGE.format(rows=rows), markup=False)
            return
        self.query_one("#diff-content", DiffContent).set_full_file(file, True)
        self._update_header(file)

    def apply_file_context(
        self,
        file: DiffFile,
        lines: tuple[str, ...],
        ends_with_newline: bool,
        gap_index: int | None,
    ) -> None:
        """Show a file's unchanged lines read at the displayed revision.

        With no gap the read was for the whole-file view, which switches on
        if the file is still on screen. A reply for a file that is no longer
        loaded, for example after a refresh, is dropped.
        """
        if not self._owns(file):
            return
        content = self.query_one("#diff-content", DiffContent)
        if not content.apply_file_context(file, lines, ends_with_newline, gap_index):
            self.app.notify(NO_MORE_LINES_MESSAGE)
            return
        model = content.context_model(file)
        if gap_index is None and model is not None and self._shown_file() is file:
            self._show_full_file(file, model)

    def file_context_failed(self, file: DiffFile, message: str) -> None:
        """Report why a file's unchanged lines could not be shown."""
        if not self._owns(file):
            return
        self.query_one("#diff-content", DiffContent).file_context_failed(file)
        self.app.notify(message, severity="warning", markup=False)

    def drop_file_context_request(self, file: DiffFile) -> None:
        """Forget a read whose reply is stale, so its gap row reads as before."""
        if self._owns(file):
            self.query_one("#diff-content", DiffContent).file_context_failed(file)

    # -- local generation ---------------------------------------------------

    def _toggle_generation(self, file: DiffFile) -> None:
        """Start generating a truncated file's diff, or cancel the running one."""
        content = self.query_one("#diff-content", DiffContent)
        if content.generation_running(file):
            self.cancel_generation()
            self.post_message(GenerateCancelRequested(file))
            return
        # One generation at a time: starting another one cancels it.
        self.cancel_generation()
        self._generating = file
        content.set_generation(file, None)
        self.post_message(GenerateRequested(file))

    def cancel_generation(self) -> None:
        """Show the running generation, if any, as cancelled.

        The screen stops its download; this only restores the file's notice
        with a line saying it was cancelled.
        """
        file = self._generating
        self._generating = None
        if file is not None and self.is_generating(file):
            content = self.query_one("#diff-content", DiffContent)
            content.set_generation(file, GENERATION_CANCELLED_MESSAGE)

    def is_generating(self, file: DiffFile) -> bool:
        """Whether a generation of this file is running and still wanted."""
        return self._owns(file) and self.query_one(
            "#diff-content", DiffContent
        ).generation_running(file)

    def replace_file(
        self, file_index: int, generated: DiffFile, new_text: FileLines | None
    ) -> bool:
        """Show a generated diff in place of the truncated file at ``file_index``.

        The tree entry stays. When ``new_text`` holds the new version, the
        file's gaps and whole-file view use it with no further read. A reply
        for a generation that was cancelled, or for files that are no longer
        loaded, is dropped and False is returned.
        """
        if not 0 <= file_index < len(self._files):
            return False
        original = self._files[file_index]
        content = self.query_one("#diff-content", DiffContent)
        if not content.generation_running(original):
            return False
        if self._generating is original:
            self._generating = None
        content.clear_generation(original, rerender=False)
        self._files[file_index] = generated
        if (
            new_text is not None
            and is_expandable(generated)
            and verify_new_side(generated, new_text.lines, new_text.ends_with_newline)
        ):
            content.seed_file_context(
                generated, tuple(new_text.lines), new_text.ends_with_newline
            )
        if self._current_index == file_index:
            self._show_file(file_index)
        return True

    def generation_failed(self, file: DiffFile, message: str) -> None:
        """Restore a truncated file's notice with the reason generation failed."""
        if not self.is_generating(file):
            return
        if self._generating is file:
            self._generating = None
        reason = message or GENERATION_FAILED_MESSAGE
        self.query_one("#diff-content", DiffContent).set_generation(file, reason)
        self.app.notify(reason, severity="warning", markup=False)

    def drop_generation(self, file: DiffFile) -> None:
        """Forget a generation whose reply is stale, restoring the plain notice."""
        if not self.is_generating(file):
            return
        if self._generating is file:
            self._generating = None
        self.query_one("#diff-content", DiffContent).clear_generation(file)

    def action_preview_markdown(self) -> None:
        if self._files and 0 <= self._current_index < len(self._files):
            content = self.query_one("#diff-content", DiffContent)
            content.toggle_markdown_preview(self._files[self._current_index])
