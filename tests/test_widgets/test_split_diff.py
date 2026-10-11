"""Behavioral coverage for the responsive side-by-side diff view."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion, InlineComment, User
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffContent,
    DiffFileTree,
    DiffOptionList,
    DiffPanel,
    ReplyRequested,
    ResolveRequested,
)
from tongs.widgets.split_diff import (
    DiffModeState,
    DiffSelection,
    DiffViewMode,
    HighlightMap,
    SplitDiffColumn,
    SplitDiffView,
    is_actionable,
    placeholder_message,
)
from tongs.widgets.two_press import TwoPressConfirm


def _file() -> DiffFile:
    context = DiffLine(10, 20, "café = '東京'", LineType.CONTEXT)
    deleted = DiffLine(11, None, "old_value = '" + "x" * 120 + "'", LineType.DELETION)
    deleted_extra = DiffLine(12, None, "obsolete = True", LineType.DELETION)
    added = DiffLine(None, 21, "new_value = 1", LineType.ADDITION)
    marker = DiffLine(None, None, "\\ No newline at end of file", LineType.NO_NEWLINE)
    return DiffFile(
        old_path="before/renamed.py",
        new_path="after/renamed.py",
        status=FileStatus.RENAMED,
        hunks=(
            DiffHunk(
                "@@ -10,3 +20,2 @@",
                10,
                3,
                20,
                2,
                (context, deleted, deleted_extra, marker, added, marker),
            ),
        ),
        additions=1,
        deletions=2,
        language="python",
    )


def _discussion() -> Discussion:
    return Discussion(
        "thread-1",
        True,
        InlineComment(
            "comment-1",
            User("reviewer"),
            "Use a clearer name",
            datetime(2026, 9, 8, tzinfo=UTC),
            "after/renamed.py",
            None,
            21,
        ),
    )


class _DiffApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.comments: list[CommentRequested] = []
        self.replies: list[ReplyRequested] = []
        self.resolutions: list[ResolveRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel()

    def on_comment_requested(self, event: CommentRequested) -> None:
        self.comments.append(event)

    def on_reply_requested(self, event: ReplyRequested) -> None:
        self.replies.append(event)

    def on_resolve_requested(self, event: ResolveRequested) -> None:
        self.resolutions.append(event)


def test_actionable_cells_are_side_specific() -> None:
    file = _file()
    context, deleted, _, marker, added, _ = file.hunks[0].lines

    assert is_actionable(context, DiffSide.OLD)
    assert is_actionable(context, DiffSide.NEW)
    assert is_actionable(deleted, DiffSide.OLD)
    assert not is_actionable(deleted, DiffSide.NEW)
    assert is_actionable(added, DiffSide.NEW)
    assert not is_actionable(added, DiffSide.OLD)
    assert not is_actionable(marker, DiffSide.OLD)
    assert not is_actionable(marker, DiffSide.NEW)
    assert not is_actionable(None, DiffSide.NEW)


def test_selection_rejects_wrong_side_and_non_source_rows() -> None:
    file = _file()
    deleted = file.hunks[0].lines[1]
    marker = file.hunks[0].lines[3]

    with pytest.raises(ValueError, match="non-actionable"):
        DiffSelection(file, DiffSide.NEW, deleted, (deleted,))
    with pytest.raises(ValueError, match="non-actionable"):
        DiffSelection(file, DiffSide.OLD, marker, (marker,))


@pytest.mark.asyncio
async def test_wide_narrow_and_restored_layout_preserves_old_selection() -> None:
    app = _DiffApp()
    file = _file()
    context, deleted = file.hunks[0].lines[:2]

    async with app.run_test(size=(160, 35)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file], [_discussion()])
        await pilot.pause()

        state = panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert state.requested is DiffViewMode.SPLIT
        assert state.effective is DiffViewMode.SPLIT

        split = app.query_one(SplitDiffView)
        assert split.jump_to(11, DiffSide.OLD)
        old = app.query_one("#split-old", SplitDiffColumn)
        old._selection_anchor = old.highlighted
        old.action_extend_up()
        await pilot.pause()
        assert panel.selection == DiffSelection(
            file, DiffSide.OLD, context, (context, deleted)
        )

        await pilot.resize_terminal(80, 35)
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.UNIFIED
        assert panel.mode_state.narrow_fallback
        assert panel.selection is not None
        assert panel.selection.side is DiffSide.OLD
        assert panel.selection.line is context

        await pilot.resize_terminal(160, 35)
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.SPLIT
        assert panel.selection == DiffSelection(
            file, DiffSide.OLD, context, (context, deleted)
        )


@pytest.mark.asyncio
async def test_split_comment_publishes_explicit_original_side() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        split = app.query_one(SplitDiffView)
        assert split.jump_to(10, DiffSide.OLD)
        app.query_one("#split-old", SplitDiffColumn).action_comment()
        await pilot.pause()

        assert len(app.comments) == 1
        event = app.comments[0]
        assert event.file is file
        assert event.line is file.hunks[0].lines[0]
        assert event.side is DiffSide.OLD
        assert event.mode is CommentMode.COMMENT


@pytest.mark.asyncio
async def test_visible_v_binding_toggles_requested_layout() -> None:
    app = _DiffApp()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file()])
        app.query_one(DiffOptionList).focus()
        await pilot.press("v")
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.SPLIT

        await pilot.press("v")
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.UNIFIED
        assert panel.mode_state.effective is DiffViewMode.UNIFIED


@pytest.mark.asyncio
async def test_narrow_fallback_comment_keeps_old_context_side() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert app.query_one(SplitDiffView).jump_to(10, DiffSide.OLD)

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        unified = app.query_one(DiffOptionList)
        assert unified.selection is not None
        assert unified.selection.side is DiffSide.OLD
        unified.action_comment()
        await pilot.pause()

        assert len(app.comments) == 1
        assert app.comments[0].line is file.hunks[0].lines[0]
        assert app.comments[0].side is DiffSide.OLD


@pytest.mark.asyncio
async def test_columns_share_rows_but_empty_and_marker_cells_are_not_anchors() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert old.option_count == new.option_count
        assert len(old._lines) == len(new._lines) == old.option_count
        assert set(old._line_map) != set(new._line_map)
        assert all(is_actionable(line, DiffSide.OLD) for line in old._line_map.values())
        assert all(is_actionable(line, DiffSide.NEW) for line in new._line_map.values())
        marker = file.hunks[0].lines[3]
        assert marker not in old._line_map.values()
        assert marker not in new._line_map.values()


@pytest.mark.asyncio
async def test_vertical_scroll_and_cursor_are_synchronized() -> None:
    app = _DiffApp()
    deletions = tuple(
        DiffLine(number, None, f"old {number}", LineType.DELETION)
        for number in range(1, 51)
    )
    additions = tuple(
        DiffLine(None, number, f"new {number}", LineType.ADDITION)
        for number in range(1, 51)
    )
    file = DiffFile(
        "many.py",
        "many.py",
        FileStatus.MODIFIED,
        (DiffHunk("@@ -1,50 +1,50 @@", 1, 50, 1, 50, (*deletions, *additions)),),
    )

    async with app.run_test(size=(160, 20)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        old.focus()
        old.scroll_to(y=20, animate=False, immediate=True)
        await pilot.pause()
        assert old.scroll_y == new.scroll_y == 20

        old.action_cursor_down()
        await pilot.pause()
        assert old.highlighted == new.highlighted


@pytest.mark.asyncio
async def test_old_side_suggestion_is_blocked_and_new_side_is_published() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)

        assert split.jump_to(10, DiffSide.OLD)
        app.query_one("#split-old", SplitDiffColumn).action_suggest()
        await pilot.pause()
        assert not app.comments
        assert any("new side only" in note.message for note in app._notifications)

        assert split.jump_to(21, DiffSide.NEW)
        app.query_one("#split-new", SplitDiffColumn).action_suggest()
        await pilot.pause()
        assert len(app.comments) == 1
        assert app.comments[0].mode is CommentMode.SUGGEST
        assert app.comments[0].side is DiffSide.NEW


@pytest.mark.asyncio
async def test_discussion_state_and_actions_survive_layout_changes() -> None:
    app = _DiffApp()
    file = _file()
    discussion = _discussion()

    async with app.run_test(size=(160, 35), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file], [discussion])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        assert split.jump_to(21, DiffSide.NEW)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert new._current_discussions() == [discussion]

        new.action_toggle_discussion()
        await pilot.pause()
        assert discussion.id in split.expanded_threads

        panel.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        assert discussion.id in app.query_one(DiffOptionList)._expanded_threads
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert discussion.id in split.expanded_threads
        assert split.jump_to(21, DiffSide.NEW)

        new.action_reply_discussion()
        new.action_resolve_discussion()
        new.action_resolve_discussion()
        await pilot.pause()
        assert [event.discussion_id for event in app.replies] == [discussion.id]
        assert [(event.discussion_id, event.resolved) for event in app.resolutions] == [
            (discussion.id, True)
        ]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_split_resolve_pressed_late_rearms_instead_of_resolving() -> None:
    app = _DiffApp()
    clock = _Clock()

    async with app.run_test(size=(160, 35), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file()], [_discussion()])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        assert split.jump_to(21, DiffSide.NEW)
        new = app.query_one("#split-new", SplitDiffColumn)
        new._resolve_confirm = TwoPressConfirm(clock=clock)

        new.action_resolve_discussion()
        clock.now += 6.0
        new.action_resolve_discussion()
        await pilot.pause()
        assert app.resolutions == []
        clock.now += 1.0
        new.action_resolve_discussion()
        await pilot.pause()
        assert [event.discussion_id for event in app.resolutions] == ["thread-1"]

        new.action_resolve_discussion()
        panel.show_placeholder("Loading diff...")
        assert not new._resolve_confirm.armed


@pytest.mark.asyncio
async def test_split_file_switch_clears_an_armed_resolve() -> None:
    app = _DiffApp()
    discussion = _discussion()

    async with app.run_test(size=(160, 35), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file()], [discussion])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert split.jump_to(21, DiffSide.NEW)
        new.action_resolve_discussion()
        old._resolve_confirm.arm(discussion.id)
        assert new._resolve_confirm.armed
        assert old._resolve_confirm.armed

        split.show_file(_file(), [discussion], HighlightMap())
        await pilot.pause()
        assert not new._resolve_confirm.armed
        assert not old._resolve_confirm.armed

        # The next press after the switch arms again instead of resolving.
        assert split.jump_to(21, DiffSide.NEW)
        new.action_resolve_discussion()
        await pilot.pause()
        assert app.resolutions == []
        new.action_resolve_discussion()
        await pilot.pause()
        assert [event.discussion_id for event in app.resolutions] == [discussion.id]


@pytest.mark.asyncio
async def test_unified_resolve_expires_and_a_rerender_clears_it() -> None:
    app = _DiffApp()
    clock = _Clock()
    discussion = _discussion()

    async with app.run_test(size=(160, 35), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_file()], [discussion])
        panel.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        content = app.query_one(DiffContent)
        unified = app.query_one(DiffOptionList)
        unified._resolve_confirm = TwoPressConfirm(clock=clock)
        assert content.jump_to(21, DiffSide.NEW, discussion.id)
        await pilot.pause()

        unified.action_resolve_discussion()
        clock.now += 6.0
        unified.action_resolve_discussion()
        await pilot.pause()
        assert app.resolutions == []

        # Redrawing the file drops the armed press, so the next one re-arms.
        assert unified._resolve_confirm.armed
        content.show_file(_file(), [discussion])
        await pilot.pause()
        assert not unified._resolve_confirm.armed
        assert content.jump_to(21, DiffSide.NEW, discussion.id)
        unified.action_resolve_discussion()
        await pilot.pause()
        assert app.resolutions == []

        unified.action_resolve_discussion()
        await pilot.pause()
        assert [event.discussion_id for event in app.resolutions] == [discussion.id]


@pytest.mark.asyncio
async def test_long_context_is_folded_without_creating_comment_anchors() -> None:
    app = _DiffApp()
    context = tuple(
        DiffLine(number, number, f"context {number}", LineType.CONTEXT)
        for number in range(1, 11)
    )
    addition = DiffLine(None, 11, "changed", LineType.ADDITION)
    file = DiffFile(
        "long.py",
        "long.py",
        FileStatus.MODIFIED,
        (DiffHunk("@@ -1,10 +1,11 @@", 1, 10, 1, 11, (*context, addition)),),
    )

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert context[4] not in old._line_map.values()
        assert context[4] not in new._line_map.values()
        assert old.option_count == new.option_count
        assert old.option_count == 10  # side header, hunk, 3 + fold + 3, change


@pytest.mark.asyncio
async def test_added_deleted_and_metadata_only_files_have_valid_sides() -> None:
    app = _DiffApp()
    added = DiffFile(
        "new.py",
        "new.py",
        FileStatus.ADDED,
        (
            DiffHunk(
                "@@ -0,0 +1 @@",
                0,
                0,
                1,
                1,
                (DiffLine(None, 1, "new", LineType.ADDITION),),
            ),
        ),
    )
    deleted = DiffFile(
        "old.py",
        "old.py",
        FileStatus.DELETED,
        (
            DiffHunk(
                "@@ -1 +0,0 @@",
                1,
                1,
                0,
                0,
                (DiffLine(1, None, "old", LineType.DELETION),),
            ),
        ),
    )
    metadata = DiffFile(
        "large.py",
        "large.py",
        FileStatus.MODIFIED,
        (),
        additions=20,
        is_truncated=True,
    )

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.request_mode(DiffViewMode.SPLIT)
        for file, populated_side in (
            (added, DiffSide.NEW),
            (deleted, DiffSide.OLD),
        ):
            panel.set_files([file])
            await pilot.pause()
            assert panel.selection is not None
            assert panel.selection.side is populated_side
            assert bool(app.query_one("#split-old", SplitDiffColumn)._line_map) is (
                populated_side is DiffSide.OLD
            )
            assert bool(app.query_one("#split-new", SplitDiffColumn)._line_map) is (
                populated_side is DiffSide.NEW
            )

        panel.set_files([metadata])
        await pilot.pause()
        assert not app.query_one("#split-old", SplitDiffColumn)._line_map
        assert not app.query_one("#split-new", SplitDiffColumn)._line_map


@pytest.mark.parametrize(
    ("message", "use_empty_files"),
    (
        ("No changes", True),
        ("Loading diff...", False),
        ("Could not load diff. Try Ctrl+R. (failure)", False),
    ),
)
@pytest.mark.asyncio
async def test_placeholder_cannot_restore_prior_source_on_layout_or_navigation(
    message: str,
    use_empty_files: bool,
) -> None:
    app = _DiffApp()
    original = _file()
    secret = replace(original.hunks[0].lines[0], content="secret_stale_line = True")
    file = replace(
        original,
        old_path="before/secret.md",
        new_path="after/secret.md",
        hunks=(
            replace(
                original.hunks[0],
                lines=(secret, *original.hunks[0].lines[1:]),
            ),
        ),
        language="markdown",
    )
    original_discussion = _discussion()
    discussion = replace(
        original_discussion,
        root_comment=replace(
            original_discussion.root_comment,
            file_path="after/secret.md",
        ),
    )

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        # Render the file in split first, so the columns hold the prior source
        # that the placeholder must clear.
        panel.request_mode(DiffViewMode.SPLIT)
        panel.set_files([file], [discussion])
        await pilot.pause()
        assert app.query_one("#split-new", SplitDiffColumn)._current_file is not None
        panel.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        content = panel.query_one("#diff-content", DiffContent)
        content._preview_selection = panel.selection
        content._showing_preview = True
        content._show_active_diff()
        await pilot.pause()
        assert content._showing_preview

        if use_empty_files:
            panel.set_files([])
        else:
            panel.show_placeholder(message)
        await pilot.pause()

        def assert_authoritative_placeholder() -> None:
            unified = app.query_one(DiffOptionList)
            split = app.query_one(SplitDiffView)
            columns = (
                app.query_one("#split-old", SplitDiffColumn),
                app.query_one("#split-new", SplitDiffColumn),
            )
            assert panel.selection is None
            assert panel._files == []
            assert panel._discussions_by_file == {}
            assert str(app.query_one(DiffFileTree).root.label) == "Changed files (0)"
            assert str(app.query_one("#diff-file-header", Static).render()) == ""
            assert content._current_file is None
            assert content._file_discussions == []
            assert content._preview_selection is None
            assert not content._showing_preview
            assert unified._current_file is None
            assert unified._line_map == {}
            assert unified._comment_map == {}
            assert split._file is None
            assert split._discussions == []
            assert split._discussion_index == {}
            for widget in (unified, *columns):
                assert widget.option_count == 1
                assert "secret_stale_line" not in str(
                    widget.get_option_at_index(0).prompt
                )
            assert all(column._current_file is None for column in columns)
            assert all(column._line_map == {} for column in columns)

        assert_authoritative_placeholder()
        app.query_one(DiffOptionList).focus()
        await pilot.press("c")
        await pilot.pause()
        assert app.comments == []

        panel.action_comment()
        await pilot.pause()
        assert len(app.comments) == 1
        assert app.comments[0].file is None
        assert app.comments[0].line is None
        app.comments.clear()

        await pilot.press("v")
        await pilot.pause()
        assert panel.mode_state == DiffModeState(DiffViewMode.SPLIT, DiffViewMode.SPLIT)
        assert_authoritative_placeholder()
        app.query_one("#split-old", SplitDiffColumn).focus()
        await pilot.press("c")
        await pilot.pause()
        assert app.comments == []

        await pilot.press("n", "N")
        await pilot.pause()
        assert_authoritative_placeholder()

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert panel.mode_state == DiffModeState(
            DiffViewMode.SPLIT, DiffViewMode.UNIFIED
        )
        assert_authoritative_placeholder()

        await pilot.resize_terminal(160, 30)
        await pilot.pause()
        assert panel.mode_state == DiffModeState(DiffViewMode.SPLIT, DiffViewMode.SPLIT)
        assert_authoritative_placeholder()


@pytest.mark.asyncio
async def test_h_and_l_move_focus_between_split_columns() -> None:
    app = _DiffApp()
    file = _file()
    deleted = file.hunks[0].lines[1]
    added = file.hunks[0].lines[4]

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        split = app.query_one(SplitDiffView)
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert split.jump_to(21, DiffSide.NEW)
        await pilot.pause()
        assert new.has_focus

        # h and l must still work after a unified and split round trip.
        panel.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        new.focus()
        await pilot.pause()

        await pilot.press("h")
        await pilot.pause()
        assert old.has_focus
        assert panel.selection is not None
        assert panel.selection.side is DiffSide.OLD
        assert panel.selection.line is deleted

        await pilot.press("l")
        await pilot.pause()
        assert new.has_focus
        assert panel.selection is not None
        assert panel.selection.side is DiffSide.NEW
        assert panel.selection.line is added


def _metadata_file(**flags: object) -> DiffFile:
    """A file the forge described without any patch content."""

    return DiffFile(
        old_path="src/tool.sh",
        new_path="src/tool.sh",
        status=FileStatus.MODIFIED,
        hunks=(),
        language="bash",
        **flags,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("flags", "expected"),
    (
        ({"is_binary": True}, "[Binary file]"),
        ({"is_empty": True}, "[Empty file]"),
        ({"is_mode_only": True}, "[File mode changed]"),
        ({"is_rename_only": True}, "[Renamed with no content change]"),
        (
            {"is_truncated": True},
            (
                "This file's diff is too large for the forge to send. Press e to "
                "generate it locally (downloads both versions of the file)."
            ),
        ),
        (
            {"is_unavailable": True},
            "Not exposed by the forge. Press o to view in browser.",
        ),
        ({}, "Diff not available. Press o to view in browser."),
    ),
)
def test_placeholder_message_states_what_the_forge_reported(
    flags: dict[str, object], expected: str
) -> None:
    """Each metadata-only shape gets its own line, never a generic failure.

    The unavailable line is the only one that describes the forge rather than
    the file, because GitHub's files endpoint withholds the patch for binary
    content and for a mode-only change without distinguishing them.
    """

    assert placeholder_message(_metadata_file(**flags)) == expected


@pytest.mark.asyncio
async def test_unified_placeholder_never_blames_a_too_large_api_response() -> None:
    """The terminal unified view must not invent a size limit it cannot know."""

    app = _DiffApp()
    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([_metadata_file(is_unavailable=True)])
        await pilot.pause()

        unified = app.query_one(DiffOptionList)
        assert unified.option_count == 1
        assert str(unified.get_option_at_index(0).prompt) == (
            "Not exposed by the forge. Press o to view in browser."
        )


@pytest.mark.asyncio
async def test_n_and_shift_n_navigate_between_two_files() -> None:
    first = _file()
    second = DiffFile(
        old_path="utils.py",
        new_path="utils.py",
        status=FileStatus.MODIFIED,
        hunks=(
            DiffHunk(
                "@@ -1 +1 @@",
                1,
                1,
                1,
                1,
                (DiffLine(1, 1, "pass", LineType.CONTEXT),),
            ),
        ),
        additions=0,
        deletions=0,
        language="python",
    )
    app = _DiffApp()

    async with app.run_test(size=(160, 35)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([first, second])
        await pilot.pause()

        assert panel._current_index == 0
        await pilot.press("n")
        await pilot.pause()
        assert panel._current_index == 1

        await pilot.press("N")
        await pilot.pause()
        assert panel._current_index == 0


def _gitlab_chmod_files() -> list[DiffFile]:
    """A GitLab ``/diffs`` page with one chmod and one plain edit (#397)."""
    return list(
        convert_forge_changes(
            [
                {
                    "old_path": "bin/run.sh",
                    "new_path": "bin/run.sh",
                    "a_mode": "100644",
                    "b_mode": "100755",
                    "new_file": False,
                    "renamed_file": False,
                    "deleted_file": False,
                    "diff": "",
                },
                {
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "a_mode": "100644",
                    "b_mode": "100644",
                    "new_file": False,
                    "renamed_file": False,
                    "deleted_file": False,
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                },
            ]
        )
    )


def _file_header(app: App[None]) -> str:
    return str(app.query_one("#diff-file-header", Static).render())


@pytest.mark.asyncio
async def test_gitlab_chmod_shows_the_mode_change_in_both_file_headers() -> None:
    app = _DiffApp()
    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files(_gitlab_chmod_files())
        await pilot.pause()

        assert "bin/run.sh" in _file_header(app)
        assert "mode 100644 -> 100755" in _file_header(app)
        unified = app.query_one(DiffOptionList)
        assert str(unified.get_option_at_index(0).prompt) == "[File mode changed]"

        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert str(old.get_option_at_index(0).prompt) == (
            " OLD  bin/run.sh  mode 100644 -> 100755"
        )
        assert str(new.get_option_at_index(0).prompt) == (
            " NEW  bin/run.sh  mode 100644 -> 100755"
        )

        panel.action_next_file()
        await pilot.pause()
        assert "app.py" in _file_header(app)
        assert "mode" not in _file_header(app)
        assert str(new.get_option_at_index(0).prompt) == " NEW  app.py"


def _column_rows(column: SplitDiffColumn) -> list[str]:
    rows: list[str] = []
    for index in range(column.option_count):
        prompt = column.get_option_at_index(index).prompt
        assert isinstance(prompt, Text)
        rows.append(prompt.plain)
    return rows


@pytest.mark.asyncio
async def test_new_side_only_thread_on_a_context_line_shows_in_split_view() -> None:
    app = _DiffApp()
    file = _file()
    context = file.hunks[0].lines[0]
    # GitLab anchors a thread on an unchanged line by its new line number only.
    discussion = Discussion(
        "thread-context",
        True,
        InlineComment(
            "comment-context",
            User("reviewer"),
            "Explain this constant",
            datetime(2026, 9, 8, tzinfo=UTC),
            "after/renamed.py",
            None,
            20,
        ),
    )

    async with app.run_test(size=(160, 35)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file], [discussion])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)

        assert split.jump_to(20, DiffSide.NEW)
        await pilot.pause()
        row = new.highlighted
        assert row is not None
        assert new._line_map[row] is context
        assert _column_rows(new)[row].startswith("   20 * ")
        assert _column_rows(old)[row].startswith("   10   ")
        assert not any("Explain this constant" in text for text in _column_rows(new))

        await pilot.press("d")
        await pilot.pause()
        assert any("Explain this constant" in text for text in _column_rows(new))

        await pilot.press("r")
        await pilot.pause()
        assert [event.discussion_id for event in app.replies] == [discussion.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "start", "cursor_index"),
    [("K", 11, 0), ("J", 10, 1)],
    ids=["extend-up", "extend-down"],
)
async def test_extend_key_starts_a_range_when_none_is_selected(
    key: str, start: int, cursor_index: int
) -> None:
    app = _DiffApp()
    file = _file()
    context, deleted = file.hunks[0].lines[:2]

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert app.query_one(SplitDiffView).jump_to(start, DiffSide.OLD)
        await pilot.pause()
        old = app.query_one("#split-old", SplitDiffColumn)
        assert old.has_focus
        assert panel.selection is not None
        assert len(panel.selection.lines) == 1

        await pilot.press(key)
        await pilot.pause()
        assert panel.selection == DiffSelection(
            file, DiffSide.OLD, (context, deleted)[cursor_index], (context, deleted)
        )


@pytest.mark.asyncio
async def test_jump_to_an_old_side_line_makes_the_old_side_active() -> None:
    app = _DiffApp()
    file = _file()
    context = file.hunks[0].lines[0]

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        assert split.jump_to(21, DiffSide.NEW)
        await pilot.pause()
        assert panel.selection is not None
        assert panel.selection.side is DiffSide.NEW

        # Line 10 is a context line, so both columns can anchor its row; the
        # comment must still follow the side the jump asked for.
        assert split.jump_to(10, DiffSide.OLD)
        assert panel.selection == DiffSelection(file, DiffSide.OLD, context, (context,))
        split.action_comment()
        await pilot.pause()

        assert len(app.comments) == 1
        assert app.comments[0].line is context
        assert app.comments[0].side is DiffSide.OLD
        assert app.comments[0].selection == DiffSelection(
            file, DiffSide.OLD, context, (context,)
        )
