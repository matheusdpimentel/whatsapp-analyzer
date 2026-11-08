"""Single-writer lock for a workspace.

Commands that write (`import --workspace`, `metrics run`) hold an exclusive lock on
`analyzer.lock`. The operating system releases it when the process ends, even after a crash, so
a writer that finds a run still marked `running` knows that run was interrupted.
"""

import os
import sys
from pathlib import Path
from types import TracebackType


class WorkspaceBusyError(Exception):
    pass


class WorkspaceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "WorkspaceLock":
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise WorkspaceBusyError from exc
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._descriptor is not None:
            _unlock(self._descriptor)
            os.close(self._descriptor)
            self._descriptor = None


if sys.platform == "win32":
    import msvcrt

    def _lock(descriptor: int) -> None:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def _unlock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
