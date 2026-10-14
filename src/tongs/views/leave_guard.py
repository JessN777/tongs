"""The protocol for screens the app must ask before popping through them."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LeaveGuard(Protocol):
    """A screen that can refuse to be popped through.

    Screens that would lose work when popped away, like an MR detail with a
    submission still running or a dialog with unsaved text, implement
    ``prepare_leave``. It returns False to keep the app where it is, after
    explaining why with a notification. The app asks every screen above its
    navigation target, topmost first, so a screen that would refuse is never
    popped through and no screen below it runs its leave side effects.
    """

    def prepare_leave(self) -> bool: ...
