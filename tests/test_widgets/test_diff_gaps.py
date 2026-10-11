"""Gap rows and expand-in-place in the unified terminal diff."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult

from tests.test_diff.test_context import FIXTURES
from tongs.diff.models import DiffFile, FileStatus, LineType
from tongs.diff.parser import parse_diff
from tongs.forges.models import Discussion, InlineComment, User
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    MOVE_TO_GAP_MESSAGE,
    NO_MORE_LINES_MESSAGE,
    RANGE_CROSSES_GAP_MESSAGE,
    READ_ONLY_LINE_MESSAGE,
    CommentRequested,
    ContextRequested,
    DiffContent,
    DiffOptionList,
    DiffPanel,
)
from tongs.widgets.review_draft import DraftMarker
from tongs.widgets.split_diff import DiffViewMode, HighlightMap, SplitDiffView

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def _split(text: str) -> tuple[tuple[str, ...], bool]:
    parts = text.split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return tuple(parts), ends_with_newline


def _file(name: str = "two_hunks", path: str = "f.txt") -> DiffFile:
    hunks = FIXTURES[name][2]
    (file,) = parse_diff(f"--- a/{path}\n+++ b/{path}\n{hunks}", detect_language=False)
    return file


NEW_LINES, NEW_EOL = _split(FIXTURES["two_hunks"][1])


class _GapApp(App[None]):
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


def _option_list(app: _GapApp) -> DiffOptionList:
    return app.query_one(DiffOptionList)


def _rows(app: _GapApp) -> list[str]:
    option_list = _option_list(app)
    return [
        option_list.get_option_at_index(index).prompt.plain
        for index in range(option_list.option_count)
    ]


def _gap_indices(app: _GapApp) -> list[int]:
    return sorted(_option_list(app)._gap_rows)


def _messages(app: _GapApp) -> list[str]:
    return [notification.message for notification in app._notifications]


def _new_numbers_on_screen(app: _GapApp) -> list[int]:
    option_list = _option_list(app)
    lines = {**option_list._line_map, **option_list._readonly_rows}
    return [
        line.new_lineno
        for _, line in sorted(lines.items())
        if line.new_lineno is not None
    ]


async def _open(app: _GapApp, pilot, *files: DiffFile, discussions=None) -> DiffPanel:
    panel = app.query_one(DiffPanel)
    panel.set_files(list(files), discussions)
    _option_list(app).focus()
    await pilot.pause()
    return panel


async def _press_e_on_gap(app: _GapApp, pilot, position: int) -> None:
    option_list = _option_list(app)
    option_list.highlighted = _gap_indices(app)[position]
    await pilot.press("e")
    await pilot.pause()


@pytest.mark.asyncio
async def test_two_hunk_file_shows_three_gap_rows_and_reads_nothing() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60)) as pilot:
        await _open(app, pilot, _file())

        rows = _rows(app)
        gap_rows = [rows[index] for index in _gap_indices(app)]
        assert gap_rows == [
            "         ... 6 unchanged lines (e to expand) ...",
            "         ... 3 unchanged lines (e to expand) ...",
            "         ... more lines below (e to expand) ...",
        ]
        assert all(row.isascii() for row in gap_rows)
        assert app.requests == []
        # The cursor still starts on the first change.
        line = _option_list(app)._line_map[_option_list(app).highlighted]
        assert line.line_type in (LineType.ADDITION, LineType.DELETION)


@pytest.mark.asyncio
async def test_e_reads_once_and_expands_every_gap_with_both_numbers() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)

        await _press_e_on_gap(app, pilot, 1)
        assert [(event.file is file, event.gap_index) for event in app.requests] == [
            (True, 1)
        ]
        assert "         ... loading ..." in _rows(app)
        # A second press while the read is in flight asks for nothing more.
        await pilot.press("e")
        await pilot.pause()
        assert len(app.requests) == 1

        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        rows = _rows(app)
        assert "         ... loading ..." not in rows
        assert "  14   14    line 14" in rows
        assert "  16   16    line 16" in rows
        option_list = _option_list(app)
        cursor = option_list._readonly_rows[option_list.highlighted]
        assert (cursor.old_lineno, cursor.new_lineno) == (14, 14)
        assert cursor.line_type is LineType.EXPANDED

        await _press_e_on_gap(app, pilot, 0)
        await _press_e_on_gap(app, pilot, 0)
        assert len(app.requests) == 1
        assert _gap_indices(app) == []
        rows = _rows(app)
        assert "   1    1    line 1" in rows
        assert "  30   30    line 30" in rows
        assert _new_numbers_on_screen(app) == list(range(1, 31))
        assert all(row.isascii() for row in rows)
        # Expanded lines never become comment anchors or selections.
        assert not any(
            line.line_type is LineType.EXPANDED
            for line in option_list._line_map.values()
        )
        assert panel.selection is None


@pytest.mark.asyncio
async def test_after_gap_that_turns_out_empty_says_so() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file("three_after")
        panel = await _open(app, pilot, file)
        lines, eol = _split(FIXTURES["three_after"][1])

        await _press_e_on_gap(app, pilot, 1)
        panel.apply_file_context(file, lines, eol, app.requests[0].gap_index)
        await pilot.pause()

        assert NO_MORE_LINES_MESSAGE in _messages(app)
        assert [_rows(app)[index] for index in _gap_indices(app)] == [
            "         ... 23 unchanged lines (e to expand) ..."
        ]


@pytest.mark.asyncio
async def test_e_away_from_a_gap_row_explains_where_to_go() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        await _open(app, pilot, _file())

        await pilot.press("e")
        await pilot.pause()

        assert MOVE_TO_GAP_MESSAGE in _messages(app)
        assert app.requests == []


@pytest.mark.parametrize("key", ["c", "f3", "r", "R", "d"])
@pytest.mark.asyncio
async def test_comment_keys_on_expanded_and_gap_rows_explain_why(key: str) -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)

        option_list.highlighted = _gap_indices(app)[0]
        await pilot.press(key)
        await pilot.pause()
        assert _messages(app).count(READ_ONLY_LINE_MESSAGE) == 1

        await _press_e_on_gap(app, pilot, 0)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()
        assert option_list.highlighted in option_list._readonly_rows
        await pilot.press(key)
        await pilot.pause()

        assert _messages(app).count(READ_ONLY_LINE_MESSAGE) == 2
        assert app.comments == []


@pytest.mark.asyncio
async def test_a_range_across_a_gap_is_refused() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        await _open(app, pilot, file)
        option_list = _option_list(app)
        gap_row = _gap_indices(app)[1]
        last_of_first_hunk = max(
            index for index in option_list._line_map if index < gap_row
        )

        option_list.highlighted = last_of_first_hunk
        await pilot.press("J", "J")
        await pilot.pause()
        assert option_list.highlighted in option_list._line_map
        await pilot.press("c")
        await pilot.pause()

        assert RANGE_CROSSES_GAP_MESSAGE in _messages(app)
        assert app.comments == []

        # A range inside one hunk still comments as before.
        await pilot.press("escape")
        option_list.highlighted = last_of_first_hunk
        await pilot.press("K", "c")
        await pilot.pause()
        assert len(app.comments) == 1


@pytest.mark.asyncio
async def test_threads_and_draft_markers_still_work_after_expanding() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        root = InlineComment(
            "c1", User("bob"), "Why this change?", NOW, "f.txt", None, 20
        )
        discussion = Discussion("d1", True, root)
        panel = await _open(app, pilot, file, discussions=[discussion])
        marker = DraftMarker(
            uuid4(), "Draft note", "f.txt", "f.txt", None, 10, DiffSide.NEW, False
        )
        panel.set_draft_markers((marker,))
        await pilot.pause()

        for position in (2, 1, 0):
            await _press_e_on_gap(app, pilot, position)
            if position == 2:
                panel.apply_file_context(file, NEW_LINES, NEW_EOL, 2)
                await pilot.pause()
        assert _gap_indices(app) == []
        assert any(row.startswith("    DRAFT new: Draft note") for row in _rows(app))

        option_list = _option_list(app)
        option_list.highlighted = 0
        await pilot.press("right_square_bracket")
        await pilot.pause()
        line = option_list._line_map[option_list.highlighted]
        assert line.new_lineno == 20
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert option_list._line_map[option_list.highlighted].new_lineno == 20

        await pilot.press("d")
        await pilot.pause()
        assert any("Why this change?" in row for row in _rows(app))
        assert len(app.requests) == 1


@pytest.mark.parametrize(
    "message",
    [
        "File too large to expand.",
        "Binary file, there are no lines to expand.",
        "The file was not found at this commit.",
        "The file at this commit does not match the diff. Refresh the review.",
    ],
)
@pytest.mark.asyncio
async def test_failed_read_reports_the_reason_and_restores_the_row(
    message: str,
) -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)

        await _press_e_on_gap(app, pilot, 0)
        panel.file_context_failed(file, message)
        await pilot.pause()

        assert message in _messages(app)
        assert "         ... loading ..." not in _rows(app)
        assert len(_gap_indices(app)) == 3
        # The reader can try again.
        await _press_e_on_gap(app, pilot, 0)
        assert len(app.requests) == 2


@pytest.mark.asyncio
async def test_reply_for_a_file_no_longer_shown_is_dropped() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        stale = _file()
        panel = await _open(app, pilot, stale)
        await _press_e_on_gap(app, pilot, 0)

        fresh = _file()
        panel.set_files([fresh])
        await pilot.pause()
        panel.apply_file_context(stale, NEW_LINES, NEW_EOL, 0)
        panel.file_context_failed(stale, "late failure")
        await pilot.pause()

        assert len(_gap_indices(app)) == 3
        assert _option_list(app)._readonly_rows == {}
        assert "late failure" not in _messages(app)
        # The new diff starts fresh: e asks again.
        await _press_e_on_gap(app, pilot, 0)
        assert [event.file is fresh for event in app.requests] == [False, True]


@pytest.mark.asyncio
async def test_expansion_survives_redraws_and_file_switches() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        other = _file("first_line", "g.txt")
        panel = await _open(app, pilot, file, other)

        await _press_e_on_gap(app, pilot, 1)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        await pilot.pause()
        option_list = _option_list(app)
        before = option_list._readonly_rows[option_list.highlighted]

        panel.set_draft_markers(
            (
                DraftMarker(
                    uuid4(), "x", "f.txt", "f.txt", None, 10, DiffSide.NEW, False
                ),
            )
        )
        await pilot.pause()
        assert option_list._readonly_rows[option_list.highlighted] is before

        panel.action_next_file()
        panel.action_prev_file()
        await pilot.pause()
        assert "  14   14    line 14" in _rows(app)
        assert len(app.requests) == 1


@pytest.mark.asyncio
async def test_read_files_are_kept_for_the_most_recently_used_few() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        limit = DiffContent.CONTEXT_CACHE_FILES
        files = [_file(path=f"f{index}.txt") for index in range(limit + 1)]
        panel = await _open(app, pilot, *files)
        content = app.query_one(DiffContent)
        for file in files[:limit]:
            panel.apply_file_context(file, NEW_LINES, NEW_EOL, 1)
        # Use the oldest again so the second file becomes the least recent.
        assert content.context_model(files[0]) is not None
        stale = HighlightMap()
        content._context_highlights[id(files[1])] = (files[1], stale)
        panel.apply_file_context(files[limit], NEW_LINES, NEW_EOL, 1)
        await pilot.pause()

        assert len(content._context_models) == limit
        assert content.context_model(files[1]) is None
        # The file reads as before its first expansion: no open gap and no
        # whole-file colours outlive its lines.
        assert not content.file_expansion(files[1]).is_shown(1)
        assert id(files[1]) not in content._context_highlights
        for file in (files[0], *files[2:]):
            assert content.context_model(file) is not None
            assert content.file_expansion(file).is_shown(1)
        # The first file on screen still shows its expanded lines.
        assert "  14   14    line 14" in _rows(app)


@pytest.mark.asyncio
async def test_files_that_cannot_expand_render_as_before() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        added = DiffFile("n.txt", "n.txt", FileStatus.ADDED, file.hunks)
        truncated = DiffFile(
            "t.txt", "t.txt", FileStatus.MODIFIED, file.hunks, is_truncated=True
        )
        for candidate in (added, truncated):
            await _open(app, pilot, candidate)
            assert _gap_indices(app) == []
            assert not any("(e to expand)" in row for row in _rows(app))


@pytest.mark.asyncio
async def test_e_away_from_a_gap_row_in_split_view_explains_where_to_go() -> None:
    app = _GapApp()
    async with app.run_test(size=(200, 60), notifications=True) as pilot:
        panel = await _open(app, pilot, _file())
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        app.query_one(SplitDiffView).focus()

        await pilot.press("e")
        await pilot.pause()

        assert MOVE_TO_GAP_MESSAGE in _messages(app)
        assert app.requests == []


@pytest.mark.asyncio
async def test_an_expanded_line_never_shows_a_thread_marker() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        # An outdated thread on line 2, which is unchanged now, and one on a
        # context line inside the first hunk.
        outdated = InlineComment("c1", User("bob"), "Old note", NOW, "f.txt", 2, 2)
        current = InlineComment("c2", User("bob"), "New note", NOW, "f.txt", 8, 8)
        discussions = [
            Discussion("d1", True, outdated),
            Discussion("d2", True, current),
        ]
        panel = await _open(app, pilot, file, discussions=discussions)

        await _press_e_on_gap(app, pilot, 0)
        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()

        rows = _rows(app)
        assert "   2    2    line 2" in rows
        assert not any(row.startswith("   2    2 *") for row in rows)
        # A hunk line keeps its marker.
        assert "   8    8 *  line 8" in rows


@pytest.mark.asyncio
async def test_a_slow_read_keeps_the_cursor_and_range_the_reader_moved_to() -> None:
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)

        await _press_e_on_gap(app, pilot, 0)
        # While the file is read, the reader selects two lines of a hunk.
        first = min(option_list._line_map)
        option_list.highlighted = first
        await pilot.press("J")
        await pilot.pause()
        before = panel.selection
        assert before is not None and len(before.lines) == 2

        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()

        assert "   1    1    line 1" in _rows(app)
        after = panel.selection
        assert after is not None
        assert after.line is before.line
        assert after.lines == before.lines


@pytest.mark.asyncio
async def test_a_read_lands_on_the_new_lines_when_the_cursor_waited_on_the_gap() -> (
    None
):
    app = _GapApp()
    async with app.run_test(size=(100, 60), notifications=True) as pilot:
        file = _file()
        panel = await _open(app, pilot, file)
        option_list = _option_list(app)
        await _press_e_on_gap(app, pilot, 0)
        assert option_list.cursor_gap() is not None

        panel.apply_file_context(file, NEW_LINES, NEW_EOL, 0)
        await pilot.pause()

        cursor = option_list._readonly_rows[option_list.highlighted]
        assert cursor.new_lineno == 1
