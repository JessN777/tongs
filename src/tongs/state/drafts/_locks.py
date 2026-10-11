"""Process-lifetime ownership locks for draft submission attempts.

Each attempt is owned through an advisory lock on ``<attempt-id>.lock`` in a
private directory. The file carries no state, so it is deleted when its owner
releases it and leftovers from a crashed process are deleted on the next
store open. Deleting a lock file must never let two processes own one
attempt, so the rules are:

- A file is unlinked only by a process that holds its lock, before it unlocks.
- After ``flock`` succeeds, the owner re-checks that the descriptor still is
  the one linked file at the path. A process that opened the path before the
  holder unlinked it can lock the deleted inode once the holder unlocks, while
  a third process creates and locks a fresh file at the same path; the
  re-check rejects the deleted inode, so only the fresh file grants ownership.

Windows cannot unlink a file that another process has open, so deletion there
is best effort after the lock is released, and cleanup on open removes what
is left.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from enum import Enum
from pathlib import Path
from uuid import UUID

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from tongs.state.drafts.errors import DraftPermissionError, DraftStoreError

_LOCK_SUFFIX = ".lock"
# A lock file younger than this may belong to a submission that has created it
# but not yet locked it, so cleanup on open leaves it alone. Files left by a
# crashed process are older.
_STALE_LOCK_MIN_AGE_SECONDS = 60.0
# One retry covers a holder that unlinked the file between this process's
# open and its lock. A second stale descriptor reports the attempt as held,
# which every caller already handles as a transient refusal.
_ACQUIRE_TRIES = 2


class _Claim(Enum):
    """Why an open lock file descriptor did not become an owned lock."""

    HELD = "held"
    STALE = "stale"


def _check_private_directory(directory: Path) -> None:
    directory_info = directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode):
        raise DraftPermissionError("draft lock path must be a private directory")
    if os.name != "nt" and stat.S_IMODE(directory_info.st_mode) & 0o077:
        raise DraftPermissionError("draft lock directory permits access by other users")


def _open_lock_file(path: Path) -> int:
    """Open or create a private lock file without following a symlink."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        # A link count of zero is a file its holder has just unlinked, which
        # the check after locking rejects as stale; only extra links are a
        # policy violation.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise DraftPermissionError(
                "draft attempt lock must be a private regular file"
            )
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise DraftPermissionError(
                "draft attempt lock permits access by other users"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _names_path(path: Path, fd: int) -> bool:
    """Return whether ``fd`` is still the only link at ``path``."""
    info = os.fstat(fd)
    if info.st_nlink != 1:
        return False
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def _is_attempt_lock_name(name: str) -> bool:
    if not name.endswith(_LOCK_SUFFIX):
        return False
    stem = name[: -len(_LOCK_SUFFIX)]
    try:
        return str(UUID(stem)) == stem
    except ValueError:
        return False


class AttemptLock:
    """An advisory lock released by the OS when its process exits."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd = fd

    @classmethod
    def try_acquire(cls, directory: Path, attempt_id: str) -> AttemptLock | None:
        """Own an attempt's lock file, or return None when another holder has it."""
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _check_private_directory(directory)
            path = directory / f"{attempt_id}{_LOCK_SUFFIX}"
            for _ in range(_ACQUIRE_TRIES):
                claim = cls._claim(path, _open_lock_file(path))
                if isinstance(claim, AttemptLock):
                    return claim
                if claim is _Claim.HELD:
                    return None
            return None
        except DraftStoreError:
            raise
        except OSError as error:
            raise DraftPermissionError(
                "cannot create private draft submission lock"
            ) from error

    @classmethod
    def _claim(cls, path: Path, fd: int) -> AttemptLock | _Claim:
        """Lock an open descriptor of ``path``, taking ownership of ``fd``."""
        try:
            if os.name != "nt":
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return _Claim.HELD
                if not _names_path(path, fd):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return _Claim.STALE
            else:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    return _Claim.HELD
            lock = cls(path, fd)
            fd = -1
            return lock
        finally:
            if fd >= 0:
                os.close(fd)

    @classmethod
    def remove_stale(cls, directory: Path, *, keep: frozenset[str]) -> int:
        """Delete attempt lock files that no live process holds.

        Only regular ``<uuid>.lock`` files in the private lock directory are
        candidates, and each one is deleted only after its lock is taken
        without blocking. A file another process holds, a file that fails
        the private-file checks, a file modified in the last
        ``_STALE_LOCK_MIN_AGE_SECONDS`` (another process may be about to lock
        it), and every name in ``keep`` are left alone.
        Cleanup is housekeeping, so filesystem errors skip the file instead
        of raising. Returns how many lock files were released for deletion.
        """
        cutoff = time.time() - _STALE_LOCK_MIN_AGE_SECONDS
        try:
            _check_private_directory(directory)
            with os.scandir(directory) as entries:
                names = [
                    entry.name
                    for entry in entries
                    if _is_attempt_lock_name(entry.name)
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ]
        except (DraftStoreError, OSError):
            return 0
        removed = 0
        for name in names:
            attempt_id = name[: -len(_LOCK_SUFFIX)]
            if attempt_id in keep:
                continue
            try:
                lock = cls.try_acquire(directory, attempt_id)
            except DraftStoreError:
                continue
            if lock is None:
                continue
            with contextlib.suppress(OSError):
                lock.release()
            removed += 1
        return removed

    def release(self) -> None:
        """Delete the lock file while still holding it, then unlock and close."""
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            if os.name != "nt":
                self._unlink_held(fd)
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
        if os.name == "nt":
            # Fails while another process has the file open, which keeps its
            # lock valid; cleanup on the next open deletes the file instead.
            with contextlib.suppress(OSError):
                os.unlink(self.path)

    def _unlink_held(self, fd: int) -> None:
        """Unlink the path only while it still names the locked descriptor."""
        try:
            if _names_path(self.path, fd):
                os.unlink(self.path)
        except OSError:
            # Best effort: a file left behind is removed on the next open.
            pass
