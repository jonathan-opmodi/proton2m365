"""Keep the computer awake while a long command runs, and say when it is done.

Uploading a large export takes hours; a laptop that falls asleep halfway stalls it. While a long command
runs, KeepAwake holds off idle sleep with the operating system's own tool, tied to this process so the hold
ends even if proton2m365 is killed:

    macOS    caffeinate -i -w <pid>              (exits with the process)
    Linux    systemd-inhibit ... tail --pid=<pid> (exits with the process), when systemd-inhibit exists
    Windows  SetThreadExecutionState from the main thread for the run (Windows drops it with the process)

Closing a laptop lid still sleeps the computer; only idle sleep is held off. --allow-sleep (or
PROTON2M365_ALLOW_SLEEP=1) turns this off.

notify() shows one desktop notification when a long command ends, where there is a desktop to show it on:
not over SSH and not without a display. --no-notify (or PROTON2M365_NO_NOTIFY=1) turns it off. Failing to
notify never changes a command's result.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable

ENV_ALLOW_SLEEP = "PROTON2M365_ALLOW_SLEEP"
ENV_NO_NOTIFY = "PROTON2M365_NO_NOTIFY"

_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def hold_command(pid: int, platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which):
    """The helper command that holds off idle sleep until `pid` exits, or None when there is none."""
    if platform == "darwin" and which("caffeinate"):
        return ["caffeinate", "-i", "-w", str(pid)]
    if platform.startswith("linux") and which("systemd-inhibit") and which("tail"):
        return ["systemd-inhibit", "--what=idle:sleep", "--who=proton2m365", "--why=migrating mail",
                "--mode=block", "tail", f"--pid={pid}", "-f", "/dev/null"]
    return None


class KeepAwake:
    """Context manager: hold off idle sleep while the block runs (see the module docstring)."""

    def __init__(self, *, enabled: bool = True, warn: Callable[[str], None] | None = None,
                 platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which,
                 popen: Callable = subprocess.Popen):
        self.enabled = enabled and not _env_on(ENV_ALLOW_SLEEP)
        self.warn = warn or (lambda msg: print(msg, file=sys.stderr))
        self.platform, self.which, self.popen = platform, which, popen
        self.helper = None
        self.windows = False

    def __enter__(self) -> KeepAwake:
        if not self.enabled:
            return self
        if self.platform == "win32":
            try:
                import ctypes

                self.windows = bool(ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
            except (AttributeError, OSError):
                self.windows = False
            if not self.windows:
                self.warn("Could not keep the computer awake; if it sleeps, run the same command again to resume.")
            return self
        command = hold_command(os.getpid(), self.platform, self.which)
        if command is None:
            self.warn("Could not keep the computer awake (no caffeinate or systemd-inhibit); if it sleeps, run the "
                      "same command again to resume.")
            return self
        try:
            self.helper = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        except OSError:
            self.warn("Could not keep the computer awake; if it sleeps, run the same command again to resume.")
        return self

    def __exit__(self, *exc) -> None:
        if self.helper is not None:
            try:
                self.helper.terminate()
                self.helper.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.helper = None
        if self.windows:
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
            self.windows = False


def can_notify(platform: str = sys.platform, env: dict | None = None) -> bool:
    """A desktop to notify on: not over SSH, and on Linux a display."""
    env = os.environ if env is None else env
    no_notify = str(env.get(ENV_NO_NOTIFY, "")).strip().lower() in ("1", "true", "yes", "on")
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or no_notify:
        return False
    if platform.startswith("linux"):
        return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    return platform in ("darwin", "win32")


_WINDOWS_TOAST = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]"
    " > $null; $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); $x = $t.GetElementsByTagName('text');"
    " $x.Item(0).AppendChild($t.CreateTextNode($env:P2M_TITLE)) > $null;"
    " $x.Item(1).AppendChild($t.CreateTextNode($env:P2M_TEXT)) > $null;"
    " [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)


def notify_command(title: str, text: str, platform: str = sys.platform,
                   which: Callable[[str], str | None] = shutil.which) -> tuple[list[str], dict] | None:
    """(command, extra environment) that shows the notification, or None. Text travels in the environment
    (never inside the script), so a folder or message name cannot change what runs."""
    env = {"P2M_TITLE": title, "P2M_TEXT": text}
    if platform == "darwin" and which("osascript"):
        script = ('display notification (system attribute "P2M_TEXT") '
                  'with title (system attribute "P2M_TITLE")')
        return ["osascript", "-e", script], env
    if platform.startswith("linux") and which("notify-send"):
        return ["notify-send", "--app-name=proton2m365", title, text], {}
    if platform == "win32" and which("powershell"):
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST], env
    return None


def notify(title: str, text: str, *, enabled: bool = True, platform: str = sys.platform,
           run: Callable = subprocess.run, which: Callable[[str], str | None] = shutil.which,
           env: dict | None = None) -> bool:
    """Show one desktop notification; True if one was shown. Never raises."""
    if not enabled or not can_notify(platform, env):
        return False
    found = notify_command(title, text, platform, which)
    if found is None:
        return False
    command, extra = found
    try:
        run(command, env={**os.environ, **extra}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
