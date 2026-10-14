"""Returning from an MR detail and palette navigation stay consistent (#311).

Going back from an MR that merged, closed, or was approved has to refresh the
inbox lists the user came from, so the row no longer lists the MR as open,
while going back without changing anything reloads nothing. All Open pages
through load more, so a merged or closed MR leaves by dropping its row in
place, an approve changes nothing there, and an unknown merge outcome re-reads
past the list cache. The palette routes its screen commands through one app
method that never stacks a second copy of a screen and targets the named
instance, so a repository-scoped inbox never stands in for the main one, and
it refuses to leave an MR whose submission is still running, a submit dialog
with an unsaved summary, or an open comment editor with typed text. A review
change recorded after a merge must not undo it, and an unknown-outcome re-read
stops a running All Open read first.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from textual.command import CommandList, CommandPalette
from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.widgets import TextArea

from tests.test_tui_mr_services import _app, _calls, _Forge, _settle, _wait_until
from tests.test_tui_session import settle
from tests.test_views.test_inbox_all_open_lazy_ci import (
    LazyForgeClient,
    build,
    numbers,
    status_text,
    wait_until,
)
from tests.test_views.test_inbox_all_open_lazy_ci import (
    summary as open_summary,
)
from tongs.app import TongsApp
from tongs.forges.models import ForgeHost, MRPage, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.views.inbox import InboxScreen, ListsChange
from tongs.views.mr_detail import MRDetailScreen
from tongs.views.review_submit import ReviewSubmitScreen
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.mr_table import MRTable


class _ForgeWithoutMerged(_Forge):
    """A forge whose My Reviews list drops the MR once it is merged."""

    def __init__(self, host: ForgeHost) -> None:
        super().__init__(host)
        self.merged = False

    async def list_my_reviews(self) -> list[MRSummary]:
        self.calls.append(("list_my_reviews",))
        return [] if self.merged else [self.summary]


def _merge_app(tmp_path: Path) -> tuple[TongsApp, _ForgeWithoutMerged]:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    forge = _ForgeWithoutMerged(host)
    app, _returned = _app(tmp_path, forge)
    return app, forge


def _stack_names(app: TongsApp) -> list[str]:
    return [type(screen).__name__ for screen in app.screen_stack]


# Textual starts every app on its own default Screen; the inbox is pushed on
# top of it, so it stays the bottom of every stack the app builds.
_DEFAULT_SCREEN = "Screen"


class _GatedPagesClient(LazyForgeClient):
    """A lazy client whose later pages wait for a gate before arriving."""

    def __init__(self, summaries: list[MRSummary], page_size: int = 2) -> None:
        super().__init__(summaries, page_size=page_size)
        self.page_gate: asyncio.Event | None = None
        self.gated_reads: list[tuple[str, int]] = []

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        if page > 1 and self.page_gate is not None:
            self.gated_reads.append((repo_path, page))
            await self.page_gate.wait()
        return await super().list_mrs_page(
            repo_path, state=state, page=page, per_page=per_page, with_ci=with_ci
        )


def _palette_hit(app: TongsApp) -> str | None:
    """The title of the command the palette highlights, if it offers one."""
    if not isinstance(app.screen, CommandPalette):
        return None
    try:
        options = app.screen.query_one(CommandList).options
        highlighted = app.screen.query_one(CommandList).highlighted
    except NoMatches:
        return None
    if highlighted is None or highlighted >= len(options):
        return None
    return str(options[highlighted].prompt).partition("\n")[0]


def _submit_body(screen: ReviewSubmitScreen) -> TextArea | None:
    """The dialog's summary field, once the dialog has mounted it."""
    try:
        return screen.query_one("#review-submit-body", TextArea)
    except NoMatches:
        return None


async def _palette(pilot: Pilot, app: TongsApp, query: str) -> None:
    """Run one command from the real palette, the way a user would.

    The waits read what the palette settled on rather than sleeping: Enter
    selects the highlighted command, so it must be the one asked for, and
    the command runs once the palette has closed.
    """
    app.action_command_palette()
    await pilot.pause()
    await pilot.press(*query)
    await _wait_until(app, lambda: _palette_hit(app) == query)
    await pilot.press("enter")
    await _wait_until(app, lambda: not isinstance(app.screen, CommandPalette))


@pytest.mark.asyncio
async def test_back_from_a_merged_mr_reloads_the_active_tab(tmp_path: Path) -> None:
    app, forge = _merge_app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        inbox = app.screen
        table = inbox.query_one("#reviews-table", MRTable)
        assert table.row_count == 1
        reads = len(_calls(forge, "list_my_reviews"))

        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        # The two presses arm and confirm the merge.
        screen.action_merge()
        screen.action_merge()
        await _settle(app)
        assert len(_calls(forge, "merge")) == 1
        forge.merged = True

        await pilot.press("escape")
        await _wait_until(app, lambda: app.screen is inbox)
        await _wait_until(app, lambda: table.row_count == 0)
        assert len(_calls(forge, "list_my_reviews")) == reads + 1


@pytest.mark.asyncio
async def test_back_without_a_change_reloads_nothing(tmp_path: Path) -> None:
    app, forge = _merge_app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        inbox = app.screen
        table = inbox.query_one("#reviews-table", MRTable)
        reads = len(_calls(forge, "list_my_reviews"))

        table.focus()
        await pilot.press("enter")
        await _settle(app)
        await pilot.press("escape")
        await _wait_until(app, lambda: app.screen is inbox)
        await _settle(app)

        # Nothing changed, so the rows the user was reading stay as they were.
        assert table.row_count == 1
        assert len(_calls(forge, "list_my_reviews")) == reads


@pytest.mark.asyncio
async def test_palette_navigation_never_stacks_a_second_copy(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        inbox = app.screen
        table = inbox.query_one("#reviews-table", MRTable)
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        assert _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "MRDetailScreen"]

        # From the MR detail, Inbox pops back to the inbox underneath it.
        await _palette(pilot, app, "Inbox")
        await _wait_until(
            app, lambda: _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]
        )
        assert app.screen is inbox

        # Repos is not on the stack, so it is pushed.
        await _palette(pilot, app, "Repos")
        await _wait_until(
            app,
            lambda: (
                _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "RepoListScreen"]
            ),
        )

        # Asking for the screen on top again stays put.
        await _palette(pilot, app, "Repos")
        await _settle(app)
        assert _stack_names(app) == [
            _DEFAULT_SCREEN,
            "InboxScreen",
            "RepoListScreen",
        ]

        # From the repository list, Inbox pops back to the inbox underneath.
        await _palette(pilot, app, "Inbox")
        await _wait_until(
            app, lambda: _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]
        )

        # No screen class ever appears twice.
        names = _stack_names(app)
        assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_palette_inbox_from_a_scoped_inbox_returns_to_the_main_one(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        main_inbox = app.screen

        # r opens the repository list; enter opens that repository's inbox.
        await pilot.press("r")
        await _wait_until(
            app,
            lambda: (
                _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "RepoListScreen"]
            ),
        )
        await pilot.press("enter")
        await _wait_until(
            app,
            lambda: (
                isinstance(app.screen, InboxScreen) and app.screen is not main_inbox
            ),
        )
        await _settle(app)

        # The palette asks for the main inbox by name, so the scoped copy on
        # top is popped through instead of standing in for it.
        await _palette(pilot, app, "Inbox")
        await _wait_until(app, lambda: app.screen is main_inbox)
        assert _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]


@pytest.mark.asyncio
async def test_palette_inbox_from_a_scoped_mr_returns_to_the_main_inbox(
    tmp_path: Path,
) -> None:
    app, forge = _merge_app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        main_inbox = app.screen
        main_table = main_inbox.query_one("#reviews-table", MRTable)
        assert main_table.row_count == 1

        await pilot.press("r")
        await _wait_until(
            app,
            lambda: (
                _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "RepoListScreen"]
            ),
        )
        await pilot.press("enter")
        await _wait_until(
            app,
            lambda: (
                isinstance(app.screen, InboxScreen) and app.screen is not main_inbox
            ),
        )
        await _settle(app)
        scoped_table = app.screen.query_one("#reviews-table", MRTable)

        scoped_table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        # The two presses arm and confirm the merge.
        screen.action_merge()
        screen.action_merge()
        await _settle(app)
        assert len(_calls(forge, "merge")) == 1
        forge.merged = True

        # Leaving through the palette reloads the main inbox it returns to,
        # so the merged MR does not stay listed there as open.
        await _palette(pilot, app, "Inbox")
        await _wait_until(app, lambda: app.screen is main_inbox)
        assert _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]
        await _wait_until(app, lambda: main_table.row_count == 0)


@pytest.mark.asyncio
async def test_palette_stays_on_an_mr_whose_submission_is_running(
    tmp_path: Path,
) -> None:
    app, forge = _merge_app(tmp_path)
    forge.blocked_mutation = "verdict"

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        table = app.screen.query_one("#reviews-table", MRTable)
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        # Start review mode, then submit a verdict that blocks in the forge.
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("v")
        await pilot.press("ctrl+s")
        await asyncio.wait_for(forge.mutation_started.wait(), timeout=2)
        app.clear_notifications()

        # The palette command refuses to leave while the submission runs, so
        # a cancelled submission cannot leave its outcome unknown. The
        # submission worker is deliberately blocked, so the pauses read the
        # message pump without waiting on the worker barrier, which would
        # hang until the notification toast has already expired.
        await _palette(pilot, app, "Inbox")
        await pilot.pause()
        assert app.screen is screen
        assert _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "MRDetailScreen"]
        assert any(
            "Submission in progress" in str(notice.message)
            for notice in app._notifications
        )

        # Let the blocked worker unwind before the app tears down.
        app.workers.cancel_group(screen, "review-draft-submit")
        await _settle(app)


@pytest.mark.asyncio
async def test_a_merged_mr_leaves_all_open_in_place(tmp_path: Path) -> None:
    client = LazyForgeClient(
        [open_summary("acme/big", number, number * 2) for number in range(1, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        inbox = app.screen
        table = inbox.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        # The cursor sits on the first row, whose MR merged.
        client.page_reads.clear()
        inbox.invalidate(client.summaries[0], ListsChange.CLOSED)
        await wait_until(pilot, lambda: numbers(table) == [2])

        # The row left in place: no re-read, the cursor moved to the row
        # that took the removed one's place, and the pages loaded so far
        # still offer load more.
        assert client.page_reads == []
        assert table.get_selected_mr() is not None
        assert table.get_selected_mr().number == 2
        assert inbox.check_action("load_more", ()) is True
        assert status_text(app) == (
            "1 open MR loaded across 1 repository, more to load (m)"
        )

        # Load more still reads the pages that did not arrive, and takes
        # the cursor, which was on the last row, to the first new row.
        client.page_reads.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: numbers(table) == [2, 3, 4])
        assert client.page_reads == [("acme/big", 2, False)]

        # A second merged MR leaves the same way while the cursor reads a
        # row of its own, which follows the removal up one row.
        client.page_reads.clear()
        assert table.get_selected_mr() is not None
        assert table.get_selected_mr().number == 3
        inbox.invalidate(client.summaries[1], ListsChange.CLOSED)
        await wait_until(pilot, lambda: numbers(table) == [3, 4])
        assert client.page_reads == []
        assert table.get_selected_mr() is not None
        assert table.get_selected_mr().number == 3


@pytest.mark.asyncio
async def test_a_merge_while_a_load_more_runs_stops_it_cleanly(
    tmp_path: Path,
) -> None:
    client = _GatedPagesClient(
        [open_summary("acme/big", number, number * 2) for number in range(1, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        inbox = app.screen
        table = inbox.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        # A load more starts and blocks inside the forge read.
        client.page_gate = asyncio.Event()
        client.page_reads.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: bool(client.gated_reads))

        # The merged MR leaves while that read is in flight. Stopping the
        # read first keeps the rows and leaves load more ready again,
        # instead of the offer stuck half done.
        inbox.invalidate(client.summaries[0], ListsChange.CLOSED)
        await settle(app)
        assert numbers(table) == [2]
        assert client.gated_reads == [("acme/big", 2)]
        assert inbox.check_action("load_more", ()) is True

        # With the gate open, the next press reads the pending page.
        client.page_gate.set()
        client.page_reads.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: numbers(table) == [2, 3, 4])
        assert client.page_reads == [("acme/big", 2, False)]


@pytest.mark.asyncio
async def test_a_merge_of_unknown_outcome_reloads_all_open(tmp_path: Path) -> None:
    client = LazyForgeClient(
        [open_summary("acme/big", number, number * 2) for number in range(1, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        inbox = app.screen
        table = inbox.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        # The merge may not have landed, so the tab re-reads its first page
        # instead of dropping a row that may still be open.
        client.page_reads.clear()
        inbox.invalidate(client.summaries[0], ListsChange.MAYBE_CLOSED)
        await wait_until(pilot, lambda: len(client.page_reads) == 1)
        await wait_until(pilot, lambda: numbers(table) == [1, 2])
        assert client.page_reads == [("acme/big", 1, False)]


@pytest.mark.asyncio
async def test_an_approval_changes_nothing_in_all_open(tmp_path: Path) -> None:
    client = LazyForgeClient(
        [open_summary("acme/big", number, number * 2) for number in range(1, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        inbox = app.screen
        table = inbox.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        # An approve changes only the viewer's own review state, which the
        # open list does not show, so its rows and pages stay as they were.
        client.page_reads.clear()
        inbox.invalidate(client.summaries[0], ListsChange.REVIEWED)
        await pilot.pause()
        assert client.page_reads == []
        assert numbers(table) == [1, 2]
        assert inbox.check_action("load_more", ()) is True


@pytest.mark.asyncio
async def test_palette_stays_on_a_submit_dialog_with_unsaved_text(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        table = app.screen.query_one("#reviews-table", MRTable)
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        dialog = cast(ReviewSubmitScreen, app.screen)
        await _wait_until(app, lambda: _submit_body(dialog) is not None)
        dialog.query_one("#review-submit-body", TextArea).text = "unsaved summary"

        # The palette command refuses to drop the dialog and its typed
        # summary, the way the dialog's own Escape asks before discarding.
        await _palette(pilot, app, "Inbox")
        await pilot.pause()
        assert app.screen is dialog
        assert _stack_names(app) == [
            _DEFAULT_SCREEN,
            "InboxScreen",
            "MRDetailScreen",
            "ReviewSubmitScreen",
        ]
        assert any(
            "Unsaved summary or verdict changes" in str(notice.message)
            for notice in app._notifications
        )
        assert (
            dialog.query_one("#review-submit-body", TextArea).text == "unsaved summary"
        )

        # With the text back to the draft's own, the same command leaves,
        # popping through the dialog and the MR detail below it.
        dialog.query_one("#review-submit-body", TextArea).text = dialog.draft.body
        await _palette(pilot, app, "Inbox")
        await _wait_until(
            app, lambda: _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]
        )


@pytest.mark.asyncio
async def test_palette_stays_with_an_open_comment_editor(tmp_path: Path) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        table = app.screen.query_one("#reviews-table", MRTable)
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        # A plain comment, not a draft one: its typed text is just as lost
        # when the palette drops the screen out from under the editor.
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "typed comment"

        await _palette(pilot, app, "Inbox")
        await pilot.pause()
        assert app.screen is screen
        assert _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen", "MRDetailScreen"]
        assert any(
            "open editor text" in str(notice.message) for notice in app._notifications
        )
        assert editor.display
        assert editor.query_one("#comment-input", TextArea).text == "typed comment"

        # The editor's own Escape still discards on two presses, and then
        # the same palette command leaves.
        await pilot.press("escape")
        await pilot.pause()
        assert editor.display
        await pilot.press("escape")
        await _wait_until(app, lambda: not editor.display)
        await _palette(pilot, app, "Inbox")
        await _wait_until(
            app, lambda: _stack_names(app) == [_DEFAULT_SCREEN, "InboxScreen"]
        )


@pytest.mark.asyncio
async def test_a_review_change_after_a_merge_keeps_the_row_dropped(
    tmp_path: Path,
) -> None:
    app, forge = _merge_app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        inbox = app.screen
        all_open = inbox.query_one("#all-open-table", MRTable)

        # Load All Open first, so the merged MR has a row there to lose.
        await pilot.press("3")
        await _wait_until(app, lambda: all_open.row_count == 1)
        await pilot.press("1")
        await pilot.pause()

        table = inbox.query_one("#reviews-table", MRTable)
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)

        # The two presses arm and confirm each action.
        screen.action_merge()
        screen.action_merge()
        await _settle(app)
        assert len(_calls(forge, "merge")) == 1
        screen.action_approve()
        screen.action_approve()
        await _settle(app)
        assert any(call[0] == "verdict" for call in forge.calls)

        # The approval is a review change, which must not replace the merge
        # that took the MR out of the open lists.
        assert screen._lists_change is ListsChange.CLOSED

        forge.merged = True
        await pilot.press("escape")
        await _wait_until(app, lambda: app.screen is inbox)
        await _wait_until(app, lambda: all_open.row_count == 0)
        await _wait_until(app, lambda: table.row_count == 0)


@pytest.mark.asyncio
async def test_a_merge_of_unknown_outcome_stops_a_running_load_more(
    tmp_path: Path,
) -> None:
    client = _GatedPagesClient(
        [open_summary("acme/big", number, number * 2) for number in range(1, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        inbox = app.screen
        table = inbox.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        # A load more starts and blocks inside the forge read.
        client.page_gate = asyncio.Event()
        client.page_reads.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: bool(client.gated_reads))

        # The unknown outcome re-reads the first page, so the blocked read
        # stops first instead of leaving its bookkeeping half done.
        client.page_reads.clear()
        inbox.invalidate(client.summaries[0], ListsChange.MAYBE_CLOSED)
        await wait_until(pilot, lambda: len(client.page_reads) == 1)
        await settle(app)
        assert client.page_reads == [("acme/big", 1, False)]
        assert numbers(table) == [1, 2]
        assert inbox.check_action("load_more", ()) is True

        # With the gate open, the next press reads the pending page.
        client.page_gate.set()
        client.page_reads.clear()
        await pilot.press("m")
        await wait_until(pilot, lambda: numbers(table) == [1, 2, 3, 4])
        assert client.page_reads == [("acme/big", 2, False)]
