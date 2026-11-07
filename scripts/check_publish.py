#!/usr/bin/env python3
"""Refuse to publish personal data. Standard library only; run through scripts/check-publish.sh.

    check-publish.sh [--stage DIR]       scan the allowlisted files in the working tree (and copy them to DIR)
    check-publish.sh --range BASE..TIP   scan every commit, tag and blob reachable from TIP but not BASE
                                         (an empty BASE scans all history)
    check-publish.sh --pre-push REMOTE   the same for each ref git is about to push (reads git's hook input)

Checks, on every file (and in history mode on every blob, commit message and tag message):
  * the private deny-list (~/.config/proton2m365/publish-denylist.txt, one term per line, never in the repo),
    in plain text and in quoted-printable, URL-encoded and (for .eml files) MIME-decoded form;
  * any email address whose domain is not a reserved example domain, in the same forms, plus Proton's
    "name-at-domain" folder spelling;
  * paths on the never-list, SQLite databases (by header) and receipt JSON (by its first key), whatever
    the file is called;
  * .eml files under tests/ that are not listed in tests/fixtures/SYNTHETIC.txt;
  * uv.lock pointing at any package index other than PyPI;
  * in history mode, commit authors and committers that are not a GitHub noreply address.

Findings name the file and the rule, never the private term itself. Exit 0 clean, 1 findings, 2 setup error.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import fnmatch
import os
import quopri
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

# A receipt is JSON whose first key is this (the receipt writer must keep it first).
_RECEIPT = re.compile(rb'\A\s*\{\s*"proton2m365_receipt"\s*:')
SQLITE_HEADER = b"SQLite format 3\x00"
SYNTHETIC_MANIFEST = "tests/fixtures/SYNTHETIC.txt"
DEFAULT_DENYLIST = Path.home() / ".config" / "proton2m365" / "publish-denylist.txt"
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")
ALLOWED_SUFFIXES = (".invalid", ".example", ".test", ".localhost", "users.noreply.github.com")
ALLOWED_ADDRESSES = ("noreply@anthropic.com", "noreply@github.com")
NOREPLY_AUTHOR = re.compile(r"^(\d+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com$")
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
ZERO = "0" * 40

_ADDRESS = re.compile(rb"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_AT_FOLDER = re.compile(rb"[A-Za-z0-9._%+-]+-at-((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_LOCK_URL = re.compile(rb'(?:url|registry|source)\s*=\s*"https?://([^/"]+)')


@dataclass
class Report:
    findings: list[str] = field(default_factory=list)
    scanned: int = 0

    def add(self, where: str, rule: str, detail: str = "") -> None:
        self.findings.append(f"{where}: {rule}" + (f" ({detail})" if detail else ""))


# --- configuration -------------------------------------------------------------------------------

def load_allowlist(repo: Path) -> tuple[list[str], list[str]]:
    include, never = [], []
    for line in (repo / "scripts" / "publish-files.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (never if line.startswith("!") else include).append(line.lstrip("!"))
    return include, never


def load_denylist(path: Path) -> list[re.Pattern[bytes]]:
    if not path.is_file():
        raise SystemExit(f"check-publish: deny-list {path} not found. Create it (one private term per line: "
                         "addresses, domains, tenant and client IDs, label and folder names); it stays out of git.")
    terms = [t.strip() for t in path.read_text().splitlines() if t.strip() and not t.strip().startswith("#")]
    if not terms:
        raise SystemExit(f"check-publish: deny-list {path} is empty")
    return [re.compile(rb"(?<![A-Za-z0-9])" + re.escape(t.encode()) + rb"(?![A-Za-z0-9])", re.IGNORECASE)
            for t in terms]


def is_never(path: str, never: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in never)


def is_included(path: str, include: list[str]) -> bool:
    return any(path == pat or (pat.endswith("/") and path.startswith(pat)) for pat in include)


# --- content checks ------------------------------------------------------------------------------

def views(path: str, data: bytes) -> list[tuple[str, bytes]]:
    """The forms an address can hide in: as written, URL-encoded, quoted-printable, and decoded MIME."""
    out = [("text", data)]
    try:
        out.append(("url-decoded", unquote(data.decode("utf-8", "replace")).encode()))
    except Exception:  # noqa: BLE001
        pass
    try:
        out.append(("quoted-printable", quopri.decodestring(data)))
    except Exception:  # noqa: BLE001
        pass
    if path.endswith(".eml"):
        try:
            msg = email.message_from_bytes(data, policy=email.policy.default)
            for key, value in msg.items():
                out.append((f"header {key}", str(value).encode()))
            for part in msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    out.append(("decoded body", payload))
        except Exception:  # noqa: BLE001
            pass
    return out


def _domain_ok(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    return d in ALLOWED_DOMAINS or d.endswith(ALLOWED_SUFFIXES) or any(d.endswith("." + a) for a in ALLOWED_DOMAINS)


def scan_text(where: str, path: str, data: bytes, deny: list[re.Pattern[bytes]], report: Report) -> None:
    seen: set[str] = set()
    for form, text in views(path, data):
        for i, pattern in enumerate(deny, 1):
            if pattern.search(text) and f"deny{i}" not in seen:
                seen.add(f"deny{i}")
                report.add(where, f"deny-list term #{i}", form)
        for regex, kind in ((_ADDRESS, "email address"), (_AT_FOLDER, "-at- address")):
            for m in regex.finditer(text):
                full = m.group(0).decode(errors="replace").lower()
                if full in ALLOWED_ADDRESSES or _domain_ok(m.group(1).decode(errors="replace")):
                    continue
                key = f"{kind}:{m.group(1).lower()!r}"
                if key not in seen:
                    seen.add(key)
                    report.add(where, f"{kind} at a non-example domain", f"{m.group(1).decode(errors='replace')}, {form}")


def scan_file(where: str, path: str, data: bytes, deny, never, synthetic: set[str], report: Report) -> None:
    report.scanned += 1
    if is_never(path, never):
        report.add(where, "path is on the never-publish list")
    if data.startswith(SQLITE_HEADER):
        report.add(where, "SQLite database")
    if _RECEIPT.match(data):
        report.add(where, "migration receipt")
    if path.startswith("tests/") and path.endswith(".eml") and path not in synthetic:
        report.add(where, f".eml under tests/ not listed in {SYNTHETIC_MANIFEST}")
    if path == "uv.lock":
        for host in {m.group(1).decode() for m in _LOCK_URL.finditer(data)}:
            if host not in PYPI_HOSTS:
                report.add(where, "package index other than PyPI", host)
    scan_text(where, path, data, deny, report)


def synthetic_list(text: str | None) -> set[str]:
    if not text:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


# --- working tree --------------------------------------------------------------------------------

def git(repo: Path, *args: str, binary: bool = False, check: bool = True):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check)
    return out.stdout if binary else out.stdout.decode()


def tree_files(repo: Path, include: list[str], never: list[str]) -> list[str]:
    tracked = git(repo, "ls-files", "-z").split("\0")
    return sorted(p for p in tracked if p and is_included(p, include) and not is_never(p, never))


def check_tree(repo: Path, deny, stage: Path | None) -> Report:
    include, never = load_allowlist(repo)
    report = Report()
    manifest = repo / SYNTHETIC_MANIFEST
    synthetic = synthetic_list(manifest.read_text() if manifest.exists() else None)
    files = tree_files(repo, include, never)
    for path in files:
        scan_file(path, path, (repo / path).read_bytes(), deny, never, synthetic, report)
    if stage is not None and not report.findings:
        if stage.exists() and any(stage.iterdir()):
            raise SystemExit(f"check-publish: staging directory {stage} is not empty")
        for path in files:
            dest = stage / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / path, dest)
    return report


# --- history -------------------------------------------------------------------------------------

def check_history(repo: Path, deny, tip: str, bases: list[str]) -> Report:
    """Everything reachable from tip and not from bases: commits, annotated tags and blobs at any path."""
    _, never = load_allowlist(repo)
    report = Report()
    exclude = ["--not", *bases] if bases else []
    manifest = git(repo, "show", f"{tip}:{SYNTHETIC_MANIFEST}", check=False)
    synthetic = synthetic_list(manifest)
    if git(repo, "cat-file", "-t", tip).strip() == "tag":
        body = git(repo, "cat-file", "-p", tip, binary=True)
        tagger = next((line for line in body.split(b"\n") if line.startswith(b"tagger ")), b"")
        email = tagger.split(b"<", 1)[1].split(b">", 1)[0].decode(errors="replace") if b"<" in tagger else ""
        if tagger and not NOREPLY_AUTHOR.match(email):
            report.add(f"tag {tip[:12]}", "tagger email is not a GitHub noreply address")
        scan_text(f"tag {tip[:12]}", "", body, deny, report)
    for commit in git(repo, "rev-list", tip, *exclude).split():
        meta = git(repo, "show", "-s", "--format=%ae%n%ce", commit).split("\n")
        for role, addr in zip(("author", "committer"), meta[:2]):
            if not NOREPLY_AUTHOR.match(addr.strip()):
                report.add(f"commit {commit[:12]}", f"{role} email is not a GitHub noreply address")
        scan_text(f"commit {commit[:12]} message", "", git(repo, "show", "-s", "--format=%B", commit, binary=True),
                  deny, report)
    for line in git(repo, "rev-list", "--objects", tip, *exclude).splitlines():
        sha, _, path = line.partition(" ")
        if not path or git(repo, "cat-file", "-t", sha).strip() != "blob":
            continue
        data = git(repo, "cat-file", "blob", sha, binary=True)
        scan_file(f"{path} (blob {sha[:12]})", path, data, deny, never, synthetic, report)
    return report


def check_pre_push(repo: Path, deny, remote: str, stdin: str) -> Report:
    total = Report()
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:  # deleting a ref publishes nothing
            continue
        _, local_sha, _, remote_sha = parts
        bases = [remote_sha] if remote_sha != ZERO else [f"--remotes={remote}"]
        r = check_history(repo, deny, local_sha, bases)
        total.findings += r.findings
        total.scanned += r.scanned
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, help="copy the checked files here when clean (to build the fresh commit)")
    ap.add_argument("--range", dest="rng", metavar="BASE..TIP", help="scan history instead of the working tree")
    ap.add_argument("--pre-push", metavar="REMOTE", help="scan what git is about to push (hook mode)")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    args = ap.parse_args(argv)
    repo = Path(git(args.repo, "rev-parse", "--show-toplevel").strip())
    deny = load_denylist(Path(os.environ.get("PROTON2M365_DENYLIST", DEFAULT_DENYLIST)))
    if args.pre_push:
        report = check_pre_push(repo, deny, args.pre_push, sys.stdin.read())
        what = "objects about to be pushed"
    elif args.rng:
        base, _, tip = args.rng.partition("..")
        report = check_history(repo, deny, tip or "HEAD", [base] if base else [])
        what = f"objects in {args.rng}"
    else:
        report = check_tree(repo, deny, args.stage)
        what = "allowlisted files"
    for finding in report.findings:
        print(f"  {finding}", file=sys.stderr)
    if report.findings:
        print(f"check-publish: REFUSED, {len(report.findings)} problem(s) in {report.scanned} {what}", file=sys.stderr)
        return 1
    print(f"check-publish: clean ({report.scanned} {what})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
