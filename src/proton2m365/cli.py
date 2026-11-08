"""Command-line entry point: `proton2m365 <command> ...`."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import sys
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import auth as auth_mod
from . import graph_store, imap_store, pipeline
from . import proton_export as pe
from . import receipt as receipt_mod
from . import status as st
from . import targets as targets_mod
from .lease import LeaseHeld as _LeaseHeld
from .locks import FileLock, LockHeld
from .sample_export import SAMPLE_MARKER
from .state import StateError, open_state

MB = 1024 * 1024
_SHOW = 10  # how many example IDs to print per problem list
DEFAULT_STATE = "state.sqlite"
ENV_STATE = "PROTON2M365_STATE"


def _state_arg(args: argparse.Namespace) -> str | None:
    """The --state value to repeat in printed commands (None when it is the default)."""
    value = getattr(args, "state", DEFAULT_STATE)
    return None if value == DEFAULT_STATE else value


def _same_command(argv: list[str] | None = None) -> str:
    import shlex

    return " ".join([_runner(), *(shlex.quote(a) for a in (sys.argv[1:] if argv is None else argv))])


def _status_line(args: argparse.Namespace):
    from .progress import StatusLine

    return StatusLine(getattr(args, "progress", "auto"))


def _runner() -> str:
    """How the user ran us, so printed commands work when pasted: `uvx proton2m365` or `proton2m365`."""
    exe = sys.executable.replace("\\", "/")
    return "uvx proton2m365" if "/uv/" in exe and ("/archive-v" in exe or "/builds-v" in exe) else "proton2m365"


def _invocation(args: argparse.Namespace) -> dict:
    """Options to repeat in Next: and resume lines: a non-default state, and the sign-in options in effect."""
    extra: list[str] = []
    for flag, attr in (("--client-id", "client_id"), ("--tenant", "tenant"), ("--account", "account")):
        if getattr(args, attr, None):
            extra += [flag, getattr(args, attr)]
    if getattr(args, "device_code", False):
        extra.append("--device-code")
    return {"prefix": _runner(), "state_arg": _state_arg(args), "extra": tuple(extra)}


def _print_next(state, args: argparse.Namespace) -> st.MigrationStatus:
    """Print the Next: line every command ends with, from the same rules verify uses."""
    s = st.migration_status(state.progress(), **_invocation(args))
    print(st.next_line(s))
    _NEXT[:] = [s]
    return s


_NEXT: list = []


def _next_fields() -> dict:
    return {"next": _NEXT[-1].next_command, "next_hint": _NEXT[-1].next_hint} if _NEXT else {}


def _exit_code_epilog() -> str:
    return "exit codes:\n" + "\n".join(f"  {code:>2}  {text}" for code, text in st.EXIT_CODES)


def _fmt_bytes(n: int) -> str:
    for unit, size in (("GB", 1024**3), ("MB", MB), ("KB", 1024)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n:,} B"


NAME_WIDTH = 44
NARROW = 80  # columns below which tables stack into `name: value` lines


def _columns() -> int:
    import shutil

    return shutil.get_terminal_size((100, 24)).columns


def _row(name: str, *values: str, mark: str = "") -> list[str]:
    """One table row. On a narrow terminal it stacks as `name: values`; a name too long for its column gets
    the numbers on the next line, so the name (often a folder path someone will copy) is never cut."""
    if _columns() < NARROW:
        return [f"  {name}: {' / '.join(v.strip() for v in values)}{mark}"]
    cells = " ".join(values)
    if len(name) > NAME_WIDTH:
        return [f"    {name}", f"    {'':<{NAME_WIDTH}} {cells}{mark}"]
    return [f"    {name:<{NAME_WIDTH}} {cells}{mark}"]


def _date(ts: int | None) -> str:
    return "?" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _examples(ids: list[str]) -> str:
    if not ids:
        return ""
    more = f" (+{len(ids) - _SHOW} more)" if len(ids) > _SHOW else ""
    return "  e.g. " + ", ".join(ids[:_SHOW]) + more


def _sidebar_lines(inv: pe.Inventory) -> list[str]:
    lines = ["Compare these totals with Proton's sidebar (switch Proton to message view, not conversations):"]

    system_ids = [lid for lid in pe.SIDEBAR_ORDER if lid in inv.labels or inv.label_counts[lid]]
    extra = sorted(
        {lid for lid in list(inv.labels) + list(inv.label_counts) if lid.isdigit()} - set(system_ids), key=int
    )
    lines.append("  System")
    for lid in system_ids + extra:
        lines.append(f"    {inv.label_name(lid):<28} {inv.label_counts[lid]:>9,}   [id {lid}]")

    for title, kind in (("Folders", "folder"), ("Labels", "label")):
        entries = sorted((lab for lab in inv.labels.values() if lab.kind == kind), key=lambda l: l.path.lower())
        if not entries:
            continue
        lines.append(f"  {title}")
        for lab in entries:
            depth = lab.path.count("/") if kind == "folder" else 0
            name = lab.path.rsplit("/", 1)[-1] if kind == "folder" else lab.name
            lines.append(f"    {'  ' * depth}{name:<{28 - 2 * depth}} {inv.label_counts[lab.id]:>9,}")

    others = sorted((lab for lab in inv.labels.values() if lab.kind not in ("system", "folder", "label")),
                    key=lambda l: l.name.lower())
    if others:
        lines.append("  Other label types")
        for lab in others:
            lines.append(f"    {lab.name:<28} {inv.label_counts[lab.id]:>9,}   [{lab.kind}]")
    return lines


def _draft_lines(inv: pe.Inventory) -> list[str]:
    lines = ["Draft check (Drafts label x Received/Sent bit x Imported bit):",
             "    in Drafts  Received/Sent  Imported   messages   outcome"]
    yn = {True: "yes", False: "no"}
    for (d, rs, imp), n in sorted(inv.draft_table.items(), key=lambda kv: (not kv[0][0], kv[0][1], kv[0][2])):
        outcome = pe.classify_draft_bits(d, rs, imp).value
        lines.append(f"    {yn[d]:<10} {yn[rs]:<14} {yn[imp]:<9} {n:>9,}   {outcome}")
    if inv.draft_disagreements:
        lines.append(f"  {len(inv.draft_disagreements):,} disagreements will be kept and reported.")
        lines.append(_examples(inv.draft_disagreements))
    top = inv.flag_values.most_common(12)
    lines.append("  Flags values seen: " + ", ".join(f"{flags}: {n:,}" for flags, n in top))
    return [l for l in lines if l]


def _completeness_lines(inv: pe.Inventory) -> list[str]:
    lines: list[str] = []
    sample = (inv.export_dir / SAMPLE_MARKER).exists()  # sample-export just wrote it; that is not the Export Tool
    if inv.seconds_since_modified is not None and inv.seconds_since_modified < pe.RECENTLY_MODIFIED_SECONDS \
            and not sample:
        lines.append(f"WARNING: the export folder changed {inv.seconds_since_modified:.0f}s ago. "
                     "Is the Export Tool still running? Re-run scan after it finishes.")
    missing = inv.missing_vs_log
    if missing is not None:
        if missing > 0:
            lines.append(f"WARNING: the Export Tool log expected {inv.expected_count:,} messages; "
                         f"{missing:,} are not in this folder. The export looks incomplete.")
        else:
            lines.append(f"Export Tool log expected {inv.expected_count:,} messages; all are present.")
    if inv.leftover_dirs:
        lines.append("WARNING: non-empty working folders left in the export: " + ", ".join(inv.leftover_dirs))
    return lines


def render_scan(inv: pe.Inventory) -> str:
    out: list[str] = [
        f"Proton export: {inv.export_dir}",
        f"Messages: {inv.message_count:,} paired .eml + .metadata.json, {_fmt_bytes(inv.total_bytes)} on disk, "
        f"dated {_date(inv.earliest)} to {_date(inv.latest)} (UTC)",
        *_completeness_lines(inv),
        "",
        *_sidebar_lines(inv),
        "",
        *_draft_lines(inv),
        "",
        f"Starred: {inv.starred:,}   Trash: {inv.label_counts[pe.TRASH]:,}   Spam: {inv.label_counts[pe.SPAM]:,}",
        f"Message-ID: {len(inv.missing_message_id):,} missing (one will be synthesized), "
        f"{len(inv.duplicate_message_ids):,} duplicated values",
        f"Long lines (> {inv.long_line_limit:,} bytes): {len(inv.long_line):,} to re-encode, "
        f"{len(inv.long_line_signed):,} signed/encrypted (will be skipped)",
        f"Oversize (> {_fmt_bytes(inv.max_size)}): {len(inv.oversize):,}",
        "Largest: " + ", ".join(f"{pid} ({_fmt_bytes(b)})" for b, pid in inv.largest[:5]),
        "",
        f"With default settings: upload {inv.upload_count:,} messages ({_fmt_bytes(inv.upload_bytes)}); skip "
        + (", ".join(f"{n:,} {reason.value}" for reason, n in sorted(inv.skip_counts.items())) or "none"),
        "  Check your mailbox quota has room for that, plus what is already there.",
        "",
    ]

    problems: list[str] = []
    if inv.orphan_eml:
        problems += [f"  .eml with no .metadata.json: {len(inv.orphan_eml):,}", _examples(inv.orphan_eml)]
    if inv.orphan_metadata:
        problems += [f"  .metadata.json with no .eml: {len(inv.orphan_metadata):,}", _examples(inv.orphan_metadata)]
    if inv.bad_metadata:
        problems.append(f"  unreadable files: {len(inv.bad_metadata):,}")
        problems += [f"    {name}: {err}" for name, err in inv.bad_metadata[:_SHOW]]
    if inv.partial_files:
        problems += [f"  empty or partial files: {len(inv.partial_files):,}", _examples(inv.partial_files)]
    if inv.unknown_label_ids:
        problems.append("  label IDs on messages but not in labels.json: "
                        + ", ".join(f"{lid} ({n:,})" for lid, n in inv.unknown_label_ids.most_common(_SHOW)))
    if inv.logs_checked:
        problems.append(f"  Export Tool logs checked: {len(inv.logs_checked)}, error lines: {inv.log_error_count:,}")
        problems += [f"    {p.name}:{n}: {t}" for p, n, t in inv.log_errors[:5]]
    elif not (inv.export_dir / SAMPLE_MARKER).exists():
        problems.append("  No Export Tool log found next to the export; confirm the export finished without errors.")

    if any(problems):
        out.append("Problems:" if any(p and not p.startswith("  No Export") for p in problems) else "Checks:")
        out += [p for p in problems if p]
    return "\n".join(out) + "\n"


def _progress(done: int, total: int) -> None:
    if done == total or done % 1000 == 0:
        print(f"\r  read {done:,}/{total:,} messages", end="" if done < total else "\n", file=sys.stderr)


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        export_dir = pe.resolve_export_dir(Path(args.export_dir))
        fmt = _check_format(export_dir, args)
        inv = pe.build_inventory(
            export_dir,
            max_size=int(args.max_size * MB),
            progress=_progress if sys.stderr.isatty() and not args.json else None,
        )
    except pe.ExportError as exc:
        print(f"scan: {exc}", file=sys.stderr)
        _json_error(args, exc)
        return st.EXIT_USAGE
    if args.json:
        json.dump({**inv.to_dict(), "command": "scan", "exit_code": st.EXIT_OK, "json_version": JSON_VERSION},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_scan(inv))
        print(f"Export format: {fmt}")
    return st.EXIT_OK


def _check_format(export_dir: Path, args: argparse.Namespace) -> str:
    try:
        return pe.detect_format(export_dir)
    except pe.UnknownExportFormat as exc:
        if not getattr(args, "allow_unknown_format", False):
            raise
        print(f"WARNING: {str(exc).split('. Please')[0]}; continuing because of --allow-unknown-format",
              file=sys.stderr)
        return "unknown (allowed)"


def render_plan(r: pipeline.PlanReport) -> str:
    """Selection first (what goes, what stays out and why), then where it goes; diagnostics last."""
    ps = r.settings
    yn = {True: "yes", False: "no"}
    dates = f", received {_date(r.first_date)} to {_date(r.last_date)}" if r.first_date is not None else ""
    out = [
        f"Plan for: {r.export_dir}",
        f"State: {r.state_path}" if r.state_path else "Dry run: nothing written",
        "",
        f"Upload {r.upload_count:,} of {r.total:,} messages ({_fmt_bytes(r.upload_bytes)}){dates}.",
        "Left out: " + (", ".join(f"{n:,} {reason}" for reason, n in sorted(r.skips.items(), key=lambda kv: -kv[1]))
                        or "nothing"),
    ]
    if r.excluded:
        out.append("  Excluding a label leaves out whole messages, including ones that also carry other labels.")
    if r.bad_files or r.orphans:
        out.append(f"  Unresolved export files: {len(r.bad_files):,} unreadable, {r.orphans:,} unpaired "
                   "(see scan; fix them, or leave them out with --exclude-file)")
    out.append(f"Settings: include trash {yn[ps.include_trash]}, include spam {yn[ps.include_spam]}, "
               f"max size {_fmt_bytes(ps.max_size)}, starred -> "
               + ("flag" if ps.starred_as_flag else f'category "{pe.STARRED_CATEGORY}"'))

    out += ["", f"Folders ({len(r.folders):,}), largest first:"]
    for folder, n in sorted(r.folders.items(), key=lambda kv: (-kv[1], [s.casefold() for s in kv[0]])):
        out += _row("/".join(folder), f"{n:>9,}")
    labels = [(name, n, "") for name, n in r.categories.items()]
    labels += [(name, n, "  excluded") for name, n in r.excluded.items()]
    out += ["", f"Labels ({len(labels):,}), largest first; kept ones become categories:"]
    for name, n, mark in sorted(labels, key=lambda x: (-x[1], x[0].casefold())):
        out += _row(name, f"{n:>9,}", mark=mark)
    if not labels:
        out.append("    (none)")
    if r.flagged:
        out.append(f"  Flagged (starred): {r.flagged:,}")

    notes_out = []
    if r.renamed_folders:
        notes_out.append("  Folder names changed for Exchange:")
        notes_out += [f"    {old}  ->  {new}" for old, new in sorted(r.renamed_folders.items())]
    if r.case_collisions:
        notes_out.append("  WARNING: these folders differ only by case and will merge in Exchange:")
        notes_out += [f"    {' | '.join(group)}" for group in r.case_collisions]
    if r.multi_folder:
        notes_out += [f"  {len(r.multi_folder):,} messages were in more than one Proton folder; "
                      "the first by path is used.", _examples(r.multi_folder)]
    if r.dropped_numeric:
        notes_out.append("  Dropped unnamed Proton labels (never categories): " + ", ".join(
            f"{lid} ({n:,})" for lid, n in sorted(r.dropped_numeric.items(), key=lambda kv: int(kv[0]))))
    if r.renamed_categories:
        notes_out.append("  Category names changed (no commas or semicolons): "
                         + "; ".join(f"{old!r} -> {new!r}" for old, new in sorted(r.renamed_categories.items())))
    notes = [
        (r.reencode, "have over-long lines; text parts will be re-encoded"),
        (r.synthesized_ids, "have no Message-ID; one will be added"),
        (r.no_date, "have no date; Exchange will stamp the upload time"),
        (r.draft_disagreements, "kept despite disagreeing draft signals"),  # draft diagnostics come last
    ]
    for ids, text in notes:
        if ids:
            notes_out += [f"  {len(ids):,} {text}", _examples(ids)]
    if r.mapped:
        notes_out.insert(0, "  Mapped: " + "; ".join(r.mapped))
    if notes_out:
        out += ["", "Notes:"] + notes_out

    rp = r.replan
    if rp is not None:
        out += ["", f"State: {rp.new:,} new, {rp.updated:,} updated, {rp.unchanged:,} unchanged"]
        if rp.reclassified:
            moves = Counter(f"{old} -> {new}" for _, old, new in rp.reclassified)
            out.append("  Re-classified: " + ", ".join(f"{n:,} {m}" for m, n in sorted(moves.items())))
        if rp.applied_to_uploaded:
            out.append(f"  {len(rp.applied_to_uploaded):,} changes to categories/flag will apply at the next tag run")
        if rp.drift:
            fields = Counter(d.field for d in rp.drift)
            out.append("  Already uploaded, NOT changed in the mailbox: "
                       + ", ".join(f"{n:,} {f}" for f, n in sorted(fields.items())))
            if rp.unresolved_drift:
                out.append(f"  {len(rp.unresolved_drift):,} of them keep verify at INCOMPLETE until you undo the "
                           "change or run plan again with --accept-drift")
            else:
                out.append("  (accepted with --accept-drift: verify checks them against what was uploaded)")
        if rp.to_prune:
            out.append(f"  {len(rp.to_prune):,} already uploaded are now left out; "
                       "`proton2m365 prune` moves them to Deleted Items:")
            for folder, n in sorted(rp.to_prune_by_folder.items(), key=lambda kv: (-kv[1], kv[0].casefold())):
                out += _row(folder, f"{n:>9,}")
        if rp.missing_from_export:
            out += [f"  {len(rp.missing_from_export):,} messages in state are missing from this export (kept in the "
                    "mailbox, not uploaded if they were not yet; listed in the receipt)",
                    _examples(rp.missing_from_export)]
    return "\n".join(out) + "\n"


def _parse_map(value: str, flag: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"{flag} takes FROM=TO, e.g. {flag} \"Projects=Clients\" (got {value!r})")
    src, dst = (s.strip() for s in value.split("=", 1))
    if not src:
        raise ValueError(f"{flag} needs something before '=' (got {value!r})")
    if flag == "--map-folder" and not dst.strip("/"):
        raise ValueError(f"--map-folder needs a destination folder after '=' (got {value!r})")
    return src.strip("/") if flag == "--map-folder" else src, dst.strip("/") if flag == "--map-folder" else dst


def _mappings(args: argparse.Namespace, saved: dict, reused: list[str]) -> dict[str, tuple[tuple[str, str], ...]]:
    """--map-folder / --map-label: flags, then [map.folders] / [map.labels] in config.toml, then what an earlier
    plan saved. A flag wins over the same key from the config or the saved plan."""
    try:
        config = auth_mod.load_config().get("map", {})
    except auth_mod.AuthError:
        config = {}
    out = {}
    for kind, flag, attr in (("folders", "--map-folder", "map_folder"), ("labels", "--map-label", "map_label")):
        merged: dict[str, tuple[str, str]] = {}
        for src, dst in [tuple(x) for x in saved.get(f"map_{kind}", [])]:
            merged[src.casefold()] = (src, dst)
        for src, dst in (config.get(kind) or {}).items():
            merged[src.casefold()] = _parse_map(f"{src}={dst}", flag)
        flags = [_parse_map(v, flag) for v in getattr(args, attr)]
        earlier = [m for k, m in merged.items() if k not in {s.casefold() for s, _ in flags}]
        reused += [f"{flag} {s}={d}" for s, d in earlier if [s, d] in saved.get(f"map_{kind}", [])]
        for src, dst in flags:
            merged[src.casefold()] = (src, dst)
        out[kind] = tuple(merged.values())
    return out


SELECTION_FLAGS = ("include_trash", "include_spam", "starred_as_flag")


def _saved_selection(args: argparse.Namespace) -> dict:
    """The selection an earlier plan saved in this state file (empty with --reset-selection or no state)."""
    if args.reset_selection or not Path(args.state).exists():
        return {}
    try:
        with open_state(Path(args.state), write=False) as state:
            return json.loads(state.get_meta("plan_settings") or "{}")
    except StateError:
        return {}


def cmd_plan(args: argparse.Namespace) -> int:
    saved = _saved_selection(args)
    reused = []
    for name in SELECTION_FLAGS:
        if getattr(args, name) is None:
            setattr(args, name, bool(saved.get(name, False)))
            if saved.get(name):
                reused.append(f"--{name.replace('_', '-')}")
    if args.max_size is None:
        args.max_size = saved["max_size"] / MB if saved.get("max_size") else pe.DEFAULT_MAX_SIZE_BYTES / MB
        if saved.get("max_size") and saved["max_size"] != pe.DEFAULT_MAX_SIZE_BYTES:
            reused.append(f"--max-size {args.max_size:g}")
    for flag, key, attr in (("--exclude-label", "exclude_labels", "exclude_label"),
                            ("--exclude-file", "exclude_files", "exclude_file")):
        earlier = [x for x in saved.get(key, []) if x not in getattr(args, attr)]
        if earlier:
            reused += [f"{flag} {x}" for x in earlier]
            setattr(args, attr, earlier + getattr(args, attr))
    try:
        maps = _mappings(args, saved, reused)
    except ValueError as exc:
        print(f"plan: {exc}", file=sys.stderr)
        return st.EXIT_USAGE
    if reused:
        print("Reusing the selection saved in this state file: " + " ".join(reused)
              + " (use --reset-selection to start from the defaults)")
    settings = pipeline.PlanSettings(
        include_trash=args.include_trash,
        include_spam=args.include_spam,
        max_size=int(args.max_size * MB),
        starred_as_flag=args.starred_as_flag,
        exclude_labels=tuple(args.exclude_label),
        exclude_files=tuple(args.exclude_file),
        map_folders=maps["folders"],
        map_labels=maps["labels"],
    )
    try:
        export_dir = pe.resolve_export_dir(Path(args.export_dir))
        fmt = _check_format(export_dir, args)
        report = pipeline.plan_export(
            export_dir, settings,
            state_path=None if args.dry_run else Path(args.state),
            progress=_progress if sys.stderr.isatty() else None,
            extra={"export_format": fmt}, accept_drift=args.accept_drift, accept_missing=args.accept_missing,
        )
    except (pe.ExportError, StateError) as exc:
        print(f"plan: {exc}", file=sys.stderr)
        _json_error(args, exc)
        return st.EXIT_USAGE
    _result(dry_run=args.dry_run, state=None if args.dry_run else str(Path(args.state).resolve()),
            export_format=fmt, messages=report.total, upload_count=report.upload_count,
            upload_bytes=report.upload_bytes, folders={"/".join(f): n for f, n in report.folders.items()},
            categories=dict(report.categories), skipped=dict(report.skips), excluded_labels=report.excluded,
            unresolved_files=len(report.bad_files) + report.orphans)
    sys.stdout.write(render_plan(report))
    if args.dry_run:
        import shlex

        print("Next: " + " ".join(["proton2m365", "plan", shlex.quote(args.export_dir), *_plan_flags(args)])
              + "  (the same command without --dry-run)")
        return st.EXIT_OK
    with open_state(Path(args.state), write=False) as state:
        _print_next(state, args)
    return st.EXIT_OK


def _plan_flags(args: argparse.Namespace) -> list[str]:
    import shlex

    flags = [f"--exclude-label {shlex.quote(x)}" for x in args.exclude_label]
    flags += [f for f, on in (("--include-trash", args.include_trash), ("--include-spam", args.include_spam),
                              ("--starred-as-flag", args.starred_as_flag)) if on]
    if _state_arg(args):
        flags.append(f"--state {shlex.quote(args.state)}")
    return flags


# --- commands that talk to Microsoft 365 -----------------------------------------------------------


class _Connections:
    """Sign in once, then build the IMAP and Graph stores on demand."""

    def __init__(self, args: argparse.Namespace, *, recorded: str | None = None):
        config = auth_mod.resolve_app_config(args.client_id, args.tenant)
        self.auth = auth_mod.Auth(config, device_code=args.device_code, account=getattr(args, "account", None))
        print(f"Signed in as {self.auth.sign_in(recorded=recorded)}", file=sys.stderr)
        if self.auth.is_personal:
            print(f"Outlook.com support is experimental in v0.1; please report what you see: {pe.ISSUES_URL}",
                  file=sys.stderr)
        self.imap: imap_store.ImapStore | None = None

    def imap_store(self, args: argparse.Namespace) -> imap_store.ImapStore:
        if self.imap is not None:
            return self.imap
        pacer = imap_store.Pacer(getattr(args, "max_msgs_per_sec", imap_store.DEFAULT_MSGS_PER_SEC),
                                 getattr(args, "max_bytes_per_sec", imap_store.DEFAULT_BYTES_PER_SEC))
        self.imap = imap_store.ImapStore(self.auth.username, self.auth.imap_token, pacer=pacer, say=print)
        self.imap.connect()
        return self.imap

    def graph(self) -> graph_store.GraphStore:
        return graph_store.GraphStore(self.auth.graph_token, say=print)

    def close(self) -> None:
        if self.imap is not None:
            self.imap.close()


ACCOUNT_META = "account"
WRITING_COMMANDS = frozenset({"upload", "tag", "prune", "dedupe"})


def _recorded_account(state) -> dict | None:
    raw = state.get_meta(ACCOUNT_META)
    return json.loads(raw) if raw else None


def _bind_account(state, auth, args: argparse.Namespace) -> None:
    """A state file belongs to one mailbox. Refuse any other; record it on the first write.

    State that already has mail in a mailbox but no recorded account (made before accounts were recorded)
    is bound only with --bind-account, after the user has seen which account and how many messages.
    """
    recorded = _recorded_account(state)
    if recorded:
        if recorded["home_account_id"] != auth.home_account_id:
            raise auth_mod.AuthError(
                f"P2M-ACCOUNT-MISMATCH: this state file belongs to {recorded['username']}, but you are signed in as {auth.username}. "
                f"Run again with --account {recorded['username']}, or use a different --state for this mailbox")
        return
    progress = state.progress()
    in_mailbox = progress.in_mailbox + progress.waiting_prune
    if in_mailbox and not getattr(args, "bind_account", False):
        raise auth_mod.AuthError(
            f"P2M-ACCOUNT-UNBOUND: this state file says {in_mailbox:,} messages are already in a mailbox, but not which one. "
            f"If that mailbox is {auth.username}, run the command again with --bind-account")
    if args.command in WRITING_COMMANDS or getattr(args, "bind_account", False):
        state.record_meta(ACCOUNT_META, json.dumps({"home_account_id": auth.home_account_id,
                                                    "username": auth.username}))
        print(f"This state file now belongs to {auth.username}.")


PERSONAL_LIMIT_BYTES = 14_000_000_000  # GB = 10^9 here and in the message
SIZE_ACK_META = "size_acknowledged_bytes"


def _size_preflight(state, auth, args: argparse.Namespace) -> None:
    """Print what this upload will send. A personal mailbox (15 GB free Outlook.com) over 14 GB needs --yes."""
    from .state import IN_MAILBOX, PLANNED, UPLOAD_FAILED

    this_run = state.bytes_in([UPLOAD_FAILED] if args.retry_failed else [PLANNED])
    already = state.bytes_in(IN_MAILBOX)
    total = this_run + already
    print(f"Size: this run {this_run / 1e9:,.2f} GB + already uploaded {already / 1e9:,.2f} GB "
          f"= {total / 1e9:,.2f} GB (available mailbox space is not checked)")
    if not auth.is_personal or total <= PERSONAL_LIMIT_BYTES:
        return
    acknowledged = int(state.get_meta(SIZE_ACK_META) or 0)
    if total <= acknowledged:
        return
    if not args.yes:
        raise _Refused(
            f"P2M-SIZE: {auth.username} is a personal Microsoft account: free Outlook.com mailboxes hold 15 GB, "
            f"Microsoft 365 Personal/Family 50 GB; mail already in the mailbox is not counted. Nothing was "
            "uploaded. Exclude labels or folders and run plan again, add storage, or run again with --yes "
            "if the mailbox has room.")
    state.record_meta(SIZE_ACK_META, str(total))


class _Refused(Exception):
    pass


MAILBOX_LOCKED = frozenset({"upload", "prune", "dedupe"})  # commands that add or move mail over IMAP


class _MailboxBusy(Exception):
    pass


@contextmanager
def _mailbox_lock(auth, lock_dir: Path | None = None):
    """One writer per mailbox on this machine, whichever state file it uses (two state files could
    otherwise both decide a message is missing and upload it twice). Runs on other machines are not seen."""
    name = hashlib.sha256(auth.home_account_id.encode()).hexdigest()[:16]
    lock = FileLock((lock_dir or auth_mod.CONFIG_DIR / "locks") / f"mailbox-{name}.lock")
    try:
        lock.acquire()
    except LockHeld as exc:
        raise _MailboxBusy(f"P2M-MAILBOX-BUSY: another proton2m365 run is writing to {auth.username}: wait for pid {exc.holder or '?'} "
                           "to finish; if no proton2m365 run is active, run the command again") from None
    try:
        yield
    finally:
        lock.release()


def _connect(args: argparse.Namespace, **kwargs):
    """Sign in to the destination chosen with --target (see targets.py)."""
    return targets_mod.get(getattr(args, "target", targets_mod.DEFAULT_TARGET)).connect(args, **kwargs)


@contextmanager
def _mailbox_lease(conns, args: argparse.Namespace):
    """The lease in the mailbox, so a run on another computer is seen too (see lease.py)."""
    from .lease import MailboxLease

    lease = MailboxLease(conns.imap_store(args))
    lease.acquire(break_lease=getattr(args, "break_lease", False))
    try:
        yield lease
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001 - it expires by itself; never hide the run's own result
            pass


def _run_online(args: argparse.Namespace, body) -> int:
    """Open state (locked), sign in as its account, run `body(state, connections)`, turn known errors into messages."""
    conns = None
    try:
        with open_state(Path(args.state)) as state:
            print(f"State: {Path(args.state).resolve()}", file=sys.stderr)
            recorded = _recorded_account(state)
            conns = _connect(args, recorded=recorded["home_account_id"] if recorded else None)
            if args.command == "upload":
                if json.loads(state.get_meta("plan_settings") or "{}").get("sample") and \
                        not (args.sample_ok or args.yes):
                    raise _Refused("P2M-SAMPLE: this state was planned from the synthetic sample export: upload it "
                                   "only into a test mailbox, with --sample-ok")
                _size_preflight(state, conns.auth, args)  # before any write, and before binding
            _bind_account(state, conns.auth, args)
            if args.command in WRITING_COMMANDS:
                print(f"Mailbox: {conns.auth.username}")
            if args.command in MAILBOX_LOCKED:
                with _mailbox_lock(conns.auth), _mailbox_lease(conns, args) as lease:
                    args.heartbeat = lease.renew
                    return body(state, conns)
            return body(state, conns)
    except (StateError, auth_mod.AuthError, pe.ExportError, graph_store.GraphError, imap_store.ImapError,
            _MailboxBusy, _Refused, _LeaseHeld) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        _json_error(args, exc)
        return st.EXIT_USAGE
    finally:
        if conns is not None:
            conns.close()


JSON_VERSION = 1  # bumped only when a field changes meaning or goes away (added fields keep the version)


def _json_error(args: argparse.Namespace, exc: Exception) -> None:
    """With --json, a failure is also one JSON object on stdout: {"error": {code, message, fix, retryable}}."""
    if not getattr(args, "json", False):
        return
    text = str(exc)
    code = text.split(":", 1)[0] if text.startswith("P2M-") else "P2M-ERROR"
    message = text.split(": ", 1)[1] if text.startswith("P2M-") and ": " in text else text
    retryable = code in ("P2M-STATE-LOCKED", "P2M-MAILBOX-BUSY", "P2M-AUTH-TIMEOUT", "P2M-AUTH-TOKEN")
    out = _JSON_OUT[-1] if _JSON_OUT else sys.stdout
    _ERRORED.append(True)
    json.dump({"error": {"code": code, "message": message, "fix": f"see docs/errors.md#{code.lower()}",
                         "retryable": retryable, "context": {"command": args.command}},
               "exit_code": st.EXIT_USAGE, "json_version": JSON_VERSION}, out)
    out.write("\n")


def _list_ids(pairs, limit: int = _SHOW) -> str:
    ids = [p[0] if isinstance(p, tuple) else p for p in pairs]
    return _examples(ids)


def cmd_upload(args: argparse.Namespace) -> int:
    def body(state, conns):
        if not state.rows_to_upload(retry_failed=args.retry_failed):
            print("Nothing to upload.")
            _print_next(state, args)
            return st.EXIT_OK
        imap = conns.imap_store(args)
        line = _status_line(args)
        imap.say = line.say
        r = pipeline.upload_run(state, imap, retry_failed=args.retry_failed, limit=args.limit,
                                say=line.say, status=line, heartbeat=getattr(args, "heartbeat", None))
        print(f"Uploaded {r.sent:,} ({_fmt_bytes(r.bytes_sent)}), {r.already_there:,} were already there, "
              f"in {r.seconds / 60:.1f} min ({r.rate_per_hour:,.0f} msgs/h). {r.remaining:,} still planned.")
        for label, items in (("rejected by Exchange", r.rejected), ("gave up after retries", r.gave_up),
                             ("unreadable .eml", r.unreadable)):
            if items:
                print(f"  {len(items):,} {label}:" + _list_ids(items))
        if r.source_changed:
            print(f"  {len(r.source_changed):,} not uploaded because the export file changed after plan "
                  "(run plan again to read it):" + _list_ids(r.source_changed))
        _result(sent=r.sent, already_there=r.already_there, bytes_sent=r.bytes_sent, remaining=r.remaining,
                per_hour=round(r.rate_per_hour, 1), stopped=r.stopped,
                rejected=[x[0] for x in r.rejected], gave_up=[x[0] for x in r.gave_up],
                unreadable=[x[0] for x in r.unreadable], source_changed=r.source_changed)
        if r.stopped:
            print(f"Stopped: {r.stopped}")
            if not r.stopped.startswith("mailbox full"):
                print(f"Stopped safely after {r.sent + r.already_there:,} messages. Run `{_same_command()}` again "
                      "to resume; nothing will be sent twice.")
            _print_next(state, args)
            return st.EXIT_MAILBOX_FULL if r.stopped.startswith("mailbox full") else st.EXIT_INTERRUPTED
        if r.sent or r.already_there:
            print("Mail is in the mailbox; categories and flags are not applied yet.")
        _print_next(state, args)
        return st.EXIT_INCOMPLETE if (r.rejected or r.gave_up or r.unreadable or r.source_changed
                                      or r.remaining) else st.EXIT_OK
    return _run_online(args, body)


def cmd_tag(args: argparse.Namespace) -> int:
    def body(state, conns):
        if not state.rows_to_tag(retry_failed=args.retry_failed):
            print("Nothing to tag.")
            _print_next(state, args)
            return st.EXIT_OK
        settings = json.loads(state.get_meta("plan_settings") or "{}")
        colors = pipeline.label_colors(Path(settings["export_dir"]) if settings.get("export_dir") else None)
        line = _status_line(args)
        r = pipeline.tag_run(state, conns.graph(), retry_failed=args.retry_failed, colors=colors,
                             say=line.say, status=line)
        line.clear()
        _result(tagged=r.tagged, unmatched=list(r.unmatched), patch_errors=list(r.patch_errors),
                now_failed=list(r.now_failed))
        print(f"Tagged {r.tagged:,}.")
        if r.unmatched:
            print(f"  {len(r.unmatched):,} not found in Graph yet (retried next run):" + _list_ids(r.unmatched))
        if r.patch_errors:
            print(f"  {len(r.patch_errors):,} could not be updated:" + _list_ids(r.patch_errors))
        if r.now_failed:
            print(f"  {len(r.now_failed):,} are now tag_failed; `tag --retry-failed` tries them again.")
        _print_next(state, args)
        return st.EXIT_INCOMPLETE if (r.unmatched or r.patch_errors) else st.EXIT_OK
    return _run_online(args, body)


def cmd_prune(args: argparse.Namespace) -> int:
    def body(state, conns):
        todo = state.rows_to_prune()
        if not todo:
            print("Nothing to remove.")
            _print_next(state, args)
            return st.EXIT_OK
        print(f"Moving {sum(map(len, todo.values())):,} uploaded messages the plan now skips to Deleted Items:")
        r = pipeline.prune_run(state, conns.imap_store(args))
        _result(removed=r.removed, not_found=list(r.not_found))
        print(f"Removed {r.removed:,}.")
        if r.not_found:
            print(f"  {len(r.not_found):,} were not in their folder any more (marked skipped):"
                  + _list_ids(r.not_found))
        _print_next(state, args)
        return st.EXIT_OK
    return _run_online(args, body)


def cmd_dedupe(args: argparse.Namespace, *, ask: Callable[[str], str] = input) -> int:
    def body(state, conns):
        imap = conns.imap_store(args)
        report = pipeline.find_duplicates(state, imap, args.folder)
        pending = json.loads(state.get_meta(pipeline.DEDUPE_PENDING) or "[]")
        if report.other_ids:
            print(f"{report.other_ids:,} duplicated messages were not uploaded by this state; left alone.")
        if not report.found and not pending:
            print("No extra copies found.")
            _result(extra_copies=0, moved=0, by_folder={})
            _print_next(state, args)
            return st.EXIT_OK
        if report.found:
            print(f"{report.extra_copies:,} extra copies of {len(report.found):,} messages:")
            for folder, n in sorted(report.by_folder().items(), key=lambda kv: kv[0].casefold()):
                print(f"  {folder}: {n:,}")
            print("  for example:" + _list_ids([d.proton_id for d in report.found]))
        if args.dry_run:
            _result(extra_copies=report.extra_copies, moved=0, by_folder=report.by_folder(), dry_run=True,
                    ids=[d.proton_id for d in report.found])
            print("Nothing moved (--dry-run).")
            return st.EXIT_OK
        if report.found and not args.yes:
            if args.json or not sys.stdin.isatty():
                raise _Refused(f"P2M-CONFIRM-NEEDED: dedupe would move {report.extra_copies:,} extra copies in "
                               f"{len(report.by_folder()):,} folders to Deleted Items and cannot ask here; run it "
                               "with --dry-run to list them, then with --yes")
            answer = ask(f"Move {report.extra_copies:,} extra copies to Deleted Items (one copy of each message "
                         "stays; Deleted Items may be emptied automatically by your retention policy)? "
                         "Type yes to continue: ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing moved.")
                return st.EXIT_OK
        pipeline.dedupe_run(state, imap, report)
        _result(extra_copies=report.extra_copies, moved=len(report.moved), by_folder=report.by_folder(),
                repaired=report.repaired)
        print(f"Moved {len(report.moved):,} extra copies to Deleted Items."
              + (f" Reset {len(report.repaired):,} messages left by an interrupted dedupe." if report.repaired else ""))
        print("The kept copies are tagged again by the next `tag`, then checked by `verify`.")
        _print_next(state, args)
        return st.EXIT_OK
    return _run_online(args, body)


_LAST_RESULT: list[dict] = []   # what a command reports with --json (the last one wins)
_JSON_OUT: list = []            # the real stdout while prose is redirected to stderr


def _result(**fields) -> None:
    _LAST_RESULT.append(fields)


def cmd_verify(args: argparse.Namespace) -> int:
    def body(state, conns):
        export_dir = pipeline.check_export_unchanged(state)
        export = pipeline.export_accounting(state, export_dir)
        r = pipeline.verify_run(state, conns.graph(), sample=args.sample, check_all=args.all, status=_status_line(args),
                                imap=conns.imap_store(args) if args.all else None, content=args.content,
                                export=export, invocation=_invocation(args))
        s = r.status
        run_id, now = f"{random.getrandbits(32):08x}", time.time()
        summary = receipt_mod.summary_lines(r, state, run_id, now)
        print(summary[0])                                            # the outcome comes first
        p = r.progress
        print(f"Checked {r.checked:,} messages this run: {r.passed:,} match, "
              f"{len({m[0] for m in r.mismatches}):,} do not. "
              f"In the mailbox and checked: {p.verified:,} of {p.in_mailbox:,}.")
        for pid, what, want, got in r.mismatches[:_SHOW]:
            print(f"    {pid}: {what}: expected {want!r}, got {got!r}")
        print("Folders (planned vs in mailbox; extra mail already there is fine):")
        for folder, want, got in r.folders:
            flag = "  SHORT" if got < want else ""
            print("\n".join(_row(folder, f"{want:>9,}", f"{got:>9,}", mark=flag)))
        for line in summary[2:-1]:                                   # totals, assurance, unresolved files
            print(line)
        for reason in s.reasons:
            print(f"  - {reason}")
        json_path, txt_path = receipt_mod.receipt_paths(Path(args.state), Path(args.receipt) if args.receipt else None)
        errors = receipt_mod.write_receipt(r, state, json_path, txt_path, run_id=run_id, now=now,
                                           mailbox=conns.auth.username)
        csv_error = _write_verify_csv(Path(args.report), r, run_id)
        if csv_error:
            errors.errors[args.report] = csv_error
        for path, reason in errors.errors.items():
            print(f"could not write {path}: {reason}", file=sys.stderr)
        if str(json_path) not in errors.errors:
            print(f"Receipt: {json_path.resolve()}")
        if str(txt_path) not in errors.errors:
            print(f"Summary: {txt_path.resolve()}")
        html = receipt_mod.html_path(json_path)
        if str(html) not in errors.errors:
            print(f"Receipt page: {html.resolve()}")
        if args.report not in errors.errors:
            print(f"Problems (CSV): {args.report}")
        print(st.next_line(s))
        code = st.EXIT_NOT_SAVED if errors and s.exit_code == st.EXIT_OK else s.exit_code
        _result(outcome=str(s.outcome), sentence=s.sentence, run_id=run_id,
                checked_this_run=r.checked, mismatched=len({m[0] for m in r.mismatches}),
                migrated=p.in_mailbox, verified=p.verified, skipped=p.skipped, reasons=s.reasons,
                next=s.next_command, next_hint=s.next_hint,
                artifacts={"receipt": str(json_path.resolve()), "summary": str(txt_path.resolve()),
                           "page": str(receipt_mod.html_path(json_path).resolve()),
                           "csv": args.report, "errors": errors.errors})
        return code
    return _run_online(args, body)


def _write_verify_csv(path: Path, r: pipeline.VerifyReport, run_id: str) -> str | None:
    """Same rules as the receipt: temporary file renamed into place, owner-only. Returns an error or None."""
    import csv

    tmp = path.with_name(path.name + f".{run_id}.tmp")
    try:
        with receipt_mod.open_private(tmp) as fh:
            w = csv.writer(fh)
            w.writerow(["kind", "proton_id_or_folder", "check", "expected", "actual", "run_id"])
            for pid, what, want, got in r.mismatches:
                w.writerow(["mismatch", pid, what, want, got, run_id])
            for folder, want, got in r.folders:
                w.writerow(["folder", folder, "count", want, got, run_id])
            for pid, stage, error in r.failed_rows:
                w.writerow([stage, pid, "", "", error, run_id])
            if r.export is not None:
                for name in r.export.unresolved:
                    w.writerow(["export-unresolved", name, "", "", "", run_id])
                for pid in r.export.missing_from_export:
                    w.writerow(["missing-from-export", pid, "", "", "", run_id])
            for pid in r.duplicates:
                w.writerow(["duplicate", pid, "copies", 1, "more than 1", run_id])
        receipt_mod.replace_atomically(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return exc.strerror or str(exc)
    return None


def cmd_sample_export(args: argparse.Namespace) -> int:
    from .sample_export import write_sample_export

    try:
        export_dir = write_sample_export(Path(args.dir), with_orphan=args.with_orphan)
    except FileExistsError as exc:
        print(f"sample-export: {exc}", file=sys.stderr)
        return st.EXIT_USAGE
    print(f"Wrote a synthetic Proton export (40 messages, no real mail) to {export_dir}")
    print("Next: proton2m365 scan " + _q(str(export_dir)) + "  then  proton2m365 plan --dry-run " + _q(str(export_dir)))
    return st.EXIT_OK


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)


def _error_code(exc: Exception) -> str:
    """Only the P2M- code of an error: its text can hold paths and addresses, which doctor never prints."""
    text = str(exc)
    return text.split(":", 1)[0] + " (see docs/errors.md)" if text.startswith("P2M-") else type(exc).__name__


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as dr

    if args.issue and args.show_names:
        print("doctor: --issue always redacts names; leave out --show-names", file=sys.stderr)
        return st.EXIT_USAGE
    redact = dr.Redactor(dr.install_salt(auth_mod.CONFIG_DIR), show_names=args.show_names)
    state_path = Path(args.state)
    auth, auth_error = None, None
    try:
        config = auth_mod.resolve_app_config(args.client_id, args.tenant)
        auth = auth_mod.Auth(config, account=args.account)
    except auth_mod.AuthError as exc:
        auth_error = _error_code(exc)
    imap = None

    def online(redact):
        nonlocal imap
        lines = []
        try:
            graph_store.GraphStore(auth.graph_token).request("GET", "/me/mailFolders/inbox")
            lines.append("Graph: reachable")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Graph: failed ({dr.error_kind(str(exc))}, {type(exc).__name__})")
        try:
            imap = imap_store.ImapStore(auth.username, auth.imap_token)
            imap.connect()
            lines.append("IMAP: reachable")
            if state is not None:
                folders = state.upload_folders()
                present = sum(1 for f in folders if imap.has_folder(f))
                lines.append(f"planned folders present in the mailbox: {present:,} of {len(folders):,}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"IMAP: failed ({dr.error_kind(str(exc))}, {type(exc).__name__})")
        return lines

    state = None
    ctx = open_state(state_path, write=False) if state_path.exists() else None
    try:
        if ctx is not None:
            state = ctx.__enter__()
        if auth is not None:
            try:
                recorded = json.loads(state.get_meta("account"))["home_account_id"] if state is not None \
                    and state.get_meta("account") else None
                if not auth.sign_in_silent(recorded=recorded):
                    auth_error = "no cached sign-in"
            except auth_mod.AuthError as exc:
                auth_error = _error_code(exc)
        sources = dict(getattr(args, "setting_sources", {}))
        if auth is not None:
            sources["client_id"] = auth.config.client_id_source
        sections = dr.build_report(
            settings_sources=sources,
            state=state, state_path=state_path, auth=auth, auth_error=auth_error, online_checks=online,
            redact=redact, crash_dirs=dr.crash_dirs(state_path, auth_mod.CONFIG_DIR),
            version=receipt_mod.tool_version())
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
        if imap is not None:
            imap.close()
    _result(sections={s.title: s.lines for s in sections})
    sys.stdout.write(dr.render_issue(sections) if args.issue else dr.render(sections))
    return st.EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    try:
        with open_state(Path(args.state), write=False) as state:
            s = pipeline.status_report(state)
            progress = state.progress()
    except StateError as exc:
        print(f"status: {exc}", file=sys.stderr)
        _json_error(args, exc)
        return st.EXIT_USAGE
    ms = st.migration_status(progress, **_invocation(args))
    _result(state=str(Path(args.state).resolve()), stages=s.stage_counts, skipped=s.skip_counts,
            migrated=progress.in_mailbox, verified=progress.verified, remaining_bytes=s.remaining_bytes,
            last_upload=s.last_upload, eta=s.eta, next=ms.next_command, next_hint=ms.next_hint)
    total = sum(s.stage_counts.values())
    print(f"State: {Path(args.state).resolve()} ({total:,} messages)")
    for stage in ("planned", "appended", "tagged", "verified", "skipped", "upload_failed", "tag_failed"):
        if s.stage_counts.get(stage):
            print(f"  {stage:<14} {s.stage_counts[stage]:>9,}")
    if s.skip_counts:
        print("  skipped: " + ", ".join(f"{n:,} {reason}" for reason, n in sorted(s.skip_counts.items())))
    if s.last_upload:
        lu = s.last_upload
        print(f"Last upload run: {lu['sent']:,} sent at {lu['per_hour']:,.0f} msgs/h. "
              f"Remaining {s.stage_counts.get('planned', 0):,} messages ({_fmt_bytes(s.remaining_bytes)}): "
              f"about {s.eta} at that rate.")
    for pid, stage, error in s.failed_rows[:_SHOW]:
        print(f"  {stage}: {pid}: {error}")
    try:
        with open_state(Path(args.state), write=False) as state:
            _print_next(state, args)
    except StateError:
        pass
    return st.EXIT_OK


TAG_RETRIES, TAG_RETRY_WAIT = 3, 60  # Graph can take a minute to see newly uploaded mail


def cmd_migrate(args: argparse.Namespace, *, wait: Callable[[float], None] = time.sleep,
                ask: Callable[[str], str] = input) -> int:
    """The whole migration in one resumable command: scan, plan, confirm, selftest, upload, tag, verify.

    Every step is the same command a person would run, so running migrate again continues where it stopped:
    plan is kept, nothing is uploaded twice, and verify only reports.
    """
    online = []
    for flag, attr in (("--client-id", "client_id"), ("--tenant", "tenant"), ("--account", "account"),
                       ("--target", "target"), ("--progress", "progress")):
        if getattr(args, attr, None):
            online += [flag, str(getattr(args, attr))]
    online += [f for f, on in (("--device-code", args.device_code), ("--break-lease", args.break_lease)) if on]
    state = ["--state", args.state]
    steps = 6

    def step(n: int, title: str) -> None:
        print(f"\n== Step {n}/{steps}: {title} ==")

    step(1, "check the export")
    if (code := main(["scan", args.export_dir, *(["--allow-unknown-format"] if args.allow_unknown_format else [])])) \
            != st.EXIT_OK:
        return code
    step(2, "plan")
    recorded_export = None
    if Path(args.state).exists():
        with open_state(Path(args.state), write=False) as s:
            recorded_export = json.loads(s.get_meta("plan_settings") or "{}").get("export_dir")
    try:
        new_export = pe.resolve_export_dir(Path(args.export_dir)).resolve()
    except pe.ExportError:
        new_export = Path(args.export_dir).resolve()
    if recorded_export and Path(recorded_export).resolve() != new_export:
        print(f"{new_export} is a different export from the one planned ({recorded_export}): planning it as a "
              "top-up of the same mailbox (only new messages are uploaded).")
        if (code := main(["plan", args.export_dir, *state,
                          *(["--accept-missing"] if args.accept_missing else [])])) != st.EXIT_OK:
            return code
    elif Path(args.state).exists():
        print(f"Using the plan in {Path(args.state).resolve()} (run `plan` yourself to change the selection).")
    else:
        plan_flags = [x for label in args.exclude_label for x in ("--exclude-label", label)]
        plan_flags += [f for f, on in (("--include-trash", args.include_trash), ("--include-spam", args.include_spam),
                                       ("--starred-as-flag", args.starred_as_flag)) if on]
        if (code := main(["plan", args.export_dir, *state, *plan_flags])) != st.EXIT_OK:
            return code
    with open_state(Path(args.state), write=False) as s:
        progress = s.progress()
        to_send = s.remaining_upload_bytes()
        selftest_done = s.get_meta("migrate_selftest") == "passed"
    if progress.planned and not args.yes:
        if (not sys.stdin.isatty() or getattr(args, "json", False)) and ask is input:
            refusal = _Refused(f"P2M-CONFIRM-NEEDED: migrate would upload {progress.planned:,} messages and cannot "
                               "ask here; confirm with --yes")
            print(f"migrate: {refusal}", file=sys.stderr)
            _json_error(args, refusal)
            return st.EXIT_USAGE
        answer = ask(f"\nUpload {progress.planned:,} messages ({_fmt_bytes(to_send)}) into the mailbox you sign in "
                     "to next? Type yes to continue: ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing uploaded. Run migrate again when you are ready.")
            return st.EXIT_OK
    step(3, "selftest (first time only)")
    if selftest_done or args.skip_selftest:
        print("Already passed." if selftest_done else "Skipped (--skip-selftest).")
    else:
        if (code := main(["selftest", *online])) != st.EXIT_OK:
            print("selftest did not pass: nothing was uploaded. Fix what it reports and run migrate again.")
            return code
        with open_state(Path(args.state)) as s:
            s.record_meta("migrate_selftest", "passed")
    step(4, "upload")
    yes = ["--yes"] if args.yes else []
    code = main(["upload", *state, *online, *yes, *(["--sample-ok"] if args.sample_ok else [])])
    if code in (st.EXIT_USAGE, st.EXIT_MAILBOX_FULL, st.EXIT_INTERRUPTED, st.EXIT_ERROR):
        return code  # its own message says what to do; migrate again resumes
    step(5, "tag")
    for attempt in range(1, TAG_RETRIES + 1):
        code = main(["tag", *state, *online])
        if code != st.EXIT_INCOMPLETE or attempt == TAG_RETRIES:
            break
        print(f"Some messages are not visible in Microsoft Graph yet; trying again in {TAG_RETRY_WAIT}s.")
        wait(TAG_RETRY_WAIT)
    if code in (st.EXIT_USAGE, st.EXIT_ERROR):
        return code
    step(6, "verify everything")
    return main(["verify", "--all", *state, *online])


def cmd_selftest(args: argparse.Namespace) -> int:
    conns = None
    try:
        conns = _connect(args)
        imap = conns.imap_store(args)
        if args.cleanup:
            print("Removing folders left by earlier selftest runs (only folders holding nothing but selftest mail):")
            _, kept = pipeline.cleanup_selftest_leftovers(imap)
            return st.EXIT_INCOMPLETE if kept else st.EXIT_OK
        print(f"Testing in {conns.auth.username}. Running live checks with synthetic messages "
              "(your real mail is not touched):")
        checks = pipeline.run_selftest(imap, conns.graph(), keep=args.keep)
    except (auth_mod.AuthError, graph_store.GraphError, imap_store.ImapError) as exc:
        print(f"selftest: {exc}", file=sys.stderr)
        _json_error(args, exc)
        return st.EXIT_USAGE
    finally:
        if conns is not None:
            conns.close()
    failed = [c for c in checks if not c.ok]
    not_run = sum(1 for c in checks if not c.ran)
    _result(checks=[{"name": c.name, "ok": c.ok, "ran": c.ran, "detail": c.detail} for c in checks],
            passed=len(checks) - len(failed), failed=len(failed) - not_run, not_run=not_run)
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed"
          + (f", {not_run} not run" if not_run else "") + "." + (" SELFTEST FAILED" if failed else ""))
    return st.EXIT_FAILED if failed else st.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proton2m365", description=__doc__, epilog=_exit_code_epilog(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true",
                         help="print the result as one JSON object on stdout (everything else goes to stderr)")

    scan = sub.add_parser("scan", help="inventory a Proton export (read-only, no network)")
    scan.add_argument("export_dir", help="the Proton export folder (or a folder containing exactly one)")
    scan.add_argument("--max-size", type=float, default=pe.DEFAULT_MAX_SIZE_BYTES / MB, metavar="MB",
                      help="largest message to upload, in MB; set it to your mailbox MaxReceiveSize (default 35)")
    scan.add_argument("--json", action="store_true", help="print the inventory as JSON")
    scan.add_argument("--allow-unknown-format", action="store_true",
                      help="scan an export whose format proton2m365 does not recognise")
    scan.set_defaults(func=cmd_scan)

    plan = sub.add_parser("plan", parents=[as_json], help="decide folder, categories and flags for every message; write state.sqlite")
    plan.add_argument("export_dir", help="the Proton export folder (or a folder containing exactly one)")
    plan.add_argument("--state", default=None, help="state database to write (default ./state.sqlite, or PROTON2M365_STATE)")
    plan.add_argument("--exclude-label", action="append", default=[], metavar="LABEL",
                      help="skip every message carrying this Proton label (name, path or ID); repeatable")
    plan.add_argument("--exclude-file", action="append", default=[], metavar="NAME",
                      help="leave this export file (or message id) out on purpose; it is listed in the receipt as "
                           "excluded-by-user. Repeatable")
    plan.add_argument("--map-folder", action="append", default=[], metavar="FROM=TO",
                      help='put a folder (and its subfolders) somewhere else, e.g. "Projects=Clients" or '
                           '"Proton Unfiled=Archive"; repeatable; or [map.folders] in config.toml')
    plan.add_argument("--map-label", action="append", default=[], metavar="FROM=TO",
                      help='rename a label\'s category, e.g. "Work=Office"; "Work=" drops the category; repeatable; '
                           "or [map.labels] in config.toml")
    plan.add_argument("--include-trash", action="store_true", default=None,
                      help='upload Trash into a "Proton Trash" folder')
    plan.add_argument("--include-spam", action="store_true", default=None, help='upload Spam into a "Proton Spam" folder')
    plan.add_argument("--starred-as-flag", action="store_true", default=None,
                      help='flag starred mail (creates To Do tasks) instead of a "Starred" category')
    plan.add_argument("--max-size", type=float, default=None, metavar="MB",
                      help="largest message to upload, in MB; set it to your mailbox MaxReceiveSize (default 35)")
    plan.add_argument("--reset-selection", action="store_true",
                      help="forget the selection an earlier plan saved (by default a re-plan reuses it)")
    plan.add_argument("--allow-unknown-format", action="store_true",
                      help="plan an export whose format proton2m365 does not recognise (recorded in the state)")
    plan.add_argument("--accept-drift", action="store_true",
                      help="keep already-uploaded messages as they are when this plan would put them elsewhere")
    plan.add_argument("--accept-missing", action="store_true",
                      help="accept that messages planned from an earlier export are missing from this one "
                           "(deleted in Proton or left out); they stay in the mailbox and are listed in the receipt")
    plan.add_argument("--dry-run", action="store_true", help="print the plan without writing state")
    plan.set_defaults(func=cmd_plan)

    online = argparse.ArgumentParser(add_help=False)
    online.add_argument("--client-id", help=f"your own Entra app (client) ID; or set {auth_mod.ENV_CLIENT_ID} "
                                            f"or client_id in {auth_mod.DEFAULT_CONFIG}")
    online.add_argument("--tenant", help="common (default), organizations, consumers, or a tenant ID or domain; "
                                         f"or set {auth_mod.ENV_TENANT}")
    online.add_argument("--device-code", action="store_true",
                        help="sign in with a device code instead of a browser (automatic over SSH or with no display)")
    online.add_argument("--target", default=targets_mod.DEFAULT_TARGET, choices=sorted(targets_mod.TARGETS),
                        help="where the mail goes (only microsoft365: Microsoft 365 and Outlook.com)")
    online.add_argument("--account", metavar="ADDRESS", help="which signed-in account to use when there are several")
    online.add_argument("--progress", choices=("auto", "plain", "none"), default="auto",
                        help="status line while working: auto (in place on a terminal), plain lines, or none")
    online.add_argument("--allow-sleep", action="store_true",
                        help="let the computer sleep during long commands (by default idle sleep is held off; "
                             "or set PROTON2M365_ALLOW_SLEEP=1)")
    online.add_argument("--no-notify", action="store_true",
                        help="no desktop notification when a long command ends (or set PROTON2M365_NO_NOTIFY=1)")
    online.add_argument("--bind-account", action="store_true",
                        help="record the signed-in account in a state file made before accounts were recorded")
    stateful = argparse.ArgumentParser(add_help=False)
    stateful.add_argument("--state", default=None, help="state database (default ./state.sqlite, or PROTON2M365_STATE)")
    rates = argparse.ArgumentParser(add_help=False)
    rates.add_argument("--max-msgs-per-sec", type=float, default=None, metavar="N",
                       help="ceiling for the upload rate, which starts at 2 msgs/s, rises while uploads go well "
                            "and halves when Exchange pushes back (default 6)")
    rates.add_argument("--max-bytes-per-sec", type=float, default=None, metavar="N",
                       help="ceiling for upload bytes per second (default 2 MB/s)")

    up = sub.add_parser("upload", parents=[online, stateful, rates, as_json], help="APPEND planned messages over IMAP")
    up.add_argument("--limit", type=int, help="upload at most N messages this run (e.g. a 1000-message trial)")
    up.add_argument("--retry-failed", action="store_true", help="retry only upload_failed messages")
    up.add_argument("--yes", action="store_true",
                    help="answer yes to every confirmation: more than 14 GB into a personal (Outlook.com) mailbox, "
                         "and uploading the synthetic sample")
    up.add_argument("--break-lease", action="store_true",
                    help="take over the mailbox lease another computer left behind (only if none is running)")
    up.add_argument("--sample-ok", action="store_true",
                    help="upload a state planned from `sample-export` (only into a test mailbox)")
    up.set_defaults(func=cmd_upload)

    tag = sub.add_parser("tag", parents=[online, stateful, as_json], help="set categories/flags through Graph")
    tag.add_argument("--retry-failed", action="store_true", help="retry only tag_failed messages")
    tag.set_defaults(func=cmd_tag)

    prune = sub.add_parser("prune", parents=[online, stateful, as_json],
                           help="move uploaded messages that the plan now skips to Deleted Items")
    prune.add_argument("--break-lease", action="store_true",
                       help="take over the mailbox lease another computer left behind (only if none is running)")
    prune.set_defaults(func=cmd_prune)

    ver = sub.add_parser("verify", parents=[online, stateful, as_json], help="check uploaded mail against the plan")
    ver.add_argument("--sample", type=int, default=200, help="messages to check (default 200)")
    ver.add_argument("--all", action="store_true", help="check every uploaded message")
    ver.add_argument("--content", action="store_true",
                     help="also download the checked messages and compare text, attachments, HTML text and links "
                          "with the export (with --all: every message, which downloads the whole mailbox)")
    ver.add_argument("--report", default="verify-report.csv", help="CSV of every problem found")
    ver.add_argument("--receipt", metavar="PATH",
                     help="where to write the receipt (default <state>.receipt.json next to the state file; "
                          "the plain-text summary goes beside it as .receipt.txt)")
    ver.set_defaults(func=cmd_verify)

    dd = sub.add_parser("dedupe", parents=[online, stateful, as_json],
                        help="move extra copies of uploaded messages to Deleted Items (one copy stays)")
    dd.add_argument("--dry-run", action="store_true", help="list the extra copies; move nothing")
    dd.add_argument("--yes", action="store_true", help="do not ask before moving (needed without a terminal)")
    dd.add_argument("--folder", action="append", default=[], metavar="PATH",
                    help='only this folder, as planned (e.g. "Archive" or "Projects/Alpha"); repeatable')
    dd.add_argument("--break-lease", action="store_true",
                    help="take over the mailbox lease another computer left behind (only if none is running)")
    dd.set_defaults(func=cmd_dedupe)

    stat = sub.add_parser("status", parents=[stateful, as_json], help="counts per stage, failures, rate and ETA (offline)")
    stat.set_defaults(func=cmd_status)

    mig = sub.add_parser("migrate", parents=[online, stateful, rates, as_json],
                         help="the whole migration in one resumable command (scan, plan, selftest, upload, tag, verify)")
    mig.add_argument("export_dir", help="the Proton export folder (or a folder containing exactly one)")
    mig.add_argument("--exclude-label", action="append", default=[], metavar="LABEL",
                     help="as for plan (used only when there is no plan yet)")
    mig.add_argument("--include-trash", action="store_true", help="as for plan")
    mig.add_argument("--include-spam", action="store_true", help="as for plan")
    mig.add_argument("--starred-as-flag", action="store_true", help="as for plan")
    mig.add_argument("--allow-unknown-format", action="store_true", help="as for plan")
    mig.add_argument("--yes", action="store_true", help="do not ask before uploading (and as for upload)")
    mig.add_argument("--sample-ok", action="store_true", help="as for upload")
    mig.add_argument("--skip-selftest", action="store_true", help="do not run selftest first")
    mig.add_argument("--accept-missing", action="store_true", help="as for plan (a newer export of the same mailbox)")
    mig.add_argument("--break-lease", action="store_true", help="as for upload")
    mig.set_defaults(func=cmd_migrate)

    sample = sub.add_parser("sample-export", help="write a synthetic Proton export to try proton2m365 safely")
    sample.add_argument("dir", help="an empty or new folder")
    sample.add_argument("--with-orphan", action="store_true", help="add one .eml without metadata")
    sample.set_defaults(func=cmd_sample_export)

    doc = sub.add_parser("doctor", parents=[stateful, as_json],
                         help="what an issue report needs; names and addresses are redacted")
    doc.add_argument("--issue", action="store_true", help="print the report as a GitHub issue body (always redacted)")
    doc.add_argument("--show-names", action="store_true", help="show names and addresses (for your own debugging)")
    doc.add_argument("--client-id", help="as for the other commands")
    doc.add_argument("--tenant", help="as for the other commands")
    doc.add_argument("--account", metavar="ADDRESS", help="as for the other commands")
    doc.set_defaults(func=cmd_doctor)

    self_test = sub.add_parser("selftest", parents=[online, rates, as_json],
                               help="live checks with synthetic messages in a temporary folder")
    self_test.add_argument("--keep", action="store_true", help="keep the test folder to inspect in Outlook")
    self_test.add_argument("--cleanup", action="store_true",
                           help="remove folders left by earlier selftest runs (--keep or interrupted), then exit")
    self_test.set_defaults(func=cmd_selftest)
    return parser


# Settings that may come from a flag, the environment or config.toml (first found wins), and their defaults.
SETTINGS = (
    ("state", "PROTON2M365_STATE", "state", lambda: DEFAULT_STATE, str),
    ("account", "PROTON2M365_ACCOUNT", "account", lambda: None, str),
    ("max_msgs_per_sec", "PROTON2M365_MAX_MSGS_PER_SEC", "max_msgs_per_sec",
     lambda: imap_store.DEFAULT_MSGS_PER_SEC, float),
    ("max_bytes_per_sec", "PROTON2M365_MAX_BYTES_PER_SEC", "max_bytes_per_sec",
     lambda: imap_store.DEFAULT_BYTES_PER_SEC, float),
)


def _resolve_settings(args: argparse.Namespace) -> None:
    """Fill settings the command line left out from the environment, then config.toml, then the default,
    and remember where each came from (doctor prints it)."""
    try:
        config = auth_mod.load_config()
    except auth_mod.AuthError:
        config = {}
    sources = {}
    for attr, env, key, default, kind in SETTINGS:
        if not hasattr(args, attr):
            continue
        if getattr(args, attr) is not None:
            sources[attr] = "flag"
        elif os.environ.get(env):
            setattr(args, attr, kind(os.environ[env]))
            sources[attr] = "environment"
        elif config.get(key) is not None:
            setattr(args, attr, kind(config[key]))
            sources[attr] = "config.toml"
        else:
            setattr(args, attr, default())
            sources[attr] = "default"
    args.setting_sources = sources


def _safe_console() -> None:
    """A console that cannot show a character (a Windows code page) prints an escape instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(errors="backslashreplace")


LONG_COMMANDS = frozenset({"upload", "tag", "prune", "dedupe", "migrate"})  # and verify --all
_DEPTH: list[int] = []  # migrate runs other commands through main(); only the outermost holds sleep off


def _is_long(args: argparse.Namespace) -> bool:
    return args.command in LONG_COMMANDS or (args.command == "verify" and getattr(args, "all", False))


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    words = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None) and "--json" in words:  # argparse already printed the usage to stderr
            json.dump({"error": {"code": "P2M-USAGE", "message": "the command line was not understood (see stderr)",
                                 "fix": "see docs/errors.md#p2m-usage", "retryable": False,
                                 "context": {"command": next((w for w in words if not w.startswith("-")), None)}},
                       "exit_code": st.EXIT_USAGE, "json_version": JSON_VERSION}, sys.stdout)
            sys.stdout.write("\n")
            return st.EXIT_USAGE
        raise
    _resolve_settings(args)
    if _DEPTH or not _is_long(args):
        return _dispatch(args, argv)
    from . import awake

    _DEPTH.append(1)
    try:
        with awake.KeepAwake(enabled=not getattr(args, "allow_sleep", False)):
            code = _dispatch(args, argv)
    finally:
        _DEPTH.pop()
    meaning = dict(st.EXIT_CODES).get(code, f"exit {code}")
    awake.notify(f"proton2m365 {args.command} finished", meaning, enabled=not getattr(args, "no_notify", False))
    return code


def _dispatch(args: argparse.Namespace, argv: list[str] | None) -> int:
    if getattr(args, "json", False) and args.command != "scan":
        return _run_json(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(f"Stopped safely. Run `{_same_command(argv)}` again to continue; nothing will be done twice.",
              file=sys.stderr)
        return st.EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - the last line of defence: log it, say where, exit 1
        return _crash(exc, args)


def _run_json(args: argparse.Namespace) -> int:
    """--json: everything a command prints goes to stderr; stdout gets exactly one JSON object, the command's
    result (with its exit code and Next: command) or {"error": ...}."""
    _LAST_RESULT.clear()
    _NEXT.clear()
    _ERRORED.clear()
    _JSON_OUT.append(sys.stdout)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            try:
                code = args.func(args)
            except KeyboardInterrupt:
                code = st.EXIT_INTERRUPTED
            except Exception as exc:  # noqa: BLE001
                code = _crash(exc, args)
                _json_error(args, exc)
    finally:
        out = _JSON_OUT.pop()
    if _LAST_RESULT or not _ERRORED:  # an error already printed {"error": ...}; anything else prints a result
        json.dump({"command": args.command, "exit_code": code, "json_version": JSON_VERSION, **_next_fields(),
                   **(_LAST_RESULT[-1] if _LAST_RESULT else {})}, out, default=str)
        out.write("\n")
    return code


_ERRORED: list = []


def _crash(exc: Exception, args: argparse.Namespace) -> int:
    from . import doctor as dr

    state = getattr(args, "state", None)
    directory = Path(state).resolve().parent if state and Path(state).resolve().parent.is_dir() else auth_mod.CONFIG_DIR
    try:
        path = dr.write_crash_log(exc, directory, time.time())
        where = f"details in {path}"
    except OSError:
        path, where = None, "the crash log could not be written"
    print(f"unexpected error {type(exc).__name__}: {where}; when reporting it, attach `proton2m365 doctor --issue` "
          "output, not the log, unless asked", file=sys.stderr)
    return st.EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
