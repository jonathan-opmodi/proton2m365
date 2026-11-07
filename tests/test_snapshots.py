"""Golden output of the commands a new user runs first, on the synthetic sample export.

The output is the interface people read, so a change to it should be deliberate: run
`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py` and review the diff of tests/snapshots/.
One helper normalises what differs between machines (paths, versions, terminal width).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from proton2m365 import cli

SNAPSHOTS = Path(__file__).parent / "snapshots"


def normalise(text: str, tmp: Path) -> str:
    text = text.replace(str(tmp.resolve()), "<TMP>").replace(str(tmp), "<TMP>")
    text = re.sub(r"proton2m365 \d[\w.+-]*", "proton2m365 <VERSION>", text)
    text = re.sub(r"Python \d+\.\d+\.\d+", "Python <X.Y.Z>", text)
    text = re.sub(r"^  (Darwin|Linux|Windows) .*$", "  <OS>", text, flags=re.M)
    text = re.sub(r"<(path|address|label|tenant) [0-9a-f]{8}>", r"<\1 HASH>", text)
    return text.replace("\\", "/")


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    assert cli.main(["sample-export", "sample"]) == 0
    return tmp_path


def check(name: str, text: str) -> None:
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(text)
    assert path.exists(), f"no snapshot {path.name}; run with UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), f"{name} output changed; review it and run with UPDATE_SNAPSHOTS=1"


@pytest.mark.parametrize(("name", "argv"), [
    ("sample-export", None),
    ("scan", ["scan", "sample"]),
    ("plan-dry-run", ["plan", "--dry-run", "sample"]),
    ("plan", ["plan", "sample"]),
    ("replan-exclude", ["plan", "sample", "--exclude-label", "Work"]),
    ("status", ["status"]),
    ("doctor", ["doctor"]),
])
def test_output(sample, capsys, name, argv):
    if argv is None:
        out = capsys.readouterr().out          # what sample-export printed in the fixture
    else:
        if name in ("replan-exclude", "status", "doctor"):
            cli.main(["plan", "sample"])
        capsys.readouterr()
        cli.main(argv)
        out = capsys.readouterr().out
    check(name, normalise(out, sample))


def test_narrow_terminal_stacks_tables(sample, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    cli.main(["plan", "--dry-run", "sample"])
    out = capsys.readouterr().out
    assert "  Archive: 10" in out and "  Projects/Alpha: 8" in out and "  Work: 6" in out
    assert all(len(line) <= 120 for line in out.splitlines())


def test_long_folder_names_are_never_cut(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    name = "Clients/" + "A very long project folder name that a user may copy"
    assert cli._row(name, "      12") == [f"    {name}", "    " + " " * cli.NAME_WIDTH + "       12"]
