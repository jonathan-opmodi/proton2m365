"""The status line long-running commands print while they work (--progress=auto|plain|none).

auto:  on a terminal, one line updated in place; otherwise like plain.
plain: ordinary lines, no cursor movement (for logs and pipes).
none:  no status lines at all (messages that matter are still printed).
Status lines go to stderr, so stdout stays clean for --json and for piping the results.
"""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Callable
from typing import TextIO

MODES = ("auto", "plain", "none")


class StatusLine:
    def __init__(self, mode: str = "auto", stream: TextIO | None = None, *,
                 clock: Callable[[], float] = time.monotonic, min_interval: float = 0.5, plain_interval: float = 10):
        self.stream = stream or sys.stderr
        if mode == "auto":
            mode = "inplace" if getattr(self.stream, "isatty", lambda: False)() else "plain"
        self.mode = mode
        self.clock = clock
        self.min_interval = min_interval     # in place: redraw at most this often
        self.plain_interval = plain_interval  # plain: a line at least this far apart (and at each stage change)
        self._shown = ""                     # what the in-place line shows now
        self._last = float("-inf")

    def update(self, text: str, *, force: bool = False) -> None:
        """Show progress. In place it replaces the previous status; plain prints a line."""
        if self.mode == "none":
            return
        now = self.clock()
        interval = self.plain_interval if self.mode == "plain" else self.min_interval
        if not force and now - self._last < interval:
            return
        self._last = now
        if self.mode == "plain":
            print(text, file=self.stream, flush=True)
            return
        width = max(20, shutil.get_terminal_size((100, 20)).columns - 1)
        line = text if len(text) <= width else text[: width - 3] + "..."
        self.stream.write("\r" + line.ljust(len(self._shown)))  # pad over a longer previous line
        self.stream.flush()
        self._shown = line

    def say(self, text: str, file: TextIO | None = None) -> None:
        """A message that stays: clears the in-place status first so the two never mix."""
        self.clear()
        print(text, file=file or sys.stdout, flush=True)

    def clear(self) -> None:
        if self.mode == "inplace" and self._shown:
            self.stream.write("\r" + " " * len(self._shown) + "\r")
            self.stream.flush()
            self._shown = ""
