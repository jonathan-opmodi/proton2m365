"""scripts/make-homebrew-formula.py: a formula with every runtime dependency pinned from uv.lock."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("make_homebrew_formula", ROOT / "scripts" / "make-homebrew-formula.py")
hb = importlib.util.module_from_spec(_spec)
sys.modules["make_homebrew_formula"] = hb
_spec.loader.exec_module(hb)


def test_formula_pins_every_runtime_dependency_and_no_dev_tools():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    text = hb.formula("0.1.0", "a" * 64, lock)
    resources = set(re.findall(r'resource "([^"]+)"', text))
    assert {"httpx", "imapclient", "msal", "msal-extensions", "requests", "pyjwt"} <= resources
    assert not resources & {"pytest", "pluggy", "iniconfig", "pygments", "cryptography", "cffi", "colorama"}
    assert 'depends_on "cryptography"' in text and 'depends_on "python@3.12"' in text
    assert "proton2m365-0.1.0.tar.gz" in text and 'sha256 "' + "a" * 64 + '"' in text
    for block in re.findall(r'resource "[^"]+" do\n(.*?)\n  end', text, re.S):
        assert re.search(r'url "https://files\.pythonhosted\.org/.+\.tar\.gz"', block)
        assert re.search(r'sha256 "[0-9a-f]{64}"', block)
    assert 'system bin/"proton2m365", "sample-export"' in text


def test_set_repo_url_rewrites_every_link(tmp_path, monkeypatch):
    import shutil

    spec = importlib.util.spec_from_file_location("set_repo_url", ROOT / "scripts" / "set-repo-url.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in mod.FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    assert mod.main(["someone/proton2m365"]) == 0
    for name in mod.FILES:
        text = (tmp_path / name).read_text()
        assert "github.com/proton2m365/proton2m365" not in text and "github.com/someone/proton2m365" in text
    assert "github.com/someone/proton2m365/issues" in (tmp_path / "src/proton2m365/proton_export.py").read_text()
    assert mod.main(["not a repo"]) == 2
