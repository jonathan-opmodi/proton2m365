"""state.sqlite: one row per Proton message. This module is the only code that changes a row's stage.

Stages:
    planned        will be uploaded
    skipped        will not be uploaded (skip_reason says why)
    upload_failed  not in the mailbox; `upload --retry-failed` picks it up
    appended       in the mailbox, not yet tagged
    tagged         in the mailbox with categories/flag applied
    tag_failed     in the mailbox, but not matched in Graph
    verified       checked by `verify`

Writers (plan, upload, tag, verify) hold an exclusive lock on <state>.lock so two runs can never
work on the same rows at once.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .locks import FileLock, LockHeld
from .status import Progress

SCHEMA_VERSION = 3  # 2: per-row verification evidence (check_result, checked_at); 3: source_present

PLANNED, SKIPPED, UPLOAD_FAILED = "planned", "skipped", "upload_failed"
APPENDED, TAGGED, TAG_FAILED, VERIFIED = "appended", "tagged", "tag_failed", "verified"
NOT_IN_MAILBOX = frozenset({PLANNED, SKIPPED, UPLOAD_FAILED})
IN_MAILBOX = frozenset({APPENDED, TAGGED, TAG_FAILED, VERIFIED})
UNTAGGED_IN_MAILBOX = frozenset({APPENDED, TAG_FAILED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    proton_id        TEXT PRIMARY KEY,
    eml_path         TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    size             INTEGER NOT NULL,
    target_folder    TEXT NOT NULL,          -- JSON array of folder segments
    categories       TEXT NOT NULL,          -- JSON array of category names
    seen             INTEGER NOT NULL,
    flagged          INTEGER NOT NULL,
    internal_date    INTEGER,                -- Unix seconds, UTC
    message_id       TEXT NOT NULL,
    synth_message_id INTEGER NOT NULL,       -- 1 when upload must add the Message-ID header
    needs_reencode   INTEGER NOT NULL,       -- 1 when a text part has over-long lines
    stage            TEXT NOT NULL,
    skip_reason      TEXT,
    imap_uid         INTEGER,
    graph_id         TEXT,
    upload_attempts  INTEGER NOT NULL DEFAULT 0,
    tag_attempts     INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    check_result     TEXT,                   -- latest verify result: 'pass' or 'fail:<check>; <check>'
    checked_at       INTEGER,                -- Unix seconds of that check
    source_present   INTEGER NOT NULL DEFAULT 1  -- 0 when the newest planned export no longer has this message
);
CREATE INDEX IF NOT EXISTS messages_stage ON messages(stage);
CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY, graph_folder_id TEXT);
"""


class StateError(Exception):
    pass


class StateLocked(StateError):
    def __init__(self, lock_path: Path, holder: str):
        super().__init__(f"P2M-STATE-LOCKED: another proton2m365 run is using this state file: wait for pid {holder or '?'} to finish; "
                         f"if no proton2m365 run is active, run the command again (lock: {lock_path})")
        self.holder = holder


@dataclass(frozen=True)
class PlanRow:
    """What plan decided for one message. Folder and categories are tuples; stored as JSON."""

    proton_id: str
    eml_path: str
    sha256: str
    size: int
    target_folder: tuple[str, ...]
    categories: tuple[str, ...]
    seen: bool
    flagged: bool
    internal_date: int | None
    message_id: str
    synth_message_id: bool
    needs_reencode: bool
    skip_reason: str | None


@dataclass(frozen=True)
class Drift:
    proton_id: str
    stage: str
    field: str
    old: object
    new: object


@dataclass
class ReplanResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    reclassified: list[tuple[str, str, str]] = field(default_factory=list)  # (id, old stage, new stage)
    applied_to_uploaded: list[Drift] = field(default_factory=list)         # categories/flag before tagging
    drift: list[Drift] = field(default_factory=list)                       # reported, not applied
    to_prune: list[str] = field(default_factory=list)  # already uploaded, now skipped: remove with `prune`
    unresolved_drift: list[str] = field(default_factory=list)  # ids whose uploaded folder/date/content differ
    to_prune_by_folder: dict[str, int] = field(default_factory=dict)  # folder -> uploaded messages now skipped
    missing_from_export: list[str] = field(default_factory=list)
    newly_missing: list[str] = field(default_factory=list)  # not skipped, present before this plan, gone now


def _row_values(row: PlanRow) -> dict:
    return {
        "proton_id": row.proton_id,
        "eml_path": row.eml_path,
        "sha256": row.sha256,
        "size": row.size,
        "target_folder": json.dumps(list(row.target_folder), ensure_ascii=False),
        "categories": json.dumps(list(row.categories), ensure_ascii=False),
        "seen": int(row.seen),
        "flagged": int(row.flagged),
        "internal_date": row.internal_date,
        "message_id": row.message_id,
        "synth_message_id": int(row.synth_message_id),
        "needs_reencode": int(row.needs_reencode),
        "skip_reason": row.skip_reason,
    }


DRIFT_META, ACCEPTED_DRIFT_META = "unresolved_drift", "accepted_drift"
MAX_UPLOAD_ATTEMPTS = 5  # transient failures per row before it becomes upload_failed
MAX_TAG_ATTEMPTS = 3     # tag runs that fail to find a row in Graph before it becomes tag_failed


@dataclass(frozen=True)
class WorkRow:
    """A row as upload and tag need it."""

    proton_id: str
    eml_path: str
    size: int
    target_folder: tuple[str, ...]
    categories: tuple[str, ...]
    seen: bool
    flagged: bool
    internal_date: int | None
    message_id: str
    synth_message_id: bool
    needs_reencode: bool
    stage: str
    imap_uid: int | None
    graph_id: str | None
    upload_attempts: int
    tag_attempts: int
    sha256: str = ""
    source_present: bool = True

    @classmethod
    def from_db(cls, r: sqlite3.Row) -> WorkRow:
        return cls(
            proton_id=r["proton_id"], eml_path=r["eml_path"], size=r["size"],
            target_folder=tuple(json.loads(r["target_folder"])), categories=tuple(json.loads(r["categories"])),
            seen=bool(r["seen"]), flagged=bool(r["flagged"]), internal_date=r["internal_date"],
            message_id=r["message_id"], synth_message_id=bool(r["synth_message_id"]),
            needs_reencode=bool(r["needs_reencode"]), stage=r["stage"], imap_uid=r["imap_uid"],
            graph_id=r["graph_id"], upload_attempts=r["upload_attempts"], tag_attempts=r["tag_attempts"],
            sha256=r["sha256"],
            source_present=bool(r["source_present"]) if "source_present" in r.keys() else True,
        )


# Fields upload writes into the mailbox; once appended they can't change without re-uploading.
_FIXED_AFTER_APPEND = ("target_folder", "seen", "sha256", "message_id", "synth_message_id", "needs_reencode",
                       "internal_date")
# Fields tag writes; they can still change until the row is tagged.
_SET_BY_TAG = ("categories", "flagged")


class State:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- reads ----------------------------------------------------------------------------------

    def stage_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT stage, COUNT(*) FROM messages GROUP BY stage"))

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def rows(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM messages ORDER BY proton_id")

    def _work_rows(self, stages: Iterable[str], *, present_only: bool = False) -> list[WorkRow]:
        stages = list(stages)
        # Rows in the mailbox that a re-plan now skips wait for `prune`; nothing else touches them.
        present = " AND source_present = 1" if present_only else ""
        query = (f"SELECT * FROM messages WHERE stage IN ({', '.join('?' for _ in stages)}) "
                 f"AND skip_reason IS NULL{present} ORDER BY target_folder, internal_date, proton_id")
        return [WorkRow.from_db(r) for r in self.conn.execute(query, stages)]

    def rows_to_upload(self, *, retry_failed: bool = False) -> list[WorkRow]:
        """Planned rows; with retry_failed, only upload_failed rows (a plain run never touches them).
        Rows missing from the newest export have no file to send and are left out."""
        return self._work_rows([UPLOAD_FAILED] if retry_failed else [PLANNED], present_only=True)

    def rows_to_tag(self, *, retry_failed: bool = False) -> list[WorkRow]:
        """Appended rows; with retry_failed, only tag_failed rows. Tagging never re-uploads."""
        return self._work_rows([TAG_FAILED] if retry_failed else [APPENDED])

    def upload_folders(self) -> set[tuple[str, ...]]:
        """Every target folder of a row that is or will be in the mailbox."""
        stages = sorted({PLANNED, UPLOAD_FAILED} | IN_MAILBOX)
        query = f"SELECT DISTINCT target_folder FROM messages WHERE stage IN ({', '.join('?' for _ in stages)})"
        return {tuple(json.loads(r[0])) for r in self.conn.execute(query, stages)}

    # -- upload / tag transitions ----------------------------------------------------------------

    def _transition(self, proton_id: str, allowed: Iterable[str], changes: dict) -> None:
        allowed = list(allowed)
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE messages SET {sets} WHERE proton_id = ? AND stage IN ({', '.join('?' for _ in allowed)})",
                [*changes.values(), proton_id, *allowed],
            )
        if cur.rowcount != 1:
            row = self.conn.execute("SELECT stage FROM messages WHERE proton_id = ?", (proton_id,)).fetchone()
            raise StateError(f"{proton_id}: cannot move from {row[0] if row else 'missing'} "
                             f"(allowed from {', '.join(allowed)})")

    def mark_appended(self, proton_id: str, imap_uid: int | None) -> None:
        self._transition(proton_id, (PLANNED, UPLOAD_FAILED),
                         {"stage": APPENDED, "imap_uid": imap_uid, "last_error": None})

    def record_upload_failure(self, proton_id: str, error: str, *, permanent: bool = False) -> str:
        """Count a failed upload attempt. Returns the row's stage afterwards."""
        row = self.conn.execute("SELECT stage, upload_attempts FROM messages WHERE proton_id = ?",
                                (proton_id,)).fetchone()
        attempts = row["upload_attempts"] + 1
        stage = UPLOAD_FAILED if permanent or attempts >= MAX_UPLOAD_ATTEMPTS else row["stage"]
        self._transition(proton_id, (PLANNED, UPLOAD_FAILED),
                         {"stage": stage, "upload_attempts": attempts, "last_error": error[:500]})
        return stage

    def mark_source_changed(self, proton_id: str, detail: str) -> None:
        """The export file no longer matches the plan (same path, different bytes): the row stays where it is,
        nothing is uploaded, and the next `plan` reads the file again."""
        self._transition(proton_id, (PLANNED, UPLOAD_FAILED), {"last_error": f"source-changed: {detail}"[:500]})

    def mark_tagged(self, proton_id: str, graph_id: str) -> None:
        self._transition(proton_id, (APPENDED, TAG_FAILED),
                         {"stage": TAGGED, "graph_id": graph_id, "last_error": None})

    def record_tag_miss(self, proton_id: str, error: str) -> str:
        """Count a tag run that couldn't find the row in Graph. Returns the row's stage afterwards."""
        row = self.conn.execute("SELECT stage, tag_attempts FROM messages WHERE proton_id = ?",
                                (proton_id,)).fetchone()
        attempts = row["tag_attempts"] + 1
        stage = TAG_FAILED if attempts >= MAX_TAG_ATTEMPTS else row["stage"]
        self._transition(proton_id, (APPENDED, TAG_FAILED),
                         {"stage": stage, "tag_attempts": attempts, "last_error": error[:500]})
        return stage

    def rows_to_prune(self) -> dict[tuple[str, ...], list[str]]:
        """Uploaded rows that the current plan skips, by folder."""
        stages = sorted(IN_MAILBOX)
        query = (f"SELECT target_folder, proton_id FROM messages WHERE stage IN ({', '.join('?' for _ in stages)}) "
                 "AND skip_reason IS NOT NULL ORDER BY target_folder, proton_id")
        out: dict[tuple[str, ...], list[str]] = {}
        for folder, pid in self.conn.execute(query, stages):
            out.setdefault(tuple(json.loads(folder)), []).append(pid)
        return out

    def mark_pruned(self, proton_id: str) -> None:
        """Removed from the mailbox: back to skipped (its skip_reason says why)."""
        self._transition(proton_id, sorted(IN_MAILBOX),
                         {"stage": SKIPPED, "imap_uid": None, "graph_id": None, "last_error": None})

    def recorded_uid(self, proton_id: str) -> int | None:
        row = self.conn.execute("SELECT imap_uid FROM messages WHERE proton_id = ?", (proton_id,)).fetchone()
        return row[0] if row else None

    def mark_deduped(self, proton_id: str, keeper_uid: int | None) -> None:
        """Extra copies of this message were moved away. The kept copy may not be the one tag found in Graph
        (its Graph id and categories may belong to a moved copy), so the row goes back to appended: the next
        `tag` finds the kept copy by its X-Proton-Id and applies categories again, and verify checks it."""
        self._transition(proton_id, sorted(IN_MAILBOX),
                         {"stage": APPENDED, "imap_uid": keeper_uid, "graph_id": None, "check_result": None,
                          "tag_attempts": 0, "last_error": None})

    def mark_verified(self, proton_id: str, checked_at: int) -> None:
        self._transition(proton_id, (TAGGED, VERIFIED),
                         {"stage": VERIFIED, "last_error": None, "check_result": "pass", "checked_at": checked_at})

    def mark_check_failed(self, proton_id: str, checks: Iterable[str], checked_at: int) -> None:
        """A checked row did not match. It stops counting as verified until a later check passes."""
        result = "fail:" + "; ".join(sorted(set(checks)))
        self._transition(proton_id, (TAGGED, VERIFIED),
                         {"stage": TAGGED, "last_error": result, "check_result": result, "checked_at": checked_at})

    def rows_to_verify(self) -> list[WorkRow]:
        return self._work_rows([TAGGED, VERIFIED])

    def progress(self) -> Progress:
        """Counts for status.migration_status. In-mailbox rows a re-plan now skips count only as waiting_prune."""
        has_checks = "check_result" in {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        bad = "COALESCE(check_result LIKE 'fail:%', 0)" if has_checks else "0"
        has_presence = "source_present" in {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        present = "source_present" if has_presence else "1"
        counts = Counter()
        for stage, waiting, failed, here, n in self.conn.execute(
                f"SELECT stage, skip_reason IS NOT NULL, {bad}, {present}, COUNT(*) FROM messages "
                "GROUP BY 1, 2, 3, 4"):
            if not here and stage in (PLANNED, UPLOAD_FAILED):
                counts["missing_source"] += n  # never uploaded and no longer in the export (accepted at plan)
            elif stage == SKIPPED:
                counts["skipped"] += n
            elif waiting and stage in IN_MAILBOX:
                counts["waiting_prune"] += n
            elif stage == TAGGED:
                counts["failed_check" if failed else "unchecked"] += n
            else:
                counts[stage] += n
        counts["drift"] = len(self.unresolved_drift())
        counts["upload_rejected"] = self.conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE stage = ? AND {present} = 1 AND (last_error LIKE 'rejected:%' "
            "OR last_error LIKE 'cannot read%')", (UPLOAD_FAILED,)).fetchone()[0]
        counts["source_changed"] = self.conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE stage IN (?, ?) AND {present} = 1 "
            "AND last_error LIKE 'source-changed:%'", (PLANNED, UPLOAD_FAILED)).fetchone()[0]
        if has_presence:
            counts["missing_in_mailbox"] = self.conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE source_present = 0 AND stage IN "
                f"({', '.join('?' for _ in IN_MAILBOX)})", sorted(IN_MAILBOX)).fetchone()[0]
        return Progress(**counts)

    def active_ids(self) -> set[str]:
        """Every row that is not skipped and that the newest export still had (these need their files)."""
        has_presence = "source_present" in {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        present = " AND source_present = 1" if has_presence else ""
        return {r[0] for r in self.conn.execute(f"SELECT proton_id FROM messages WHERE stage != ?{present}",
                                                (SKIPPED,))}

    def missing_source_ids(self) -> list[str]:
        """Rows the newest export no longer has (accepted with plan --accept-missing)."""
        return [r[0] for r in self.conn.execute(
            "SELECT proton_id FROM messages WHERE source_present = 0 ORDER BY proton_id")]

    def all_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT proton_id FROM messages")}

    def oldest_check(self) -> int | None:
        """When the least recently checked verified row was checked (None if unknown or none)."""
        return self.conn.execute("SELECT MIN(checked_at) FROM messages WHERE stage = ?", (VERIFIED,)).fetchone()[0]

    def failed_check_ids(self) -> set[str]:
        """Rows in the mailbox whose latest check failed; verify re-checks them every run."""
        stages = sorted(IN_MAILBOX)
        query = (f"SELECT proton_id FROM messages WHERE stage IN ({', '.join('?' for _ in stages)}) "
                 "AND check_result LIKE 'fail:%'")
        return {r[0] for r in self.conn.execute(query, stages)}

    def in_mailbox_counts(self) -> dict[tuple[str, ...], int]:
        """Rows that should be in each folder right now."""
        stages = sorted(IN_MAILBOX)
        query = (f"SELECT target_folder, COUNT(*) FROM messages "
                 f"WHERE stage IN ({', '.join('?' for _ in stages)}) GROUP BY target_folder")
        return {tuple(json.loads(path)): n for path, n in self.conn.execute(query, stages)}

    def skip_counts(self) -> dict[str, int]:
        return dict(self.conn.execute(
            "SELECT skip_reason, COUNT(*) FROM messages WHERE stage = ? GROUP BY skip_reason", (SKIPPED,)))

    def failed_rows(self) -> list[tuple[str, str, str]]:
        """(proton_id, stage, last_error) for upload_failed and tag_failed rows."""
        return [tuple(r) for r in self.conn.execute(
            "SELECT proton_id, stage, COALESCE(last_error, '') FROM messages "
            "WHERE stage IN (?, ?) ORDER BY stage, proton_id", (UPLOAD_FAILED, TAG_FAILED))]

    def total_rows(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def remaining_upload_bytes(self) -> int:
        return self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM messages WHERE stage = ?",
                                 (PLANNED,)).fetchone()[0]

    def bytes_in(self, stages: Iterable[str]) -> int:
        stages = list(stages)
        return self.conn.execute(f"SELECT COALESCE(SUM(size), 0) FROM messages WHERE stage IN "
                                 f"({', '.join('?' for _ in stages)})", stages).fetchone()[0]

    def record_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.set_meta(key, value)

    def reset_attempts(self, stage: str) -> int:
        """Give every row in a failed stage a fresh attempt budget (for --retry-failed)."""
        column = {UPLOAD_FAILED: "upload_attempts", TAG_FAILED: "tag_attempts"}[stage]
        with self.conn:
            return self.conn.execute(f"UPDATE messages SET {column} = 0 WHERE stage = ?", (stage,)).rowcount

    # -- Graph folder ids ------------------------------------------------------------------------

    def folder_id(self, path: tuple[str, ...]) -> str | None:
        row = self.conn.execute("SELECT graph_folder_id FROM folders WHERE path = ?",
                                (json.dumps(list(path), ensure_ascii=False),)).fetchone()
        return row[0] if row else None

    def set_folder_id(self, path: tuple[str, ...], graph_folder_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO folders(path, graph_folder_id) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET graph_folder_id = excluded.graph_folder_id",
                (json.dumps(list(path), ensure_ascii=False), graph_folder_id),
            )

    # -- plan -----------------------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def apply_plan(self, rows: Iterable[PlanRow], *, settings: dict | None = None,
                   accept_drift: bool = False, meta: dict[str, str] | None = None) -> ReplanResult:
        """Upsert plan rows. Never re-plans mail already in the mailbox: changes to its folder, date or content
        are recorded as unresolved drift (verify stays INCOMPLETE) until accepted with accept_drift."""
        result = ReplanResult()
        existing = {r["proton_id"]: r for r in self.conn.execute("SELECT * FROM messages")}
        seen_ids: set[str] = set()
        with self.conn:
            for row in rows:
                seen_ids.add(row.proton_id)
                values = _row_values(row)
                old = existing.get(row.proton_id)
                if old is None:
                    stage = SKIPPED if row.skip_reason else PLANNED
                    self._insert(values, stage)
                    result.new += 1
                elif old["stage"] in NOT_IN_MAILBOX:
                    self._replan_not_uploaded(old, values, result)
                else:
                    self._replan_uploaded(old, values, result)
            missing = set(existing) - seen_ids
            result.missing_from_export = sorted(missing)
            result.newly_missing = sorted(pid for pid in missing if existing[pid]["stage"] != SKIPPED
                                          and existing[pid]["source_present"])
            if missing:
                self.conn.executemany("UPDATE messages SET source_present = 0 WHERE proton_id = ?",
                                      [(pid,) for pid in sorted(missing)])
            back = [pid for pid in seen_ids if pid in existing and not existing[pid]["source_present"]]
            if back:  # in the export again: the row keeps its stage
                self.conn.executemany("UPDATE messages SET source_present = 1 WHERE proton_id = ?",
                                      [(pid,) for pid in sorted(back)])
            for key, value in (meta or {}).items():
                self.set_meta(key, value)
            if settings is not None:
                self.set_meta("plan_settings", json.dumps(settings, sort_keys=True))
            accepted = json.loads(self.get_meta(ACCEPTED_DRIFT_META) or "{}")
            current = {f"{d.proton_id}:{d.field}": json.dumps(d.new, sort_keys=True, default=list) for d in result.drift}
            if accept_drift:
                accepted.update(current)
                self.set_meta(ACCEPTED_DRIFT_META, json.dumps(accepted, sort_keys=True))
            unresolved = sorted({k.split(":", 1)[0] for k, v in current.items() if accepted.get(k) != v})
            self.set_meta(DRIFT_META, json.dumps(unresolved))
            result.unresolved_drift = unresolved
        return result

    def unresolved_drift(self) -> list[str]:
        return json.loads(self.get_meta(DRIFT_META) or "[]")

    def _insert(self, values: dict, stage: str) -> None:
        cols = list(values) + ["stage"]
        self.conn.execute(
            f"INSERT INTO messages({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
            [*values.values(), stage],
        )

    def _update(self, proton_id: str, changes: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.conn.execute(f"UPDATE messages SET {sets} WHERE proton_id = ?", [*changes.values(), proton_id])

    def _replan_not_uploaded(self, old: sqlite3.Row, values: dict, result: ReplanResult) -> None:
        """Not in the mailbox yet: take every new value, and re-classify planned/skipped/upload_failed."""
        changes = {k: v for k, v in values.items() if old[k] != v}
        stage = old["stage"]
        if values["skip_reason"]:
            new_stage = SKIPPED
        elif stage == SKIPPED:
            new_stage = PLANNED
        else:
            new_stage = stage  # planned stays planned; upload_failed waits for --retry-failed
        if new_stage != stage:
            changes["stage"] = new_stage
            result.reclassified.append((old["proton_id"], stage, new_stage))
        if changes:
            self._update(old["proton_id"], changes)
            result.updated += 1
        else:
            result.unchanged += 1

    def _replan_uploaded(self, old: sqlite3.Row, values: dict, result: ReplanResult) -> None:
        """Already in the mailbox: report what would change; only apply tag fields not yet tagged."""
        pid, stage = old["proton_id"], old["stage"]
        changes: dict = {}
        if old["eml_path"] != values["eml_path"] or old["size"] != values["size"]:
            changes["eml_path"], changes["size"] = values["eml_path"], values["size"]
        for name in _FIXED_AFTER_APPEND:
            if old[name] != values[name]:
                result.drift.append(Drift(pid, stage, name, _decode(name, old[name]), _decode(name, values[name])))
        if values["skip_reason"] != old["skip_reason"]:
            changes["skip_reason"] = values["skip_reason"]  # set: `prune` removes it; cleared: it stays
        if values["skip_reason"]:
            result.to_prune.append(pid)
            folder = "/".join(json.loads(old["target_folder"]))
            result.to_prune_by_folder[folder] = result.to_prune_by_folder.get(folder, 0) + 1
        for name in _SET_BY_TAG:
            if old[name] == values[name]:
                continue
            # Categories and flag can always be fixed in place: take the new value; a tagged or verified row
            # goes back to appended so the next `tag` applies it (and verify checks it again).
            changes[name] = values[name]
            result.applied_to_uploaded.append(Drift(pid, stage, name, _decode(name, old[name]),
                                                    _decode(name, values[name])))
            if stage not in UNTAGGED_IN_MAILBOX and not values["skip_reason"]:
                changes.update(stage=APPENDED, check_result=None)
        if changes:
            self._update(pid, changes)
            result.updated += 1
        else:
            result.unchanged += 1


def _decode(name: str, value: object) -> object:
    if name in ("target_folder", "categories") and isinstance(value, str):
        return tuple(json.loads(value))
    return value


def _connect(path: Path, *, write: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    version = _stored_version(conn)  # read before any DDL
    if version is not None and version > SCHEMA_VERSION:
        conn.close()
        raise StateError(f"P2M-STATE-NEWER: {path} has schema version {version}; this proton2m365 understands up to "
                         f"{SCHEMA_VERSION}. Upgrade proton2m365 to use it.")
    if version is not None and version < SCHEMA_VERSION:
        if not write:
            return conn  # read-only commands work on the old layout; the next writing command upgrades it
        _upgrade(conn, path, version)
        return conn
    conn.executescript(_SCHEMA)
    if version is None:
        with conn:
            conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    return conn


def _stored_version(conn: sqlite3.Connection) -> int | None:
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if not has_meta:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else None


def _upgrade(conn: sqlite3.Connection, path: Path, version: int) -> None:
    """Back the state up (SQLite backup API, so nothing still in the WAL is missed), then migrate in one transaction."""
    backup_path = path.with_name(f"{path.name}.bak-v{version}")
    if not _backup_ok(backup_path, version):
        backup_path.unlink(missing_ok=True)  # an interrupted backup: make it again
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
        os.chmod(backup_path, 0o600)
    with conn:
        if version < 2:
            conn.execute("ALTER TABLE messages ADD COLUMN check_result TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN checked_at INTEGER")
            # Rows verified before v2 passed every check at some unknown time.
            conn.execute("UPDATE messages SET check_result = 'pass' WHERE stage = ?", (VERIFIED,))
        columns = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if version < 3 and "source_present" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN source_present INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
    conn.executescript(_SCHEMA)  # indexes and tables added since; all IF NOT EXISTS


def _backup_ok(backup_path: Path, version: int) -> bool:
    """An existing backup counts only if SQLite reads it as whole and it holds the version being upgraded."""
    if not backup_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            return _stored_version(conn) == version
        finally:
            conn.close()
    except sqlite3.Error:
        return False


@contextmanager
def open_state(path: Path, *, write: bool = True, create: bool = False) -> Iterator[State]:
    """Open state.sqlite. Only `plan` creates one (create=True); every other command needs it to exist.
    Writers take an exclusive, non-blocking lock on <path>.lock first."""
    path = Path(path)
    lock = None
    if not create and not path.exists():
        raise StateError(_no_state(path))
    if write:
        lock = FileLock(path.with_name(path.name + ".lock"))
        try:
            lock.acquire()
        except LockHeld as exc:
            raise StateLocked(exc.path, exc.holder) from None
    try:
        conn = _connect(path, write=write)
        try:
            yield State(conn)
        finally:
            conn.close()
    finally:
        if lock is not None:
            lock.release()


def _no_state(path: Path) -> str:
    """No state file here: say where we looked and offer the state files that do exist nearby."""
    path = Path(path).resolve()
    found = sorted({p.resolve() for d in {path.parent, Path.cwd()} if d.is_dir()
                    for p in d.glob("*.sqlite") if p.resolve() != path})
    hint = ("; did you mean " + " or ".join(f"--state {p}" for p in found[:3]) + "?") if found else \
        "; run `proton2m365 plan <export>` first (only plan creates a state file)"
    return f"P2M-STATE-MISSING: no state at {path}{hint}"
