"""Proton Mail Export Tool backups: format knowledge and the read-only inventory behind `scan`.

Everything that knows the Proton export layout lives here:

    <email>/mail_YYYYMMDD_HHMMSS/
        labels.json                 {"Version": 1, "Payload": [{ID, Name, Path, ParentID, Color, Type}, ...]}
        <id>.eml                    the raw MIME message
        <id>.metadata.json          {"Version": 1, "Payload": {ID, LabelIDs, Flags, Time, Unread, ...}}

Nothing in this module touches the network or writes to the export.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path

EML_SUFFIX = ".eml"
METADATA_SUFFIX = ".metadata.json"
LABELS_FILE = "labels.json"

# Proton message Flags bits (go-proton-api MessageFlag).
FLAG_RECEIVED = 1
FLAG_SENT = 2
FLAG_IMPORTED = 512

# Proton system label IDs (go-proton-api). Names found in labels.json win over this table;
# scan prints the IDs it sees so the table can be checked against a real export.
INBOX, ALL_DRAFTS, ALL_SENT, TRASH, SPAM, ALL_MAIL, ARCHIVE, SENT, DRAFTS, OUTBOX, STARRED = (
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
)
SYSTEM_LABEL_NAMES = {
    INBOX: "Inbox",
    ALL_DRAFTS: "All Drafts",
    ALL_SENT: "All Sent",
    TRASH: "Trash",
    SPAM: "Spam",
    ALL_MAIL: "All Mail",
    ARCHIVE: "Archive",
    SENT: "Sent",
    DRAFTS: "Drafts",
    OUTBOX: "Outbox",
    STARRED: "Starred",
    # Confirmed in a real export's labels.json (Export Tool 1.0.7):
    "12": "All Scheduled",
    "15": "All Mail",  # "almost all mail": All Mail without Spam/Trash
    "16": "Snoozed",
}
# Order of Proton's sidebar, so scan totals can be compared line by line.
SIDEBAR_ORDER = [INBOX, DRAFTS, ALL_DRAFTS, SENT, ALL_SENT, STARRED, ARCHIVE, SPAM, TRASH, ALL_MAIL, OUTBOX]
DRAFT_LABELS = frozenset({DRAFTS, ALL_DRAFTS})

LABEL_TYPE_LABEL = 1
LABEL_TYPE_FOLDER = 3

DEFAULT_MAX_SIZE_BYTES = 35 * 1024 * 1024
LONG_LINE_LIMIT = 10_000  # bytes; Exchange IMAP rejects lines over ~10,500 characters

_LOG_ERROR_RE = re.compile(rb"\b(error|errors|failed|failure|fatal|panic)\b", re.IGNORECASE)
_LOG_FOUND_RE = re.compile(rb"Found (\d+) Messages for download")
RECENTLY_MODIFIED_SECONDS = 120
_PARTIAL_SUFFIXES = (".tmp", ".part", ".partial", ".download")
_SIGNED_CONTENT_TYPES = frozenset(
    {"multipart/signed", "multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime"}
)
_PGP_ARMOR_PREFIXES = (b"-----BEGIN PGP SIGNED MESSAGE-----", b"-----BEGIN PGP MESSAGE-----")


class ExportError(Exception):
    """The folder is not a readable Proton export."""


class DraftVerdict(StrEnum):
    """Outcome of the draft truth table (Drafts label x Received/Sent bit x Imported bit)."""

    SKIP_DRAFT = "skip as draft"
    KEEP = "keep"
    KEEP_IMPORTED = "keep (imported mail)"
    KEEP_DISAGREEMENT = "keep, report disagreement"


class SkipReason(StrEnum):
    DRAFT = "draft"
    TRASH = "trash"
    SPAM = "spam"
    OVERSIZE = "oversize"
    LONG_LINE_SIGNED = "long-line-signed"
    EXCLUDED_LABEL = "excluded-label"  # carries a label passed to plan --exclude-label
    EXCLUDED_BY_USER = "excluded-by-user"  # named with plan --exclude-file


def file_stem(name: str) -> str:
    """The message id a file name refers to: `x.eml`, `x.metadata.json` and `x` all mean `x`."""
    name = Path(name).name
    for suffix in (METADATA_SUFFIX, EML_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    path: str
    parent_id: str | None
    color: str | None
    type: int | None

    @property
    def is_system(self) -> bool:
        return self.id.isdigit()

    @property
    def kind(self) -> str:
        if self.is_system:
            return "system"
        if self.type == LABEL_TYPE_FOLDER:
            return "folder"
        if self.type == LABEL_TYPE_LABEL:
            return "label"
        return f"type-{self.type}"


@dataclass(frozen=True)
class MessageMeta:
    proton_id: str
    label_ids: tuple[str, ...]
    flags: int
    time: int | None
    unread: bool


@dataclass(frozen=True)
class EmlFacts:
    size: int
    message_id: str | None
    max_line: int
    signed: bool
    date: int | None = None      # Date header as Unix seconds, if parseable
    sha256: str | None = None    # over the original bytes, only when requested


@dataclass
class Inventory:
    export_dir: Path
    labels: dict[str, Label]
    max_size: int
    long_line_limit: int
    message_count: int = 0
    total_bytes: int = 0
    earliest: int | None = None
    latest: int | None = None
    label_counts: Counter[str] = field(default_factory=Counter)
    unknown_label_ids: Counter[str] = field(default_factory=Counter)
    flag_values: Counter[int] = field(default_factory=Counter)
    draft_table: Counter[tuple[bool, bool, bool]] = field(default_factory=Counter)
    draft_disagreements: list[str] = field(default_factory=list)
    skip_counts: Counter[SkipReason] = field(default_factory=Counter)
    upload_count: int = 0
    upload_bytes: int = 0
    missing_message_id: list[str] = field(default_factory=list)
    message_ids: dict[str, list[str]] = field(default_factory=dict)
    long_line: list[str] = field(default_factory=list)
    long_line_signed: list[str] = field(default_factory=list)
    oversize: list[tuple[str, int]] = field(default_factory=list)
    largest: list[tuple[int, str]] = field(default_factory=list)
    orphan_eml: list[str] = field(default_factory=list)
    orphan_metadata: list[str] = field(default_factory=list)
    bad_metadata: list[tuple[str, str]] = field(default_factory=list)
    partial_files: list[str] = field(default_factory=list)
    logs_checked: list[Path] = field(default_factory=list)
    log_errors: list[tuple[Path, int, str]] = field(default_factory=list)
    log_error_count: int = 0
    expected_count: int | None = None  # "Found N Messages for download" from the Export Tool log
    seconds_since_modified: float | None = None  # export folder mtime age; small means still being written
    leftover_dirs: list[str] = field(default_factory=list)  # non-empty subfolders, e.g. the Export Tool's temp/

    @property
    def missing_vs_log(self) -> int | None:
        if self.expected_count is None:
            return None
        return self.expected_count - (self.message_count + len(self.bad_metadata))

    @property
    def duplicate_message_ids(self) -> dict[str, list[str]]:
        return {mid: ids for mid, ids in self.message_ids.items() if len(ids) > 1}

    @property
    def starred(self) -> int:
        return self.label_counts[STARRED]

    def label_name(self, label_id: str) -> str:
        label = self.labels.get(label_id)
        if label is not None and label.name:
            return label.name
        return SYSTEM_LABEL_NAMES.get(label_id, f"label {label_id}")

    def to_dict(self) -> dict:
        return {
            "export_dir": str(self.export_dir),
            "message_count": self.message_count,
            "total_bytes": self.total_bytes,
            "earliest": _iso(self.earliest),
            "latest": _iso(self.latest),
            "label_counts": {
                lid: {"name": self.label_name(lid), "kind": self._kind(lid), "count": n}
                for lid, n in sorted(self.label_counts.items())
            },
            "unknown_label_ids": dict(self.unknown_label_ids),
            "flag_values": {str(k): v for k, v in sorted(self.flag_values.items())},
            "draft_table": [
                {"in_drafts": d, "received_or_sent": rs, "imported": imp, "count": n,
                 "outcome": classify_draft_bits(d, rs, imp).value}
                for (d, rs, imp), n in sorted(self.draft_table.items())
            ],
            "draft_disagreements": self.draft_disagreements,
            "starred": self.starred,
            "skip_counts": {reason.value: n for reason, n in self.skip_counts.items()},
            "upload_count": self.upload_count,
            "upload_bytes": self.upload_bytes,
            "missing_message_id": self.missing_message_id,
            "duplicate_message_ids": self.duplicate_message_ids,
            "long_line": self.long_line,
            "long_line_signed": self.long_line_signed,
            "oversize": [{"id": i, "bytes": b} for i, b in self.oversize],
            "largest": [{"id": i, "bytes": b} for b, i in self.largest],
            "orphan_eml": self.orphan_eml,
            "orphan_metadata": self.orphan_metadata,
            "bad_metadata": [{"file": f, "error": e} for f, e in self.bad_metadata],
            "partial_files": self.partial_files,
            "logs_checked": [str(p) for p in self.logs_checked],
            "log_error_count": self.log_error_count,
            "log_errors": [{"file": str(p), "line": n, "text": t} for p, n, t in self.log_errors],
            "expected_count": self.expected_count,
            "missing_vs_log": self.missing_vs_log,
            "seconds_since_modified": self.seconds_since_modified,
            "leftover_dirs": self.leftover_dirs,
        }

    def _kind(self, label_id: str) -> str:
        label = self.labels.get(label_id)
        if label is not None:
            return label.kind
        return "system" if label_id.isdigit() else "unknown"


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat()


# --- locating and parsing -------------------------------------------------------------------


def find_export_dirs(root: Path) -> list[Path]:
    """Every directory at or below `root` that holds a labels.json."""
    root = Path(root)
    if not root.is_dir():
        raise ExportError(f"{root} is not a directory")
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if LABELS_FILE in filenames:
            found.append(Path(dirpath))
    return sorted(found)


def resolve_export_dir(root: Path) -> Path:
    """The one export folder under `root`; refuse to guess between several."""
    dirs = find_export_dirs(root)
    if not dirs:
        raise ExportError(f"no {LABELS_FILE} found under {root}; point scan at a Proton export folder")
    if len(dirs) > 1:
        listing = "\n  ".join(str(d) for d in dirs)
        raise ExportError(f"found {len(dirs)} Proton exports under {root}; pass one of:\n  {listing}")
    return dirs[0]


ISSUES_URL = "https://github.com/jonathan-opmodi/proton2m365/issues"
KNOWN_FORMATS = ("export-tool-v1", "unwrapped-v0")


class UnknownExportFormat(ExportError):
    pass


def detect_format(export_dir: Path) -> str:
    """Which Proton export layout this is, judged by labels.json and one metadata file.

    Raises UnknownExportFormat for anything else, so a changed Export Tool is reported instead of being
    misread. `export-tool-v1`: {"Version": 1, "Payload": [...]} with {ID, LabelIDs, Flags, Time} metadata.
    """
    try:
        doc = json.loads((Path(export_dir) / LABELS_FILE).read_bytes())
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read {Path(export_dir) / LABELS_FILE}: {exc}") from exc
    if isinstance(doc, dict) and doc.get("Version") == 1 and isinstance(doc.get("Payload"), list):
        fmt = "export-tool-v1"
    elif isinstance(doc, list):
        fmt = "unwrapped-v0"
    else:
        version = doc.get("Version") if isinstance(doc, dict) else type(doc).__name__
        raise UnknownExportFormat(_unknown(f"labels.json version {version!r}"))
    sample = next(iter(sorted(Path(export_dir).glob(f"*{METADATA_SUFFIX}"))), None)
    if sample is not None:
        try:
            meta = json.loads(sample.read_bytes())
        except (OSError, ValueError):
            return fmt  # one damaged file is plan's problem to report, not a format change
        payload = meta.get("Payload") if isinstance(meta, dict) else None
        if not isinstance(payload, dict) or not {"ID", "LabelIDs", "Flags", "Time"} <= set(payload):
            raise UnknownExportFormat(_unknown("metadata files without ID, LabelIDs, Flags and Time"))
    return fmt


def _unknown(what: str) -> str:
    return (f"P2M-EXPORT-FORMAT: unsupported Proton export format ({what}). Please open an issue at {ISSUES_URL} "
            "with `proton2m365 doctor --issue`; --allow-unknown-format tries anyway")


def export_fingerprint(export_dir: Path) -> dict:
    """What identifies an export: where it is and its labels.json (verify refuses a different one)."""
    labels = Path(export_dir) / LABELS_FILE
    return {"root": str(Path(export_dir).resolve()), "labels_sha256": hashlib.sha256(labels.read_bytes()).hexdigest()}


def _payload(doc):
    if isinstance(doc, dict) and "Payload" in doc:
        return doc["Payload"]
    return doc


def load_labels(path: Path) -> dict[str, Label]:
    try:
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read {path}: {exc}") from exc
    items = _payload(doc)
    if not isinstance(items, list):
        raise ExportError(f"{path}: expected a list of labels")
    labels: dict[str, Label] = {}
    for item in items:
        if not isinstance(item, dict) or "ID" not in item:
            raise ExportError(f"{path}: label entry without an ID: {item!r}")
        lid = str(item["ID"])
        labels[lid] = Label(
            id=lid,
            name=str(item.get("Name") or ""),
            path=str(item.get("Path") or item.get("Name") or ""),
            parent_id=(str(item["ParentID"]) if item.get("ParentID") else None),
            color=item.get("Color"),
            type=item.get("Type"),
        )
    return labels


def load_metadata(path: Path) -> MessageMeta:
    doc = json.loads(Path(path).read_bytes())
    payload = _payload(doc)
    if not isinstance(payload, dict) or "ID" not in payload:
        raise ValueError("no Payload.ID")
    labels = payload.get("LabelIDs") or []
    if not isinstance(labels, list):
        raise ValueError("LabelIDs is not a list")
    ts = payload.get("Time")
    return MessageMeta(
        proton_id=str(payload["ID"]),
        label_ids=tuple(str(x) for x in labels),
        flags=int(payload.get("Flags") or 0),
        time=int(ts) if ts is not None else None,
        unread=bool(payload.get("Unread")),
    )


def read_eml_facts(path: Path, *, with_sha256: bool = False) -> EmlFacts:
    """Size, Message-ID, Date, longest line and signed/encrypted status (and sha256), reading the file once."""
    header = bytearray()
    in_header = True
    max_line = 0
    pgp_armor = False
    digest = hashlib.sha256() if with_sha256 else None
    with open(path, "rb") as fh:
        for line in fh:
            if digest is not None:
                digest.update(line)
            stripped = line.rstrip(b"\r\n")
            max_line = max(max_line, len(stripped))
            if in_header:
                if not stripped:
                    in_header = False
                else:
                    header += line
            elif not pgp_armor and stripped.startswith(_PGP_ARMOR_PREFIXES):
                pgp_armor = True
    msg = BytesHeaderParser(policy=compat32).parsebytes(bytes(header))
    message_id = (msg.get("Message-ID") or "").strip() or None
    signed = pgp_armor or msg.get_content_type() in _SIGNED_CONTENT_TYPES
    return EmlFacts(
        size=Path(path).stat().st_size, message_id=message_id, max_line=max_line, signed=signed,
        date=_header_date(msg.get("Date")), sha256=digest.hexdigest() if digest is not None else None,
    )


def _header_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


# --- classification rules (shared with plan) ---------------------------------------------------


def classify_draft_bits(in_drafts: bool, received_or_sent: bool, imported: bool) -> DraftVerdict:
    """The draft truth table: skip only when both signals say draft; report every mismatch."""
    if in_drafts:
        return DraftVerdict.KEEP_DISAGREEMENT if received_or_sent else DraftVerdict.SKIP_DRAFT
    if received_or_sent:
        return DraftVerdict.KEEP
    return DraftVerdict.KEEP_IMPORTED if imported else DraftVerdict.KEEP_DISAGREEMENT


def draft_bits(meta: MessageMeta) -> tuple[bool, bool, bool]:
    return (
        bool(DRAFT_LABELS.intersection(meta.label_ids)),
        bool(meta.flags & (FLAG_RECEIVED | FLAG_SENT)),
        bool(meta.flags & FLAG_IMPORTED),
    )


def classify_draft(meta: MessageMeta) -> DraftVerdict:
    return classify_draft_bits(*draft_bits(meta))


def default_skip_reason(
    meta: MessageMeta, facts: EmlFacts, *, max_size: int, long_line_limit: int = LONG_LINE_LIMIT,
    include_trash: bool = False, include_spam: bool = False,
) -> SkipReason | None:
    """Why a message would not be uploaded; first match wins: draft, trash, spam, oversize, long-line-signed."""
    if classify_draft(meta) is DraftVerdict.SKIP_DRAFT:
        return SkipReason.DRAFT
    if TRASH in meta.label_ids and not include_trash:
        return SkipReason.TRASH
    if SPAM in meta.label_ids and not include_spam:
        return SkipReason.SPAM
    if facts.size > max_size:
        return SkipReason.OVERSIZE
    if facts.max_line > long_line_limit and facts.signed:
        return SkipReason.LONG_LINE_SIGNED
    return None


# --- target folder, categories and dates (plan) ---------------------------------------------------

INBOX_FOLDER = ("Inbox",)
SENT_FOLDER = ("Sent Items",)
ARCHIVE_FOLDER = ("Archive",)
UNFILED_FOLDER = ("Proton Unfiled",)
TRASH_FOLDER = ("Proton Trash",)
SPAM_FOLDER = ("Proton Spam",)
STARRED_CATEGORY = "Starred"
RESERVED_SUFFIX = " (Proton)"
# Root-level names Exchange reserves or uses for its own folders; a Proton folder with one of these
# names gets RESERVED_SUFFIX so it neither fails CREATE nor merges into the system folder.
EXCHANGE_RESERVED_ROOT_NAMES = frozenset(
    n.casefold() for n in (
        "Files", "Inbox", "Drafts", "Sent Items", "Deleted Items", "Junk Email", "Archive", "Outbox",
        "Conversation History", "Calendar", "Contacts", "Tasks", "Notes", "Journal",
    )
)
_MSGID_UNSAFE = re.compile(r"[^A-Za-z0-9!#$%&'*+\-/=?^_`{|}~.]")


def sanitize_segment(name: str) -> str:
    """One folder name as created in Exchange: NFC, no '/' (would split the path), never empty."""
    name = unicodedata.normalize("NFC", name).strip().replace("/", "\u2215")
    return name or "(unnamed)"


def folder_segments(label_id: str, labels: dict[str, Label]) -> tuple[str, ...]:
    """A Proton folder's path as sanitized segments, built from the ParentID chain."""
    chain: list[Label] = []
    current = labels.get(label_id)
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = labels.get(current.parent_id) if current.parent_id else None
    segments = [sanitize_segment(lab.name or lab.path) for lab in reversed(chain)]
    if segments and segments[0].casefold() in EXCHANGE_RESERVED_ROOT_NAMES:
        segments[0] += RESERVED_SUFFIX
    return tuple(segments)


def target_folder(
    meta: MessageMeta, labels: dict[str, Label], *, include_trash: bool = False, include_spam: bool = False,
) -> tuple[tuple[str, ...], bool]:
    """(folder segments, whether the message sat in more than one Proton folder)."""
    if include_trash and TRASH in meta.label_ids:
        return TRASH_FOLDER, False
    if include_spam and SPAM in meta.label_ids:
        return SPAM_FOLDER, False
    folders = sorted(
        folder_segments(lid, labels) for lid in set(meta.label_ids)
        if lid in labels and labels[lid].kind == "folder"
    )
    if folders:
        return folders[0], len(folders) > 1
    ids = set(meta.label_ids)
    if INBOX in ids:  # sent-to-self carries Inbox and Sent; the received copy belongs in Inbox
        return INBOX_FOLDER, False
    if ids & {SENT, ALL_SENT}:
        return SENT_FOLDER, False
    if ARCHIVE in ids:
        return ARCHIVE_FOLDER, False
    return UNFILED_FOLDER, False


def sanitize_category(name: str) -> str:
    """Outlook category name: NFC, no commas or semicolons (Outlook treats them as separators)."""
    name = unicodedata.normalize("NFC", name).replace(",", " ").replace(";", " ")
    return " ".join(name.split())


def resolve_label_selectors(selectors: Iterable[str], labels: dict[str, Label]) -> set[str]:
    """Map --exclude-label values (name, path or ID, case-insensitive) to user label IDs."""
    user_labels = [lab for lab in labels.values() if lab.kind == "label"]
    ids: set[str] = set()
    for raw in selectors:
        wanted = raw.strip().casefold()
        matches = [lab.id for lab in user_labels
                   if wanted in (lab.name.casefold(), lab.path.casefold()) or raw.strip() == lab.id]
        if not matches:
            names = ", ".join(sorted(lab.path for lab in user_labels)) or "(none)"
            raise ExportError(f"--exclude-label {raw!r} matches no label. Labels in this export: {names}")
        ids.update(matches)
    return ids


def categories_for(
    meta: MessageMeta, labels: dict[str, Label], *, excluded_ids: frozenset[str] | set[str] = frozenset(),
    starred_as_flag: bool = False,
) -> tuple[str, ...]:
    """Outlook categories: the message's user labels (never numeric system/category labels), plus Starred."""
    names = {
        sanitize_category(labels[lid].path or labels[lid].name)
        for lid in meta.label_ids
        if lid in labels and labels[lid].kind == "label" and lid not in excluded_ids
    }
    if STARRED in meta.label_ids and not starred_as_flag:
        names.add(STARRED_CATEGORY)
    names.discard("")
    return tuple(sorted(names, key=str.casefold))


def synthesized_message_id(proton_id: str) -> str:
    return f"<proton-{_MSGID_UNSAFE.sub('_', proton_id)}@proton2m365.invalid>"


def internal_date(meta: MessageMeta, facts: EmlFacts) -> int | None:
    """When the message arrived: Proton's Time, else the Date header."""
    return meta.time if meta.time is not None else facts.date


# --- inventory --------------------------------------------------------------------------------


def _log_candidates(export_dir: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    # The Export Tool writes <tool dir>/logs/<run>_export.log, two levels above mail_<timestamp>/.
    for base in (export_dir, export_dir.parent, export_dir.parent.parent):
        for d in (base, base / "logs", base / "log"):
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.log")) + sorted(d.glob("*.log.*")):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    yield p


def _scan_logs(inv: Inventory, keep: int = 50) -> None:
    """Read Export Tool logs. Prefer logs that name this export folder, so other runs don't count."""
    contents: list[tuple[Path, bytes]] = []
    for log in _log_candidates(inv.export_dir):
        try:
            contents.append((log, log.read_bytes()))
        except OSError as exc:
            inv.logs_checked.append(log)
            inv.log_errors.append((log, 0, f"could not read log: {exc}"))
            inv.log_error_count += 1
    marker = str(inv.export_dir).encode()
    relevant = [(p, data) for p, data in contents if marker in data] or contents
    for log, data in relevant:
        inv.logs_checked.append(log)
        for n, line in enumerate(data.splitlines(), 1):
            found = _LOG_FOUND_RE.search(line)
            if found:
                inv.expected_count = int(found.group(1))
            if _LOG_ERROR_RE.search(line):
                inv.log_error_count += 1
                if len(inv.log_errors) < keep:
                    inv.log_errors.append((log, n, line.decode("utf-8", "replace").strip()[:300]))


@dataclass
class ExportListing:
    """The files in one export folder, paired by message ID stem."""

    emls: dict[str, Path] = field(default_factory=dict)
    metas: dict[str, Path] = field(default_factory=dict)
    partial_files: list[str] = field(default_factory=list)
    leftover_dirs: list[str] = field(default_factory=list)

    @property
    def paired(self) -> list[str]:
        return sorted(set(self.emls) & set(self.metas))

    @property
    def orphan_eml(self) -> list[str]:
        return sorted(set(self.emls) - set(self.metas))

    @property
    def orphan_metadata(self) -> list[str]:
        return sorted(set(self.metas) - set(self.emls))


@dataclass(frozen=True)
class ExportMessage:
    meta: MessageMeta
    facts: EmlFacts
    eml_path: Path


def list_export(export_dir: Path) -> ExportListing:
    listing = ExportListing()
    for entry in os.scandir(export_dir):
        if entry.is_dir():
            if any(os.scandir(entry.path)):
                listing.leftover_dirs.append(entry.name)
            continue
        if not entry.is_file():
            continue
        name = entry.name
        if name.endswith(METADATA_SUFFIX):
            listing.metas[name[: -len(METADATA_SUFFIX)]] = Path(entry.path)
        elif name.endswith(EML_SUFFIX):
            listing.emls[name[: -len(EML_SUFFIX)]] = Path(entry.path)
        elif name.endswith(_PARTIAL_SUFFIXES):
            listing.partial_files.append(name)
    return listing


def iter_messages(
    listing: ExportListing,
    *,
    with_sha256: bool = False,
    progress: Callable[[int, int], None] | None = None,
    on_bad_file: Callable[[str, str], None] | None = None,
) -> Iterator[ExportMessage]:
    """Every paired message, in a stable order. Unreadable pairs go to `on_bad_file(name, error)`."""
    paired = listing.paired
    for i, stem in enumerate(paired, 1):
        if progress is not None:
            progress(i, len(paired))
        try:
            meta = load_metadata(listing.metas[stem])
        except (OSError, ValueError, TypeError) as exc:
            if on_bad_file is not None:
                on_bad_file(listing.metas[stem].name, str(exc))
            continue
        try:
            facts = read_eml_facts(listing.emls[stem], with_sha256=with_sha256)
        except OSError as exc:
            if on_bad_file is not None:
                on_bad_file(listing.emls[stem].name, f"cannot read .eml: {exc}")
            continue
        yield ExportMessage(meta=meta, facts=facts, eml_path=listing.emls[stem])


def build_inventory(
    export_dir: Path,
    *,
    max_size: int = DEFAULT_MAX_SIZE_BYTES,
    long_line_limit: int = LONG_LINE_LIMIT,
    progress: Callable[[int, int], None] | None = None,
) -> Inventory:
    """Read every message in one export folder. Read-only; no network."""
    export_dir = Path(export_dir)
    labels = load_labels(export_dir / LABELS_FILE)
    inv = Inventory(export_dir=export_dir, labels=labels, max_size=max_size, long_line_limit=long_line_limit)
    inv.seconds_since_modified = max(0.0, time.time() - export_dir.stat().st_mtime)

    listing = list_export(export_dir)
    inv.leftover_dirs = listing.leftover_dirs
    inv.partial_files = listing.partial_files
    inv.orphan_eml = listing.orphan_eml
    inv.orphan_metadata = listing.orphan_metadata
    sizes: list[tuple[int, str]] = []

    def bad(name: str, error: str) -> None:
        inv.bad_metadata.append((name, error))

    for msg in iter_messages(listing, progress=progress, on_bad_file=bad):
        meta, facts = msg.meta, msg.facts
        pid = meta.proton_id
        if facts.size == 0:
            inv.partial_files.append(msg.eml_path.name)

        inv.message_count += 1
        inv.total_bytes += facts.size
        sizes.append((facts.size, pid))
        if meta.time is not None:
            inv.earliest = meta.time if inv.earliest is None else min(inv.earliest, meta.time)
            inv.latest = meta.time if inv.latest is None else max(inv.latest, meta.time)

        for lid in meta.label_ids:
            inv.label_counts[lid] += 1
            if lid not in labels and lid not in SYSTEM_LABEL_NAMES:
                inv.unknown_label_ids[lid] += 1

        inv.flag_values[meta.flags] += 1
        bits = draft_bits(meta)
        inv.draft_table[bits] += 1
        if classify_draft_bits(*bits) is DraftVerdict.KEEP_DISAGREEMENT:
            inv.draft_disagreements.append(pid)

        if facts.message_id is None:
            inv.missing_message_id.append(pid)
        else:
            inv.message_ids.setdefault(facts.message_id, []).append(pid)

        if facts.max_line > long_line_limit:
            (inv.long_line_signed if facts.signed else inv.long_line).append(pid)
        if facts.size > max_size:
            inv.oversize.append((pid, facts.size))

        reason = default_skip_reason(meta, facts, max_size=max_size, long_line_limit=long_line_limit)
        if reason is None:
            inv.upload_count += 1
            inv.upload_bytes += facts.size
        else:
            inv.skip_counts[reason] += 1

    inv.largest = sorted(sizes, reverse=True)[:10]
    _scan_logs(inv)
    return inv
