"""`proton2m365 doctor`: what an issue report needs, with nothing private in it.

Redaction is allow-list based: the report is built only from the fields named below, and every value
that could identify a person or a mailbox (addresses, tenant and object IDs, folder and category names,
label selectors, paths, error text from a server) is replaced by a short hash salted per install, or by
a fixed description. `--show-names` reveals names for the user's own debugging; `--issue` always redacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# The issue template (.github/ISSUE_TEMPLATE/bug_report.md) uses exactly these headings; a test checks it.
ISSUE_HEADINGS = (
    "What happened",
    "What you expected",
    "Command you ran",
    "doctor report",
)

PACKAGE_DIR = Path(__file__).resolve().parent

# Last-error text from a server is free text: report only which kind of problem it was.
_ERROR_KINDS = (
    (re.compile(r"cannot read", re.I), "unreadable export file"),
    (re.compile(r"throttl|suggested backoff", re.I), "throttled by the server"),
    (re.compile(r"connection|socket|timed? ?out|reset|bye", re.I), "connection problem"),
    (re.compile(r"quota|mailbox (is )?full", re.I), "mailbox full"),
    (re.compile(r"not found in graph|no match|not visible", re.I), "not found in Graph"),
    (re.compile(r"^fail:", re.I), "check did not match"),
    (re.compile(r"reject|argument error|bad|NO ", re.I), "rejected by the server"),
)


def error_kind(text: str | None) -> str:
    if not text:
        return "none"
    for pattern, kind in _ERROR_KINDS:
        if pattern.search(text):
            return kind
    return "other error"


class Redactor:
    """Short, stable, salted hashes: the same name gives the same hash on this install only."""

    def __init__(self, salt: bytes, show_names: bool = False):
        self.salt = salt
        self.show_names = show_names

    def __call__(self, value: str | None, kind: str = "name") -> str:
        if value is None or value == "":
            return "-"
        if self.show_names:
            return value
        digest = hashlib.sha256(self.salt + value.encode()).hexdigest()[:8]
        return f"<{kind} {digest}>"

    def path(self, value: str | Path | None) -> str:
        return self(str(value), "path") if value else "-"


def install_salt(config_dir: Path) -> bytes:
    path = config_dir / "doctor-salt"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        salt = secrets.token_bytes(16)
        config_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(salt.hex())
        return salt


# --- crash logs ----------------------------------------------------------------------------------

def safe_frames(tb) -> list[str]:
    """file:line for frames inside this package only, relative to it (no home directories, no site-packages)."""
    out = []
    for frame in traceback.extract_tb(tb):
        try:
            rel = Path(frame.filename).resolve().relative_to(PACKAGE_DIR)
        except ValueError:
            continue
        out.append(f"proton2m365/{rel.as_posix()}:{frame.lineno}")
    return out


def write_crash_log(exc: BaseException, directory: Path, now: float) -> Path:
    """Traceback in full (it stays on this machine); the first two lines are what doctor may repeat."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"crash-{datetime.fromtimestamp(now, UTC).strftime('%Y%m%d-%H%M%S')}.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"type: {type(exc).__name__}\n")
        fh.write("frames: " + " ".join(safe_frames(exc.__traceback__)) + "\n\n")
        fh.write("".join(traceback.format_exception(exc)))
    return path


def last_crash(directories: list[Path]) -> dict | None:
    logs = sorted((p for d in directories if d.is_dir() for p in d.glob("crash-*.log")), key=lambda p: p.name)
    if not logs:
        return None
    head = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[:2]
    fields = dict(line.split(": ", 1) for line in head if ": " in line)
    return {"when": logs[-1].name.removeprefix("crash-").removesuffix(".log"),
            "type": re.sub(r"[^A-Za-z0-9_.]", "", fields.get("type", "?")),
            "frames": [f for f in fields.get("frames", "").split() if re.fullmatch(r"proton2m365/[\w/]+\.py:\d+", f)]}


# --- the report ----------------------------------------------------------------------------------

@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)


def build_report(
    *, settings_sources: dict | None = None, state=None, state_path: Path | None, auth=None, auth_error: str | None, online_checks: Callable | None,
    redact: Redactor, crash_dirs: list[Path], version: str,
) -> list[Section]:
    sections = [Section("Versions", [f"proton2m365 {version}", f"Python {platform.python_version()}",
                                     f"{platform.system()} {platform.release()} ({platform.machine()})"])]
    if settings_sources:
        sections.append(Section("Settings (where each came from)",
                                [f"{k}: {v}" for k, v in sorted(settings_sources.items())]))

    account = Section("Account")
    if auth is not None and getattr(auth, "account", None):
        account.lines += [f"account type: {'personal' if auth.is_personal else 'work or school'}",
                          f"account: {redact(auth.username, 'address')}",
                          f"client ID source: {auth.config.client_id_source}",
                          f"tenant: {redact(auth.config.tenant, 'tenant') if auth.config.tenant not in ('common', 'organizations', 'consumers') else auth.config.tenant}"]
    else:
        account.lines.append(f"not run: {auth_error or 'not signed in'} (doctor never opens a browser; "
                             "run any online command once to sign in)")
    sections.append(account)

    if online_checks is not None and auth is not None and getattr(auth, "account", None):
        sections.append(Section("Mailbox", online_checks(redact)))
    else:
        sections.append(Section("Mailbox", ["not run: not signed in"]))

    st = Section("State")
    if state is None:
        st.lines.append(f"no state file at {redact.path(state_path)}")
    else:
        st.lines.append(f"state file: {redact.path(state_path)}, schema {state.get_meta('schema_version')}")
        for stage, n in sorted(state.stage_counts().items()):
            st.lines.append(f"{stage}: {n:,}")
        for reason, n in sorted(state.skip_counts().items()):
            st.lines.append(f"skipped {reason}: {n:,}")
        settings = json.loads(state.get_meta("plan_settings") or "{}")
        safe = {k: v for k, v in settings.items() if k in ("include_trash", "include_spam", "max_size",
                                                           "long_line_limit", "starred_as_flag", "export_format",
                                                           "sample")}
        safe["exclude_labels"] = [redact(x, "label") for x in settings.get("exclude_labels", [])]
        safe["export_dir"] = redact.path(settings.get("export_dir"))
        st.lines.append("plan settings: " + json.dumps(safe, sort_keys=True))
        recorded = state.get_meta("account")
        st.lines.append("bound to account: " + (redact(json.loads(recorded)["username"], "address")
                                                if recorded else "no"))
        last = state.get_meta("last_upload")
        if last:
            lu = json.loads(last)
            st.lines.append(f"last upload: {lu.get('sent', 0):,} messages at {lu.get('per_hour', 0):,.0f} msgs/h")
        errors: dict[str, dict[str, int]] = {}
        for _, stage, error in state.failed_rows():
            kinds = errors.setdefault(stage, {})
            kinds[error_kind(error)] = kinds.get(error_kind(error), 0) + 1
        for stage, kinds in sorted(errors.items()):
            st.lines.append(f"{stage} errors: " + ", ".join(f"{n:,} {k}" for k, n in sorted(kinds.items())))
    sections.append(st)

    crash = last_crash(crash_dirs)
    sections.append(Section("Last crash", [f"{crash['when']} {crash['type']} at " + (" < ".join(crash["frames"]) or "?")]
                            if crash else ["none"]))
    return sections


def render(sections: list[Section]) -> str:
    out = []
    for s in sections:
        out.append(f"{s.title}:")
        out += [f"  {line}" for line in s.lines]
    return "\n".join(out) + "\n"


def render_issue(sections: list[Section]) -> str:
    body = []
    for heading in ISSUE_HEADINGS[:-1]:
        body += [f"### {heading}", "", "<!-- please fill in -->", ""]
    body += [f"### {ISSUE_HEADINGS[-1]}", "", "```", render(sections).rstrip(), "```", ""]
    return "\n".join(body)


def crash_dirs(state_path: Path | None, config_dir: Path) -> list[Path]:
    dirs = [config_dir]
    if state_path is not None:
        dirs.insert(0, Path(state_path).resolve().parent)
    return dirs


def stderr(msg: str) -> None:
    print(msg, file=sys.stderr)
