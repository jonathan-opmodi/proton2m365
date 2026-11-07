from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from proton2m365.locks import FileLock, LockHeld


def hold_in_child(path):
    child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import time
        from proton2m365.locks import FileLock
        with FileLock({str(path)!r}):
            print("locked", flush=True)
            time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "locked"
    return child


def test_a_second_process_is_refused_and_told_the_holder(tmp_path):
    path = tmp_path / "locks" / "x.lock"
    child = hold_in_child(path)
    try:
        with pytest.raises(LockHeld) as exc:
            FileLock(path).acquire()
        assert exc.value.holder == str(child.pid)
    finally:
        child.kill()
        child.wait()
    with FileLock(path):  # the OS released it when the holder died
        assert path.read_text() == str(__import__("os").getpid())


def test_release_lets_the_next_holder_in(tmp_path):
    path = tmp_path / "x.lock"
    first = FileLock(path).acquire()
    first.release()
    with FileLock(path):
        pass
