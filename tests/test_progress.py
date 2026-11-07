from __future__ import annotations

import io

from proton2m365.progress import StatusLine


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_auto_is_in_place_on_a_terminal_and_plain_when_piped():
    assert StatusLine("auto", Tty()).mode == "inplace"
    assert StatusLine("auto", io.StringIO()).mode == "plain"


def test_in_place_redraws_one_line_and_pads_over_a_longer_one():
    out, clock = Tty(), Clock()
    s = StatusLine("auto", out, clock=clock)
    s.update("upload 1/10, a long status line")
    clock.t = 1
    s.update("upload 2/10")
    assert out.getvalue() == "\rupload 1/10, a long status line" + "\r" + "upload 2/10".ljust(31)
    assert "\n" not in out.getvalue()


def test_in_place_throttles_but_stage_changes_always_show():
    out, clock = Tty(), Clock()
    s = StatusLine("inplace", out, clock=clock)
    s.update("a")
    s.update("b")                       # within 0.5 s: skipped
    s.update("c", force=True)           # a stage change: shown
    assert out.getvalue().count("\r") == 2 and out.getvalue().endswith("c")


def test_plain_prints_lines_at_most_every_10_seconds():
    out, clock = io.StringIO(), Clock()
    s = StatusLine("plain", out, clock=clock)
    for second in range(25):
        clock.t = second
        s.update(f"at {second}")
    assert out.getvalue().splitlines() == ["at 0", "at 10", "at 20"]


def test_none_prints_nothing():
    out = io.StringIO()
    StatusLine("none", out).update("x", force=True)
    assert out.getvalue() == ""


def test_say_clears_the_status_line_first(capsys):
    out = Tty()
    s = StatusLine("inplace", out)
    s.update("upload 3/10")
    s.say("Created folder Projects")
    assert out.getvalue().endswith("\r" + " " * len("upload 3/10") + "\r")
    assert capsys.readouterr().out == "Created folder Projects\n"
