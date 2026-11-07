"""Docs that must not drift from the code."""

from __future__ import annotations

import re
from pathlib import Path

from proton2m365 import status

ROOT = Path(__file__).resolve().parents[1]


def test_every_error_code_has_an_entry_in_docs_errors():
    emitted = set()
    for path in (ROOT / "src" / "proton2m365").glob("*.py"):
        emitted |= set(re.findall(r"P2M-[A-Z0-9]+(?:-[A-Z0-9]+)*", path.read_text()))
    documented = set(re.findall(r"^## (P2M-[A-Z0-9-]+)$", (ROOT / "docs" / "errors.md").read_text(), re.M))
    assert emitted, "no error codes found"
    assert emitted - documented == set(), "codes without a docs/errors.md entry"
    assert documented - emitted == set(), "docs/errors.md lists codes the code never emits"


def test_readme_exit_code_table_matches_the_code():
    rows = dict(re.findall(r"^\| (\d+) \| (.+?) \|$", (ROOT / "README.md").read_text(), re.M))
    assert sorted(int(c) for c in rows) == sorted(code for code, _ in status.EXIT_CODES)


def test_readme_try_it_route_is_the_sample_route():
    readme = (ROOT / "README.md").read_text()
    for line in ("proton2m365 sample-export ./sample", "proton2m365 scan ./sample", "proton2m365 plan --dry-run ./sample"):
        assert line in readme
