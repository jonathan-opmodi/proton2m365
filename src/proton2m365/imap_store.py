"""Exchange Online over IMAP: folders, the exact bytes uploaded, pacing, and APPEND without duplicates.

Duplicate prevention (eng review D12): every uploaded message carries `X-Proton-Id`. The first time a
folder is used in a run, one header FETCH lists the Proton IDs already in it; those messages are not
sent again. After an ambiguous APPEND (connection dropped, server hung up) the folder's list is
refreshed for new UIDs before anything is re-sent, so a message that did land is never sent twice.
"""

from __future__ import annotations

import io
import quopri
import re
import socket
import ssl
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.generator import BytesGenerator
from email.parser import BytesParser
from email.policy import compat32

from .proton_export import LONG_LINE_LIMIT, SENT_FOLDER, ARCHIVE_FOLDER, INBOX_FOLDER

HOST = "outlook.office365.com"
PROTON_ID_HEADER = "X-Proton-Id"
FETCH_ITEM = "BODY.PEEK[HEADER.FIELDS (X-PROTON-ID)]"
FETCH_CHUNK = 500  # UIDs per FETCH command; keeps the command line short
# Adaptive pacing (additive increase, multiplicative decrease). The defaults are ceilings; --max-* lower them.
DEFAULT_MSGS_PER_SEC = 6.0           # ceiling; measured unpaced Exchange Online run: ~3.1 msgs/s, no throttling
DEFAULT_BYTES_PER_SEC = 2 * 1024 * 1024
START_MSGS_PER_SEC = 2.0
START_BYTES_PER_SEC = 400 * 1024
FLOOR_MSGS_PER_SEC = 0.5
FLOOR_BYTES_PER_SEC = 64 * 1024
STEP_MSGS_PER_SEC = 0.25
STEP_BYTES_PER_SEC = 64 * 1024
CLEAN_APPENDS_PER_STEP = 50
OBSERVED_WINDOW = 50                 # completed APPENDs the observed link rate is measured over
SLOW_FACTOR, SLOW_GRACE_SECONDS = 3.0, 10.0
DEFAULT_THROTTLE_WAIT = 300.0  # seconds, when the server throttles without saying for how long

_DNT_RE = re.compile(rb"(?im)^Disposition-Notification-To:")
# Exchange turns a sender's X-Message-Flag ("Follow up") into a real follow-up flag on the stored
# message; in Proton only the user's own star counts, so keep the header but under another name.
_MSG_FLAG_RE = re.compile(rb"(?im)^X-Message-Flag:")
_BARE_LF_RE = re.compile(rb"(?<!\r)\n")
_PROTON_ID_RE = re.compile(rb"(?im)^X-Proton-Id:[ \t]*(\S+)")
_APPENDUID_RE = re.compile(rb"\[APPENDUID (\d+) (\d+)\]")
_BACKOFF_RE = re.compile(r"Suggested Backoff Time:\s*(\d+)\s*milli", re.IGNORECASE)
_QUOTA_RE = re.compile(r"OVERQUOTA|quota|mailbox (?:is )?full", re.IGNORECASE)
_THROTTLE_RE = re.compile(r"throttl", re.IGNORECASE)


class ImapError(Exception):
    pass


class QuotaExceeded(ImapError):
    """The mailbox is full. Stop the run; nothing about this message is wrong."""


class AppendRejected(ImapError):
    """The server refused this message. Retrying it won't help."""


class UploadGaveUp(ImapError):
    """Too many connection failures or throttles for one message."""


# --- the bytes we upload ----------------------------------------------------------------------


def normalize_crlf(raw: bytes) -> bytes:
    return _BARE_LF_RE.sub(b"\r\n", raw)


def reencode_long_lines(raw: bytes, limit: int = LONG_LINE_LIMIT) -> bytes:
    """Re-encode text parts that have lines over `limit` bytes as quoted-printable. Decoded text is unchanged."""
    msg = BytesParser(policy=compat32).parsebytes(raw)
    changed = False
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        cte = (part.get("Content-Transfer-Encoding") or "7bit").strip().lower()
        if cte in ("base64", "quoted-printable"):
            continue  # transport lines are already short
        text = part.get_payload(decode=True) or b""  # 7bit/8bit/binary: the bytes as sent
        if max((len(line) for line in text.splitlines()), default=0) <= limit:
            continue
        text = text.replace(b"\r\n", b"\n")
        del part["Content-Transfer-Encoding"]
        part["Content-Transfer-Encoding"] = "quoted-printable"
        part.set_payload(quopri.encodestring(text).decode("ascii"))
        changed = True
    if not changed:
        return raw
    out = io.BytesIO()
    BytesGenerator(out, mangle_from_=False, maxheaderlen=0, policy=compat32).flatten(msg)
    return out.getvalue()


def prepare_message(
    raw: bytes,
    *,
    proton_id: str,
    synthesized_message_id: str | None = None,
    needs_reencode: bool = False,
    long_line_limit: int = LONG_LINE_LIMIT,
) -> bytes:
    """The documented changes and nothing else: X-Proton-Id, Message-ID if missing, receipts and sender flags off, CRLF."""
    if needs_reencode:
        raw = reencode_long_lines(raw, long_line_limit)
    raw = normalize_crlf(raw)
    head, sep, body = raw.partition(b"\r\n\r\n")
    head = _DNT_RE.sub(b"X-Original-Disposition-Notification-To:", head)
    head = _MSG_FLAG_RE.sub(b"X-Original-Message-Flag:", head)
    added = f"{PROTON_ID_HEADER}: {proton_id}\r\n"
    if synthesized_message_id:
        added += f"Message-ID: {synthesized_message_id}\r\n"
    return added.encode() + head + b"\r\n\r\n" + body


# --- reading server replies -------------------------------------------------------------------


def parse_backoff_seconds(text: str) -> float | None:
    """'Request is throttled. Suggested Backoff Time: 299961 milliseconds' -> 299.961."""
    match = _BACKOFF_RE.search(text)
    return int(match.group(1)) / 1000 if match else None


def is_throttled(text: str) -> bool:
    return bool(_THROTTLE_RE.search(text)) or parse_backoff_seconds(text) is not None


def is_quota_error(text: str) -> bool:
    return bool(_QUOTA_RE.search(text))


def parse_appenduid(response: bytes | str | None) -> tuple[int, int] | None:
    if response is None:
        return None
    data = response.encode() if isinstance(response, str) else response
    match = _APPENDUID_RE.search(data)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_proton_id(header_block: bytes) -> str | None:
    match = _PROTON_ID_RE.search(header_block)
    return match.group(1).decode("ascii", "replace") if match else None


# --- pacing -----------------------------------------------------------------------------------


class Pacer:
    """Adaptive upload pacing: a message rate and a byte rate that rise while APPENDs go well and halve
    on any congestion signal (a throttle reply, a dropped connection, or an APPEND much slower than this
    link usually is). The flags set the ceiling; the controller never goes below the floor."""

    def __init__(
        self,
        msgs_per_sec: float = DEFAULT_MSGS_PER_SEC,
        bytes_per_sec: float = DEFAULT_BYTES_PER_SEC,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if msgs_per_sec <= 0 or bytes_per_sec <= 0:
            raise ValueError("rates must be positive")
        self.max_msgs, self.max_bytes = msgs_per_sec, bytes_per_sec
        # A ceiling below the start (or the floor) lowers them: the user's limit always wins.
        self.floor_msgs = min(FLOOR_MSGS_PER_SEC, msgs_per_sec)
        self.floor_bytes = min(FLOOR_BYTES_PER_SEC, bytes_per_sec)
        self.msgs_per_sec = min(START_MSGS_PER_SEC, msgs_per_sec)
        self.bytes_per_sec = min(START_BYTES_PER_SEC, bytes_per_sec)
        self.clock = clock
        self.sleep = sleep
        self._next: float | None = None
        self.started: float | None = None
        self.sent = 0
        self.sent_bytes = 0
        self.clean = 0                                   # APPENDs since the last change of rate
        self._window: deque[tuple[int, float]] = deque(maxlen=OBSERVED_WINDOW)

    # -- before each APPEND ----------------------------------------------------------------------

    def wait(self, size: int) -> None:
        now = self.clock()
        if self.started is None:
            self.started = now
        if self._next is not None and self._next > now:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + max(1 / self.msgs_per_sec, size / self.bytes_per_sec)
        self.sent += 1
        self.sent_bytes += size

    def push_back(self, seconds: float) -> None:
        """After a server-imposed wait, start the pacing clock again from now."""
        self._next = self.clock() + seconds

    # -- after each APPEND -----------------------------------------------------------------------

    @property
    def observed_bytes_per_sec(self) -> float | None:
        """Bytes over seconds across the last completed APPENDs (not an average of per-message rates,
        which small, latency-bound messages would drag down)."""
        seconds = sum(s for _, s in self._window)
        return sum(b for b, _ in self._window) / seconds if self._window and seconds > 0 else None

    def is_slow(self, size: int, seconds: float) -> bool:
        if len(self._window) < OBSERVED_WINDOW:      # warm-up: no idea yet what this link does
            return False
        observed = self.observed_bytes_per_sec
        return observed is not None and seconds > SLOW_FACTOR * (size / observed) + SLOW_GRACE_SECONDS

    def completed(self, size: int, seconds: float) -> str | None:
        """Record a successful APPEND. Returns why the rate changed, if it did."""
        slow = self.is_slow(size, seconds)
        self._window.append((size, seconds))
        if slow:
            return self.congestion(f"an APPEND took {seconds:.0f}s, far longer than this link usually needs")
        self.clean += 1
        if self.clean >= CLEAN_APPENDS_PER_STEP:  # rising is routine: no line for it
            self.clean = 0
            self.msgs_per_sec = min(self.max_msgs, self.msgs_per_sec + STEP_MSGS_PER_SEC)
            self.bytes_per_sec = min(self.max_bytes, self.bytes_per_sec + STEP_BYTES_PER_SEC)
        return None

    def congestion(self, reason: str) -> str:
        """Halve both rates (not below the floor). Returns a line saying so."""
        self.clean = 0
        self.msgs_per_sec = max(self.floor_msgs, self.msgs_per_sec / 2)
        self.bytes_per_sec = max(self.floor_bytes, self.bytes_per_sec / 2)
        return f"slowing to {self.msgs_per_sec:g} msgs/s and {self.bytes_per_sec / 1024:,.0f} KB/s: {reason}"


# --- the store --------------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadResult:
    uid: int | None
    already_present: bool  # it was in the folder already (earlier run, or it landed before a disconnect)


def _default_client():
    from imapclient import IMAPClient

    return IMAPClient(HOST, ssl=True, timeout=120)


def is_connection_error(exc: BaseException) -> bool:
    from imapclient.exceptions import IMAPClientAbortError

    return isinstance(exc, (IMAPClientAbortError, OSError, socket.timeout, ssl.SSLError))


class ImapStore:
    def __init__(
        self,
        username: str,
        token: Callable[[], str],
        *,
        pacer: Pacer | None = None,
        client_factory: Callable[[], object] = _default_client,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        max_throttle_waits: int = 10,
        say: Callable[[str], None] = lambda _msg: None,
    ):
        self.username = username
        self.token = token
        self.pacer = pacer or Pacer(sleep=sleep)
        self.client_factory = client_factory
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.max_throttle_waits = max_throttle_waits
        self.say = say
        self.client = None
        self.delimiter = "/"
        self._special: dict[tuple[str, ...], str] = {}
        self._trash: str | None = None
        self._existing: dict[str, str] = {}             # casefolded name -> name
        self._present: dict[str, dict[str, int | None]] = {}  # folder -> {proton_id: uid}
        self._validity: dict[str, int | None] = {}  # folder -> UIDVALIDITY seen at its last SELECT
        self._max_uid: dict[str, int] = {}
        self._dupes: dict[str, dict[str, set[int]]] = {}  # folder -> {proton_id: uids}, when more than one
        self._selected: str | None = None

    # -- connection ------------------------------------------------------------------------------

    def connect(self) -> None:
        client = self.client_factory()
        client.oauth2_login(self.username, self.token())
        self.client = client
        self._selected = None
        self._load_folders()

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:  # noqa: BLE001 - closing a dead connection must not mask the real error
                pass
            self.client = None

    def _reconnect(self) -> None:
        self.close()
        self.connect()

    def _load_folders(self) -> None:
        self._existing.clear()
        self._special.clear()
        for flags, delimiter, name in self.client.list_folders():
            if delimiter:
                self.delimiter = delimiter.decode() if isinstance(delimiter, bytes) else delimiter
            self._existing[name.casefold()] = name
            names = {f.decode() if isinstance(f, bytes) else f for f in flags}
            if "\\Sent" in names:
                self._special[SENT_FOLDER] = name
            if "\\Archive" in names:
                self._special[ARCHIVE_FOLDER] = name
            if "\\Trash" in names:
                self._trash = name

    # -- folders ---------------------------------------------------------------------------------

    def imap_name(self, path: tuple[str, ...]) -> str:
        if path == INBOX_FOLDER:
            return "INBOX"
        if path in self._special:
            return self._special[path]
        return self.delimiter.join(path)

    def ensure_folders(self, paths: Iterable[tuple[str, ...]]) -> list[str]:
        """Create every missing folder (and parent). Names compare case-insensitively, like Exchange."""
        created = []
        for path in sorted(set(paths)):
            if path == INBOX_FOLDER or path in self._special:
                continue
            for depth in range(1, len(path) + 1):
                name = self.delimiter.join(path[:depth])
                if name.casefold() in self._existing:
                    continue
                try:
                    self.client.create_folder(name)
                except Exception as exc:  # noqa: BLE001
                    if "ALREADYEXISTS" not in str(exc).upper():
                        raise ImapError(f"could not create folder {name!r}: {exc}") from exc
                self._existing[name.casefold()] = name
                created.append(name)
        return created

    def has_folder(self, path: tuple[str, ...]) -> bool:
        return path == INBOX_FOLDER or path in self._special or self.imap_name(path).casefold() in self._existing

    def delete_folder(self, path: tuple[str, ...]) -> None:
        name = self.imap_name(path)
        if self._selected == name:
            self.client.close_folder()  # can't delete the selected folder
            self._selected = None
        self.client.delete_folder(name)
        self._existing.pop(name.casefold(), None)
        self._present.pop(name, None)
        self._max_uid.pop(name, None)

    def folders_under(self, top: str) -> list[tuple[str, ...]]:
        """Existing folders whose top level starts with `top` (case-insensitive), deepest first."""
        out = []
        for name in self._existing.values():
            path = tuple(name.split(self.delimiter))
            if path[0].casefold().startswith(top.casefold()):
                out.append(path)
        return sorted(out, key=lambda p: (-len(p), p))

    def message_count(self, path: tuple[str, ...]) -> int:
        return int(self._select(self.imap_name(path)).get(b"EXISTS", 0))

    def remove(self, path: tuple[str, ...], proton_ids: Iterable[str]) -> set[str]:
        """Move these messages (by X-Proton-Id) to Deleted Items. Returns the ids found and moved.

        Reads the folder fresh first, so only messages proven to carry one of these ids are touched. Needs
        IMAP MOVE (Exchange Online and Outlook.com have it): the COPY + expunge fallback would leave a second
        copy in Deleted Items whenever a COPY succeeded but its reply was lost and the run was repeated.
        """
        if self._trash is None:
            raise ImapError("no Deleted Items folder (\\Trash) found over IMAP")
        if b"MOVE" not in self.client.capabilities():
            raise ImapError("P2M-IMAP-NO-MOVE: this server does not support IMAP MOVE, which removing messages needs; "
                            "nothing was moved")
        folder = self.imap_name(path)
        if folder.casefold() not in self._existing:
            return set()
        self._load_presence(folder)
        present = self._present[folder]
        wanted = {pid: present[pid] for pid in proton_ids if present.get(pid) is not None}
        uids = sorted(wanted.values())
        for start in range(0, len(uids), FETCH_CHUNK):
            self.client.move(uids[start:start + FETCH_CHUNK], self._trash)
        for pid in wanted:
            present.pop(pid, None)
        self._present.pop(self._trash, None)  # Deleted Items changed; read it fresh next time
        return set(wanted)

    def uid_validity(self, path: tuple[str, ...]) -> int | None:
        """The folder's UIDVALIDITY when it was last read (UIDs are only meaningful together with it)."""
        return self._validity.get(self.imap_name(path))

    def move_uids(self, path: tuple[str, ...], uids: Iterable[int], *, validity: int | None) -> list[int]:
        """Move these exact messages (by UID) to Deleted Items; for dedupe, which keeps one copy of a Proton id.

        The UIDs were read under `validity`; if the folder's UIDVALIDITY has changed since (the server renumbered
        it), nothing is moved. Only UIDs still in the folder are moved; returns them."""
        if self._trash is None:
            raise ImapError("no Deleted Items folder (\\Trash) found over IMAP")
        if b"MOVE" not in self.client.capabilities():
            raise ImapError("P2M-IMAP-NO-MOVE: this server does not support IMAP MOVE, which removing messages needs; "
                            "nothing was moved")
        folder = self.imap_name(path)
        self._select(folder)
        if validity is not None and self._validity.get(folder) != validity:
            raise ImapError(f"P2M-IMAP-UIDVALIDITY: the server renumbered {folder} since it was read, so nothing was "
                            "moved; run the command again")
        wanted = sorted(set(uids))
        still = set(self.client.search(["UID", f"{wanted[0]}:*"])) if wanted else set()
        moving = [u for u in wanted if u in still]
        for start in range(0, len(moving), FETCH_CHUNK):
            self.client.move(moving[start:start + FETCH_CHUNK], self._trash)
        self._present.pop(folder, None)
        self._dupes.pop(folder, None)
        self._present.pop(self._trash, None)
        return moving

    @property
    def trash_path(self) -> tuple[str, ...]:
        if self._trash is None:
            raise ImapError("no Deleted Items folder (\\Trash) found over IMAP")
        return tuple(self._trash.split(self.delimiter))

    def purge(self, path: tuple[str, ...], proton_ids: Iterable[str]) -> set[str]:
        """Permanently delete these messages (by X-Proton-Id) from one folder. Used only for selftest mail."""
        folder = self.imap_name(path)
        self._load_presence(folder)
        present = self._present[folder]
        wanted = {pid: present[pid] for pid in proton_ids if present.get(pid) is not None}
        if wanted:
            uids = sorted(wanted.values())
            self.client.delete_messages(uids)
            self.client.uid_expunge(uids)
            for pid in wanted:
                present.pop(pid, None)
        return set(wanted)

    def reconnect(self) -> None:
        """Fresh connection (and fresh token) after a dropped one."""
        self._reconnect()

    # -- presence --------------------------------------------------------------------------------

    def _select(self, folder: str) -> dict:
        info = self.client.select_folder(folder)
        self._selected = folder
        validity = info.get(b"UIDVALIDITY")
        self._validity[folder] = int(validity) if validity is not None else None
        return info

    def _fetch_ids(self, folder: str, first_uid: int) -> None:
        present = self._present.setdefault(folder, {})
        highest = self._max_uid.get(folder, 0)
        # imapclient's fetch() only takes explicit UIDs (a "N:*" string raises ValueError), so
        # find the UIDs with UID SEARCH first. "N:*" also returns the last message when N is past it.
        uids = sorted(u for u in self.client.search(["UID", f"{first_uid}:*"]) if u >= first_uid)
        for start in range(0, len(uids), FETCH_CHUNK):
            chunk = uids[start:start + FETCH_CHUNK]
            response = self.client.fetch(chunk, [FETCH_ITEM])
            for uid, data in response.items():
                for key, value in data.items():
                    if isinstance(key, bytes) and key.startswith(b"BODY[") and isinstance(value, bytes):
                        pid = parse_proton_id(value)
                        if pid:
                            if pid in present and present[pid] != uid:
                                dupes = self._dupes.setdefault(folder, {})
                                dupes.setdefault(pid, {present[pid]}).add(uid)
                            present[pid] = uid
        if uids:
            highest = max(highest, uids[-1])
        self._max_uid[folder] = highest

    def _load_presence(self, folder: str) -> None:
        info = self._select(folder)
        self._present[folder] = {}
        self._max_uid[folder] = 0
        if info.get(b"EXISTS", 0):
            self._fetch_ids(folder, 1)

    def _refresh_presence(self, folder: str) -> None:
        if folder not in self._present:
            self._load_presence(folder)
            return
        info = self._select(folder)
        if info.get(b"EXISTS", 0):
            self._fetch_ids(folder, self._max_uid[folder] + 1)

    def fresh_ids(self, path: tuple[str, ...]) -> dict[str, int | None]:
        """Like present_ids, but read from the server now (another computer may have changed the folder)."""
        folder = self.imap_name(path)
        if folder.casefold() not in self._existing:
            return {}
        self._load_presence(folder)
        return dict(self._present[folder])

    def duplicates(self, path: tuple[str, ...]) -> dict[str, list[int]]:
        """Proton ids that more than one message in this folder carries (read fresh), with their UIDs."""
        folder = self.imap_name(path)
        if folder.casefold() not in self._existing:
            return {}
        self._dupes.pop(folder, None)
        self._load_presence(folder)
        return {pid: sorted(uids) for pid, uids in self._dupes.get(folder, {}).items()}

    def present_ids(self, path: tuple[str, ...]) -> dict[str, int | None]:
        folder = self.imap_name(path)
        if folder not in self._present:
            self._load_presence(folder)
        return self._present[folder]

    # -- upload ----------------------------------------------------------------------------------

    def upload(
        self, path: tuple[str, ...], proton_id: str, message: bytes, *, seen: bool, internal_date: int | None,
    ) -> UploadResult:
        """APPEND once. Raises QuotaExceeded, AppendRejected or UploadGaveUp; never sends a duplicate."""
        folder = self.imap_name(path)
        present = self.present_ids(path)
        if proton_id in present:
            return UploadResult(present[proton_id], True)
        flags = (r"\Seen",) if seen else ()
        msg_time = datetime.fromtimestamp(internal_date, UTC) if internal_date is not None else None
        failures = 0
        throttles = 0
        while True:
            self.pacer.wait(len(message))
            began = self.pacer.clock()
            try:
                response = self.client.append(folder, message, flags=flags, msg_time=msg_time)
            except Exception as exc:  # noqa: BLE001 - classified below
                text = str(exc)
                if is_throttled(text):
                    throttles += 1
                    self.say(self.pacer.congestion("Exchange asked us to slow down"))
                    if throttles > self.max_throttle_waits:
                        raise UploadGaveUp(f"throttled {throttles} times: {text}") from exc
                    wait = parse_backoff_seconds(text) or DEFAULT_THROTTLE_WAIT
                    self.say(f"Waiting {wait:.0f}s as Exchange asked")
                    self.sleep(wait)
                    self.pacer.push_back(0)
                elif is_connection_error(exc):
                    failures += 1
                    self.say(self.pacer.congestion("the connection dropped during an upload"))
                    if failures >= self.max_attempts:
                        raise UploadGaveUp(f"{failures} connection failures: {text}") from exc
                elif is_quota_error(text):
                    raise QuotaExceeded(text) from exc
                else:
                    raise AppendRejected(text) from exc
                # The APPEND may or may not have landed: reconnect and look before sending again.
                self._reconnect()
                self._refresh_presence(folder)
                present = self._present[folder]
                if proton_id in present:
                    return UploadResult(present[proton_id], True)
                continue
            change = self.pacer.completed(len(message), self.pacer.clock() - began)
            if change:
                self.say(change)
            appended = parse_appenduid(response)
            uid = appended[1] if appended else None
            present[proton_id] = uid
            if uid is not None:
                self._max_uid[folder] = max(self._max_uid.get(folder, 0), uid)
            return UploadResult(uid, False)
