"""The migration receipt: one entry per export file, and a plain-text summary a person can keep.

verify writes both when it reaches an outcome, from cumulative state (a row checked in an earlier run
stays checked). Files are written to a temporary name and renamed into place, owner-only, and carry the
same run ID, so a reader can tell which files belong together.

receipt.json starts with the key "proton2m365_receipt" (the schema version; scripts/check_publish.py
recognises a receipt by it). Entry outcomes:
    migrated-verified      in the mailbox; every check passed
    migrated-unchecked     in the mailbox and tagged; not checked yet
    waiting:tag            in the mailbox; categories and flag not applied yet
    waiting:prune          in the mailbox, but the current plan skips it (prune moves it to Deleted Items)
    failed:verify          in the mailbox; the latest check did not match (checks says which)
    failed:upload          not in the mailbox; uploading failed (error says why)
    failed:tag             in the mailbox; tag could not find it in Graph
    planned                not uploaded yet
    skipped:<reason>       intentionally not uploaded
    orphan-eml             an .eml file with no .metadata.json
    orphan-metadata        a .metadata.json file with no .eml
    unreadable             an export file plan could not read (partial, empty or damaged)
    in-state-not-in-export a planned message whose export files are gone
    missing-from-newer-export-never-uploaded  not uploaded, and the newest export no longer has it (accepted with
                           plan --accept-missing)
Entries for messages the newest export no longer has carry "source": "missing-from-newer-export"; their
checks cover presence and metadata only (no content comparison).
    excluded-by-user       an export file left out on purpose with plan --exclude-file
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
CHECKS = "date, read state, flag, categories, not-a-draft and folder"


@dataclass
class ArtifactErrors:
    errors: dict[str, str] = field(default_factory=dict)  # path -> reason

    def __bool__(self) -> bool:
        return bool(self.errors)


def receipt_paths(state_path: Path, override: Path | None = None) -> tuple[Path, Path]:
    """<state-stem>.receipt.json and .receipt.txt next to the state file, or next to --receipt."""
    json_path = Path(override) if override else state_path.with_name(f"{state_path.stem}.receipt.json")
    stem = json_path.name.removesuffix(".json")
    return json_path, json_path.with_name(stem + ".txt")


def tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("proton2m365")
    except Exception:  # noqa: BLE001
        return "unknown"


# --- entries -------------------------------------------------------------------------------------

def _row_entry(row, missing: set[str]) -> dict:
    stage, skip, check = row["stage"], row["skip_reason"], row["check_result"] if "check_result" in row.keys() else None
    pid = row["proton_id"]
    present = bool(row["source_present"]) if "source_present" in row.keys() else True
    if not present and stage in ("planned", "upload_failed"):
        outcome = "missing-from-newer-export-never-uploaded"
    elif pid in missing:
        outcome = "in-state-not-in-export"
    elif stage == "skipped":
        outcome = f"skipped:{skip}"
    elif skip is not None:
        outcome = "waiting:prune"
    elif stage == "verified":
        outcome = "migrated-verified"
    elif stage == "tagged":
        outcome = "failed:verify" if (check or "").startswith("fail:") else "migrated-unchecked"
    elif stage == "appended":
        outcome = "waiting:tag"
    elif stage == "tag_failed":
        outcome = "failed:tag"
    elif stage == "upload_failed":
        outcome = "failed:upload"
    else:
        outcome = "planned"
    entry = {"id": pid, "outcome": outcome, "folder": "/".join(json.loads(row["target_folder"])),
             "categories": json.loads(row["categories"]),
             "checks": "pass" if check == "pass" else (check or "not-checked")}
    if outcome.startswith("failed:") and row["last_error"]:
        entry["error"] = row["last_error"]
    if not present:
        entry["source"] = "missing-from-newer-export"
    return entry


def entries(state, export) -> Iterator[dict]:
    """One entry per state row, then one per export file nobody planned."""
    missing = set(export.missing_from_export) if export is not None else set()
    for row in state.rows():
        yield _row_entry(row, missing)
    for name in (export.unresolved if export is not None else ()):
        if name.endswith(".metadata.json"):
            outcome = "orphan-metadata"
        elif name.endswith(".eml") and name.removesuffix(".eml") not in export.unplanned_pairs:
            outcome = "orphan-eml"
        else:
            outcome = "unreadable"
        yield {"id": name, "outcome": outcome, "folder": None, "categories": [], "checks": "not-checked"}
    for name in (export.excluded_files if export is not None else ()):
        yield {"id": name, "outcome": "excluded-by-user", "folder": None, "categories": [], "checks": "not-checked"}


# --- summary -------------------------------------------------------------------------------------

def selection(state) -> dict:
    """What plan selected, per folder, and what it left out and why."""
    folders: dict[str, dict] = {}
    for row in state.rows():
        if row["stage"] == "skipped":
            continue
        name = "/".join(json.loads(row["target_folder"]))
        f = folders.setdefault(name, {"messages": 0, "bytes": 0, "first": None, "last": None})
        f["messages"] += 1
        f["bytes"] += row["size"]
        date = row["internal_date"]
        if date is not None:
            f["first"] = date if f["first"] is None else min(f["first"], date)
            f["last"] = date if f["last"] is None else max(f["last"], date)
    for f in folders.values():
        for key in ("first", "last"):
            f[key] = _day(f[key])
    settings = json.loads(state.get_meta("plan_settings") or "{}")
    settings.pop("export_dir", None)  # a local path; the receipt is about the mail, not this machine
    return {"settings": settings, "folders": dict(sorted(folders.items())), "skipped": state.skip_counts()}


def _day(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts is not None else None


def assurance(report, state) -> list[str]:
    p = report.progress
    export = report.export
    if export is None or not export.available:
        accounting = "Export accounting: NOT DONE (the export folder could not be read)."
    elif export.unresolved or export.missing_from_export:
        accounting = (f"Export accounting: {len(export.unresolved):,} export files unresolved and "
                      f"{len(export.missing_from_export):,} planned messages missing from the export (listed below).")
    else:
        accounting = "Export accounting: every export file is migrated, skipped with a reason, or listed below."
    earlier = max(p.verified - report.passed, 0)
    oldest = state.oldest_check()
    freshness = f"; oldest check {_day(oldest)}" if oldest else ""
    checks = (f"Mailbox checks ({CHECKS}): {p.verified:,} of {p.in_mailbox:,} messages in the mailbox passed "
              f"({report.passed:,} checked this run, {earlier:,} in earlier runs{freshness}).")
    if report.content_checked:
        content = (f"Content: compared on {report.content_checked:,} messages this run (text, attachments, HTML "
                   "text and links, after decoding); the rest not compared (verify --all --content compares all).")
    else:
        content = "Content: not compared; run `verify --content` (or `verify --all --content`) to compare it."
    return [accounting, checks, content]


def summary_lines(report, state, run_id: str, now: float, *, limit: int = 10) -> list[str]:
    s = report.status
    lines = [f"VERIFY {s.outcome}: {s.sentence}",
             f"Run {run_id} at {datetime.fromtimestamp(now, UTC).strftime('%Y-%m-%d %H:%M UTC')}, "
             f"proton2m365 {tool_version()}"]
    counts = Counter()
    unresolved: list[str] = []
    full: list[str] = []  # orphans and unreadable files are always listed in full
    for e in entries(state, report.export):
        o = e["outcome"]
        counts["skipped" if o.startswith("skipped:") or o == "excluded-by-user" else o] += 1
        if o in ("orphan-eml", "orphan-metadata", "unreadable"):
            full.append(f"{e['id']} ({o})")
        elif o not in ("migrated-verified", "migrated-unchecked", "excluded-by-user") and not o.startswith("skipped:"):
            unresolved.append(f"{e['id']} ({o})")
    migrated = counts["migrated-verified"] + counts["migrated-unchecked"]
    lines.append(f"Totals: {migrated:,} migrated ({counts['migrated-verified']:,} checked), "
                 f"{counts['skipped']:,} skipped on purpose, {len(full) + len(unresolved):,} unresolved.")
    skips = state.skip_counts()
    if skips:
        lines.append("Skipped: " + ", ".join(f"{n:,} {reason}" for reason, n in sorted(skips.items())))
    lines += assurance(report, state)
    if full:
        lines.append("Export files not migrated:")
        lines += [f"  {x}" for x in full]
    if unresolved:
        lines.append("Still to do:")
        lines += [f"  {x}" for x in unresolved[:limit]]
        if len(unresolved) > limit:
            lines.append(f"  ...and {len(unresolved) - limit:,} more; see the receipt")
    lines.append(f"Next: {s.next_hint}")
    return lines


# --- writing -------------------------------------------------------------------------------------

def replace_atomically(tmp: Path, dest: Path, sleep: Callable[[float], None] = time.sleep) -> None:
    """os.replace, retried on Windows where an open editor or a virus scan can briefly hold the target."""
    for attempt in range(5):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 4:
                raise
            sleep(0.2 * 2 ** attempt)


def open_private(tmp: Path):
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def write_receipt(report, state, json_path: Path, txt_path: Path, *, run_id: str, now: float,
                  mailbox: str | None) -> ArtifactErrors:
    """Write receipt.json (streamed) and receipt.txt. Never raises for a write failure: returns it."""
    errors = ArtifactErrors()
    s = report.status
    head = {"proton2m365_receipt": SCHEMA_VERSION, "run_id": run_id,
            "generated_at": datetime.fromtimestamp(now, UTC).isoformat(), "tool_version": tool_version(),
            "mailbox": mailbox, "outcome": str(s.outcome), "sentence": s.sentence,
            "assurance": assurance(report, state), "selection": selection(state)}
    tmp = json_path.with_name(json_path.name + f".{run_id}.tmp")
    try:
        with open_private(tmp) as fh:
            fh.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "entries": [\n')
            first = True
            for entry in entries(state, report.export):
                fh.write(("" if first else ",\n") + json.dumps(entry, ensure_ascii=False))
                first = False
            fh.write("\n]}\n")
        replace_atomically(tmp, json_path)
    except OSError as exc:
        errors.errors[str(json_path)] = exc.strerror or str(exc)
        tmp.unlink(missing_ok=True)
    for path, render in ((txt_path, lambda: "\n".join(summary_lines(report, state, run_id, now))
                          + f"\nReceipt: {json_path}\n"),
                         (html_path(json_path), lambda: render_html(report, state, run_id, now, mailbox, head,
                                                                   json_path.name))):
        tmp = path.with_name(path.name + f".{run_id}.tmp")
        try:
            with open_private(tmp) as fh:
                fh.write(render())
            replace_atomically(tmp, path)
        except OSError as exc:
            errors.errors[str(path)] = exc.strerror or str(exc)
            tmp.unlink(missing_ok=True)
    return errors


def html_path(json_path: Path) -> Path:
    return json_path.with_name(json_path.name.removesuffix(".json") + ".html")


# --- receipt.html (a page a person can keep or print) --------------------------------------------

HTML_LIST_LIMIT = 1000  # unresolved rows shown in the page; the JSON always has all of them


def render_html(report, state, run_id: str, now: float, mailbox: str | None, head: dict,
                json_name: str = "receipt.json") -> str:
    from html import escape as e

    s = report.status
    counts: Counter = Counter()
    problems: list[tuple[str, str, str]] = []
    for entry in entries(state, report.export):
        o = entry["outcome"]
        counts["skipped" if o.startswith("skipped:") or o == "excluded-by-user" else o] += 1
        if not (o in ("migrated-verified", "migrated-unchecked", "excluded-by-user") or o.startswith("skipped:")):
            problems.append((entry["id"], o, entry.get("checks", "") if o == "failed:verify" else entry.get("error", "")))
    migrated = counts["migrated-verified"] + counts["migrated-unchecked"]
    sel = head["selection"]
    tone = {"PASSED": "ok", "SAMPLE OK": "warn", "INCOMPLETE": "warn", "FAILED": "bad"}[str(s.outcome)]
    folder_rows = "".join(
        f"<tr><td>{e(name)}</td><td class=n>{f['messages']:,}</td><td class=n>{f['bytes'] / 1e6:,.1f} MB</td>"
        f"<td>{e(f['first'] or '')} to {e(f['last'] or '')}</td></tr>"
        for name, f in sorted(sel["folders"].items(), key=lambda kv: -kv[1]["messages"]))
    skip_rows = "".join(f"<tr><td>{e(reason)}</td><td class=n>{n:,}</td></tr>"
                        for reason, n in sorted(sel["skipped"].items(), key=lambda kv: -kv[1]))
    problem_rows = "".join(f"<tr><td><code>{e(pid)}</code></td><td>{e(o)}</td><td>{e(detail)}</td></tr>"
                           for pid, o, detail in problems[:HTML_LIST_LIMIT])
    more = (f"<p>...and {len(problems) - HTML_LIST_LIMIT:,} more; the JSON receipt lists every one.</p>"
            if len(problems) > HTML_LIST_LIMIT else "")
    when = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration receipt</title>
<style>
:root {{ --fg:#1b1f24; --bg:#ffffff; --muted:#59636e; --line:#d1d9e0; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fg:#e6edf3; --bg:#0d1117; --muted:#9198a1; --line:#3d444d; --ok:#3fb950; --warn:#d29922; --bad:#f85149; }}
}}
body {{ font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg); background: var(--bg);
       max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
.verdict {{ border-left: 6px solid var(--{tone}); padding: .5rem 1rem; margin: 1rem 0; }}
.verdict strong {{ color: var(--{tone}); }}
.muted {{ color: var(--muted); }}
table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }}
th, td {{ text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
code {{ font-size: .9em; }}
@media print {{ body {{ margin: 0; }} }}
</style></head><body>
<h1>Migration receipt</h1>
<p class=muted>Proton Mail export to {e(mailbox or "the mailbox")} &middot; {e(when)} &middot; run {e(run_id)}
&middot; proton2m365 {e(tool_version())}</p>
<div class=verdict><strong>{e(str(s.outcome))}</strong>: {e(s.sentence)}</div>
<p>{migrated:,} migrated ({counts['migrated-verified']:,} checked), {counts['skipped']:,} left out on purpose,
{len(problems):,} unresolved.</p>
<h2>What was checked</h2>
<ul>{"".join(f"<li>{e(line)}</li>" for line in head["assurance"])}</ul>
<h2>Folders</h2>
<table><tr><th>Folder</th><th class=n>Messages</th><th class=n>Size</th><th>Received</th></tr>{folder_rows}</table>
<h2>Left out on purpose</h2>
{f"<table><tr><th>Reason</th><th class=n>Messages</th></tr>{skip_rows}</table>" if skip_rows else "<p>Nothing.</p>"}
<h2>Unresolved</h2>
{f"<table><tr><th>Message or file</th><th>Outcome</th><th>Detail</th></tr>{problem_rows}</table>{more}"
 if problems else "<p>Nothing: every export file is accounted for.</p>"}
<h2>Next</h2>
<p>{e(s.next_hint)}</p>
<p class=muted>Keep your Proton export until you are satisfied. The full per-message record is in
<code>{e(json_name)}</code> next to this file.</p>
</body></html>
"""
