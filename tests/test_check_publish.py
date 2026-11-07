"""scripts/check_publish.py: nothing private leaves through the files, their encodings or git history.

Private-looking values are assembled at runtime so this file itself passes the check it tests.
"""

from __future__ import annotations

import base64
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_publish", ROOT / "scripts" / "check_publish.py")
cp = importlib.util.module_from_spec(_spec)
sys.modules["check_publish"] = cp  # dataclasses look the module up by name
_spec.loader.exec_module(cp)

AT = "@"
PRIVATE_ADDR = "bob" + AT + "corp-private" + ".com"
SECRET = "Zanzibar" + " Holdings"
NOREPLY = "12345+dev" + AT + "users.noreply.github.com"


def git(repo: Path, *args: str, email: str = NOREPLY) -> str:
    env = {"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "dev",
           "GIT_COMMITTER_EMAIL": email, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "scripts" / "publish-files.txt").write_text((ROOT / "scripts" / "publish-files.txt").read_text())
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("print('hello')  # contact: alice@example.com\n")
    (r / "tests" / "fixtures").mkdir(parents=True)
    (r / "tests" / "fixtures" / "SYNTHETIC.txt").write_text("# synthetic\n")
    (r / "CLAUDE.md").write_text("private notes\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "start")
    deny = tmp_path / "deny.txt"
    deny.write_text(f"# private\n{SECRET}\n")
    monkeypatch.setenv("PROTON2M365_DENYLIST", str(deny))
    return r


def tree(repo: Path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = cp.main(["--repo", str(repo), *extra])
    return code, err.getvalue()


def add(repo: Path, path: str, data: bytes | str, *, commit: bool = True, email: str = NOREPLY) -> None:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    git(repo, "add", "-A")
    if commit:
        git(repo, "commit", "-q", "-m", f"add {path}", email=email)


def test_clean_tree_passes_and_never_list_is_not_scanned(repo):
    code, err = tree(repo)
    assert code == 0, err


def test_deny_list_term_is_refused_without_printing_it(repo):
    add(repo, "src/notes.py", f"# owner: {SECRET.upper()}\n")
    code, err = tree(repo)
    assert code == 1 and "deny-list term #1" in err and SECRET not in err


@pytest.mark.parametrize("encoded", [
    PRIVATE_ADDR,                                 # plain
    PRIVATE_ADDR.replace(AT, "=40"),              # quoted-printable
    PRIVATE_ADDR.replace(AT, "%40"),              # URL-encoded
    PRIVATE_ADDR.replace(AT, "-at-"),             # Proton export folder spelling
])
def test_addresses_are_found_in_every_encoding(repo, encoded):
    add(repo, "docs/example.md", f"write to {encoded} for help\n")
    code, err = tree(repo)
    assert code == 1 and "corp-private.com" in err


def test_base64_eml_body_is_decoded(repo):
    body = base64.b64encode(f"Thanks, {SECRET}\n".encode()).decode()
    eml = ("From: a@example.com\r\nTo: b@example.com\r\nSubject: hi\r\nMIME-Version: 1.0\r\n"
           "Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n" + body + "\r\n")
    add(repo, "docs/sample.eml", eml)
    code, err = tree(repo)
    assert code == 1 and "deny-list term #1 (decoded body)" in err


def test_eml_under_tests_must_be_listed_as_synthetic(repo):
    add(repo, "tests/fixtures/one.eml", "From: a@example.com\r\n\r\nhi\r\n")
    assert tree(repo)[0] == 1
    add(repo, "tests/fixtures/SYNTHETIC.txt", "# synthetic\ntests/fixtures/one.eml\n")
    assert tree(repo)[0] == 0


def test_sqlite_and_receipt_are_refused_whatever_their_name(repo):
    add(repo, "tests/data.bin", b"SQLite format 3\x00" + b"\x00" * 100)
    add(repo, "docs/report.json", '{"proton2m365_receipt": 1, "rows": []}')
    code, err = tree(repo)
    assert "SQLite database" in err and "migration receipt" in err and code == 1


def test_uv_lock_must_use_pypi(repo):
    add(repo, "uv.lock", '[[package]]\nsource = { registry = "https://pypi.org/simple" }\n')
    assert tree(repo)[0] == 0
    add(repo, "uv.lock", '[[package]]\nsource = { registry = "https://pkgs.corp-private.com/simple" }\n')
    code, err = tree(repo)
    assert code == 1 and "package index other than PyPI" in err


def test_stage_copies_exactly_the_allowlisted_files(repo, tmp_path):
    out = tmp_path / "public"
    assert tree(repo, "--stage", str(out))[0] == 0
    copied = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert copied == ["scripts/publish-files.txt", "src/app.py", "tests/fixtures/SYNTHETIC.txt"]


def test_stage_writes_nothing_when_refused(repo, tmp_path):
    add(repo, "src/notes.py", f"# {SECRET}\n")
    out = tmp_path / "public"
    assert tree(repo, "--stage", str(out))[0] == 1
    assert not out.exists()


def test_history_finds_a_secret_removed_before_the_tip(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    add(repo, "src/config.py", f"OWNER = '{SECRET}'\n")
    add(repo, "src/config.py", "OWNER = 'someone'\n")              # the tip is clean
    assert tree(repo)[0] == 0
    code, err = tree(repo, "--range", f"{base}..HEAD")
    assert code == 1 and "src/config.py" in err and "deny-list term #1" in err


def test_history_checks_commit_authors_and_messages(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    add(repo, "src/x.py", "x = 1\n", email=PRIVATE_ADDR)
    code, err = tree(repo, "--range", f"{base}..HEAD")
    assert code == 1 and "author email is not a GitHub noreply address" in err
    # History up to base has no author problem, but committing CLAUDE.md would publish it.
    code, err = tree(repo, "--range", f"..{base}")
    assert code == 1 and "noreply" not in err
    assert [line.split(":")[0].strip() for line in err.splitlines() if "never-publish" in line] == \
        [f"CLAUDE.md (blob {git(repo, 'rev-parse', f'{base}:CLAUDE.md').strip()[:12]})"]


def test_pre_push_scans_only_what_is_new(repo, monkeypatch):
    import io

    first = git(repo, "rev-parse", "HEAD").strip()
    add(repo, "src/config.py", f"OWNER = '{SECRET}'\n")
    tip = git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr("sys.stdin", io.StringIO(f"refs/heads/main {tip} refs/heads/main {first}\n"))
    assert tree(repo, "--pre-push", "origin")[0] == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(f"refs/heads/main {cp.ZERO} refs/heads/main {first}\n"))
    assert tree(repo, "--pre-push", "origin")[0] == 0                # deleting a ref publishes nothing


def test_missing_deny_list_is_a_setup_error(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PROTON2M365_DENYLIST", str(tmp_path / "nope.txt"))
    with pytest.raises(SystemExit, match="deny-list .* not found"):
        cp.main(["--repo", str(repo)])


def test_this_repository_publishes_clean(monkeypatch, tmp_path):
    """The real allowlist over this checkout: no non-example address, receipt or database in any file.

    The private deny-list is checked by running scripts/check-publish.sh locally; here it is a placeholder.
    """
    deny = tmp_path / "deny.txt"
    deny.write_text("placeholder" + "-private-term\n")
    monkeypatch.setenv("PROTON2M365_DENYLIST", str(deny))
    code, err = tree(ROOT)
    assert code == 0, err
