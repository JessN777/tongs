"""Gap rows, expanded lines and in-hunk folds in the split diff view."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from tests.test_diff.test_context import FIXTURES
from tongs.diff.context import GapPosition
from tongs.diff.models import DiffFile, LineType
from tongs.diff.parser import parse_diff
from tongs.forges.models import Discussion, InlineComment, User
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    FOLD_READ_ONLY_MESSAGE,
    RANGE_CROSSES_GAP_MESSAGE,
    READ_ONLY_LINE_MESSAGE,
    CommentRequested,
    ContextRequested,
    DiffOptionList,
    DiffPanel,
)
from tongs.widgets.split_diff import (
    DiffViewMode,
    SplitDiffColumn,
    SplitDiffView,
    _fold_context_rows,
)

# ``git diff --no-index -U8`` of a 40-line file with lines 10 and 25 changed:
# one hunk whose long context runs fold inside it, one line above it and
# seven below.
WIDE_CONTEXT = (
    "@@ -2,32 +2,32 @@ line 1\n line 2\n line 3\n line 4\n line 5\n line 6\n"
    " line 7\n line 8\n line 9\n-line 10\n+changed 10\n line 11\n line 12\n"
    " line 13\n line 14\n line 15\n line 16\n line 17\n line 18\n line 19\n"
    " line 20\n line 21\n line 22\n line 23\n line 24\n-line 25\n+changed 25\n"
    " line 26\n line 27\n line 28\n line 29\n line 30\n line 31\n line 32\n"
    " line 33\n"
)


def _split_text(text: str) -> tuple[tuple[str, ...], bool]:
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return tuple(parts), ends_with_newline


NEW_LINES, NEW_EOL = _split_text(FIXTURES["two_hunks"][1])
OLD_LINES, _ = _split_text(FIXTURES["two_hunks"][0])

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def _file(hunks: str | None = None, path: str = "f.txt") -> DiffFile:
    body = hunks if hunks is not None else FIXTURES["two_hunks"][2]
    (file,) = parse_diff(f"--- a/{path}\n+++ b/{path}\n{body}", detect_language=False)
    return file


class _SplitApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ContextRequested] = []
        self.comments: list[CommentRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel(id="diff-panel")

    def on_context_requested(self, event: ContextRequested) -> None:
        self.requests.append(event)

    def on_comment_requested(self, event: CommentRequested) -> None:
        self.comments.append(event)


def _columns(app: _SplitApp) -> tuple[SplitDiffColumn, SplitDiffColumn]:
    return (
        app.query_one("#split-old", SplitDiffColumn),
        app.query_one("#split-new", SplitDiffColumn),
    )


def _rows(column: SplitDiffColumn) -> list[str]:
    return [
        column.get_option_at_index(index).prompt.plain
        for index in range(column.option_count)
    ]


def _messages(app: _SplitApp) -> list[str]:
    return [notification.message for notification in app._notifications]


async def _open_split(app: _SplitApp, pilot, *files: DiffFile) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files(list(files))
    panel.request_mode(DiffViewMode.SPLIT)
    await pilot.pause()
    _columns(app)[1].focus()
    await pilot.pause()
    return panel


def _side_numbers(column: SplitDiffColumn) -> list[int]:
    cells = {**column._line_map, **column._readonly_rows}
    return [
        number
        for _, line in sorted(cells.items())
        if (
            number := line.old_lineno
            if column.side is DiffSide.OLD
            else line.new_lineno
        )
        is not None
    ]


async def _press_e_on_gap(
    app: _SplitApp, pilot, column: SplitDiffColumn, position: int
) -> None:
    column.focus()
    await pilot.pause()
    column.highlighted = sorted(column._gap_rows)[position]
    await pilot.pause()
    await pilot.press("e")
    await pilot.pause()


@pytest.mark.asyncio
async def test_gap_rows_read_the_same_in_both_columns_and_read_nothing() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60)) as pilot:
        await _open_split(app, pilot, _file())
        old, new = _columns(app)

        assert sorted(old._gap_rows) == sorted(new._gap_rows)
        assert len(new._gap_rows) == 3
        old_rows, new_rows = _rows(old), _rows(new)
        labels = [new_rows[index] for index in sorted(new._gap_rows)]
        assert labels == [
            "      ... 6 unchanged lines (e to expand) ...",
            "      ... 3 unchanged lines (e to expand) ...",
            "      ... more lines below (e to expand) ...",
        ]
        assert [old_rows[index] for index in sorted(old._gap_rows)] == labels
        for index in new._gap_rows:
            assert not new.get_option_at_index(index).disabled
            assert not old.get_option_at_index(index).disabled
        assert app.requests == []


@pytest.mark.asyncio
async def test_e_from_either_column_reads_once_and_expands_both_sides() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open_split(app, pilot, file)
        old, new = _columns(app)

        await _press_e_on_gap(app, pilot, new, 1)
        assert [(event.file is file, event.gap_index) for event in app.requests] == [
            (True, 1)
        ]
        assert "      ... loading ..." in _rows(new)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()

        # The cursor lands on the first new line in both columns.
        assert old.highlighted == new.highlighted
        landed = new._readonly_rows[new.highlighted]
        assert landed.line_type is LineType.EXPANDED
        assert (landed.old_lineno, landed.new_lineno) == (14, 14)
        assert old._readonly_rows[old.highlighted] is landed

        # The other gaps open from the old column with no further read.
        await _press_e_on_gap(app, pilot, old, 0)
        await _press_e_on_gap(app, pilot, old, 0)
        assert len(app.requests) == 1
        assert old._gap_rows == {} and new._gap_rows == {}
        assert _side_numbers(new) == list(range(1, 31))
        assert _side_numbers(old) == list(range(1, 31))
        for line in new._readonly_rows.values():
            assert line.new_lineno is not None and line.old_lineno is not None
            assert NEW_LINES[line.new_lineno - 1] == line.content
            assert OLD_LINES[line.old_lineno - 1] == line.content
        assert all(row.isascii() for row in _rows(old) + _rows(new))
        # Expanded cells take the cursor but are never anchors.
        for column in (old, new):
            assert not any(
                line.line_type is LineType.EXPANDED
                for line in column._line_map.values()
            )
            index = next(iter(column._readonly_rows))
            assert not column.get_option_at_index(index).disabled
        # The cursor sits on an expanded line, which is no selection.
        assert panel.selection is None


@pytest.mark.asyncio
async def test_cursor_moves_in_step_across_gap_rows() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60)) as pilot:
        await _open_split(app, pilot, _file())
        old, new = _columns(app)
        gap_row = sorted(new._gap_rows)[1]

        new.highlighted = gap_row - 1
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        assert new.highlighted == gap_row
        assert old.highlighted == gap_row
        assert new.cursor_gap() is not None

        await pilot.press("j")
        await pilot.pause()
        # The hunk header after the gap row is skipped in both columns.
        assert new.highlighted == old.highlighted == gap_row + 2
        assert new.highlighted in new._line_map

        # h and l keep the cursor on a gap row instead of jumping away.
        new.highlighted = gap_row
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert old.has_focus
        assert old.highlighted == new.highlighted == gap_row


@pytest.mark.parametrize("key", ["c", "f3", "d", "r", "R"])
@pytest.mark.asyncio
async def test_comment_keys_on_gap_rows_and_expanded_cells_are_refused(
    key: str,
) -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open_split(app, pilot, file)
        old, new = _columns(app)

        new.highlighted = min(new._gap_rows)
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert _messages(app).count(READ_ONLY_LINE_MESSAGE) == 1

        await _press_e_on_gap(app, pilot, new, 0)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()
        for column in (new, old):
            column.focus()
            await pilot.pause()
            assert column.highlighted in column._readonly_rows
            await pilot.press(key)
            await pilot.pause()

        assert _messages(app).count(READ_ONLY_LINE_MESSAGE) == 3
        assert app.comments == []


@pytest.mark.asyncio
async def test_an_expanded_line_with_a_thread_shows_and_opens_it() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        # An outdated thread on line 2, which is unchanged now.
        outdated = InlineComment("c1", User("bob"), "Old note", NOW, "f.txt", 2, 2)
        discussions = [Discussion("d1", True, outdated)]
        panel = app.query_one(DiffPanel)
        panel.set_files([file], discussions)
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        old, new = _columns(app)
        new.focus()
        await pilot.pause()

        await _press_e_on_gap(app, pilot, new, 0)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()
        new.focus()
        await pilot.pause()

        assert "    2 *  line 2" in _rows(new)
        assert "    2 *  line 2" in _rows(old)

        # ] stops on the expanded line, in step across both columns.
        await pilot.press("]")
        await pilot.pause()
        landed = new._readonly_rows[new.highlighted]
        assert (landed.old_lineno, landed.new_lineno) == (2, 2)
        assert old.highlighted == new.highlighted

        # d shows the thread in the new column only, hides it again, and keeps
        # the cursor on the expanded line instead of the first change.
        await pilot.press("d")
        await pilot.pause()
        assert any("Old note" in row for row in _rows(new))
        assert not any("Old note" in row for row in _rows(old))
        await pilot.press("d")
        await pilot.pause()
        assert not any("Old note" in row for row in _rows(new))
        assert new._readonly_rows[new.highlighted].new_lineno == 2

        # The line still refuses a new comment.
        await pilot.press("c")
        await pilot.pause()
        assert READ_ONLY_LINE_MESSAGE in _messages(app)
        assert app.comments == []


@pytest.mark.asyncio
async def test_a_split_range_across_a_gap_is_refused() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_split(app, pilot, _file())
        _old, new = _columns(app)
        gap_row = sorted(new._gap_rows)[1]
        last_before = max(index for index in new._line_map if index < gap_row)

        new.highlighted = last_before
        await pilot.pause()
        await pilot.press("J", "J")
        await pilot.press("c")
        await pilot.pause()

        assert RANGE_CROSSES_GAP_MESSAGE in _messages(app)
        assert app.comments == []


@pytest.mark.asyncio
async def test_switching_layouts_keeps_what_was_expanded() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        file = _file()
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        option_list = app.query_one(DiffOptionList)
        option_list.focus()
        await pilot.pause()
        option_list.highlighted = sorted(option_list._gap_rows)[1]
        await pilot.press("e")
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        unified_expanded = list(option_list._readonly_rows.values())

        await pilot.press("v")
        await pilot.pause()
        assert app.query_one(SplitDiffView).display
        _old, new = _columns(app)
        assert list(new._readonly_rows.values()) == unified_expanded
        assert len(new._gap_rows) == 2

        await pilot.press("v")
        await pilot.pause()
        assert list(option_list._readonly_rows.values()) == unified_expanded
        assert len(app.requests) == 1


@pytest.mark.asyncio
async def test_inside_fold_opens_with_no_read_in_the_split_view() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        await _open_split(app, pilot, _file(WIDE_CONTEXT))
        old, new = _columns(app)
        folds = sorted(
            index
            for index, gap in new._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )
        assert [new._gap_rows[index].length for index in folds] == [2, 8, 2]
        assert sorted(old._gap_rows) == sorted(new._gap_rows)
        assert "      ... 8 unchanged lines (e to expand) ..." in _rows(new)

        new.highlighted = folds[1]
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        assert app.requests == []
        landed = new._line_map[new.highlighted]
        assert (landed.old_lineno, landed.new_lineno) == (14, 14)
        assert old.highlighted == new.highlighted
        assert {14, 15, 16, 17, 18, 19, 20, 21} <= set(_side_numbers(new))
        # The other folds stay closed.
        remaining = [
            gap.length
            for gap in new._gap_rows.values()
            if gap.position is GapPosition.INSIDE
        ]
        assert remaining == [2, 2]


@pytest.mark.asyncio
async def test_inside_fold_opens_with_no_read_in_the_unified_view() -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file(WIDE_CONTEXT)])
        option_list = app.query_one(DiffOptionList)
        option_list.focus()
        await pilot.pause()
        folds = sorted(
            index
            for index, gap in option_list._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )
        assert len(folds) == 3

        # A range may still span a fold inside one hunk.
        first = max(index for index in option_list._line_map if index < folds[1])
        option_list.highlighted = first
        await pilot.press("J", "J", "c")
        await pilot.pause()
        assert len(app.comments) == 1

        option_list.highlighted = folds[1]
        await pilot.press("e")
        await pilot.pause()
        assert app.requests == []
        landed = option_list._line_map[option_list.highlighted]
        assert landed.new_lineno == 14
        numbers = [line.new_lineno for _, line in sorted(option_list._line_map.items())]
        assert [n for n in numbers if n is not None and 13 <= n <= 22] == list(
            range(13, 23)
        )


@pytest.mark.parametrize("mode", [DiffViewMode.UNIFIED, DiffViewMode.SPLIT])
@pytest.mark.parametrize("key", ["c", "f3", "d", "r", "R"])
@pytest.mark.asyncio
async def test_comment_keys_on_a_fold_ask_to_show_its_lines_first(
    key: str, mode: DiffViewMode
) -> None:
    app = _SplitApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file(WIDE_CONTEXT)])
        panel.request_mode(mode)
        await pilot.pause()
        column = (
            _columns(app)[1]
            if mode is DiffViewMode.SPLIT
            else app.query_one(DiffOptionList)
        )
        column.focus()
        await pilot.pause()
        fold = min(
            index
            for index, gap in column._gap_rows.items()
            if gap.position is GapPosition.INSIDE
        )

        column.highlighted = fold
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()

        assert _messages(app) == [FOLD_READ_ONLY_MESSAGE]
        assert FOLD_READ_ONLY_MESSAGE == (
            "Press e to show these lines, then comment on one."
        )
        assert app.comments == []


def test_fold_markers_still_read_as_text() -> None:
    """Callers that treat a fold marker as its text keep working."""
    file = _file(WIDE_CONTEXT)
    from tongs.diff.alignment import align_hunk

    rows = _fold_context_rows(align_hunk(file.hunks[0]), hunk_index=0)
    markers = [row for row in rows if isinstance(row, str)]
    assert markers == [
        "... 2 unchanged lines (e to expand) ...",
        "... 8 unchanged lines (e to expand) ...",
        "... 2 unchanged lines (e to expand) ...",
    ]
    assert [marker.gap.new_start for marker in markers] == [5, 14, 29]
