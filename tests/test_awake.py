from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from proton2m365 import awake


def which_all(name):
    return f"/usr/bin/{name}"


def which_none(name):
    return None


def test_hold_command_is_tied_to_this_process():
    assert awake.hold_command(42, "darwin", which_all) == ["caffeinate", "-i", "-w", "42"]
    linux = awake.hold_command(42, "linux", which_all)
    assert linux[0] == "systemd-inhibit" and "--pid=42" in linux and "--what=idle:sleep" in linux
    assert awake.hold_command(42, "linux", which_none) is None


def test_missing_tool_warns_once_and_the_run_continues(monkeypatch):
    monkeypatch.delenv(awake.ENV_ALLOW_SLEEP, raising=False)
    said = []
    with awake.KeepAwake(platform="linux", which=which_none, warn=said.append):
        pass
    assert len(said) == 1 and "run the same command again" in said[0]


def test_helper_is_started_and_stopped(monkeypatch):
    monkeypatch.delenv(awake.ENV_ALLOW_SLEEP, raising=False)
    started = []

    class Helper:
        stopped = False

        def terminate(self):
            Helper.stopped = True

        def wait(self, timeout):
            return 0

    def popen(command, **kwargs):
        started.append(command)
        return Helper()

    with awake.KeepAwake(platform="darwin", which=which_all, popen=popen):
        assert started == [["caffeinate", "-i", "-w", str(os.getpid())]]
    assert Helper.stopped


def test_allow_sleep_starts_nothing(monkeypatch):
    started = []
    with awake.KeepAwake(enabled=False, platform="darwin", which=which_all, popen=lambda c, **k: started.append(c)):
        pass
    monkeypatch.setenv(awake.ENV_ALLOW_SLEEP, "1")
    with awake.KeepAwake(platform="darwin", which=which_all, popen=lambda c, **k: started.append(c)):
        pass
    assert started == []


@pytest.mark.parametrize(("platform", "env", "expected"), [
    ("darwin", {}, True),
    ("darwin", {"SSH_CONNECTION": "1 2 3 4"}, False),
    ("linux", {}, False),
    ("linux", {"DISPLAY": ":0"}, True),
    ("win32", {}, True),
    ("darwin", {"PROTON2M365_NO_NOTIFY": "1"}, False),
])
def test_can_notify(platform, env, expected, monkeypatch):
    monkeypatch.delenv(awake.ENV_NO_NOTIFY, raising=False)
    assert awake.can_notify(platform, env) is expected


def test_notification_text_never_enters_the_script():
    title, text = 'a"; do shell script "rm -rf ~', "x$(whoami)"
    for platform in ("darwin", "win32"):
        command, env = awake.notify_command(title, text, platform, which_all)
        assert title not in " ".join(command) and text not in " ".join(command)
        assert env == {"P2M_TITLE": title, "P2M_TEXT": text}


def test_notify_never_raises(monkeypatch):
    monkeypatch.delenv(awake.ENV_NO_NOTIFY, raising=False)

    def boom(*a, **k):
        raise OSError("no")

    assert awake.notify("t", "x", platform="darwin", run=boom, which=which_all, env={}) is False


@pytest.mark.skipif(sys.platform != "darwin" or not awake.hold_command(1), reason="needs macOS caffeinate")
def test_caffeinate_ends_with_the_process_that_was_killed(tmp_path, monkeypatch):
    monkeypatch.delenv(awake.ENV_ALLOW_SLEEP, raising=False)
    script = ("import os, subprocess, time\n"
              "from proton2m365 import awake\n"
              "k = awake.KeepAwake(warn=lambda m: None)\n"
              "k.__enter__()\n"
              "print(k.helper.pid, flush=True)\n"
              "time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    helper_pid = int(child.stdout.readline())
    child.kill()                                       # SIGKILL: no cleanup runs
    child.wait()
    for _ in range(50):
        try:
            os.kill(helper_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("caffeinate outlived the killed process")
