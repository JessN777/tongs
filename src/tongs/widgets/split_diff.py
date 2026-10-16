"""Stable mode and selection contracts for split diff widgets."""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Self

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from tongs.config import DEFAULT_SYNTAX_THEME, app_config
from tongs.diff.alignment import align_hunk
from tongs.diff.context import (
    DiffGap,
    ExpandedBlock,
    FileExpansion,
    GapPosition,
    GapRow,
    HunkBlock,
    Segment,
    inside_gap,
)
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, LineType, SplitDiffRow
from tongs.forges.models import Discussion
from tongs.helpers import markdown_lines, relative_time
from tongs.state.drafts import DiffSide
from tongs.widgets.tinted_option_list import TintedOptionList
from tongs.widgets.two_press import TwoPressConfirm

if TYPE_CHECKING:
    from tongs.widgets.review_draft import DraftMarker


READ_ONLY_LINE_MESSAGE = (
    "Expanded lines are read-only. Comment on a line inside a hunk."
)
"""Shown when a comment key is pressed on a gap row or an expanded line."""

FOLD_READ_ONLY_MESSAGE = "Press e to show these lines, then comment on one."
"""Shown when a comment key is pressed on a fold inside a hunk."""

RANGE_CROSSES_GAP_MESSAGE = "A comment range must stay inside one hunk."
"""Shown when a J/K range includes a gap row or an expanded line."""

GENERATED_COMMENT_MESSAGE = "Comments are not available on a diff generated locally."
"""Shown when a comment, suggest or reply key is pressed on a generated diff.

The forge has no diff for the file to anchor a comment to, so it would refuse
one; tongs says so before an editor opens.
"""

TRUNCATED_NOTICE = (
    "This file's diff is too large for the forge to send. Press e to generate "
    "it locally (downloads both versions of the file)."
)
"""Shown for a file whose diff the forge withheld in full or in part."""


def gap_message(gap: DiffGap, *, loading: bool = False) -> str:
    """Return the ASCII text of a collapsed gap or fold, without indentation."""
    if loading:
        return "... loading ..."
    if gap.length is None:
        return "... more lines below (e to expand) ..."
    noun = "line" if gap.length == 1 else "lines"
    return f"... {gap.length} unchanged {noun} (e to expand) ..."


class FoldMarker(str):
    """A fold marker row that carries the in-hunk gap it hides.

    It is still the marker text, so code that treats fold markers as plain
    strings keeps working.
    """

    gap: DiffGap

    def __new__(cls, gap: DiffGap) -> Self:
        marker = super().__new__(cls, gap_message(gap))
        marker.gap = gap
        return marker


@dataclass(frozen=True, slots=True)
class FileLayout:
    """What both diff layouts draw for one file around and inside its hunks.

    ``segments`` interleave hunks with collapsed and expanded gaps,
    ``loading`` names the gaps whose file read is in flight, and
    ``expansion`` says which in-hunk folds stay open. ``notice`` holds the
    rows that stand above the hunks of a file the forge truncated, or in
    place of them when it sent none.
    """

    segments: tuple[Segment, ...]
    loading: frozenset[int] = frozenset()
    expansion: FileExpansion = field(default_factory=FileExpansion)
    notice: tuple[str, ...] = ()


def _counts_against_range(gap: DiffGap) -> bool:
    """Whether a gap row splits a J/K range: only gaps outside the hunks do."""
    return gap.position is not GapPosition.INSIDE


class DiffViewMode(str, Enum):
    """User-selectable diff layouts."""

    UNIFIED = "unified"
    SPLIT = "split"


@dataclass(frozen=True, slots=True)
class DiffModeState:
    """Requested layout and the layout currently possible at this width."""

    requested: DiffViewMode
    effective: DiffViewMode

    @property
    def narrow_fallback(self) -> bool:
        return self.requested is not self.effective


@dataclass(frozen=True)
class HighlightMap:
    """Syntax-highlighted line content for one file, lexed per hunk and side.

    The old side is lexed from context plus deleted lines and the new side
    from context plus added lines, so a string opened on one side never
    colours the other. Context lines appear in both, keyed by ``id(line)``.
    """

    old: Mapping[int, Text] = field(default_factory=dict)
    new: Mapping[int, Text] = field(default_factory=dict)

    def get(self, line: DiffLine, side: DiffSide | None = None) -> Text | None:
        """Return a line's highlighted text on one side, or None if unknown.

        Without a side, a deleted line reads the old side and every other
        line the new side, which is what the unified layout shows.
        """
        if side is None:
            side = DiffSide.OLD if line.line_type is LineType.DELETION else DiffSide.NEW
        return (self.old if side is DiffSide.OLD else self.new).get(id(line))


@dataclass(frozen=True, slots=True)
class DiffSelection:
    """Original source coordinates selected in either diff layout."""

    file: DiffFile
    side: DiffSide
    line: DiffLine
    lines: tuple[DiffLine, ...]

    def __post_init__(self) -> None:
        if not self.lines or self.line not in self.lines:
            raise ValueError("selection lines must include the selected line")
        if not all(is_actionable(line, self.side) for line in self.lines):
            raise ValueError("selection contains a non-actionable diff cell")


class DiffModeChanged(Message):
    """Published when requested or effective diff layout changes."""

    def __init__(self, state: DiffModeState) -> None:
        super().__init__()
        self.state = state


class DiffSelectionChanged(Message):
    """Published when the current original-coordinate selection changes."""

    def __init__(self, selection: DiffSelection | None) -> None:
        super().__init__()
        self.selection = selection


class SplitCommentRequested(Message):
    """Request a legacy immediate comment for an explicit split side."""

    def __init__(self, selection: DiffSelection, *, suggest: bool = False) -> None:
        super().__init__()
        self.selection = selection
        self.suggest = suggest


class SplitReplyRequested(Message):
    """Request a reply using an original-coordinate split selection."""

    def __init__(self, selection: DiffSelection, discussion: Discussion) -> None:
        super().__init__()
        self.selection = selection
        self.discussion = discussion


class SplitResolveRequested(Message):
    """Request a discussion resolution change from the split layout."""

    def __init__(self, discussion_id: str, resolved: bool) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.resolved = resolved


class _SplitThreadToggled(Message):
    def __init__(self, discussion_ids: frozenset[str]) -> None:
        super().__init__()
        self.discussion_ids = discussion_ids


class _SplitCursorChanged(Message):
    def __init__(self, side: DiffSide, option_index: int) -> None:
        super().__init__()
        self.side = side
        self.option_index = option_index


class _SplitScrollChanged(Message):
    def __init__(self, side: DiffSide, offset: float) -> None:
        super().__init__()
        self.side = side
        self.offset = offset


def is_actionable(line: DiffLine | None, side: DiffSide) -> bool:
    """Return whether a rendered cell is a source-backed comment anchor."""
    if line is None or line.line_type in {LineType.HUNK_HEADER, LineType.NO_NEWLINE}:
        return False
    if side is DiffSide.OLD:
        return line.old_lineno is not None and line.line_type in {
            LineType.CONTEXT,
            LineType.DELETION,
        }
    return line.new_lineno is not None and line.line_type in {
        LineType.CONTEXT,
        LineType.ADDITION,
    }


class SplitDiffColumn(TintedOptionList):
    """One synchronized, source-side column of a split diff."""

    DEFAULT_CSS = """
    SplitDiffColumn {
        width: 1fr;
        height: 1fr;
        border: none;
        padding: 0;
        overflow-x: auto;
        text-wrap: nowrap;
        text-overflow: clip;
        & > .option-list--option {
            padding: 0;
        }
        & > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        &:focus > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 20%;
        }
        & > .option-list--option-disabled {
            color: $text-disabled;
        }
    }
    SplitDiffColumn#split-old {
        border-right: solid $accent;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("J", "extend_down", "Sel down", show=False),
        Binding("K", "extend_up", "Sel up", show=False),
        Binding("h", "focus_old", "Old side", show=False),
        Binding("l", "focus_new", "New side", show=False),
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
        Binding("d", "toggle_discussion", "Show/Hide thread", show=True),
        Binding("r", "reply_discussion", "Reply", show=True),
        Binding("R", "resolve_discussion", "Resolve", show=True),
        Binding("c", "comment", "Comment", show=True),
        Binding("f3", "suggest", "Suggest", show=False),
    ]

    def __init__(self, side: DiffSide, **kwargs: object) -> None:
        self.side = side
        self._syncing = False
        super().__init__(compact=True, markup=False, **kwargs)
        self._line_map: dict[int, DiffLine] = {}
        self._comment_map: dict[int, list[Discussion]] = {}
        self._comment_indices: list[int] = []
        self._selection_anchor: int | None = None
        self._current_file: DiffFile | None = None
        self._expanded_threads: set[str] = set()
        self._resolve_confirm = TwoPressConfirm()
        # Gap rows and expanded lines take the cursor in both columns but
        # never enter _line_map, so they never form a comment anchor.
        self._gap_rows: dict[int, DiffGap] = {}
        self._readonly_rows: dict[int, DiffLine] = {}

    @property
    def selection(self) -> DiffSelection | None:
        if (
            self._current_file is None
            or self.highlighted is None
            or (line := self._line_map.get(self.highlighted)) is None
        ):
            return None
        lines = self._selection_lines() or [line]
        return DiffSelection(self._current_file, self.side, line, tuple(lines))

    def cursor_gap(self) -> DiffGap | None:
        """Return the gap under the cursor, if the cursor is on a gap row."""
        if self.highlighted is None:
            return None
        return self._gap_rows.get(self.highlighted)

    def _on_read_only_row(self) -> bool:
        highlighted = self.highlighted
        return highlighted is not None and (
            highlighted in self._gap_rows or highlighted in self._readonly_rows
        )

    def _range_crosses_read_only_row(self) -> bool:
        if self._selection_anchor is None or self.highlighted is None:
            return False
        low = min(self._selection_anchor, self.highlighted)
        high = max(self._selection_anchor, self.highlighted)
        return any(
            low <= index <= high and _counts_against_range(gap)
            for index, gap in self._gap_rows.items()
        ) or any(low <= index <= high for index in self._readonly_rows)

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

    def reset(self, file: DiffFile, expanded_threads: set[str]) -> None:
        self.clear_options()
        self._line_map.clear()
        self._line_types.clear()
        self._comment_map.clear()
        self._comment_indices.clear()
        self._gap_rows.clear()
        self._readonly_rows.clear()
        self._selection_anchor = None
        self._current_file = file
        self._expanded_threads = expanded_threads

    def register_line(
        self,
        option_index: int,
        line: DiffLine,
        discussions: list[Discussion],
    ) -> None:
        self._line_map[option_index] = line
        self._line_types[option_index] = line.line_type
        if discussions:
            self._comment_map[option_index] = discussions
            self._comment_indices.append(option_index)

    def watch_highlighted(self, highlighted: int | None) -> None:
        super().watch_highlighted(highlighted)
        if highlighted is not None and not self._syncing and self.is_mounted:
            self.post_message(_SplitCursorChanged(self.side, highlighted))

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if old_value != new_value and not self._syncing and self.is_mounted:
            self.post_message(_SplitScrollChanged(self.side, new_value))

    def _in_selection_range(self, option_index: int) -> bool:
        if self._selection_anchor is None or self.highlighted is None:
            return False
        low = min(self._selection_anchor, self.highlighted)
        high = max(self._selection_anchor, self.highlighted)
        return low <= option_index <= high and option_index in self._line_map

    def _selection_lines(self) -> list[DiffLine]:
        if self._selection_anchor is None or self.highlighted is None:
            return []
        low = min(self._selection_anchor, self.highlighted)
        high = max(self._selection_anchor, self.highlighted)
        return [
            self._line_map[index]
            for index in range(low, high + 1)
            if index in self._line_map
        ]

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "clear_selection":
            return self._selection_anchor is not None
        return True

    def action_clear_selection(self) -> None:
        self._selection_anchor = None
        self.refresh()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_cursor_down(self) -> None:
        self._selection_anchor = None
        OptionList.action_cursor_down(self)

    def action_cursor_up(self) -> None:
        self._selection_anchor = None
        OptionList.action_cursor_up(self)

    def action_extend_down(self) -> None:
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        OptionList.action_cursor_down(self)
        self.refresh()

    def action_extend_up(self) -> None:
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        OptionList.action_cursor_up(self)
        self.refresh()

    def action_focus_old(self) -> None:
        self._split_view().focus_side(DiffSide.OLD)

    def action_focus_new(self) -> None:
        self._split_view().focus_side(DiffSide.NEW)

    def action_comment(self) -> None:
        if self._refuse_generated() or self._refuse_read_only(commenting=True):
            return
        selection = self.selection
        if selection is None:
            self.app.notify("Move to a source line to comment")
            return
        self.post_message(SplitCommentRequested(selection))
        self._selection_anchor = None
        self.post_message(DiffSelectionChanged(self.selection))

    def action_suggest(self) -> None:
        if self._refuse_generated() or self._refuse_read_only(commenting=True):
            return
        if self.side is DiffSide.OLD:
            self.app.notify("Suggestions are available on the new side only")
            return
        selection = self.selection
        if selection is None:
            self.app.notify("Move to a new-side source line to suggest")
            return
        self.post_message(SplitCommentRequested(selection, suggest=True))
        self._selection_anchor = None
        self.post_message(DiffSelectionChanged(self.selection))

    def action_toggle_discussion(self) -> None:
        discussions = self._current_discussions()
        if not discussions:
            # A read-only row without a thread explains itself; one with a
            # thread, such as an expanded line carrying an outdated thread,
            # can still show and hide it.
            self._refuse_read_only()
            return
        ids = frozenset(discussion.id for discussion in discussions)
        if ids & self._expanded_threads:
            self._expanded_threads.difference_update(ids)
        else:
            self._expanded_threads.update(ids)
        self.post_message(_SplitThreadToggled(ids))

    def action_reply_discussion(self) -> None:
        if self._refuse_generated() or self._refuse_read_only():
            return
        discussion = self._target_discussion()
        selection = self.selection
        if discussion is None or selection is None:
            self.app.notify("No discussion on this line")
            return
        self.post_message(SplitReplyRequested(selection, discussion))

    def action_resolve_discussion(self) -> None:
        if self._refuse_read_only():
            return
        discussion = self._target_discussion()
        if discussion is None:
            self.app.notify("No discussion on this line")
            return
        if not discussion.resolvable:
            self.app.notify("Thread resolution not supported")
            return
        if self._resolve_confirm.arm(discussion.id):
            self.post_message(
                SplitResolveRequested(discussion.id, not discussion.is_resolved)
            )
            return
        action = "Unresolve" if discussion.is_resolved else "Resolve"
        self.app.notify(
            f"{action} thread by @{discussion.root_comment.author.username}? Press R again.",
            markup=False,
        )

    def action_next_comment(self) -> None:
        if not self._comment_indices:
            self.app.notify("No comments on this side")
            return
        current = self.highlighted or 0
        position = bisect.bisect_right(self._comment_indices, current)
        if position >= len(self._comment_indices):
            position = 0
        self.highlighted = self._comment_indices[position]
        self.scroll_to_highlight()

    def action_prev_comment(self) -> None:
        if not self._comment_indices:
            self.app.notify("No comments on this side")
            return
        current = self.highlighted or 0
        position = bisect.bisect_left(self._comment_indices, current) - 1
        if position < 0:
            position = len(self._comment_indices) - 1
        self.highlighted = self._comment_indices[position]
        self.scroll_to_highlight()

    async def _on_click(self, event: events.Click) -> None:
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
            self.highlighted = clicked_option

    def _current_discussions(self) -> list[Discussion]:
        if self.highlighted is None:
            return []
        return self._comment_map.get(self.highlighted, [])

    def _target_discussion(self) -> Discussion | None:
        discussions = self._current_discussions()
        return next(
            (discussion for discussion in discussions if not discussion.is_resolved),
            discussions[0] if discussions else None,
        )

    def _split_view(self) -> SplitDiffView:
        # The columns live inside a Horizontal laid out by SplitDiffView.compose,
        # so the owning view is an ancestor rather than the direct parent.
        for node in self.ancestors:
            if isinstance(node, SplitDiffView):
                return node
        raise TypeError("split diff column is detached")


class SplitDiffView(Widget):
    """Two source-side columns with synchronized rows and vertical scrolling."""

    DEFAULT_CSS = """
    SplitDiffView {
        width: 1fr;
        height: 1fr;
    }
    SplitDiffView > Horizontal {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._file: DiffFile | None = None
        self._discussions: list[Discussion] = []
        self._discussion_index: dict[
            tuple[int | None, int | None], list[Discussion]
        ] = {}
        self._highlight_map = HighlightMap()
        self._expanded_threads: set[str] = set()
        self._active_side = DiffSide.NEW
        self._draft_markers: tuple[DraftMarker, ...] = ()
        self._layout: FileLayout | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SplitDiffColumn(DiffSide.OLD, id="split-old")
            yield SplitDiffColumn(DiffSide.NEW, id="split-new")

    @property
    def selection(self) -> DiffSelection | None:
        return self._column(self._active_side).selection

    @property
    def expanded_threads(self) -> set[str]:
        return self._expanded_threads

    def show_file(
        self,
        file: DiffFile,
        discussions: list[Discussion],
        highlight_map: HighlightMap,
        selection: DiffSelection | None = None,
        draft_markers: tuple[DraftMarker, ...] = (),
        layout: FileLayout | None = None,
    ) -> None:
        """Draw one file, keeping the cursor where it was when redrawing it.

        ``layout`` adds gap rows, expanded lines and open folds; without it
        the file shows its hunks with long context folded, as before.
        """
        cursor_mark = self._cursor_mark() if self._file is file else None
        self._file = file
        self._discussions = discussions
        self._discussion_index = _discussion_index(discussions)
        self._highlight_map = highlight_map
        self._draft_markers = draft_markers
        self._layout = layout
        # Like the unified view, redrawing the file drops an armed resolve.
        for column in self._columns():
            column._resolve_confirm.reset()
        self._populate()
        restored = selection is not None and self.restore_selection(selection)
        if not restored and cursor_mark is not None:
            self._restore_cursor_mark(cursor_mark)

    def cursor_gap(self) -> DiffGap | None:
        """Return the gap row under the cursor, in whichever column has it."""
        return self._cursor_column().cursor_gap()

    def cursor_new_line(self) -> int | None:
        """Return the new-side line number under the cursor, if any.

        A gap row answers with its first line, so a redraw that opens or
        closes it can keep the cursor on the same stretch of the file.
        """
        column = self._cursor_column()
        index = column.highlighted
        if index is None:
            return None
        new_column = self._column(DiffSide.NEW)
        if (gap := column._gap_rows.get(index)) is not None:
            return gap.new_start
        line = new_column._readonly_rows.get(index) or new_column._line_map.get(index)
        return line.new_lineno if line is not None else None

    def cursor_line(self) -> DiffLine | None:
        """Return the expanded or unchanged line under the cursor, if any.

        Both are drawn in the new column too, so it answers for either side.
        """
        index = self._cursor_column().highlighted
        if index is None:
            return None
        new_column = self._column(DiffSide.NEW)
        return new_column._readonly_rows.get(index) or new_column._line_map.get(index)

    def folds_line(
        self, file: DiffFile, hunk: DiffHunk, line: DiffLine, expansion: FileExpansion
    ) -> bool:
        """Whether a hunk line would sit inside a fold under ``expansion``.

        It runs the same fold as :meth:`show_file`, so a line a thread or a
        draft keeps on screen answers False, and nothing is drawn.
        """
        return not any(
            not isinstance(row, str) and row.new is line
            for row in _fold_context_rows(
                align_hunk(hunk), preserve=self._fold_preserve(file, expansion)
            )
        )

    def _fold_preserve(
        self, file: DiffFile, expansion: FileExpansion
    ) -> Callable[[SplitDiffRow], bool]:
        """Rows that never fold: opened by the reader, or carrying a thread or draft."""
        return lambda row: (
            (row.old is not None and expansion.reveals(row.old))
            or self._row_has_visible_detail(file, row)
        )

    def land_on(self, new_line: int) -> bool:
        """Move the cursor to the row that shows a new-side line number.

        A line that a gap row or a fold now hides lands on that row.
        """
        new_column = self._column(DiffSide.NEW)
        rows = {**new_column._line_map, **new_column._readonly_rows}
        index = next(
            (i for i, line in sorted(rows.items()) if line.new_lineno == new_line),
            None,
        )
        if index is None:
            index = next(
                (
                    i
                    for i, gap in sorted(new_column._gap_rows.items())
                    if gap.covers(new_line)
                ),
                None,
            )
        if index is None:
            return False
        self._set_synchronized_highlight(index)
        self._cursor_column().scroll_to_highlight()
        return True

    def _cursor_column(self) -> SplitDiffColumn:
        old_column, new_column = self._columns()
        if old_column.has_focus:
            return old_column
        if new_column.has_focus:
            return new_column
        return self._column(self._active_side)

    def _cursor_mark(self) -> tuple[str, object] | None:
        column = self._cursor_column()
        index = column.highlighted
        if index is None:
            return None
        if (gap := column._gap_rows.get(index)) is not None:
            return ("gap", gap.key)
        if (line := column._readonly_rows.get(index)) is not None:
            return ("line", line)
        return None

    def _restore_cursor_mark(self, mark: tuple[str, object]) -> bool:
        kind, value = mark
        column = self._column(self._active_side)
        if kind == "gap":
            index = next(
                (i for i, gap in column._gap_rows.items() if gap.key == value), None
            )
        else:
            index = self.find_read_only(value)
        if index is None:
            return False
        self._set_synchronized_highlight(index)
        column.scroll_to_highlight()
        return True

    def find_read_only(self, target: object) -> int | None:
        """Return the row of an expanded line, the same in both columns."""
        return next(
            (
                index
                for index, line in self._column(DiffSide.NEW)._readonly_rows.items()
                if line is target
            ),
            None,
        )

    def highlight_row(self, index: int) -> None:
        """Put the cursor of both columns on one row and scroll to it."""
        self._set_synchronized_highlight(index)
        self._column(self._active_side).scroll_to_highlight()

    def show_placeholder(self, message: str) -> None:
        self._file = None
        self._discussions = []
        self._discussion_index.clear()
        self._highlight_map = HighlightMap()
        self._expanded_threads.clear()
        self._active_side = DiffSide.NEW
        self._draft_markers = ()
        self._layout = None
        for column in self._columns():
            column.clear_options()
            column._line_map.clear()
            column._line_types.clear()
            column._comment_map.clear()
            column._comment_indices.clear()
            column._gap_rows.clear()
            column._readonly_rows.clear()
            column._selection_anchor = None
            column._current_file = None
            column._expanded_threads.clear()
            column._resolve_confirm.reset()
            column.add_option(
                Option(_single_line(message, Style(dim=True)), disabled=True)
            )

    def focus_side(self, side: DiffSide) -> None:
        target = self._column(side)
        source = self._column(self._active_side)
        index = source.highlighted
        if index is None or not (
            index in target._line_map
            or index in target._gap_rows
            or index in target._readonly_rows
        ):
            index = self._nearest_actionable(target, index or 0)
        if index is None:
            self.app.notify(f"No {side.value}-side source lines in this file")
            return
        self._active_side = side
        self._set_synchronized_highlight(index)
        target.focus()
        self.post_message(DiffSelectionChanged(self.selection))

    def restore_selection(self, selection: DiffSelection) -> bool:
        if self._file is None or selection.file != self._file:
            return False
        column = self._column(selection.side)
        selected = self._find_line(column, selection.line)
        if selected is None:
            return False
        self._active_side = selection.side
        self._set_synchronized_highlight(selected)
        range_indices = [
            index
            for line in selection.lines
            if (index := self._find_line(column, line)) is not None
        ]
        if len(range_indices) > 1:
            column._selection_anchor = (
                min(range_indices)
                if selected == max(range_indices)
                else max(range_indices)
            )
        column.focus()
        column.scroll_to_highlight()
        return True

    def jump_to(
        self, line_number: int, side: DiffSide, discussion_id: str = ""
    ) -> bool:
        if discussion_id:
            self._expanded_threads.add(discussion_id)
            selection = self.selection
            self._populate()
            if selection is not None:
                self.restore_selection(selection)
        column = self._column(side)
        for index, line in column._line_map.items():
            coordinate = line.old_lineno if side is DiffSide.OLD else line.new_lineno
            if coordinate == line_number:
                self._active_side = side
                self._set_synchronized_highlight(index)
                column.focus()
                column.scroll_to_highlight()
                return True
        return False

    def action_comment(self) -> None:
        self._column(self._active_side).action_comment()

    def on__split_cursor_changed(self, event: _SplitCursorChanged) -> None:
        event.stop()
        source = self._column(event.side)
        if not source.has_focus:
            return
        self._active_side = event.side
        other = self._column(
            DiffSide.OLD if event.side is DiffSide.NEW else DiffSide.NEW
        )
        if other.highlighted != event.option_index:
            other._syncing = True
            other.highlighted = event.option_index
            other.scroll_to_highlight()
            other._syncing = False
        self.post_message(DiffSelectionChanged(self.selection))

    def on__split_scroll_changed(self, event: _SplitScrollChanged) -> None:
        event.stop()
        other = self._column(
            DiffSide.OLD if event.side is DiffSide.NEW else DiffSide.NEW
        )
        if other.scroll_y != event.offset:
            other._syncing = True
            other.scroll_to(y=event.offset, animate=False, immediate=True)
            other._syncing = False

    def on__split_thread_toggled(self, event: _SplitThreadToggled) -> None:
        event.stop()
        selection = self.selection
        # A toggle from an expanded line has no selection to restore, so the
        # cursor is put back on that line instead of on the first change.
        cursor_mark = self._cursor_mark()
        self._populate()
        if selection is not None:
            self.restore_selection(selection)
        elif cursor_mark is not None:
            self._restore_cursor_mark(cursor_mark)

    def _populate(self) -> None:
        file = self._file
        if file is None:
            return
        old_column, new_column = self._columns()
        for column in (old_column, new_column):
            column.reset(file, self._expanded_threads)

        mode_suffix = f"  {file.mode_change}" if file.mode_change else ""
        old_options = [
            Option(
                _single_line(f" OLD  {file.old_path}{mode_suffix}", Style(bold=True)),
                disabled=True,
            )
        ]
        new_options = [
            Option(
                _single_line(f" NEW  {file.new_path}{mode_suffix}", Style(bold=True)),
                disabled=True,
            )
        ]
        option_index = 1
        first_actionable: dict[DiffSide, int | None] = {
            DiffSide.OLD: None,
            DiffSide.NEW: None,
        }
        first_changed: dict[DiffSide, int | None] = {
            DiffSide.OLD: None,
            DiffSide.NEW: None,
        }

        layout = self._layout
        if file.is_binary or not file.hunks:
            messages = (
                layout.notice
                if layout is not None and layout.notice
                else (placeholder_message(file),)
            )
            for message in messages:
                old_options.append(
                    Option(_single_line(message, Style(dim=True)), disabled=True)
                )
                new_options.append(
                    Option(_single_line(message, Style(dim=True)), disabled=True)
                )
        else:
            if layout is None:
                layout = FileLayout(
                    tuple(HunkBlock(i, hunk) for i, hunk in enumerate(file.hunks))
                )
            expansion = layout.expansion
            # A file the forge sent only in part says so above its hunks.
            for message in layout.notice:
                old_options.append(
                    Option(_single_line(f" {message}", _NOTICE_STYLE), disabled=True)
                )
                new_options.append(
                    Option(_single_line(f" {message}", _NOTICE_STYLE), disabled=True)
                )
                option_index += 1
            for segment in layout.segments:
                if isinstance(segment, GapRow):
                    gap = segment.gap
                    label = gap_message(gap, loading=gap.index in layout.loading)
                    for options, column in (
                        (old_options, old_column),
                        (new_options, new_column),
                    ):
                        options.append(
                            Option(_single_line(f"      {label}", Style(dim=True)))
                        )
                        column._gap_rows[option_index] = gap
                    option_index += 1
                    continue
                if isinstance(segment, ExpandedBlock):
                    # A thread can point at a line outside the current hunks,
                    # for example one left on an earlier revision: it is drawn
                    # under its expanded row like a hunk row's thread, while
                    # the line itself stays read-only and never becomes a
                    # comment anchor.
                    for line in segment.lines:
                        old_discussions = _line_discussions(
                            line, DiffSide.OLD, self._discussion_index
                        )
                        new_discussions = _line_discussions(
                            line, DiffSide.NEW, self._discussion_index
                        )
                        old_options.append(
                            self._cell_option(line, DiffSide.OLD, old_discussions)
                        )
                        new_options.append(
                            self._cell_option(line, DiffSide.NEW, new_discussions)
                        )
                        for column, cell_discussions in (
                            (old_column, old_discussions),
                            (new_column, new_discussions),
                        ):
                            column._readonly_rows[option_index] = line
                            column._line_types[option_index] = line.line_type
                            if cell_discussions:
                                column._comment_map[option_index] = cell_discussions
                                column._comment_indices.append(option_index)
                        option_index += 1
                        option_index = self._thread_rows(
                            old_options,
                            new_options,
                            option_index,
                            old_discussions,
                            new_discussions,
                        )
                    continue
                hunk = segment.hunk
                header = _single_line(f" {hunk.header}", Style(bold=True, dim=True))
                old_options.append(Option(header.copy(), disabled=True))
                new_options.append(Option(header.copy(), disabled=True))
                option_index += 1
                for row in _fold_context_rows(
                    align_hunk(hunk),
                    preserve=self._fold_preserve(file, expansion),
                    hunk_index=segment.hunk_index,
                ):
                    if isinstance(row, str):
                        gap = row.gap if isinstance(row, FoldMarker) else None
                        for options, column in (
                            (old_options, old_column),
                            (new_options, new_column),
                        ):
                            options.append(
                                Option(
                                    _single_line(f"      {row}", Style(dim=True)),
                                    disabled=gap is None,
                                )
                            )
                            if gap is not None:
                                column._gap_rows[option_index] = gap
                        option_index += 1
                        continue
                    option_index = self._add_row(
                        file,
                        row,
                        option_index,
                        old_options,
                        new_options,
                        first_actionable,
                        first_changed,
                    )

        old_column.add_options(old_options)
        new_column.add_options(new_options)
        preferred = (
            first_changed[self._active_side] or first_actionable[self._active_side]
        )
        if preferred is None:
            other_side = (
                DiffSide.OLD if self._active_side is DiffSide.NEW else DiffSide.NEW
            )
            preferred = first_changed[other_side] or first_actionable[other_side]
            if preferred is not None:
                self._active_side = other_side
        if preferred is not None:
            self._set_synchronized_highlight(preferred)

    def _add_row(
        self,
        file: DiffFile,
        row: SplitDiffRow,
        option_index: int,
        old_options: list[Option],
        new_options: list[Option],
        first_actionable: dict[DiffSide, int | None],
        first_changed: dict[DiffSide, int | None],
    ) -> int:
        """Append one aligned hunk row with its threads and draft markers."""
        old_column, new_column = self._columns()
        old_discussions = _line_discussions(
            row.old, DiffSide.OLD, self._discussion_index
        )
        new_discussions = _line_discussions(
            row.new, DiffSide.NEW, self._discussion_index
        )
        old_options.append(self._cell_option(row.old, DiffSide.OLD, old_discussions))
        new_options.append(self._cell_option(row.new, DiffSide.NEW, new_discussions))
        for side, column, line, cell_discussions in (
            (DiffSide.OLD, old_column, row.old, old_discussions),
            (DiffSide.NEW, new_column, row.new, new_discussions),
        ):
            if is_actionable(line, side):
                assert line is not None
                column.register_line(option_index, line, cell_discussions)
                if first_actionable[side] is None:
                    first_actionable[side] = option_index
                if (
                    line.line_type in {LineType.ADDITION, LineType.DELETION}
                    and first_changed[side] is None
                ):
                    first_changed[side] = option_index
        option_index += 1

        option_index = self._thread_rows(
            old_options, new_options, option_index, old_discussions, new_discussions
        )

        for marker in self._markers_for_row(file, row.old, row.new):
            for marker_line in _draft_marker_lines(marker):
                if marker.side is DiffSide.OLD:
                    old_options.append(Option(marker_line, disabled=True))
                    new_options.append(Option(_single_line(""), disabled=True))
                else:
                    old_options.append(Option(_single_line(""), disabled=True))
                    new_options.append(Option(marker_line, disabled=True))
                option_index += 1
        return option_index

    def _thread_rows(
        self,
        old_options: list[Option],
        new_options: list[Option],
        option_index: int,
        *cell_discussions: list[Discussion],
    ) -> int:
        """Append a row's open thread lines, blank in the old column."""
        row_discussions = {
            discussion.id: discussion
            for discussions in cell_discussions
            for discussion in discussions
        }
        expanded = [
            discussion
            for discussion in row_discussions.values()
            if discussion.id in self._expanded_threads
        ]
        config = app_config(self.app)
        for thread_line in _thread_lines(
            expanded, ascii_mode=config.ascii_mode, code_theme=config.syntax_theme
        ):
            old_options.append(Option(_single_line(""), disabled=True))
            new_options.append(Option(thread_line, disabled=True))
            option_index += 1
        return option_index

    def _markers_for_row(
        self,
        file: DiffFile,
        old_line: DiffLine | None,
        new_line: DiffLine | None,
    ) -> tuple[DraftMarker, ...]:
        return tuple(
            marker
            for marker in self._draft_markers
            if (
                marker.side is DiffSide.OLD
                and old_line is not None
                and marker.matches(file, old_line, DiffSide.OLD)
            )
            or (
                marker.side is DiffSide.NEW
                and new_line is not None
                and marker.matches(file, new_line, DiffSide.NEW)
            )
        )

    def _row_has_visible_detail(self, file: DiffFile, row: SplitDiffRow) -> bool:
        if self._markers_for_row(file, row.old, row.new):
            return True
        return bool(
            _line_discussions(row.old, DiffSide.OLD, self._discussion_index)
            or _line_discussions(row.new, DiffSide.NEW, self._discussion_index)
        )

    def _cell_option(
        self,
        line: DiffLine | None,
        side: DiffSide,
        discussions: list[Discussion],
    ) -> Option:
        if line is None:
            return Option(_single_line("      ", Style(dim=True)), disabled=True)
        if line.line_type == LineType.NO_NEWLINE:
            return Option(
                _single_line(f"      {line.content}", Style(dim=True)), disabled=True
            )
        number = line.old_lineno if side is DiffSide.OLD else line.new_lineno
        if number is None:
            return Option(_single_line("      ", Style(dim=True)), disabled=True)
        marker = "*" if discussions else " "
        prefix = (
            "-"
            if line.line_type == LineType.DELETION
            else "+"
            if line.line_type == LineType.ADDITION
            else " "
        )
        text = _single_line(f"{number:>5} {marker} ", Style(dim=True))
        prefix_style = (
            Style(color="red")
            if prefix == "-"
            else Style(color="green")
            if prefix == "+"
            else Style()
        )
        text.append(prefix, prefix_style)
        highlighted = self._highlight_map.get(line, side)
        text.append_text(
            highlighted.copy() if highlighted is not None else Text(line.content)
        )
        # An expanded line takes the cursor like a source line, but it is not
        # an anchor: its column registers it as read-only.
        readable = is_actionable(line, side) or line.line_type is LineType.EXPANDED
        return Option(text, disabled=not readable)

    def _set_synchronized_highlight(self, option_index: int) -> None:
        for column in self._columns():
            if column.highlighted != option_index:
                column._syncing = True
                column.highlighted = option_index
                column._syncing = False

    @staticmethod
    def _nearest_actionable(column: SplitDiffColumn, origin: int) -> int | None:
        if not column._line_map:
            return None
        return min(column._line_map, key=lambda index: abs(index - origin))

    @staticmethod
    def _find_line(column: SplitDiffColumn, target: DiffLine) -> int | None:
        return next(
            (
                index
                for index, line in column._line_map.items()
                if line is target or line == target
            ),
            None,
        )

    def _column(self, side: DiffSide) -> SplitDiffColumn:
        return self.query_one(
            "#split-old" if side is DiffSide.OLD else "#split-new",
            SplitDiffColumn,
        )

    def _columns(self) -> tuple[SplitDiffColumn, SplitDiffColumn]:
        return (
            self.query_one("#split-old", SplitDiffColumn),
            self.query_one("#split-new", SplitDiffColumn),
        )


def _discussion_index(
    discussions: list[Discussion],
) -> dict[tuple[int | None, int | None], list[Discussion]]:
    index: dict[tuple[int | None, int | None], list[Discussion]] = {}
    for discussion in discussions:
        if discussion.is_inline:
            root = discussion.root_comment
            index.setdefault((root.old_line, root.new_line), []).append(discussion)
    return index


_NOTICE_STYLE = Style(dim=True, italic=True)
"""Style of the truncation notice rows above a partial file's hunks."""


def _single_line(content: str, style: Style | None = None) -> Text:
    """Build a non-wrapping row so paired columns stay vertically aligned."""
    return Text(content, style=style, no_wrap=True, overflow="crop")


def _draft_marker_lines(marker: DraftMarker) -> tuple[Text, ...]:
    state = " stale" if marker.stale else ""
    body_lines = marker.body.split("\n")
    return tuple(
        _single_line(
            (
                f"      DRAFT {marker.side.value}{state}: {body_line}"
                if index == 0
                else f"      {' ' * (7 + len(marker.side.value) + len(state))}{body_line}"
            ),
            Style(color="yellow", bold=index == 0),
        )
        for index, body_line in enumerate(body_lines)
    )


def _fold_context_rows(
    rows: tuple[SplitDiffRow, ...],
    context_lines: int = 3,
    preserve: Callable[[SplitDiffRow], bool] | None = None,
    *,
    hunk_index: int = 0,
) -> tuple[SplitDiffRow | str, ...]:
    """Fold long unchanged runs equally on both source sides.

    Each fold is a :class:`FoldMarker` carrying the INSIDE gap it hides, so
    ``e`` can reveal it without reading the file.
    """
    result: list[SplitDiffRow | str] = []
    run: list[SplitDiffRow] = []

    def flush() -> None:
        if len(run) > context_lines * 2:
            hidden = run[context_lines:-context_lines]
            first = hidden[0].new
            assert first is not None
            result.extend(run[:context_lines])
            result.append(FoldMarker(inside_gap(hunk_index, first, len(hidden))))
            result.extend(run[-context_lines:])
        else:
            result.extend(run)
        run.clear()

    for row in rows:
        if (
            row.old is not None
            and row.new is row.old
            and row.old.line_type is LineType.CONTEXT
            and not (preserve is not None and preserve(row))
        ):
            run.append(row)
        else:
            flush()
            result.append(row)
    flush()
    return tuple(result)


def placeholder_message(file: DiffFile) -> str:
    """One honest line for a file the forge described without content.

    The leading branches state what the forge reported about the file.  The
    unavailable branch is the residue GitHub's files endpoint leaves behind: it
    withholds the patch for binary content and for a mode-only change alike, so
    that line names the forge as the limit instead of blaming the read.  The
    final line covers a file that carries no metadata reason at all.
    """

    if file.is_binary:
        return "[Binary file]"
    if file.is_truncated:
        return TRUNCATED_NOTICE
    if file.is_empty:
        return "[Empty file]"
    if file.is_mode_only:
        return "[File mode changed]"
    if file.is_rename_only:
        return "[Renamed with no content change]"
    if file.is_unavailable:
        return "Not exposed by the forge. Press o to view in browser."
    return "Diff not available. Press o to view in browser."


def _line_discussions(
    line: DiffLine | None,
    side: DiffSide,
    index: dict[tuple[int | None, int | None], list[Discussion]],
) -> list[Discussion]:
    if line is None:
        return []
    exact = index.get((line.old_lineno, line.new_lineno), [])
    if exact:
        return exact
    if (
        side is DiffSide.NEW
        and line.new_lineno is not None
        and (new_side := index.get((None, line.new_lineno), []))
    ):
        return new_side
    if side is DiffSide.OLD and line.old_lineno is not None:
        return index.get((line.old_lineno, None), [])
    return []


def _thread_lines(
    discussions: list[Discussion],
    *,
    ascii_mode: bool = False,
    code_theme: str = DEFAULT_SYNTAX_THEME,
) -> list[Text]:
    lines: list[Text] = []
    for discussion in discussions:
        root = discussion.root_comment
        state = "resolved" if discussion.is_resolved else "open"
        lines.append(
            _single_line(
                f" [{state}] @{root.author.username} {relative_time(root.created_at)}",
                Style(dim=discussion.is_resolved, bold=not discussion.is_resolved),
            )
        )
        for segments in markdown_lines(
            root.body or "", 60, ascii_mode=ascii_mode, code_theme=code_theme
        ):
            text = _single_line("   ")
            for segment in segments:
                if segment.text and segment.text != "\n":
                    text.append(segment.text, segment.style)
            lines.append(text)
        for reply in root.replies:
            lines.append(_single_line(f"   @{reply.author.username}", Style(dim=True)))
            for segments in markdown_lines(
                reply.body or "", 57, ascii_mode=ascii_mode, code_theme=code_theme
            ):
                text = _single_line("     ")
                for segment in segments:
                    if segment.text and segment.text != "\n":
                        text.append(segment.text, segment.style)
                lines.append(text)
    return lines


__all__ = [
    "FOLD_READ_ONLY_MESSAGE",
    "GENERATED_COMMENT_MESSAGE",
    "RANGE_CROSSES_GAP_MESSAGE",
    "READ_ONLY_LINE_MESSAGE",
    "TRUNCATED_NOTICE",
    "DiffModeChanged",
    "DiffModeState",
    "DiffSelection",
    "DiffSelectionChanged",
    "DiffSide",
    "DiffViewMode",
    "FileLayout",
    "FoldMarker",
    "SplitCommentRequested",
    "SplitDiffColumn",
    "SplitDiffView",
    "SplitReplyRequested",
    "SplitResolveRequested",
    "gap_message",
    "is_actionable",
    "placeholder_message",
]
