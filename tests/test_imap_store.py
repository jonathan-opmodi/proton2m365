from __future__ import annotations

import quopri
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import compat32

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from proton2m365 import imap_store as im

from .conftest import eml_bytes


# --- the bytes we upload ----------------------------------------------------------------------

def test_prepare_message_adds_id_header_and_normalizes_line_endings():
    raw = b"From: a@x\nSubject: hi\nMessage-ID: <m@x>\n\nline one\nline two\r\n"
    out = im.prepare_message(raw, proton_id="PID==")
    assert out.startswith(b"X-Proton-Id: PID==\r\nFrom: a@x\r\n")
    assert b"\n" not in out.replace(b"\r\n", b"")              # no bare LF left
    head, _, body = out.partition(b"\r\n\r\n")
    assert body == b"line one\r\nline two\r\n"
    assert head.count(b"Message-ID") == 1


def test_prepare_message_synthesizes_message_id_only_when_asked():
    out = im.prepare_message(eml_bytes(message_id=None), proton_id="p", synthesized_message_id="<proton-p@proton2m365.invalid>")
    assert b"Message-ID: <proton-p@proton2m365.invalid>\r\n" in out


def test_prepare_message_defuses_sender_follow_up_flag():
    # seen live: some retailer receipts carry "X-Message-Flag: Follow up" and Exchange flagged them
    out = im.prepare_message(eml_bytes(extra_headers="X-Message-Flag: Follow up\r\n"), proton_id="p")
    head = out.partition(b"\r\n\r\n")[0]
    assert b"X-Original-Message-Flag: Follow up" in head
    assert not [l for l in head.split(b"\r\n") if l.lower().startswith(b"x-message-flag")]


def test_prepare_message_defuses_read_receipts_in_headers_only():
    raw = eml_bytes(extra_headers="disposition-notification-to: a@x\r\n",
                    body=b"Disposition-Notification-To: stays in body\r\n")
    out = im.prepare_message(raw, proton_id="p")
    head, _, body = out.partition(b"\r\n\r\n")
    assert b"X-Original-Disposition-Notification-To: a@x" in head
    assert not [l for l in head.split(b"\r\n") if l.lower().startswith(b"disposition-notification-to")]
    assert body == b"Disposition-Notification-To: stays in body\r\n"


def test_prepare_message_leaves_everything_else_alone():
    raw = eml_bytes(body=b"caf\xc3\xa9\r\n")
    out = im.prepare_message(raw, proton_id="p")
    assert out == b"X-Proton-Id: p\r\n" + raw


def _decoded_text(raw: bytes, subtype: str) -> bytes:
    msg = message_from_bytes(raw, policy=compat32)
    for part in msg.walk():
        if part.get_content_type() == f"text/{subtype}":
            return part.get_payload(decode=True)
    raise AssertionError("part not found")


def test_reencode_long_lines_is_lossless_and_short():
    html = b"<p>" + b"word " * 4000 + b"</p>\r\n<p>caf\xc3\xa9</p>\r\n"
    raw = (b"From: a@x\r\nMIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=BB\r\n\r\n"
           b"--BB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nshort text\r\n"
           b"--BB\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n" + html +
           b"--BB--\r\n")
    out = im.prepare_message(raw, proton_id="p", needs_reencode=True)
    assert max(len(line) for line in out.split(b"\r\n")) <= 78
    assert _decoded_text(out, "html") == _decoded_text(raw, "html")   # same text, byte for byte
    assert _decoded_text(out, "plain") == _decoded_text(raw, "plain") == b"short text"  # untouched part
    assert b"Content-Transfer-Encoding: quoted-printable" in out


def test_reencode_noop_when_lines_are_short():
    raw = eml_bytes()
    assert im.reencode_long_lines(raw) == raw


# --- server replies ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "seconds"),
    [("Request is throttled. Suggested Backoff Time: 299961 milliseconds", 299.961),
     ("suggested backoff time: 5000 millisecond", 5.0),
     ("NO APPEND failed", None)],
)
def test_parse_backoff(text, seconds):
    assert im.parse_backoff_seconds(text) == seconds


@pytest.mark.parametrize(("text", "quota"), [("[OVERQUOTA] mailbox full", True), ("Mailbox is full", True),
                                             ("Quota exceeded", True), ("BAD Command Argument Error. 11", False)])
def test_is_quota_error(text, quota):
    assert im.is_quota_error(text) is quota


def test_parse_appenduid():
    assert im.parse_appenduid(b"[APPENDUID 14 1234] APPEND completed.") == (14, 1234)
    assert im.parse_appenduid(b"APPEND completed.") is None
    assert im.parse_appenduid(None) is None


# --- pacing -----------------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_pacer_message_rate():
    c = FakeClock()
    p = im.Pacer(msgs_per_sec=1, bytes_per_sec=10**9, clock=c.clock, sleep=c.sleep)
    for _ in range(3):
        p.wait(100)
    assert c.slept == [1.0, 1.0]


def test_pacer_byte_rate_wins_for_big_messages():
    c = FakeClock()
    p = im.Pacer(msgs_per_sec=1, bytes_per_sec=1000, clock=c.clock, sleep=c.sleep)
    p.wait(5000)
    p.wait(10)
    assert c.slept == [5.0]


# --- adaptive pacing ------------------------------------------------------------------------------

def pacer(**kwargs):
    c = FakeClock()
    return im.Pacer(clock=c.clock, sleep=c.sleep, **kwargs), c


def clean(p, n, size=90_000, seconds=0.3):
    for _ in range(n):
        assert p.completed(size, seconds) is None


def test_pacer_starts_moderate_and_ramps_to_the_ceiling():
    p, _ = pacer()
    assert (p.msgs_per_sec, p.bytes_per_sec) == (im.START_MSGS_PER_SEC, im.START_BYTES_PER_SEC)
    clean(p, 249)
    assert p.msgs_per_sec == 3.0
    clean(p, 1)
    assert p.msgs_per_sec >= 3.1        # success criterion: >= 3.1 msgs/s after 250 clean appends
    clean(p, 5000)
    assert (p.msgs_per_sec, p.bytes_per_sec) == (im.DEFAULT_MSGS_PER_SEC, im.DEFAULT_BYTES_PER_SEC)


def test_one_throttle_halves_both_rates_and_restarts_the_count():
    p, _ = pacer()
    clean(p, 49)
    line = p.congestion("Exchange asked us to slow down")
    assert (p.msgs_per_sec, p.bytes_per_sec) == (1.0, 200 * 1024)
    assert "slowing to 1 msgs/s" in line
    clean(p, 49)
    assert p.msgs_per_sec == 1.0         # 49 since the halving: not yet a step
    clean(p, 1)
    assert p.msgs_per_sec == 1.25


def test_three_connection_drops_in_a_row_reach_the_floor():
    p, _ = pacer()
    for _ in range(3):
        p.congestion("the connection dropped")
    assert (p.msgs_per_sec, p.bytes_per_sec) == (im.FLOOR_MSGS_PER_SEC, im.FLOOR_BYTES_PER_SEC)


def test_a_big_message_at_normal_link_speed_is_not_slow_even_after_the_byte_rate_ramped():
    p, _ = pacer()
    link = 300 * 1024
    clean(p, 1300, size=92_000, seconds=92_000 / link)   # byte rate ramps all the way to 2 MB/s
    assert p.bytes_per_sec == im.DEFAULT_BYTES_PER_SEC
    before = p.msgs_per_sec
    assert p.completed(35 * 1024 * 1024, 120.0) is None  # 35 MB in 120 s on a ~300 KB/s link
    assert p.msgs_per_sec == before


def test_a_truly_slow_append_halves():
    p, _ = pacer()
    clean(p, 60, size=100_000, seconds=0.1)              # ~1 MB/s observed
    assert "far longer" in p.completed(100_000, 30.0)
    assert p.msgs_per_sec < im.START_MSGS_PER_SEC + 0.25


def test_no_slow_check_during_warm_up():
    p, _ = pacer()
    clean(p, 3, size=100_000, seconds=0.1)
    assert p.completed(100_000, 300.0) is None           # 4th APPEND: too early to call anything slow


def test_mixed_small_and_large_messages_measure_bytes_over_seconds():
    p, _ = pacer()
    for i in range(50):                                  # small messages are latency-bound: 2 KB in 0.2 s
        p.completed(500_000 if i % 10 == 0 else 2_000, 1.0 if i % 10 == 0 else 0.2)
    observed = p.observed_bytes_per_sec
    assert observed == pytest.approx((5 * 500_000 + 45 * 2_000) / (5 * 1.0 + 45 * 0.2))
    assert p.completed(500_000, 1.2) is None             # a normal large message is not slow


def test_a_ceiling_below_the_start_lowers_the_start_and_floor():
    p, _ = pacer(msgs_per_sec=0.3, bytes_per_sec=50_000)
    assert (p.msgs_per_sec, p.bytes_per_sec) == (0.3, 50_000)
    p.congestion("x")
    assert (p.msgs_per_sec, p.bytes_per_sec) == (0.3, 50_000)   # floor follows the ceiling
    clean(p, 500)
    assert (p.msgs_per_sec, p.bytes_per_sec) == (0.3, 50_000)


# --- a fake Exchange IMAP server ----------------------------------------------------------------

class FakeServer:
    def __init__(self, folders=("INBOX", "Sent Items", "Archive", "Deleted Items")):
        self.folders = {name: [] for name in folders}
        self.special = {"Sent Items": (b"\\HasNoChildren", b"\\Sent"), "Archive": (b"\\Archive",),
                        "Deleted Items": (b"\\Trash",)}
        self.move_capable = True
        self.uidvalidity = 1
        self.next_uid = 100
        self.on_append: list[str] = []
        self.logins = 0
        self.fetches: list[tuple[str, list[int]]] = []
        self.searches: list[tuple[str, str]] = []

    def store(self, folder, msg, flags=(), msg_time=None):
        self.next_uid += 1
        self.folders[folder].append((self.next_uid, msg, flags, msg_time))
        return self.next_uid

    def copies(self, folder, pid):
        return sum(1 for _, msg, _, _ in self.folders[folder] if im.parse_proton_id(msg) == pid)


class FakeClient:
    def __init__(self, server: FakeServer):
        self.s = server
        self.selected = None

    def oauth2_login(self, user, token):
        self.s.logins += 1

    def list_folders(self):
        return [(self.s.special.get(n, (b"\\HasNoChildren",)), b"/", n) for n in self.s.folders]

    def create_folder(self, name):
        if name in self.s.folders:
            raise IMAPClientError("NO [ALREADYEXISTS] Folder exists")
        self.s.folders[name] = []

    def select_folder(self, name):
        self.selected = name
        return {b"EXISTS": len(self.s.folders[name]), b"UIDNEXT": self.s.next_uid + 1,
                b"UIDVALIDITY": self.s.uidvalidity}

    def search(self, criteria):
        assert criteria[0] == "UID" and criteria[1].endswith(":*"), criteria
        first = int(criteria[1].split(":")[0])
        self.s.searches.append((self.selected, criteria[1]))
        msgs = self.s.folders[self.selected]
        return [u for u, _, _, _ in msgs if u >= first] or [u for u, _, _, _ in msgs[-1:]]  # "N:*" quirk

    def fetch(self, uids, items):
        if isinstance(uids, (str, bytes)):  # like imapclient 4.x: to_ints() fails on "N:*"
            raise ValueError(f"invalid literal for int() with base 10: {uids!r}")
        self.s.fetches.append((self.selected, list(uids)))
        out = {}
        for uid, msg, _, _ in self.s.folders[self.selected]:
            if uid not in uids:
                continue
            head = msg.partition(b"\r\n\r\n")[0]
            line = next((l for l in head.split(b"\r\n") if l.lower().startswith(b"x-proton-id:")), b"")
            out[uid] = {b"SEQ": 1, b"BODY[HEADER.FIELDS (X-PROTON-ID)]": line + b"\r\n\r\n"}
        return out

    def capabilities(self):
        return (b"IMAP4REV1", b"UIDPLUS") + ((b"MOVE",) if self.s.move_capable else ())

    def _take(self, uids, dest, remove):
        src = self.s.folders[self.selected]
        for item in [m for m in src if m[0] in uids]:
            self.s.store(dest, item[1], item[2], item[3])
            if remove:
                src.remove(item)

    def move(self, uids, dest):
        self._take(uids, dest, remove=True)

    def copy(self, uids, dest):
        self._take(uids, dest, remove=False)

    def delete_messages(self, uids):
        self.s.deleted_flagged = getattr(self.s, "deleted_flagged", set()) | set(uids)

    def uid_expunge(self, uids):
        flagged = getattr(self.s, "deleted_flagged", set())
        self.s.folders[self.selected] = [m for m in self.s.folders[self.selected]
                                         if not (m[0] in uids and m[0] in flagged)]

    def append(self, folder, msg, flags=(), msg_time=None):
        behavior = self.s.on_append.pop(0) if self.s.on_append else "ok"
        if behavior == "abort_after_store":
            self.s.store(folder, msg, flags, msg_time)
            raise IMAPClientAbortError("socket error: EOF")
        if behavior == "abort_before_store":
            raise IMAPClientAbortError("socket error: connection reset")
        if behavior == "throttle":
            raise IMAPClientAbortError("Request is throttled. Suggested Backoff Time: 299961 milliseconds")
        if behavior == "quota":
            raise IMAPClientError("APPEND failed: [OVERQUOTA] Mailbox is full")
        if behavior == "reject":
            raise IMAPClientError("BAD Command Argument Error. 11")
        uid = self.s.store(folder, msg, flags, msg_time)
        return f"[APPENDUID 1 {uid}] APPEND completed.".encode()

    def logout(self):
        pass


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def store(server):
    c = FakeClock()
    s = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server), sleep=c.sleep,
                     pacer=im.Pacer(10**6, 10**12, clock=c.clock, sleep=c.sleep))
    s.connect()
    s.clock = c
    return s


def msg(pid):
    return im.prepare_message(eml_bytes(message_id=f"<{pid}@x>"), proton_id=pid)


def test_special_use_and_localized_names(server):
    server.folders = {"INBOX": [], "Gesendete Elemente": [], "Archiv": []}
    server.special = {"Gesendete Elemente": (b"\\Sent",), "Archiv": (b"\\Archive",)}
    s = im.ImapStore("me@x", lambda: "t", client_factory=lambda: FakeClient(server))
    s.connect()
    assert s.imap_name(("Inbox",)) == "INBOX"
    assert s.imap_name(("Sent Items",)) == "Gesendete Elemente"
    assert s.imap_name(("Archive",)) == "Archiv"
    assert s.imap_name(("Alerts", "Amazon")) == "Alerts/Amazon"


def test_ensure_folders_creates_parents_once_case_insensitively(store, server):
    server.folders["alerts"] = []
    store.connect()                                   # re-read the folder list
    created = store.ensure_folders([("Alerts", "Amazon"), ("Alerts", "Apple"),
                                    ("Archive",), ("Inbox",), ("Café ∕ x",)])
    assert created == ["Alerts/Amazon", "Alerts/Apple", "Café ∕ x"]


def test_upload_sets_seen_and_utc_date_and_returns_uid(store, server):
    result = store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1539016986)
    assert result == im.UploadResult(uid=101, already_present=False)
    _, _, flags, msg_time = server.folders["INBOX"][0]
    assert flags == (r"\Seen",)
    assert msg_time == datetime(2018, 10, 8, 16, 43, 6, tzinfo=UTC)
    unread = store.upload(("Inbox",), "p2", msg("p2"), seen=False, internal_date=None)
    assert server.folders["INBOX"][1][2] == () and server.folders["INBOX"][1][3] is None
    assert unread.uid == 102


def test_upload_never_sends_flagged(store, server):
    store.upload(("Inbox",), "p1", msg("p1"), seen=False, internal_date=1)
    assert all("Flagged" not in str(f) for _, _, f, _ in server.folders["INBOX"])


def test_already_in_folder_from_an_earlier_run_is_not_sent(store, server):
    server.store("Archive", msg("old"))
    result = store.upload(("Archive",), "old", msg("old"), seen=True, internal_date=1)
    assert result.already_present is True
    assert server.copies("Archive", "old") == 1


def test_empty_folder_needs_no_fetch(store, server):
    store.upload(("Sent Items",), "p1", msg("p1"), seen=True, internal_date=1)
    assert not [f for f in server.fetches if f[0] == "Sent Items"]
    assert not [q for q in server.searches if q[0] == "Sent Items"]


def test_presence_fetches_in_chunks_of_explicit_uids(store, server, monkeypatch):
    monkeypatch.setattr(im, "FETCH_CHUNK", 3)
    for i in range(7):
        server.store("Archive", msg(f"old{i}"))
    present = store.present_ids(("Archive",))
    assert set(present) == {f"old{i}" for i in range(7)}
    assert [len(uids) for folder, uids in server.fetches if folder == "Archive"] == [3, 3, 1]


def test_same_id_twice_in_one_run_is_sent_once(store, server):
    store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    again = store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert again.already_present is True and server.copies("INBOX", "p1") == 1


def test_disconnect_after_server_stored_it_does_not_duplicate(store, server):
    server.store("INBOX", msg("earlier"))          # forces a real presence fetch first
    server.on_append = ["abort_after_store"]
    result = store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert result.already_present is True
    assert server.copies("INBOX", "p1") == 1
    assert server.logins == 2                        # reconnected


def test_disconnect_before_server_stored_it_resends_once(store, server):
    server.on_append = ["abort_before_store"]
    result = store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert result.already_present is False
    assert server.copies("INBOX", "p1") == 1


def test_refresh_ignores_the_star_quirk(store, server):
    server.store("INBOX", msg("other"))
    store.present_ids(("Inbox",))
    server.on_append = ["abort_before_store"]           # refresh asks for UIDs past the highest: gets "other" again
    store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert server.copies("INBOX", "p1") == 1 and server.copies("INBOX", "other") == 1


def test_throttle_waits_the_suggested_time(store, server):
    server.on_append = ["throttle"]
    result = store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert 299.961 in store.clock.slept
    assert result.uid is not None and server.copies("INBOX", "p1") == 1


def test_throttle_and_dropped_connection_slow_the_upload(store, server):
    said = []
    store.say = said.append
    start = store.pacer.msgs_per_sec
    server.on_append = ["throttle"]
    store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert store.pacer.msgs_per_sec == start / 2
    server.on_append = ["abort_before_store"]
    store.upload(("Inbox",), "p2", msg("p2"), seen=True, internal_date=1)
    assert store.pacer.msgs_per_sec == max(im.FLOOR_MSGS_PER_SEC, start / 4)
    assert sum("slowing to" in line for line in said) == 2


def test_quota_stops_without_changing_rates(store, server):
    rates = (store.pacer.msgs_per_sec, store.pacer.bytes_per_sec)
    server.on_append = ["quota"]
    with pytest.raises(im.QuotaExceeded):
        store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert (store.pacer.msgs_per_sec, store.pacer.bytes_per_sec) == rates


def test_successful_uploads_feed_the_observed_rate(store, server):
    for i in range(3):
        store.upload(("Inbox",), f"p{i}", msg(f"p{i}"), seen=True, internal_date=1)
    assert len(store.pacer._window) == 3 and store.pacer.clean == 3


def test_too_many_throttles_gives_up(store, server):
    store.max_throttle_waits = 2
    server.on_append = ["throttle"] * 3
    with pytest.raises(im.UploadGaveUp, match="throttled"):
        store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)


def test_quota_stops(store, server):
    server.on_append = ["quota"]
    with pytest.raises(im.QuotaExceeded):
        store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)


def test_rejected_message(store, server):
    server.on_append = ["reject"]
    with pytest.raises(im.AppendRejected, match="Argument Error"):
        store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)


def test_gives_up_after_repeated_connection_failures(store, server):
    store.max_attempts = 3
    server.on_append = ["abort_before_store"] * 3
    with pytest.raises(im.UploadGaveUp, match="3 connection failures"):
        store.upload(("Inbox",), "p1", msg("p1"), seen=True, internal_date=1)
    assert server.copies("INBOX", "p1") == 0


# --- remove (prune) -----------------------------------------------------------------------------

def test_remove_moves_only_matching_ids_to_deleted_items(store, server):
    for pid in ("keep1", "gone1", "gone2", "keep2"):
        store.upload(("Archive",), pid, msg(pid), seen=True, internal_date=1)
    removed = store.remove(("Archive",), ["gone1", "gone2", "never-uploaded"])
    assert removed == {"gone1", "gone2"}
    assert [im.parse_proton_id(m) for _, m, _, _ in server.folders["Archive"]] == ["keep1", "keep2"]
    assert sorted(im.parse_proton_id(m) for _, m, _, _ in server.folders["Deleted Items"]) == ["gone1", "gone2"]
    assert set(store.present_ids(("Archive",))) == {"keep1", "keep2"}


def test_remove_from_missing_folder_is_a_no_op(store):
    assert store.remove(("Nope",), ["x"]) == set()


def test_remove_needs_a_trash_folder(server):
    del server.special["Deleted Items"]
    s = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server))
    s.connect()
    with pytest.raises(im.ImapError, match="Deleted Items"):
        s.remove(("Archive",), ["x"])


def test_remove_refuses_a_server_without_move(store, server):
    server.move_capable = False
    store.upload(("Archive",), "gone1", msg("gone1"), seen=True, internal_date=1)
    with pytest.raises(im.ImapError, match="MOVE"):
        store.remove(("Archive",), ["gone1"])
    assert [im.parse_proton_id(m) for _, m, _, _ in server.folders["Archive"]] == ["gone1"]
    assert server.folders["Deleted Items"] == []


def test_purge_deletes_only_the_named_messages(store, server):
    for pid in ("keep", "selftest-x"):
        store.upload(("Archive",), pid, msg(pid), seen=True, internal_date=1)
    store.remove(("Archive",), ["keep", "selftest-x"])
    assert store.purge(store.trash_path, ["selftest-x", "never"]) == {"selftest-x"}
    assert [im.parse_proton_id(m) for _, m, _, _ in server.folders["Deleted Items"]] == ["keep"]


def test_duplicates_lists_ids_with_more_than_one_copy(store, server):
    store.upload(("Archive",), "a", msg("a"), seen=True, internal_date=1)
    store.upload(("Archive",), "b", msg("b"), seen=True, internal_date=1)
    server.store("Archive", msg("a"))
    dupes = store.duplicates(("Archive",))
    assert list(dupes) == ["a"] and len(dupes["a"]) == 2
    assert store.duplicates(("Nope",)) == {}
