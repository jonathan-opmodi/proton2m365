"""Stage orchestration: plan (and later upload, tag, verify, selftest)."""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import proton_export as pe
from .graph_store import MatchTarget, tag_body
from .imap_store import (
    AppendRejected, ImapError, QuotaExceeded, UploadGaveUp, UploadResult, is_connection_error, prepare_message,
)
from .sample_export import SAMPLE_MARKER
from .state import (
    PLANNED, TAG_FAILED, UPLOAD_FAILED, PlanRow, ReplanResult, State, StateError, WorkRow, open_state,
)
from .status import CheckFacts, ExportAccounting, MigrationStatus, Outcome, Progress, migration_status
from .targets import MailStore, Organizer


@dataclass(frozen=True)
class PlanSettings:
    include_trash: bool = False
    include_spam: bool = False
    max_size: int = pe.DEFAULT_MAX_SIZE_BYTES
    long_line_limit: int = pe.LONG_LINE_LIMIT
    starred_as_flag: bool = False
    exclude_labels: tuple[str, ...] = ()
    exclude_files: tuple[str, ...] = ()  # file names or message ids, from plan --exclude-file
    map_folders: tuple[tuple[str, str], ...] = ()  # (destination folder, new destination), from --map-folder
    map_labels: tuple[tuple[str, str], ...] = ()   # (category, new name or "" to drop), from --map-label


@dataclass
class PlanReport:
    export_dir: Path
    settings: PlanSettings
    state_path: Path | None                       # None for a dry run
    excluded: dict[str, int] = field(default_factory=dict)          # label path -> messages it was on
    dropped_numeric: Counter[str] = field(default_factory=Counter)  # unnamed numeric labels, never categories
    total: int = 0
    upload_count: int = 0
    upload_bytes: int = 0
    folders: Counter[tuple[str, ...]] = field(default_factory=Counter)
    renamed_folders: dict[str, str] = field(default_factory=dict)
    case_collisions: list[list[str]] = field(default_factory=list)
    multi_folder: list[str] = field(default_factory=list)
    categories: Counter[str] = field(default_factory=Counter)
    renamed_categories: dict[str, str] = field(default_factory=dict)
    flagged: int = 0
    skips: Counter[str] = field(default_factory=Counter)
    draft_disagreements: list[str] = field(default_factory=list)
    reencode: list[str] = field(default_factory=list)
    synthesized_ids: list[str] = field(default_factory=list)
    no_date: list[str] = field(default_factory=list)
    bad_files: list[tuple[str, str]] = field(default_factory=list)
    orphans: int = 0
    replan: ReplanResult | None = None
    mapped: list[str] = field(default_factory=list)  # --map-folder / --map-label in effect
    first_date: int | None = None                 # received-date range of the messages to upload
    last_date: int | None = None


def _folder_renames(labels: dict[str, pe.Label]) -> dict[str, str]:
    """Proton folder path -> Exchange path, for every folder whose name had to change."""
    renames = {}
    for lab in labels.values():
        if lab.kind != "folder":
            continue
        created = "/".join(pe.folder_segments(lab.id, labels))
        if created != lab.path:
            renames[lab.path] = created
    return renames


def _case_collisions(folders: Iterable[tuple[str, ...]]) -> list[list[str]]:
    """Folder paths that differ only by case; Exchange folder names are case-insensitive."""
    by_key: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for path in folders:
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            by_key.setdefault(tuple(s.casefold() for s in prefix), set()).add(prefix)
    return sorted(sorted("/".join(p) for p in group) for group in by_key.values() if len(group) > 1)


def map_folder(folder: tuple[str, ...], mappings: tuple[tuple[str, str], ...]) -> tuple[tuple[str, ...], str | None]:
    """The folder after --map-folder: the longest matching prefix wins, so subfolders follow their parent.
    Returns (new folder, the mapping key used or None)."""
    path = [s.casefold() for s in folder]
    best = None
    for src, dst in mappings:
        key = [s.casefold() for s in src.split("/") if s]
        if key and path[: len(key)] == key and (best is None or len(key) > len(best[0])):
            best = (key, src, dst)
    if best is None:
        return folder, None
    key, src, dst = best
    return tuple(s for s in dst.split("/") if s) + folder[len(key):], src


def map_categories(categories: tuple[str, ...], mappings: dict[str, str], used: set[str]) -> tuple[str, ...]:
    """Categories after --map-label: renamed, or dropped when mapped to an empty name; no duplicates."""
    out: list[str] = []
    for name in categories:
        new = name
        if name.casefold() in mappings:
            used.add(name.casefold())
            new = pe.sanitize_category(mappings[name.casefold()]) if mappings[name.casefold()] else ""
        if new and new not in out:
            out.append(new)
    return tuple(out)


def build_plan_rows(
    export_dir: Path, settings: PlanSettings, *, progress: Callable[[int, int], None] | None = None,
) -> tuple[list[PlanRow], PlanReport]:
    """Decide folder, categories, flags and skip reason for every message. Reads only."""
    export_dir = Path(export_dir)
    labels = pe.load_labels(export_dir / pe.LABELS_FILE)
    excluded_ids = pe.resolve_label_selectors(settings.exclude_labels, labels)  # fails before reading mail
    excluded_files = {pe.file_stem(n) for n in settings.exclude_files}
    report = PlanReport(export_dir=export_dir, settings=settings, state_path=None)
    report.excluded = {labels[lid].path: 0 for lid in sorted(excluded_ids)}
    report.renamed_folders = _folder_renames(labels)

    listing = pe.list_export(export_dir)
    report.orphans = len(listing.orphan_eml) + len(listing.orphan_metadata)
    rows: list[PlanRow] = []
    label_map = {src.casefold(): dst for src, dst in settings.map_labels}
    used_folder_maps: set[str] = set()
    used_label_maps: set[str] = set()
    seen_folders: set[str] = set()
    seen_categories: set[str] = set()

    def bad(name: str, error: str) -> None:
        report.bad_files.append((name, error))

    for msg in pe.iter_messages(listing, with_sha256=True, progress=progress, on_bad_file=bad):
        meta, facts = msg.meta, msg.facts
        pid = meta.proton_id
        report.total += 1
        for lid in meta.label_ids:
            if lid in excluded_ids:
                report.excluded[labels[lid].path] += 1
            elif lid.isdigit() and lid not in labels and lid not in pe.SYSTEM_LABEL_NAMES:
                report.dropped_numeric[lid] += 1
        if pe.classify_draft(meta) is pe.DraftVerdict.KEEP_DISAGREEMENT:
            report.draft_disagreements.append(pid)

        skip = pe.default_skip_reason(
            meta, facts, max_size=settings.max_size, long_line_limit=settings.long_line_limit,
            include_trash=settings.include_trash, include_spam=settings.include_spam,
        )
        if pid in excluded_files:
            skip = pe.SkipReason.EXCLUDED_BY_USER
        elif skip is None and excluded_ids.intersection(meta.label_ids):
            skip = pe.SkipReason.EXCLUDED_LABEL
        folder, multiple = pe.target_folder(
            meta, labels, include_trash=settings.include_trash, include_spam=settings.include_spam,
        )
        seen_folders.add("/".join(folder))
        folder, used = map_folder(folder, settings.map_folders)
        if used:
            used_folder_maps.add(used)
        categories = pe.categories_for(
            meta, labels, excluded_ids=excluded_ids, starred_as_flag=settings.starred_as_flag,
        )
        seen_categories.update(categories)
        categories = map_categories(categories, label_map, used_label_maps)
        flagged = settings.starred_as_flag and pe.STARRED in meta.label_ids
        needs_reencode = facts.max_line > settings.long_line_limit and not facts.signed
        message_id = facts.message_id or pe.synthesized_message_id(pid)
        date = pe.internal_date(meta, facts)

        if skip is None:
            report.upload_count += 1
            report.upload_bytes += facts.size
            if date is not None:
                report.first_date = date if report.first_date is None else min(report.first_date, date)
                report.last_date = date if report.last_date is None else max(report.last_date, date)
            report.folders[folder] += 1
            report.categories.update(categories)
            report.flagged += flagged
            if multiple:
                report.multi_folder.append(pid)
            if needs_reencode:
                report.reencode.append(pid)
            if facts.message_id is None:
                report.synthesized_ids.append(pid)
            if date is None:
                report.no_date.append(pid)
        else:
            report.skips[skip.value] += 1

        rows.append(PlanRow(
            proton_id=pid,
            eml_path=str(msg.eml_path),
            sha256=facts.sha256 or "",
            size=facts.size,
            target_folder=folder,
            categories=categories,
            seen=not meta.unread,
            flagged=flagged,
            internal_date=date,
            message_id=message_id,
            synth_message_id=facts.message_id is None,
            needs_reencode=needs_reencode,
            skip_reason=skip.value if skip else None,
        ))

    unused = [f"--map-folder {src!r}" for src, _ in settings.map_folders if src not in used_folder_maps]
    unused += [f"--map-label {src!r}" for src, _ in settings.map_labels if src.casefold() not in used_label_maps]
    if unused:
        raise pe.ExportError(
            f"P2M-MAP-NO-MATCH: {', '.join(unused)} matches nothing in this export. Folders: "
            + ", ".join(sorted(seen_folders, key=str.casefold)) + ". Categories: "
            + (", ".join(sorted(seen_categories, key=str.casefold)) or "(none)"))
    report.mapped = [f"{src} -> {dst or '(no category)'}" for src, dst in (*settings.map_folders, *settings.map_labels)]

    for lab in labels.values():
        if lab.kind == "label" and lab.id not in excluded_ids:
            cleaned = pe.sanitize_category(lab.path or lab.name)
            if cleaned != (lab.path or lab.name):
                report.renamed_categories[lab.path or lab.name] = cleaned
    report.case_collisions = _case_collisions(report.folders)
    return rows, report


class ConfirmNeeded(pe.ExportError):
    """A plan would change state in a way the user must accept first (P2M-CONFIRM-NEEDED)."""


def check_same_mailbox(state: State, rows: list[PlanRow], export_dir: Path, *, accept_missing: bool) -> None:
    """A newer export of the same mailbox shares Proton message ids with the state (they are unique per message).
    Refuse an export sharing none; ask before marking messages as missing from the export."""
    known = state.all_ids()
    if not known:
        return
    ids = {r.proton_id for r in rows}
    if not ids & known:
        raise pe.ExportError(
            f"P2M-EXPORT-CHANGED: the export at {export_dir} shares no message with this state's "
            f"{len(known):,} messages, so it is a different mailbox. Use a new --state for it")
    active = state.active_ids()
    newly_missing = sorted(active - ids)
    if newly_missing and not accept_missing:
        shown = ", ".join(newly_missing[:3]) + (" ..." if len(newly_missing) > 3 else "")
        raise ConfirmNeeded(
            f"P2M-CONFIRM-NEEDED: {len(newly_missing):,} of this state's {len(active):,} messages are missing "
            f"from the export at {export_dir} ({shown}). If they were deleted in Proton or left out of this "
            "export on purpose, run plan again with --accept-missing (they stay in the mailbox and are listed in "
            "the receipt); if the export is incomplete, export again from Proton")


def plan_export(
    export_dir: Path, settings: PlanSettings, *, state_path: Path | None,
    progress: Callable[[int, int], None] | None = None, extra: dict | None = None, accept_drift: bool = False,
    accept_missing: bool = False,
) -> PlanReport:
    """Plan every message and write it to state (unless `state_path` is None: dry run).
    Rows, settings and the export fingerprint are written in one transaction."""
    if state_path is None:
        return build_plan_rows(export_dir, settings, progress=progress)[1]
    with open_state(state_path, create=True) as state:  # lock before reading the export: no interleaved plans
        report = plan_into(state, export_dir, settings, progress=progress, extra=extra, accept_drift=accept_drift,
                           accept_missing=accept_missing)
        report.state_path = Path(state_path)
    return report


def plan_into(
    state: State, export_dir: Path, settings: PlanSettings, *, progress: Callable[[int, int], None] | None = None,
    extra: dict | None = None, accept_drift: bool = False, accept_missing: bool = False,
) -> PlanReport:
    """Plan an export into an open (locked) state. Nothing is written when a check refuses the export."""
    rows, report = build_plan_rows(export_dir, settings, progress=progress)
    check_same_mailbox(state, rows, Path(export_dir), accept_missing=accept_missing)
    recorded = asdict(settings) | {"export_dir": str(Path(export_dir)), **(extra or {}),
                                   "sample": (Path(export_dir) / SAMPLE_MARKER).exists()}
    report.replan = state.apply_plan(
        rows, settings=recorded, accept_drift=accept_drift,
        meta={"export_fingerprint": json.dumps(pe.export_fingerprint(export_dir))})
    return report


# --- upload -------------------------------------------------------------------------------------


@dataclass
class UploadReport:
    attempted: int = 0
    sent: int = 0
    already_there: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    gave_up: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    source_changed: list[str] = field(default_factory=list)  # export file differs from what plan read
    created_folders: list[str] = field(default_factory=list)
    stopped: str | None = None          # why the run ended early (quota, Ctrl-C, connection)
    seconds: float = 0.0
    bytes_sent: int = 0
    remaining: int = 0

    @property
    def rate_per_hour(self) -> float:
        return self.sent / self.seconds * 3600 if self.seconds > 0 else 0.0


def _eta(remaining: int, per_hour: float) -> str:
    if per_hour <= 0:
        return "unknown"
    hours = remaining / per_hour
    return f"{hours * 60:.0f} min" if hours < 1 else f"{hours:.1f} h"


def upload_run(
    state: State, imap: MailStore, *, retry_failed: bool = False, limit: int | None = None,
    say: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic, progress_seconds: float = 10,
    status=None, heartbeat: Callable[[], None] | None = None,
) -> UploadReport:
    """APPEND planned rows (or upload_failed rows with retry_failed). Safe to stop and re-run at any point."""
    report = UploadReport()
    if retry_failed:
        state.reset_attempts(UPLOAD_FAILED)
    rows = state.rows_to_upload(retry_failed=retry_failed)
    if limit is not None:
        rows = rows[:limit]
    report.created_folders = imap.ensure_folders({r.target_folder for r in rows})
    for name in report.created_folders:
        say(f"Created folder {name}")
    start = last_line = clock()
    try:
        for i, row in enumerate(rows, 1):
            report.attempted += 1
            try:
                raw = Path(row.eml_path).read_bytes()
            except OSError as exc:
                state.record_upload_failure(row.proton_id, f"cannot read {row.eml_path}: {exc}", permanent=True)
                report.unreadable.append((row.proton_id, str(exc)))
                continue
            if row.sha256 and hashlib.sha256(raw).hexdigest() != row.sha256:
                state.mark_source_changed(row.proton_id, f"{row.eml_path} differs from what plan read")
                report.source_changed.append(row.proton_id)
                continue
            message = prepare_message(
                raw, proton_id=row.proton_id, needs_reencode=row.needs_reencode,
                synthesized_message_id=row.message_id if row.synth_message_id else None,
            )
            try:
                result = _upload_one(imap, row, message)
            except QuotaExceeded as exc:
                report.stopped = f"mailbox full: {exc}"
                break
            except AppendRejected as exc:
                state.record_upload_failure(row.proton_id, f"rejected: {exc}", permanent=True)
                report.rejected.append((row.proton_id, str(exc)))
                continue
            except UploadGaveUp as exc:
                state.record_upload_failure(row.proton_id, str(exc))
                report.gave_up.append((row.proton_id, str(exc)))
                continue
            state.mark_appended(row.proton_id, result.uid)
            if heartbeat is not None:
                heartbeat()  # keeps the mailbox lease alive during long uploads
            if result.already_present:
                report.already_there += 1
            else:
                report.sent += 1
                report.bytes_sent += len(message)
            report.seconds = clock() - start
            line = (f"  upload {i:,}/{len(rows):,}, {report.bytes_sent / 1e6:,.1f} MB, "
                    f"{report.rate_per_hour:,.0f} msgs/h (pacing at up to {imap.pacer.msgs_per_sec:g} msgs/s), "
                    f"about {_eta(len(rows) - i, report.rate_per_hour)} left in this run")
            if status is not None:
                status.update(line, force=i == len(rows))
            elif clock() - last_line >= progress_seconds or i == len(rows):  # a line at least every 10 s
                last_line = clock()
                say(line)
    except KeyboardInterrupt:
        report.stopped = "interrupted (Ctrl-C); re-run upload to continue"
    except Exception as exc:  # noqa: BLE001 - a dropped connection between messages ends the run cleanly
        if not is_connection_error(exc):
            raise
        report.stopped = f"connection lost twice: {exc}; re-run upload to continue"
    if status is not None:
        status.clear()
    report.seconds = clock() - start
    report.remaining = len(state.rows_to_upload())
    if report.sent:  # an empty run must not wipe out the measured rate status uses for its ETA
        state.record_meta("last_upload", json.dumps({
            "sent": report.sent, "seconds": round(report.seconds, 1), "per_hour": round(report.rate_per_hour, 1),
            "bytes": report.bytes_sent, "at": int(time.time()),
        }))
    return report


def _upload_one(imap: MailStore, row: WorkRow, message: bytes) -> UploadResult:
    """One message; if the connection drops outside the APPEND itself (e.g. listing the folder), reconnect once."""
    try:
        return imap.upload(row.target_folder, row.proton_id, message, seen=row.seen, internal_date=row.internal_date)
    except Exception as exc:  # noqa: BLE001
        if not is_connection_error(exc):
            raise
        imap.reconnect()
        return imap.upload(row.target_folder, row.proton_id, message, seen=row.seen, internal_date=row.internal_date)


# --- tag ----------------------------------------------------------------------------------------


@dataclass
class TagReport:
    tagged: int = 0
    unmatched: list[str] = field(default_factory=list)
    patch_errors: list[tuple[str, str]] = field(default_factory=list)
    now_failed: list[str] = field(default_factory=list)
    categories_created: list[str] = field(default_factory=list)


@dataclass
class PruneReport:
    removed: int = 0
    not_found: list[str] = field(default_factory=list)  # not in the folder any more: nothing to remove


def prune_run(state: State, imap: MailStore, *, say: Callable[[str], None] = print) -> PruneReport:
    """Move uploaded messages that the current plan skips to Deleted Items, then mark them skipped."""
    report = PruneReport()
    for folder, pids in state.rows_to_prune().items():
        say(f"  {'/'.join(folder)}: removing {len(pids):,}")
        moved = imap.remove(folder, pids)
        for pid in pids:
            state.mark_pruned(pid)
            if pid in moved:
                report.removed += 1
            else:
                report.not_found.append(pid)
    return report


DEDUPE_PENDING = "dedupe_pending"  # meta: ids whose extra copies are being moved (repaired on the next run)


@dataclass
class DuplicateSet:
    folder: tuple[str, ...]
    proton_id: str
    keep: int
    extra: list[int]
    validity: int | None


@dataclass
class DedupeReport:
    found: list[DuplicateSet] = field(default_factory=list)
    other_ids: int = 0          # duplicated ids this state does not know (left alone)
    moved: list[tuple[str, int]] = field(default_factory=list)   # (folder, uid) moved to Deleted Items
    repaired: list[str] = field(default_factory=list)            # ids reset after an interrupted run

    @property
    def extra_copies(self) -> int:
        return sum(len(d.extra) for d in self.found)

    def by_folder(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.found:
            name = "/".join(d.folder)
            out[name] = out.get(name, 0) + len(d.extra)
        return out


def find_duplicates(state: State, imap: MailStore, folders: Iterable[str] = ()) -> DedupeReport:
    """Messages of this state with more than one copy in their planned folder. The copy to keep is the one
    state recorded at upload (its UID), else the lowest UID. Copies of ids the state does not know are left alone."""
    report = DedupeReport()
    wanted = {f.casefold() for f in folders}
    known = state.all_ids()
    for folder in sorted(state.in_mailbox_counts()):
        if wanted and "/".join(folder).casefold() not in wanted:
            continue
        dupes = imap.duplicates(folder)
        validity = imap.uid_validity(folder)
        for pid, uids in sorted(dupes.items()):
            if pid not in known:
                report.other_ids += 1
                continue
            recorded = state.recorded_uid(pid)
            keep = recorded if recorded in uids else min(uids)
            report.found.append(DuplicateSet(folder, pid, keep, [u for u in uids if u != keep], validity))
    return report


def dedupe_run(state: State, imap: MailStore, report: DedupeReport, *, say: Callable[[str], None] = print) -> None:
    """Move the extra copies found by find_duplicates, then send each message back to tag (see mark_deduped).

    The ids are recorded before anything moves, so if the run stops after a MOVE but before the state update,
    the next run (or dedupe with nothing left to move) still resets them."""
    pending = sorted(set(json.loads(state.get_meta(DEDUPE_PENDING) or "[]")) | {d.proton_id for d in report.found})
    state.record_meta(DEDUPE_PENDING, json.dumps(pending))
    by_folder: dict[tuple[tuple[str, ...], int | None], list[DuplicateSet]] = {}
    for d in report.found:
        by_folder.setdefault((d.folder, d.validity), []).append(d)
    for (folder, validity), sets in sorted(by_folder.items(), key=lambda kv: kv[0][0]):
        uids = [u for d in sets for u in d.extra]
        say(f"  {'/'.join(folder)}: moving {len(uids):,} extra copies to Deleted Items")
        moved = imap.move_uids(folder, uids, validity=validity)
        report.moved += [("/".join(folder), u) for u in moved]
        for d in sets:
            state.mark_deduped(d.proton_id, d.keep)
    for pid in pending:
        if pid not in {d.proton_id for d in report.found}:
            try:
                state.mark_deduped(pid, state.recorded_uid(pid))  # left over from an interrupted run
                report.repaired.append(pid)
            except StateError:
                pass  # already reset, or no longer in the mailbox
    state.record_meta(DEDUPE_PENDING, "[]")


def label_colors(export_dir: Path | None) -> dict[str, str | None]:
    """Category name -> Proton label color, so master categories get a similar Outlook color."""
    if export_dir is None:
        return {}
    try:
        labels = pe.load_labels(Path(export_dir) / pe.LABELS_FILE)
    except pe.ExportError:
        return {}
    return {pe.sanitize_category(lab.path or lab.name): lab.color for lab in labels.values() if lab.kind == "label"}


def _stage(status, say: Callable[[str], None], text: str) -> None:
    """A stage change: always shown on the status line (or said, when there is none)."""
    if status is not None:
        status.update(text, force=True)
    else:
        say(text)


def tag_run(
    state: State, graph: Organizer, *, retry_failed: bool = False, colors: dict[str, str | None] | None = None,
    say: Callable[[str], None] = print, status=None,
) -> TagReport:
    """Find each appended message in Graph and set its categories/flag. Never re-uploads."""
    report = TagReport()
    if retry_failed:
        state.reset_attempts(TAG_FAILED)
    rows = state.rows_to_tag(retry_failed=retry_failed)
    wanted = {c: (colors or {}).get(c) for r in rows for c in r.categories}
    if wanted:
        report.categories_created = graph.ensure_categories(wanted)
        for name in report.categories_created:
            say(f"Created category {name}")
    by_folder: dict[tuple[str, ...], list[WorkRow]] = {}
    for row in rows:
        by_folder.setdefault(row.target_folder, []).append(row)
    for folder, folder_rows in sorted(by_folder.items()):
        folder_id = state.folder_id(folder)
        if folder_id is None:
            folder_id = graph.resolve_folder(folder)
            state.set_folder_id(folder, folder_id)
        _stage(status, say, f"  tag: {'/'.join(folder)}: matching {len(folder_rows):,} messages")
        match = graph.match(folder_id, [MatchTarget(r.proton_id, r.message_id, r.internal_date) for r in folder_rows])
        changes = {match.matched[r.proton_id]: tag_body(r.categories, r.flagged)
                   for r in folder_rows if r.proton_id in match.matched and (r.categories or r.flagged)}
        errors = graph.patch_messages(changes) if changes else {}
        for row in folder_rows:
            gid = match.matched.get(row.proton_id)
            if gid is None:
                stage = state.record_tag_miss(row.proton_id, "graph-unmatched")
                report.unmatched.append(row.proton_id)
            elif gid in errors:
                stage = state.record_tag_miss(row.proton_id, f"patch failed: {errors[gid]}")
                report.patch_errors.append((row.proton_id, errors[gid]))
            else:
                state.mark_tagged(row.proton_id, gid)
                report.tagged += 1
                continue
            if stage == TAG_FAILED:
                report.now_failed.append(row.proton_id)
    return report


# --- verify -------------------------------------------------------------------------------------

VERIFY_SELECT = "receivedDateTime,isRead,flag,categories,isDraft,parentFolderId"
DATE_TOLERANCE_SECONDS = 60


@dataclass
class VerifyReport:
    checked: int = 0
    passed: int = 0
    mismatches: list[tuple[str, str, str, str]] = field(default_factory=list)  # id, check, expected, actual
    folders: list[tuple[str, int, int]] = field(default_factory=list)          # folder, expected, in mailbox
    failed_rows: list[tuple[str, str, str]] = field(default_factory=list)
    skip_counts: dict[str, int] = field(default_factory=dict)
    progress: Progress = field(default_factory=Progress)
    export: ExportAccounting | None = None
    status: MigrationStatus | None = None
    duplicates: list[str] = field(default_factory=list)  # ids with more than one copy (verify --all only)
    content_checked: int = 0                              # messages whose content was compared (--content)
    content_bytes: int = 0

    @property
    def folders_short(self) -> list[tuple[str, int, int]]:
        return [f for f in self.folders if f[2] < f[1]]

    @property
    def ok(self) -> bool:
        return self.status is not None and self.status.outcome == Outcome.PASSED


def check_export_unchanged(state: State) -> Path | None:
    """The export this state was planned from must still be there and be the same one (labels.json).
    Raises pe.ExportError (exit 2) otherwise; returns the export folder."""
    settings = json.loads(state.get_meta("plan_settings") or "{}")
    if not settings.get("export_dir"):
        return None
    export_dir = Path(settings["export_dir"])
    recorded = json.loads(state.get_meta("export_fingerprint") or "null")
    if not (export_dir / pe.LABELS_FILE).is_file():
        raise pe.ExportError(f"P2M-EXPORT-CHANGED: the export this state was planned from is not at {export_dir} "
                             "any more. Put it back, or run `proton2m365 plan <new location>` with the same "
                             "export, then run this again")
    if recorded and pe.export_fingerprint(export_dir)["labels_sha256"] != recorded["labels_sha256"]:
        raise pe.ExportError(f"P2M-EXPORT-CHANGED: the export at {export_dir} is not the one this state was "
                             "planned from (its labels.json differs). Use the original export, or a new --state "
                             "for a different one")
    return export_dir


def export_accounting(state: State, export_dir: Path | None) -> ExportAccounting:
    """Compare the export folder with state: files nobody planned, and planned rows whose files are gone."""
    if export_dir is None or not export_dir.is_dir():
        return ExportAccounting(available=False)
    try:
        listing = pe.list_export(export_dir)
    except OSError:
        return ExportAccounting(available=False)
    known = state.all_ids()
    settings = json.loads(state.get_meta("plan_settings") or "{}")
    excluded = {pe.file_stem(n) for n in settings.get("exclude_files", [])}
    unresolved = sorted(
        [f"{stem}{pe.EML_SUFFIX}" for stem in listing.orphan_eml]
        + [f"{stem}{pe.METADATA_SUFFIX}" for stem in listing.orphan_metadata]
        + list(listing.partial_files)
        + [f"{stem}{pe.EML_SUFFIX}" for stem in listing.paired if stem not in known]  # plan could not read it
    )
    missing = sorted(state.active_ids() - set(listing.paired))
    unplanned = tuple(stem for stem in listing.paired if stem not in known)
    excluded_files = tuple(n for n in unresolved if pe.file_stem(n) in excluded)
    unresolved = [n for n in unresolved if pe.file_stem(n) not in excluded]
    return ExportAccounting(unresolved=tuple(unresolved), missing_from_export=tuple(missing),
                            unplanned_pairs=unplanned, excluded_files=excluded_files)


def _check_content(row: WorkRow, graph: Organizer) -> list[tuple[str, str, str]]:
    from . import content

    try:
        source = Path(row.eml_path).read_bytes()
    except OSError as exc:
        return [("content: export file", "readable", f"cannot read: {exc.strerror or exc}")]
    return content.compare(source, graph.mime(row.graph_id))


def _check_message(row: WorkRow, msg: dict | None, folder_id: str | None) -> list[tuple[str, str, str]]:
    if msg is None:
        return [("exists", "in mailbox", "not found in Graph")]
    problems = []
    received = msg.get("receivedDateTime")
    if row.internal_date is not None:
        try:
            got = datetime.fromisoformat(received.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            got = None
        if got is None or abs(got - row.internal_date) > DATE_TOLERANCE_SECONDS:
            want = datetime.fromtimestamp(row.internal_date, UTC).isoformat()
            problems.append(("received date", want, str(received)))
    if bool(msg.get("isRead")) != row.seen:
        problems.append(("read", str(row.seen), str(msg.get("isRead"))))
    flagged = (msg.get("flag") or {}).get("flagStatus") == "flagged"
    if flagged != row.flagged:
        problems.append(("flag", str(row.flagged), str(flagged)))
    want_cats = sorted(c.casefold() for c in row.categories)
    got_cats = sorted(c.casefold() for c in msg.get("categories") or [])
    if want_cats != got_cats:
        problems.append(("categories", ", ".join(row.categories), ", ".join(msg.get("categories") or [])))
    if msg.get("isDraft"):
        problems.append(("not a draft", "False", "True"))
    if folder_id is not None and msg.get("parentFolderId") not in (None, folder_id):
        problems.append(("folder", "/".join(row.target_folder), str(msg.get("parentFolderId"))))
    return problems


def verify_run(
    state: State, graph: Organizer, *, sample: int = 200, check_all: bool = False,
    export: ExportAccounting | None = None, rng: random.Random | None = None,
    clock: Callable[[], float] | None = None, invocation: dict | None = None, status=None,
    imap: MailStore | None = None, content: bool = False,
) -> VerifyReport:
    """Check uploaded messages, then decide the outcome from cumulative state (see status.migration_status)."""
    report = VerifyReport(failed_rows=state.failed_rows(), skip_counts=state.skip_counts(),
                          export=export or ExportAccounting(available=False))
    rows = state.rows_to_verify()
    # Rows whose last check failed are re-checked every run, so a failure is never forgotten by sampling.
    known_bad = state.failed_check_ids()
    recheck = [r for r in rows if r.proton_id in known_bad]
    rest = [r for r in rows if r.proton_id not in known_bad]
    chosen = recheck + (rest if check_all or len(rest) <= sample else (rng or random.Random()).sample(rest, sample))
    now = int((clock or time.time)())
    folder_ids: dict[tuple[str, ...], str] = {}
    for folder in {r.target_folder for r in chosen} | set(state.in_mailbox_counts()):
        folder_ids[folder] = state.folder_id(folder) or graph.resolve_folder(folder)
        state.set_folder_id(folder, folder_ids[folder])
    _stage(status, lambda _: None, f"  verify: reading {len(chosen):,} messages from Microsoft Graph")
    messages = graph.get_messages([r.graph_id for r in chosen], VERIFY_SELECT) if chosen else {}
    if content and chosen:
        report.content_bytes = sum(r.size for r in chosen)
        _stage(status, lambda _: None, f"  verify: downloading {len(chosen):,} messages "
                                       f"({report.content_bytes / 1e6:,.1f} MB) to compare their content")
    for i, row in enumerate(chosen, 1):
        report.checked += 1
        problems = _check_message(row, messages.get(row.graph_id), folder_ids.get(row.target_folder))
        if content and messages.get(row.graph_id) is not None and row.source_present:
            problems += _check_content(row, graph)  # rows missing from the newest export: presence only
            report.content_checked += 1
            if status is not None:
                status.update(f"  verify: content {i:,}/{len(chosen):,}")
        if problems:
            report.mismatches += [(row.proton_id, *p) for p in problems]
            state.mark_check_failed(row.proton_id, [p[0] for p in problems], now)
        else:
            state.mark_verified(row.proton_id, now)
            report.passed += 1
    for folder, expected in sorted(state.in_mailbox_counts().items()):
        _stage(status, lambda _: None, f"  verify: counting {'/'.join(folder)}")
        report.folders.append(("/".join(folder), expected, graph.folder_total(folder_ids[folder])))
    if imap is not None:  # every copy over IMAP: catches a message uploaded twice
        known = state.all_ids()
        for folder in sorted(state.in_mailbox_counts()):
            _stage(status, lambda _: None, f"  verify: looking for duplicates in {'/'.join(folder)}")
            report.duplicates += sorted(pid for pid in imap.duplicates(folder) if pid in known)
    if status is not None:
        status.clear()
    report.progress = state.progress()
    report.status = migration_status(
        report.progress, report.export,
        CheckFacts(mismatched=len({m[0] for m in report.mismatches}),
                   folders_short=tuple(f[0] for f in report.folders_short),
                   duplicated=tuple(report.duplicates)),
        **(invocation or {}))
    return report


# --- status -------------------------------------------------------------------------------------


@dataclass
class StatusReport:
    stage_counts: dict[str, int]
    skip_counts: dict[str, int]
    failed_rows: list[tuple[str, str, str]]
    remaining_bytes: int
    last_upload: dict | None
    plan_settings: dict | None

    @property
    def eta(self) -> str:
        per_hour = (self.last_upload or {}).get("per_hour") or 0
        return _eta(self.stage_counts.get(PLANNED, 0), per_hour)


def status_report(state: State) -> StatusReport:
    last = state.get_meta("last_upload")
    settings = state.get_meta("plan_settings")
    return StatusReport(
        stage_counts=state.stage_counts(), skip_counts=state.skip_counts(), failed_rows=state.failed_rows(),
        remaining_bytes=state.remaining_upload_bytes(),
        last_upload=json.loads(last) if last else None, plan_settings=json.loads(settings) if settings else None,
    )


# --- selftest (T7) ------------------------------------------------------------------------------

SELFTEST_ROOT = "_proton2m365_selftest"  # prefix; each run gets its own root folder
SELFTEST_LEAF = "Ünïcode ∕ test"
SELFTEST_CATEGORY = "proton2m365 selftest"
OLD_DATE = 1560000000  # 2019-06-08T13:20:00Z: the date that must survive


@dataclass(frozen=True)
class SelftestCase:
    key: str
    raw: bytes
    seen: bool
    internal_date: int
    message_id: str
    synthesized_message_id: str | None = None
    needs_reencode: bool = False
    categories: tuple[str, ...] = ()
    flagged: bool = False
    drop: str | None = None  # "after-store" / "before-store": simulate a connection drop around APPEND


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    ran: bool = True  # False: an earlier failure meant this could not be checked (never counts as a pass)


GRAPH_CHECKS = (
    "Graph finds the folder by its exact name (NFC)", "Graph sees every uploaded message",
    "X-Proton-Id readable through Graph", "categories and flag set through Graph",
    "original received date kept (2019-06-08)", "read/unread state kept", "nothing arrived as a draft",
    "missing Message-ID synthesized", "read-receipt request defused", "flag and category visible",
    "exactly one copy of each message",
)


def _fixture(key: str, run_id: str, date: int, *, extra: str = "", body: bytes = b"proton2m365 selftest\r\n",
             message_id: bool = True) -> bytes:
    stamp = datetime.fromtimestamp(date, UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    head = (f"From: proton2m365 selftest <selftest@proton2m365.invalid>\r\n"
            f"To: you <you@proton2m365.invalid>\r\nSubject: proton2m365 selftest {key}\r\nDate: {stamp}\r\n")
    if message_id:
        head += f"Message-ID: <{key}.{run_id}@proton2m365.invalid>\r\n"
    head += f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n{extra}"
    return head.encode() + b"\r\n" + body


def selftest_folder(run_id: str) -> tuple[str, str]:
    """This run's own folder: nothing else is ever created, written or deleted by selftest."""
    return (f"{SELFTEST_ROOT}-{run_id}", SELFTEST_LEAF)


def selftest_leftovers(imap: MailStore, *, exclude: str | None = None) -> dict[str, list[tuple[str, ...]]]:
    """Folders from earlier selftest runs, grouped by their root, deepest first."""
    groups: dict[str, list[tuple[str, ...]]] = {}
    for path in imap.folders_under(SELFTEST_ROOT):
        if exclude is None or path[0].casefold() != exclude.casefold():
            groups.setdefault(path[0], []).append(path)
    return groups


def cleanup_selftest_leftovers(imap: MailStore, say: Callable[[str], None] = print) -> tuple[list[str], list[str]]:
    """Delete earlier selftest folders, but only when every message in them is a selftest message.

    Returns (removed roots, kept roots). A folder holding anything else is left alone and named.
    """
    removed, kept = [], []
    for root, paths in sorted(selftest_leftovers(imap).items()):
        foreign = []
        for path in paths:
            total = imap.message_count(path)
            ids = imap.present_ids(path)
            ours = sum(1 for pid in ids if pid.startswith("selftest-"))
            if ours != total:
                foreign.append(f"{'/'.join(path)} ({total - ours} of {total} messages are not selftest messages)")
        if foreign:
            kept.append(root)
            say(f"Kept {root}: " + "; ".join(foreign) + ". Move that mail out in Outlook, then run cleanup again.")
            continue
        for path in paths:  # deepest first
            imap.delete_folder(path)
        removed.append(root)
        say(f"Removed {root}")
    if not removed and not kept:
        say("No selftest folders found.")
    return removed, kept


def selftest_cases(run_id: str) -> list[SelftestCase]:
    def pid(key: str) -> str:
        return f"selftest-{key}-{run_id}"

    def case(key: str, raw: bytes, **kwargs) -> SelftestCase:
        synth = kwargs.get("synthesized_message_id")
        return SelftestCase(pid(key), raw, message_id=synth or f"<{key}.{run_id}@proton2m365.invalid>", **kwargs)

    return [
        case("old-read", _fixture("old-read", run_id, OLD_DATE), seen=True, internal_date=OLD_DATE,
                     categories=(SELFTEST_CATEGORY,)),
        case("unread", _fixture("unread", run_id, OLD_DATE + 86400), seen=False,
                     internal_date=OLD_DATE + 86400),
        case("no-message-id", _fixture("no-message-id", run_id, OLD_DATE, message_id=False), seen=True,
                     internal_date=OLD_DATE, synthesized_message_id=pe.synthesized_message_id(pid("no-message-id"))),
        case("long-line", _fixture("long-line", run_id, OLD_DATE,
                                                extra="Content-Transfer-Encoding: 8bit\r\n",
                                                body=b"<p>" + b"long line " * 1500 + b"caf\xc3\xa9</p>\r\n"),
                     seen=True, internal_date=OLD_DATE, needs_reencode=True),
        case("read-receipt", _fixture("read-receipt", run_id, OLD_DATE,
                                                   extra="Disposition-Notification-To: selftest@proton2m365.invalid\r\n"),
                     seen=False, internal_date=OLD_DATE),
        case("flagged", _fixture("flagged", run_id, OLD_DATE), seen=True, internal_date=OLD_DATE,
                     categories=(SELFTEST_CATEGORY,), flagged=True),
        case("drop-after-store", _fixture("drop-after-store", run_id, OLD_DATE), seen=True,
                     internal_date=OLD_DATE, drop="after-store"),
        case("drop-before-store", _fixture("drop-before-store", run_id, OLD_DATE), seen=True,
                     internal_date=OLD_DATE, drop="before-store"),
    ]


def _simulate_drop(imap: MailStore, mode: str) -> None:
    """Make the next APPEND on this connection fail like a dropped connection, after or before the server stores it."""
    from imapclient.exceptions import IMAPClientAbortError

    client = imap.client
    real_append = client.append

    def append(*args, **kwargs):
        client.append = real_append  # one-shot
        if mode == "after-store":
            real_append(*args, **kwargs)
        raise IMAPClientAbortError(f"selftest: simulated connection drop {mode.replace('-', ' ')}")

    client.append = append


def run_selftest(
    imap: MailStore, graph: Organizer, *, keep: bool = False, run_id: str | None = None,
    say: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep, visible_timeout: float = 90,
) -> list[Check]:
    """Live checks against the real mailbox using synthetic messages. Cleans up unless keep=True."""
    run_id = run_id or f"{random.getrandbits(32):08x}"
    cases = selftest_cases(run_id)
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, ok, detail))
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    def not_run(names: Iterable[str], reason: str) -> None:
        for name in names:
            checks.append(Check(name, False, reason, ran=False))
            say(f"  [NOT RUN] {name}: {reason}")

    folder = selftest_folder(run_id)
    category = f"{SELFTEST_CATEGORY} {run_id}"
    archive_pid = f"selftest-archive-{run_id}"
    say(f"  Writes: folder {'/'.join(folder)} with {len(selftest_cases(run_id))} synthetic messages, "
        f"category \"{category}\", one synthetic message in Archive that is moved to Deleted Items. "
        "All of it is removed at the end" + (" except the folder (--keep)." if keep else "."))
    leftover_items: list[str] = []
    leftovers = selftest_leftovers(imap, exclude=folder[0])
    if leftovers:
        say(f"  Earlier selftest folders are still in the mailbox ({', '.join(sorted(leftovers))}); "
            "they are not touched. Remove them with `proton2m365 selftest --cleanup`.")
    if imap.has_folder(folder):
        raise ImapError(f"selftest folder {'/'.join(folder)} already exists; run selftest again for a new run ID")
    created = imap.ensure_folders([folder])
    created_paths = [tuple(name.split(imap.delimiter)) for name in created]
    check("create nested non-ASCII folder over IMAP", len(created) == 2, ", ".join(created))

    results: dict[str, UploadResult | Exception] = {}
    for case in cases:
        message = prepare_message(case.raw, proton_id=case.key, synthesized_message_id=case.synthesized_message_id,
                                  needs_reencode=case.needs_reencode)
        if case.drop:
            imap.present_ids(folder)
            _simulate_drop(imap, case.drop)
        try:
            results[case.key] = imap.upload(folder, case.key, message, seen=case.seen,
                                            internal_date=case.internal_date)
        except Exception as exc:  # noqa: BLE001 - report every failure as a check
            results[case.key] = exc
    normal = [c for c in cases if not c.drop]
    errors = {c.key: r for c, r in ((c, results[c.key]) for c in cases) if isinstance(r, Exception)}
    check("every message accepted (incl. long line, no Message-ID)", not errors,
          "; ".join(f"{k}: {e}" for k, e in errors.items()))
    check("APPENDUID returned", all(isinstance(results[c.key], UploadResult) and results[c.key].uid
                                    for c in normal))
    after = results.get(cases[-2].key)
    check("drop after APPEND: found, not re-sent", isinstance(after, UploadResult) and after.already_present)
    before = results.get(cases[-1].key)
    check("drop before APPEND: re-sent", isinstance(before, UploadResult) and not before.already_present)
    again = imap.upload(folder, cases[0].key, b"unused", seen=True, internal_date=OLD_DATE)
    check("re-run sends nothing already in the folder", again.already_present)

    try:
        folder_id = graph.resolve_folder(folder)
        check("Graph finds the folder by its exact name (NFC)", True)
    except Exception as exc:  # noqa: BLE001
        check("Graph finds the folder by its exact name (NFC)", False, str(exc))
        not_run(GRAPH_CHECKS[1:], "Graph could not find the test folder")
        _selftest_extra(imap, graph, check, not_run, category, archive_pid, leftover_items)
        return _selftest_cleanup(imap, keep, checks, say, folder, created_paths, leftover_items)

    targets = [MatchTarget(c.key, c.message_id, c.internal_date) for c in cases]
    waited = 0.0
    while True:  # Graph can lag IMAP by a few seconds
        match = graph.match(folder_id, targets)
        if not match.unmatched or waited >= visible_timeout:
            break
        sleep(5)
        waited += 5
    check("Graph sees every uploaded message", not match.unmatched, ", ".join(match.unmatched))
    ids = list(match.matched.values())
    headers = graph.proton_ids_for(ids) if ids else {}
    check("X-Proton-Id readable through Graph", all(headers.get(g) == pid for pid, g in match.matched.items()))

    errors = graph.patch_messages({match.matched[c.key]: tag_body(c.categories, c.flagged)
                                   for c in cases if c.key in match.matched and (c.categories or c.flagged)})
    check("categories and flag set through Graph", not errors, "; ".join(errors.values()))

    msgs = graph.get_messages(ids, VERIFY_SELECT + ",internetMessageId,internetMessageHeaders") if ids else {}
    by_case = {c.key: msgs.get(match.matched.get(c.key, ""), None) for c in cases}
    old = by_case.get(cases[0].key) or {}
    received = old.get("receivedDateTime", "")
    try:
        delta = abs(datetime.fromisoformat(received.replace("Z", "+00:00")).timestamp() - OLD_DATE)
    except ValueError:
        delta = float("inf")
    check("original received date kept (2019-06-08)", delta <= DATE_TOLERANCE_SECONDS, f"Graph says {received}")
    wrong_read = [c.key for c in cases if by_case.get(c.key) and bool(by_case[c.key].get("isRead")) != c.seen]
    check("read/unread state kept", not wrong_read, ", ".join(wrong_read))
    drafts = [c.key for c in cases if (by_case.get(c.key) or {}).get("isDraft")]
    check("nothing arrived as a draft", not drafts, ", ".join(drafts))
    synth = by_case.get(cases[2].key) or {}
    check("missing Message-ID synthesized", synth.get("internetMessageId") == cases[2].synthesized_message_id,
          str(synth.get("internetMessageId")))
    dnt = {h.get("name", "").lower() for h in (by_case.get(cases[4].key) or {}).get("internetMessageHeaders") or []}
    check("read-receipt request defused", "disposition-notification-to" not in dnt
          and "x-original-disposition-notification-to" in dnt)
    flagged = by_case.get(cases[5].key) or {}
    check("flag and category visible", (flagged.get("flag") or {}).get("flagStatus") == "flagged"
          and SELFTEST_CATEGORY in (flagged.get("categories") or []))
    total = graph.folder_total(folder_id)
    check("exactly one copy of each message", total == len(cases), f"{total} in folder, expected {len(cases)}")
    _selftest_extra(imap, graph, check, not_run, category, archive_pid, leftover_items)
    return _selftest_cleanup(imap, keep, checks, say, folder, created_paths, leftover_items)


def _selftest_extra(imap: MailStore, graph: Organizer, check, not_run, category: str, archive_pid: str,
                    leftovers: list[str]) -> None:
    """What a real migration also does: create categories, write into Archive, and prune to Deleted Items."""
    try:
        created = graph.ensure_categories({category: "#E74856"})
        deleted = graph.delete_category(category)
        check("create and delete a category through Graph", created == [category] and deleted,
              "" if created == [category] and deleted else f"created {created}, deleted {deleted}")
        if created and not deleted:
            leftovers.append(f'category "{category}" (delete it in Outlook: Categories)')
    except Exception as exc:  # noqa: BLE001
        check("create and delete a category through Graph", False, str(exc))
        leftovers.append(f'category "{category}", if it was created (Outlook: Categories)')

    raw = _fixture("archive", archive_pid.removeprefix("selftest-archive-"), OLD_DATE)
    try:
        result = imap.upload(pe.ARCHIVE_FOLDER, archive_pid, prepare_message(raw, proton_id=archive_pid),
                             seen=True, internal_date=OLD_DATE)
        check("APPEND into the Archive folder", not result.already_present)
    except Exception as exc:  # noqa: BLE001
        check("APPEND into the Archive folder", False, str(exc))
        not_run(["move to Deleted Items (the prune path)"], "nothing was put into Archive")
        leftovers.append(f"synthetic message {archive_pid} in Archive, if it arrived")
        return
    try:
        moved = imap.remove(pe.ARCHIVE_FOLDER, [archive_pid])
        still_there = imap.remove(pe.ARCHIVE_FOLDER, [archive_pid])   # reads Archive fresh
        in_trash = archive_pid in imap.present_ids(imap.trash_path)
        check("move to Deleted Items (the prune path)", moved == {archive_pid} and not still_there and in_trash,
              "" if in_trash else "not found in Deleted Items")
    except Exception as exc:  # noqa: BLE001
        check("move to Deleted Items (the prune path)", False, str(exc))
        leftovers.append(f"synthetic message {archive_pid} in Archive or Deleted Items")
        return
    try:
        if imap.purge(imap.trash_path, [archive_pid]) != {archive_pid}:
            leftovers.append(f"synthetic message {archive_pid} in Deleted Items")
    except Exception as exc:  # noqa: BLE001
        leftovers.append(f"synthetic message {archive_pid} in Deleted Items ({exc})")


def _selftest_cleanup(imap: MailStore, keep: bool, checks: list[Check], say: Callable[[str], None],
                      folder: tuple[str, ...], created: list[tuple[str, ...]], leftovers: list[str]) -> list[Check]:
    """Delete only the folders this run created, deepest first; then name anything still left behind."""
    if keep:
        leftovers.insert(0, f"folder {'/'.join(folder)} (kept with --keep)")
    else:
        try:
            for path in sorted(created, key=len, reverse=True):
                imap.delete_folder(path)
            say(f"Removed {folder[0]}")
        except Exception as exc:  # noqa: BLE001
            leftovers.insert(0, f"folder {folder[0]} ({exc})")
    if leftovers:
        say("Left in the mailbox: " + "; ".join(leftovers)
            + ". Folders go with `proton2m365 selftest --cleanup`; anything else, delete in Outlook.")
    return checks
