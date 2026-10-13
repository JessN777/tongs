"""MR table widget and helper functions."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import replace as replace_summary
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist, RowKey

from tongs.forges.models import CIStatus, MRSummary
from tongs.helpers import ci_icon, forge_label, relative_time

MR_SORT_KEYS = ("updated", "title", "ci", "author")

# Shown in the CI column while a row's CI lookup is still running. It differs
# from every CI status icon, including the forge's own pending state.
CI_LOADING_MARKUP = "[dim]…[/]"
CI_LOADING_ASCII = "[dim]..[/]"


def ci_loading_icon(ascii_mode: bool = False) -> str:
    """Return the CI cell markup for a row whose CI is still being looked up."""
    return CI_LOADING_ASCII if ascii_mode else CI_LOADING_MARKUP


_CI_PRIORITY = {
    CIStatus.FAILED: 0,
    CIStatus.RUNNING: 1,
    CIStatus.PENDING: 2,
    CIStatus.SUCCESS: 3,
    CIStatus.CANCELED: 4,
    CIStatus.SKIPPED: 5,
    CIStatus.UNKNOWN: 6,
}


def sort_mrs(
    mrs: list[MRSummary], key: str, pending: Collection[str] = ()
) -> list[MRSummary]:
    """Sort merge requests by ``key``.

    ``pending`` holds the row keys whose CI is still being looked up; the CI
    sort puts them after every row with a known status.
    """
    if key == "title":
        return sorted(mrs, key=lambda m: m.title.lower())
    if key == "ci":
        return sorted(
            mrs,
            key=lambda m: (
                mr_row_key(m) in pending,
                _CI_PRIORITY.get(m.ci_status, 9),
                m.title.lower(),
            ),
        )
    if key == "author":
        return sorted(mrs, key=lambda m: (m.author.username.lower(), m.title.lower()))
    return sorted(
        mrs, key=lambda m: m.updated_at or m.created_at or m.updated_at, reverse=True
    )


def mr_row_key(mr: MRSummary) -> str:
    """Return the table row key of a merge request."""
    return f"{mr.forge_host.hostname}:{mr.repo_path}:{mr.number}"


class MRTable(DataTable):
    """MR list table with consistent columns."""

    BINDINGS: ClassVar[list] = [
        Binding("s", "cycle_sort", "Sort", show=True, key_display="s"),
    ]

    class ViewportChanged(Message):
        """The rows in view may have changed: the table scrolled or resized."""

        def __init__(self, table: MRTable) -> None:
            super().__init__()
            self.table = table

        @property
        def control(self) -> MRTable:
            return self.table

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}
        self._sort_key: str = "updated"
        # Rows whose CI cell shows the loading marker until a lookup lands.
        self._ci_pending: set[str] = set()

    _show_repo: bool = True

    @property
    def sort_key(self) -> str:
        """Return the active sort, one of ``MR_SORT_KEYS``."""
        return self._sort_key

    def on_mount(self) -> None:
        self.watch(self, "scroll_y", self._scrolled, init=False)

    def on_resize(self, _event: events.Resize) -> None:
        self.post_message(self.ViewportChanged(self))

    def _scrolled(self, old: float, new: float) -> None:
        if int(old) != int(new):
            self.post_message(self.ViewportChanged(self))

    def keys_near_viewport(self) -> list[str] | None:
        """Return the row keys in view plus one screen above and below.

        Keys come in table order. None means the table is not laid out or not
        shown, so no row is in view.
        """
        height = self.scrollable_content_region.height
        if self.show_header:
            height -= self.header_height
        if height <= 0 or not self.display:
            return None
        top = int(self.scroll_y)
        first = max(0, top - height)
        rows = self.ordered_rows[first : top + 2 * height]
        return [row.key.value for row in rows if row.key.value is not None]

    def row_keys(self) -> list[str]:
        """Return every row key in table order."""
        return [row.key.value for row in self.ordered_rows if row.key.value]

    def selected_row_key(self) -> str | None:
        """Return the row key under the cursor, if any."""
        return self._selected_row_key()

    def is_key_ci_pending(self, key: str) -> bool:
        """Return whether a row still waits for its CI lookup."""
        return key in self._ci_pending and key in self._mr_data

    def setup_columns(self, show_repo: bool = True) -> None:
        self._show_repo = show_repo
        self.add_column("Forge", key="forge", width=5)
        self.add_column("CI", key="ci", width=4)
        self.add_column("#", key="number", width=6)
        self.add_column("Title", key="title")
        self.add_column("Author", key="author", width=14)
        if show_repo:
            self.add_column("Repo", key="repo", width=35)
        self.add_column("Updated", key="updated", width=8)

    def add_mr_row(
        self, mr: MRSummary, ascii_mode: bool = False, *, ci_pending: bool = False
    ) -> None:
        """Add one row; ``ci_pending`` shows the CI loading marker for it."""
        # Forge text is wrapped in Text so DataTable never parses it as markup:
        # a title such as "Fix [/] x" would raise MarkupError, and "[docs] y"
        # would silently lose its bracketed word. Only our own fixed labels
        # (forge and CI icons) go through the markup parser.
        title = Text()
        if mr.is_draft:
            title.append("D ", style="dim")
        else:
            title.append("  ")
        title.append(mr.title)
        row_key = mr_row_key(mr)
        self._mr_data[row_key] = mr
        if ci_pending:
            self._ci_pending.add(row_key)
        else:
            self._ci_pending.discard(row_key)
        ci_cell = ci_loading_icon(ascii_mode) if ci_pending else None
        row: list[Text] = [
            Text.from_markup(forge_label(mr.forge_host.forge_type)),
            Text.from_markup(ci_cell or ci_icon(mr.ci_status, ascii_mode)),
            Text(str(mr.number)),
            title,
            Text(mr.author.username),
        ]
        if self._show_repo:
            row.append(Text(mr.repo_path))
        row.append(Text(relative_time(mr.updated_at)))
        self.add_row(*row, key=row_key)

    def merge_mrs(
        self,
        mrs: Iterable[MRSummary],
        ascii_mode: bool = False,
        *,
        replace: bool = False,
        ci_pending: bool = False,
    ) -> None:
        """Add rows in the active sort order, keeping the cursor on its MR.

        ``replace`` drops the current rows first. Otherwise only the incoming
        rows are built and the existing rows are moved into place, so adding
        one repository at a time stays cheap however many rows are shown. The
        highlighted MR keeps the cursor when it is still listed, except on the
        first row: a cursor at the top stays at the top, so newer rows that
        arrive while a list streams in do not push it down the list.
        ``ci_pending`` marks incoming rows with an unknown CI status as still
        loading, for ``set_ci_status`` to fill in.
        """
        selected = None if self.cursor_row == 0 else self._selected_row_key()
        incoming = {mr_row_key(mr): mr for mr in mrs}
        if replace:
            self.clear()
        elif not incoming:
            return
        for key in incoming:
            if key in self._mr_data:
                self.remove_row(key)
        for mr in incoming.values():
            self.add_mr_row(
                mr,
                ascii_mode,
                ci_pending=ci_pending and mr.ci_status is CIStatus.UNKNOWN,
            )
        self._order_rows(self._sorted())
        if selected is None:
            if self.row_count:
                self.move_cursor(row=0)
        else:
            self._keep_cursor_on(selected)

    def remove_mr(self, mr: MRSummary) -> bool:
        """Drop one MR's row in place, keeping the cursor on a nearby row.

        Returns whether the MR was listed. The row that takes the removed
        one's place gets the cursor, or the last row when it was last, so a
        merged or closed MR leaving the list keeps the user's place.
        """
        key = mr_row_key(mr)
        if key not in self._mr_data:
            return False
        held = self._selected_row_key()
        index = self.get_row_index(key)
        self._mr_data.pop(key)
        self._ci_pending.discard(key)
        self.remove_row(key)
        if held is not None and held in self._mr_data:
            self._keep_cursor_on(held)
        elif self.row_count:
            self.move_cursor(row=min(index, self.row_count - 1))
        return True

    def _sorted(self) -> list[MRSummary]:
        return sort_mrs(list(self._mr_data.values()), self._sort_key, self._ci_pending)

    def get_mr(self, key: str) -> MRSummary | None:
        """Return the merge request a row key shows, if it is listed."""
        return self._mr_data.get(key)

    def is_ci_pending(self, mr: MRSummary) -> bool:
        """Return whether this row still shows the CI loading marker."""
        key = mr_row_key(mr)
        return key in self._ci_pending and self._mr_data.get(key) == mr

    def pending_ci_mrs(self) -> list[MRSummary]:
        """Return the rows still waiting for CI, in table order."""
        pending: list[MRSummary] = []
        for row in self.ordered_rows:
            key = row.key.value
            if key in self._ci_pending and key in self._mr_data:
                pending.append(self._mr_data[key])
        return pending

    def set_ci_status(
        self, mr: MRSummary, status: CIStatus, ascii_mode: bool = False
    ) -> bool:
        """Fill in a pending row's CI in place and return whether it landed.

        The result lands only on the row it was looked up for: a row that was
        removed, replaced by a newer summary, or already filled is left alone.
        With the CI sort active the row moves into place and the cursor stays
        on its MR.
        """
        if not self.is_ci_pending(mr):
            return False
        key = mr_row_key(mr)
        self._ci_pending.discard(key)
        self._mr_data[key] = replace_summary(mr, ci_status=status)
        self.update_cell(
            key, "ci", Text.from_markup(ci_icon(status, ascii_mode)), update_width=False
        )
        if self._sort_key == "ci":
            selected = self._selected_row_key()
            self._order_rows(self._sorted())
            self._keep_cursor_on(selected)
        return True

    def _keep_cursor_on(self, selected: str | None) -> None:
        if selected is not None and selected in self._mr_data:
            index = self.get_row_index(selected)
            if index != self.cursor_row:
                self.move_cursor(row=index)

    def _order_rows(self, ordered: list[MRSummary]) -> None:
        """Move the existing rows into ``ordered`` without rebuilding them.

        This updates the row locations the way ``DataTable.sort`` does. Only
        the rows whose position changes are touched, and the positions they
        leave are exactly the ones they fill, so the mapping stays one to one.
        """
        locations = self._row_locations
        moved = False
        for index, mr in enumerate(ordered):
            key = mr_row_key(mr)
            if locations.get(key) != index:
                locations[RowKey(key)] = index
                moved = True
        if moved:
            self._update_count += 1
            self.refresh()

    def _selected_row_key(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except CellDoesNotExist:
            return None
        return row_key.value

    def get_selected_mr(self) -> MRSummary | None:
        if self.cursor_row is None or self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return self._mr_data.get(row_key.value)
        except CellDoesNotExist:
            return None

    def action_cycle_sort(self) -> None:
        idx = (
            MR_SORT_KEYS.index(self._sort_key) if self._sort_key in MR_SORT_KEYS else -1
        )
        self._sort_key = MR_SORT_KEYS[(idx + 1) % len(MR_SORT_KEYS)]
        self._rerender_sorted()
        self.app.notify(f"Sort: {self._sort_key}", timeout=2)

    def _rerender_sorted(self) -> None:
        sorted_mrs = self._sorted()
        ascii_mode = getattr(self.app, "config", None) and self.app.config.ascii_mode
        super().clear()
        for mr in sorted_mrs:
            self.add_mr_row(
                mr,
                ascii_mode=bool(ascii_mode),
                ci_pending=mr_row_key(mr) in self._ci_pending,
            )

    def clear(self, *args, **kwargs) -> None:
        self._mr_data.clear()
        self._ci_pending.clear()
        super().clear(*args, **kwargs)
