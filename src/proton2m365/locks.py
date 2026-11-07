"""Exclusive, non-blocking file locks that work on macOS, Linux and Windows, with the holder's PID inside.

POSIX uses flock on the whole file (advisory, so the PID stays readable). Windows' msvcrt.locking is a
mandatory byte-range lock, so it locks one byte far past the PID: another process can still read the PID
at offset 0 to say who holds the lock. The OS releases either lock when the holder exits, even on a crash.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS_LOCK_OFFSET = 1 << 30  # beyond any PID text; Windows allows locking past end of file


class LockHeld(Exception):
    def __init__(self, path: Path, holder: str):
        super().__init__(f"{path} is held by pid {holder or 'unknown'}")
        self.path = path
        self.holder = holder


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    def acquire(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError:
            holder = _read_pid(fd)
            os.close(fd)
            raise LockHeld(self.path, holder) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            if sys.platform == "win32":
                _unlock_windows(self.fd)
            os.close(self.fd)  # on POSIX closing releases the flock
            self.fd = None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _read_pid(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 64).decode(errors="replace").strip()


if sys.platform == "win32":  # pragma: no cover - exercised by the windows-latest CI job
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_windows(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_windows(fd: int) -> None:  # never called off Windows
        raise AssertionError
