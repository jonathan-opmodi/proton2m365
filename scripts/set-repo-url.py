#!/usr/bin/env python3
"""Point every link at the real GitHub repository once it exists (standard library only).

    python3 scripts/set-repo-url.py <owner>/<repo>

Replaces the placeholder `proton2m365/proton2m365` in pyproject.toml, the issues link the tool prints
(src/proton2m365/proton_export.py) and the Homebrew formula generator, then lists what changed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("pyproject.toml", "src/proton2m365/proton_export.py", "scripts/make-homebrew-formula.py")
URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+?(?=[/\"#]|$)", re.M)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    new = f"https://github.com/{argv[0]}"
    for name in FILES:
        path = ROOT / name
        text = path.read_text()
        updated = URL.sub(new, text)
        if updated != text:
            path.write_text(updated)
            print(f"updated {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
