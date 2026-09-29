"""Deterministic single-run exclusion (DL-XB-199 G3-101).

The lock is an OS byte-range lock on a fixed file in the private state
directory, taken non-blocking before the state database is opened. The kernel
owns the lock, not the file: when the holding process exits or dies, Windows
releases it, so there is no stale-lock state and nothing to expire or clean
up. The file itself is inert and may persist between runs; its contents are
never read and carry nothing private.

A second run -- a manual run overlapping the scheduled one, or a scheduled
start while a manual run is in progress -- fails fast with RUN_IN_PROGRESS and
touches no state, archive, temp directory or source.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import RunLockedError, StateError


LOCK_FILENAME = "energygrid.run.lock"


class RunLock:
    def __init__(self, directory: Path) -> None:
        self.path = directory / LOCK_FILENAME
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StateError("single-run lock file could not be opened") from exc
        try:
            _lock(fd)
        except BlockingIOError:
            os.close(fd)
            raise RunLockedError() from None
        except OSError as exc:
            os.close(fd)
            if _is_contention(exc):
                raise RunLockedError() from None
            raise StateError("single-run lock could not be taken") from exc
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        except OSError:
            # Closing the descriptor releases the lock regardless.
            pass
        finally:
            os.close(fd)


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _is_contention(exc: OSError) -> bool:
        # msvcrt reports a held region as EACCES / EDEADLOCK (errno 13 / 36).
        return exc.errno in {13, 36}

else:  # pragma: no cover - the production host is Windows
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _is_contention(exc: OSError) -> bool:
        return exc.errno in {11, 35}
