#!/usr/bin/env python3
"""Write a Homebrew formula for a released proton2m365 version, from uv.lock (standard library only).

    python3 scripts/make-homebrew-formula.py --version 0.1.0 --sha256 <sdist sha256> > Formula/proton2m365.rb

The project's own source archive comes from PyPI (its URL is derived from the version; pass its sha256, which
PyPI shows and the publish workflow knows). Every runtime dependency becomes a `resource` with the exact
version and hash pinned in uv.lock. `cryptography` is Homebrew's own formula rather than a resource (it needs
Rust to build), so its build-only dependencies are left out too. Copy the result into a tap repository
(github.com/<owner>/homebrew-proton2m365, Formula/proton2m365.rb).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDED_BY_BREW = {"cryptography": "cryptography"}   # package -> Homebrew formula to depend on instead
SKIP = {"cffi", "pycparser", "colorama"}             # cryptography's own, or Windows-only
PYTHON = "python@3.12"


def runtime_closure(lock: dict, root: str = "proton2m365") -> list[dict]:
    packages = {p["name"]: p for p in lock["package"]}
    seen: dict[str, dict] = {}
    todo = [d["name"] for d in packages[root].get("dependencies", [])]
    while todo:
        name = todo.pop()
        if name in seen or name in SKIP:
            continue
        pkg = packages[name]
        seen[name] = pkg
        if name in PROVIDED_BY_BREW:
            continue
        todo += [d["name"] for d in pkg.get("dependencies", []) if "marker" not in d or "win32" not in d["marker"]]
    return sorted(seen.values(), key=lambda p: p["name"])


def sdist_url(name: str, version: str) -> str:
    return f"https://files.pythonhosted.org/packages/source/{name[0]}/{name}/{name.replace('-', '_')}-{version}.tar.gz"


def formula(version: str, sha256: str, lock: dict) -> str:
    deps = runtime_closure(lock)
    brew_deps = sorted(PROVIDED_BY_BREW[p["name"]] for p in deps if p["name"] in PROVIDED_BY_BREW)
    resources = []
    for p in deps:
        if p["name"] in PROVIDED_BY_BREW:
            continue
        sdist = p.get("sdist")
        if not sdist:
            raise SystemExit(f"{p['name']} has no source archive in uv.lock; Homebrew needs one")
        resources.append(f'''  resource "{p["name"]}" do
    url "{sdist["url"]}"
    sha256 "{sdist["hash"].removeprefix("sha256:")}"
  end
''')
    depends = "".join(f'  depends_on "{d}"\n' for d in [*brew_deps, PYTHON])
    return f'''class Proton2m365 < Formula
  include Language::Python::Virtualenv

  desc "Move a Proton Mail export into Microsoft 365 or Outlook.com, with a receipt"
  homepage "https://github.com/jonathan-opmodi/proton2m365"
  url "{sdist_url("proton2m365", version)}"
  sha256 "{sha256}"
  license "MIT"

{depends}
{chr(10).join(resources)}
  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"proton2m365", "sample-export", testpath/"sample"
    assert_match "Upload 34 of 40 messages", shell_output("#{{bin}}/proton2m365 plan --dry-run #{{testpath}}/sample")
  end
end
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="the released version, e.g. 0.1.0")
    ap.add_argument("--sha256", required=True, help="sha256 of the release's source archive on PyPI")
    ap.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    args = ap.parse_args(argv)
    sys.stdout.write(formula(args.version, args.sha256, tomllib.loads(args.lock.read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
