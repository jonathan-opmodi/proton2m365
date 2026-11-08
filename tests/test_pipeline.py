from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from proton2m365 import graph_store as gs
from proton2m365 import imap_store as im
from proton2m365 import pipeline
from proton2m365.state import open_state
from proton2m365 import proton_export as pe
from proton2m365.status import Outcome, migration_status

from .conftest import ExportBuilder, eml_bytes, owner_only
from .test_imap_store import FakeClient, FakeClock, FakeServer

R, S = 1, 2
SPECIAL_IDS = {"INBOX": "F-INBOX", "Sent Items": "F-SENT", "Archive": "F-ARCHIVE"}
WELL_KNOWN = {"inbox": "INBOX", "sentitems": "Sent Items", "archive": "Archive"}


class LinkedGraph:
    """A Graph fake that reads the fake IMAP server, so whatever upload stored, tag/verify can see."""

    def __init__(self, server: FakeServer):
        self.s = server
        self.patches: dict[str, dict] = {}
        self.master: list[dict] = []
        self.hidden: set[str] = set()           # proton ids Graph "can't see yet"
        self.override: dict[str, dict] = {}     # graph id -> fields to fake (e.g. a wrong date)
        self.mime_override: dict[str, bytes] = {}  # graph id -> MIME Graph returns instead of what was stored

    # folder ids are opaque (hex of the IMAP name), like real Graph ids: no '/' inside
    def fid(self, name):
        return SPECIAL_IDS.get(name, "F-" + name.encode().hex())

    def name(self, fid):
        special = next((n for n, i in SPECIAL_IDS.items() if i == fid), None)
        if special:
            return special
        return bytes.fromhex(fid[2:]).decode() if fid.startswith("F-") and fid not in SPECIAL_IDS.values() else None

    def message(self, folder, uid, raw, flags, msg_time):
        head = raw.partition(b"\r\n\r\n")[0].decode("utf-8", "replace")
        headers = []
        for line in re.split(r"\r\n(?![ \t])", head):
            if ":" in line:
                k, v = line.split(":", 1)
                headers.append({"name": k.strip(), "value": v.strip()})
        mid = next((h["value"] for h in headers if h["name"].lower() == "message-id"), None)
        gid = f"g{uid}"
        m = {
            "id": gid, "internetMessageId": mid, "parentFolderId": self.fid(folder),
            "receivedDateTime": (msg_time or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "isRead": r"\Seen" in flags, "isDraft": False, "categories": [], "flag": {"flagStatus": "notFlagged"},
            "internetMessageHeaders": headers,
        }
        m.update(self.patches.get(gid, {}))
        m.update(self.override.get(gid, {}))
        return m

    def all_messages(self, folder):
        out = []
        for uid, raw, flags, msg_time in self.s.folders.get(folder, []):
            if im.parse_proton_id(raw) not in self.hidden:
                out.append(self.message(folder, uid, raw, flags, msg_time))
        return out

    def find(self, gid):
        for folder in self.s.folders:
            for m in self.all_messages(folder):
                if m["id"] == gid:
                    return m
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0")
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        parts = path.strip("/").split("/")
        if path == "/$batch":
            return self.batch(json.loads(request.content))
        if path == "/me/outlook/masterCategories":
            if request.method == "POST":
                self.master.append({**json.loads(request.content), "id": f"cat-{len(self.master)}"})
                return httpx.Response(201, json=self.master[-1])
            return httpx.Response(200, json={"value": self.master})
        if parts[:2] == ["me", "messages"] and len(parts) == 4 and parts[3] == "$value":
            for folder, msgs in self.s.folders.items():
                for uid, raw, _, _ in msgs:
                    if f"g{uid}" == parts[2]:
                        return httpx.Response(200, content=self.mime_override.get(parts[2], raw))
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": parts[2]}})
        if path.startswith("/me/outlook/masterCategories/") and request.method == "DELETE":
            self.master = [c for c in self.master if c.get("id") != parts[-1]]
            return httpx.Response(204)
        if parts[:2] == ["me", "mailFolders"]:
            key = parts[2]
            folder = WELL_KNOWN.get(key) or self.name(key) or ("" if key == "msgfolderroot" else None)
            if len(parts) == 3:
                if folder not in self.s.folders:
                    return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": key}})
                return httpx.Response(200, json={"id": self.fid(folder), "totalItemCount": len(self.s.folders[folder])})
            if parts[3] == "childFolders":
                prefix = f"{folder}/" if folder else ""
                kids = sorted({n[len(prefix):].split("/")[0] for n in self.s.folders
                               if n.startswith(prefix) and n != "INBOX"})
                return httpx.Response(200, json={"value": [{"id": self.fid(prefix + k), "displayName": k} for k in kids]})
            if parts[3] == "messages":
                items = self.all_messages(folder)
                top, skip = int(params.get("$top", 10)), int(params.get("$skip", 0))
                body = {"value": items[skip:skip + top]}
                if skip + top < len(items):
                    body["@odata.nextLink"] = str(request.url.copy_merge_params({"$skip": str(skip + top)}))
                return httpx.Response(200, json=body)
        return httpx.Response(400, json={"error": {"code": "BadRoute", "message": path}})

    def batch(self, payload):
        out = []
        for sub in payload["requests"]:
            gid = sub["url"].split("/")[3].split("?")[0]
            m = self.find(gid)
            if m is None:
                out.append({"id": sub["id"], "status": 404, "body": {"error": {"code": "ErrorItemNotFound"}}})
            elif sub["method"] == "PATCH":
                self.patches.setdefault(gid, {}).update(sub["body"])
                out.append({"id": sub["id"], "status": 200, "body": {"id": gid}})
            else:
                out.append({"id": sub["id"], "status": 200, "body": m})
        return httpx.Response(200, json={"responses": out})


USER_LABELS = [
    {"ID": "imp=", "Name": "Important", "Path": "Important", "Type": 1, "Color": "#E74856"},
    {"ID": "gm=", "Name": "work@example.org", "Path": "work@example.org", "Type": 1},
    {"ID": "amzn=", "Name": "Amazon", "Path": "Amazon", "Type": 3},
]


@pytest.fixture
def export(tmp_path) -> ExportBuilder:
    b = ExportBuilder(tmp_path, labels=USER_LABELS)
    b.add("a1", labels=("6", "imp="), flags=R, time=1_400_000_000)
    b.add("a2", labels=("6",), flags=R, time=1_400_000_100, unread=True)
    b.add("i1", labels=("0", "10"), flags=R, time=1_500_000_000)
    b.add("s1", labels=("7",), flags=S, time=1_500_000_100)
    b.add("z1", labels=("amzn=",), flags=R, time=1_600_000_000)
    b.add("nomid", labels=("0",), flags=R, time=1_600_000_100, eml=eml_bytes(message_id=None))
    b.add("d1", labels=("8",), flags=0)
    return b


@pytest.fixture
def world(export, tmp_path):
    """Planned state + fake IMAP server + linked Graph."""
    state_path = tmp_path / "state.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(exclude_labels=("work@example.org",)),
                         state_path=state_path)
    server = FakeServer()
    clock = FakeClock()
    imap = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server), sleep=clock.sleep,
                        pacer=im.Pacer(10**6, 10**12, clock=clock.clock, sleep=clock.sleep))
    imap.connect()
    linked = LinkedGraph(server)
    graph = gs.GraphStore(lambda: "tok", sleep=clock.sleep,
                          client=httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(linked.handler)))
    with open_state(state_path) as state:
        yield {"state": state, "server": server, "imap": imap, "graph": graph, "linked": linked,
               "export": export, "clock": clock}


def quiet(_msg):
    pass


# --- upload -----------------------------------------------------------------------------------

def test_upload_everything_once(world):
    st, server = world["state"], world["server"]
    r = pipeline.upload_run(st, world["imap"], say=quiet)
    assert (r.sent, r.already_there, r.remaining) == (6, 0, 0)
    assert r.created_folders == ["Amazon"]
    assert server.copies("Archive", "a1") == 1 and server.copies("Amazon", "z1") == 1
    assert server.copies("Sent Items", "s1") == 1 and server.copies("INBOX", "nomid") == 1
    nomid = next(m for _, m, _, _ in server.folders["INBOX"] if im.parse_proton_id(m) == "nomid")
    assert b"Message-ID: <proton-nomid@proton2m365.invalid>" in nomid
    assert st.stage_counts() == {"appended": 6, "skipped": 1}
    again = pipeline.upload_run(st, world["imap"], say=quiet)
    assert again.attempted == 0
    assert json.loads(st.get_meta("last_upload"))["sent"] == 6    # the empty run kept the measured rate


def test_upload_limit_then_resume(world):
    st = world["state"]
    assert pipeline.upload_run(st, world["imap"], limit=2, say=quiet).sent == 2
    rest = pipeline.upload_run(st, world["imap"], say=quiet)
    assert rest.sent == 4 and rest.remaining == 0


def test_state_lost_after_upload_does_not_duplicate(world, tmp_path):
    """If state.sqlite says planned but the mail is there (e.g. crash before saving), nothing is re-sent."""
    st, server = world["state"], world["server"]
    pipeline.upload_run(st, world["imap"], limit=1, say=quiet)
    with st.conn:
        st.conn.execute("UPDATE messages SET stage = 'planned' WHERE stage = 'appended'")
    fresh = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server),
                         pacer=im.Pacer(10**6, 10**12, clock=world["clock"].clock, sleep=world["clock"].sleep))
    fresh.connect()
    r = pipeline.upload_run(st, fresh, say=quiet)
    assert r.already_there == 1 and r.sent == 5
    assert sum(server.copies(f, pid) for f in server.folders for pid in ("a1", "a2", "i1", "s1", "z1", "nomid")) == 6


def test_rejected_and_unreadable_fail_permanently(world, export):
    st, server = world["state"], world["server"]
    server.on_append = ["reject"]
    (export.dir / "z1.eml").unlink()
    r = pipeline.upload_run(st, world["imap"], say=quiet)
    assert len(r.rejected) == 1 and [p for p, _ in r.unreadable] == ["z1"]
    assert st.stage_counts()["upload_failed"] == 2
    retry = pipeline.upload_run(st, world["imap"], retry_failed=True, say=quiet)
    assert retry.sent == 1 and [p for p, _ in retry.unreadable] == ["z1"]   # the rejected one went through


def test_gave_up_counts_one_attempt_and_stays_planned(world):
    st, imap, server = world["state"], world["imap"], world["server"]
    imap.max_attempts = 2
    server.on_append = ["abort_before_store", "abort_before_store"]
    r = pipeline.upload_run(st, imap, say=quiet)
    assert len(r.gave_up) == 1
    pid = r.gave_up[0][0]
    row = next(x for x in st.rows_to_upload() + st.rows_to_tag() if x.proton_id == pid)
    assert row.stage == "planned" and row.upload_attempts == 1


def test_quota_stops_the_run_and_leaves_rows_planned(world):
    st, server = world["state"], world["server"]
    server.on_append = ["ok", "quota"]
    r = pipeline.upload_run(st, world["imap"], say=quiet)
    assert r.stopped.startswith("mailbox full")
    assert st.stage_counts() == {"appended": 1, "planned": 5, "skipped": 1}


def test_ctrl_c_stops_cleanly(world, monkeypatch):
    st, imap = world["state"], world["imap"]
    real = imap.upload
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(imap, "upload", flaky)
    r = pipeline.upload_run(st, imap, say=quiet)
    assert r.stopped.startswith("interrupted") and r.sent == 2
    assert st.stage_counts()["appended"] == 2


def test_connection_lost_outside_append_reconnects_once(world, monkeypatch):
    st, imap, server = world["state"], world["imap"], world["server"]
    real = imap.present_ids
    calls = {"n": 0}

    def drop_once(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset while listing folder")
        return real(path)

    monkeypatch.setattr(imap, "present_ids", drop_once)
    r = pipeline.upload_run(st, imap, say=quiet)
    assert r.sent == 6 and server.logins == 2


# --- tag ----------------------------------------------------------------------------------------

def test_tag_sets_categories_and_flags_and_creates_master_categories(world):
    st, linked = world["state"], world["linked"]
    pipeline.upload_run(st, world["imap"], say=quiet)
    colors = pipeline.label_colors(world["export"].dir)
    r = pipeline.tag_run(st, world["graph"], colors=colors, say=quiet)
    assert r.tagged == 6 and not r.unmatched
    assert st.stage_counts() == {"tagged": 6, "skipped": 1}
    by_cat = {m["displayName"]: m["color"] for m in linked.master}
    assert by_cat == {"Important": "preset0", "Starred": "preset3"}
    patched = {linked.find(g)["internetMessageId"]: p for g, p in linked.patches.items()}
    not_flagged = {"flagStatus": "notFlagged"}
    assert patched == {"<a1@x>": {"categories": ["Important"], "flag": not_flagged},
                       "<i1@x>": {"categories": ["Starred"], "flag": not_flagged}}
    assert pipeline.tag_run(st, world["graph"], say=quiet).tagged == 0


def test_tag_unmatched_retries_then_fails(world):
    st, linked = world["state"], world["linked"]
    pipeline.upload_run(st, world["imap"], say=quiet)
    linked.hidden = {"z1"}
    for _ in range(2):
        r = pipeline.tag_run(st, world["graph"], say=quiet)
        assert r.unmatched == ["z1"] and not r.now_failed
    r = pipeline.tag_run(st, world["graph"], say=quiet)
    assert r.now_failed == ["z1"]
    linked.hidden = set()
    assert pipeline.tag_run(st, world["graph"], retry_failed=True, say=quiet).tagged == 1


# --- verify -------------------------------------------------------------------------------------

def _verify(world, **kwargs):
    export = pipeline.export_accounting(world["state"], world["export"].dir)
    return pipeline.verify_run(world["state"], world["graph"], export=export, **kwargs)


def _migrate(world, *, limit=None):
    pipeline.upload_run(world["state"], world["imap"], limit=limit, say=quiet)
    pipeline.tag_run(world["state"], world["graph"], say=quiet)


def test_verify_passes_after_upload_and_tag(world):
    st = world["state"]
    _migrate(world)
    r = _verify(world, check_all=True)
    assert r.ok, (r.mismatches, r.status)
    assert (r.status.outcome, r.status.exit_code) == (Outcome.PASSED, 0)
    assert (r.checked, r.passed) == (6, 6)
    assert st.stage_counts() == {"verified": 6, "skipped": 1}
    assert ("Archive", 2, 2) in r.folders


def test_verify_catches_wrong_date_read_state_and_draft(world):
    st, linked = world["state"], world["linked"]
    _migrate(world)
    a1 = next(r for r in st.rows_to_verify() if r.proton_id == "a1")
    linked.override[a1.graph_id] = {"receivedDateTime": "2026-11-07T00:00:00Z", "isRead": False, "isDraft": True}
    r = _verify(world, check_all=True)
    assert (r.status.outcome, r.status.exit_code) == (Outcome.FAILED, 12)
    assert {m[1] for m in r.mismatches if m[0] == "a1"} == {"received date", "read", "not a draft"}
    assert st.stage_counts()["tagged"] == 1   # a1 not marked verified


def test_verify_never_forgets_a_later_mismatch(world):
    st, linked = world["state"], world["linked"]
    _migrate(world)
    assert _verify(world, check_all=True).ok
    a1 = next(r for r in st.rows_to_verify() if r.proton_id == "a1")
    linked.override[a1.graph_id] = {"isRead": False}   # someone marked it unread in Outlook
    r = _verify(world, check_all=True)
    assert r.status.outcome == Outcome.FAILED and st.stage_counts()["tagged"] == 1
    # A sample of 0 would skip a1, but a known failure is always re-checked.
    r = _verify(world, sample=0)
    assert r.status.outcome == Outcome.FAILED and r.checked == 1 and {m[0] for m in r.mismatches} == {"a1"}
    del linked.override[a1.graph_id]
    r = _verify(world, sample=0)
    assert r.ok and r.checked == 1
    assert st.stage_counts() == {"verified": 6, "skipped": 1}


def test_verify_sample_with_unchecked_rows_is_sample_ok(world):
    _migrate(world)
    r = _verify(world, sample=2, rng=random.Random(1))
    assert r.checked == 2
    assert (r.status.outcome, r.status.exit_code) == (Outcome.SAMPLE_OK, 10)
    assert r.status.next_command == "proton2m365 verify --all"
    # Once every row has been checked, a later sampled run can pass.
    assert _verify(world, check_all=True).ok
    assert _verify(world, sample=1, rng=random.Random(2)).ok


def test_verify_with_planned_rows_is_incomplete_even_when_sampled(world):
    _migrate(world, limit=2)
    for kwargs in ({"check_all": True}, {"sample": 1, "rng": random.Random(1)}):
        r = _verify(world, **kwargs)
        assert (r.status.outcome, r.status.exit_code) == (Outcome.INCOMPLETE, 11), kwargs
        assert r.status.next_command == "proton2m365 upload"
        assert any("planned, not uploaded yet" in reason for reason in r.status.reasons)


def test_verify_with_appended_rows_points_to_tag(world):
    pipeline.upload_run(world["state"], world["imap"], say=quiet)
    r = _verify(world, check_all=True)
    assert r.status.outcome == Outcome.INCOMPLETE and r.status.next_command == "proton2m365 tag"


def test_verify_names_an_unplanned_export_file(world):
    _migrate(world)
    (world["export"].dir / "broken.eml").write_bytes(b"x")       # no metadata: plan never planned it
    r = _verify(world, check_all=True)
    assert r.status.outcome == Outcome.INCOMPLETE
    assert r.export.unresolved == ("broken.eml",)
    assert any("broken.eml" in reason for reason in r.status.reasons)


def test_verify_with_missing_export_files_is_incomplete(world):
    _migrate(world)
    (world["export"].dir / "a1.eml").unlink()
    (world["export"].dir / "a1.metadata.json").unlink()
    r = _verify(world, check_all=True)
    assert r.status.outcome == Outcome.INCOMPLETE and r.export.missing_from_export == ("a1",)


def test_verify_without_the_export_folder_is_incomplete(world):
    _migrate(world)
    r = pipeline.verify_run(world["state"], world["graph"], check_all=True,
                            export=pipeline.export_accounting(world["state"], None))
    assert r.status.outcome == Outcome.INCOMPLETE
    assert any("not readable" in reason for reason in r.status.reasons)


def test_verify_reports_short_folder(world):
    server = world["server"]
    _migrate(world)
    server.folders["Amazon"].clear()           # someone deleted the mail
    r = _verify(world, sample=0)
    assert ("Amazon", 1, 0) in r.folders_short and r.status.outcome == Outcome.FAILED


def test_verify_empty_export_is_nothing_to_migrate(tmp_path):
    export = ExportBuilder(tmp_path / "empty", labels=USER_LABELS)
    state_path = tmp_path / "empty.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=state_path)
    with open_state(state_path) as st:
        r = pipeline.verify_run(st, None, check_all=True, export=pipeline.export_accounting(st, export.dir))
    assert (r.status.outcome, r.status.sentence) == (Outcome.PASSED, "nothing to migrate")


# --- status -----------------------------------------------------------------------------------

def test_status_eta(world):
    st = world["state"]
    pipeline.upload_run(st, world["imap"], limit=2, say=quiet)
    st.record_meta("last_upload", json.dumps({"sent": 2, "seconds": 2, "per_hour": 3600.0, "bytes": 1, "at": 0}))
    s = pipeline.status_report(st)
    assert s.stage_counts["planned"] == 4 and s.eta == "0 min"
    st.record_meta("last_upload", json.dumps({"sent": 2, "seconds": 2, "per_hour": 2.0, "bytes": 1, "at": 0}))
    assert pipeline.status_report(st).eta == "2.0 h"


# --- selftest -----------------------------------------------------------------------------------

@pytest.fixture
def live(tmp_path):
    server = FakeServer()
    clock = FakeClock()
    imap = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server), sleep=clock.sleep,
                        pacer=im.Pacer(10**6, 10**12, clock=clock.clock, sleep=clock.sleep))
    imap.connect()
    linked = LinkedGraph(server)
    graph = gs.GraphStore(lambda: "tok", sleep=clock.sleep,
                          client=httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(linked.handler)))
    return server, imap, graph, clock


def _delete_folder(self, name):
    del self.s.folders[name]


def _close_folder(self):
    self.selected = None


FakeClient.delete_folder = _delete_folder
FakeClient.close_folder = _close_folder


def test_selftest_all_pass_and_cleans_up(live):
    server, imap, graph, clock = live
    checks = pipeline.run_selftest(imap, graph, run_id="t1", say=quiet, sleep=clock.sleep)
    failed = [c for c in checks if not c.ok]
    assert not failed, failed
    assert len(checks) == 20
    assert not any(n.startswith(pipeline.SELFTEST_ROOT) for n in server.folders)
    # nothing left anywhere: Archive, Deleted Items and the category list are as before
    assert server.folders["Archive"] == [] and server.folders["Deleted Items"] == []
    assert not [c for c in graph_categories(graph) if c.startswith(pipeline.SELFTEST_CATEGORY)]


def graph_categories(graph):
    return [c["displayName"] for c in graph.paged("/me/outlook/masterCategories")]


def test_selftest_extra_checks_cover_category_archive_and_prune(live):
    server, imap, graph, clock = live
    said = []
    checks = {c.name: c for c in pipeline.run_selftest(imap, graph, run_id="t9", say=said.append, sleep=clock.sleep)}
    for name in ("create and delete a category through Graph", "APPEND into the Archive folder",
                 "move to Deleted Items (the prune path)"):
        assert checks[name].ok, checks[name]
    assert said[0].startswith("  Writes: folder _proton2m365_selftest-t9/") and "Deleted Items" in said[0]
    assert not any(line.startswith("Left in the mailbox") for line in said)


def test_failed_graph_lookup_marks_graph_checks_not_run_and_still_runs_the_rest(live, monkeypatch):
    server, imap, graph, clock = live
    monkeypatch.setattr(gs.GraphStore, "resolve_folder", lambda self, path: (_ for _ in ()).throw(gs.GraphError(404, "x")))
    checks = pipeline.run_selftest(imap, graph, run_id="t10", say=quiet, sleep=clock.sleep)
    not_run = [c.name for c in checks if not c.ran]
    assert not_run == list(pipeline.GRAPH_CHECKS[1:])
    assert all(not c.ok for c in checks if not c.ran)
    by_name = {c.name: c for c in checks}
    assert by_name["move to Deleted Items (the prune path)"].ok          # IMAP checks still ran
    assert len(checks) == 20


def test_selftest_without_move_reports_it_and_names_the_leftover(live):
    server, imap, graph, clock = live
    server.move_capable = False
    said = []
    checks = {c.name: c for c in pipeline.run_selftest(imap, graph, run_id="t11", say=said.append, sleep=clock.sleep)}
    assert not checks["move to Deleted Items (the prune path)"].ok
    assert "MOVE" in checks["move to Deleted Items (the prune path)"].detail
    assert any(line.startswith("Left in the mailbox") and "selftest-archive-t11" in line for line in said)


def test_selftest_keep_then_new_run_uses_its_own_folder(live):
    server, imap, graph, clock = live
    pipeline.run_selftest(imap, graph, run_id="t1", keep=True, say=quiet, sleep=clock.sleep)
    kept = "/".join(pipeline.selftest_folder("t1"))
    assert kept in server.folders
    checks = pipeline.run_selftest(imap, graph, run_id="t2", say=quiet, sleep=clock.sleep)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert kept in server.folders  # a later run never deletes an earlier run's folder
    assert "/".join(pipeline.selftest_folder("t2")) not in server.folders


def _put_real_mail(server, imap, path):
    imap.ensure_folders([path])
    server.store("/".join(path), b"From: a@example.com\r\nSubject: real\r\n\r\nkeep me\r\n")


def test_selftest_never_deletes_a_folder_with_the_old_fixed_name(live):
    server, imap, graph, clock = live
    old = (pipeline.SELFTEST_ROOT, pipeline.SELFTEST_LEAF)  # the name earlier versions reused every run
    _put_real_mail(server, imap, old)
    said = []
    checks = pipeline.run_selftest(imap, graph, run_id="t5", say=said.append, sleep=clock.sleep)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert "/".join(old) in server.folders
    assert len(server.folders["/".join(old)]) == 1
    assert any("selftest --cleanup" in line for line in said)


def test_cleanup_removes_only_folders_holding_only_selftest_mail(live):
    server, imap, graph, clock = live
    pipeline.run_selftest(imap, graph, run_id="t6", keep=True, say=quiet, sleep=clock.sleep)
    mixed = (f"{pipeline.SELFTEST_ROOT}-t7", pipeline.SELFTEST_LEAF)
    _put_real_mail(server, imap, mixed)
    removed, kept = pipeline.cleanup_selftest_leftovers(imap, say=quiet)
    assert removed == [f"{pipeline.SELFTEST_ROOT}-t6"]
    assert kept == [f"{pipeline.SELFTEST_ROOT}-t7"]
    assert not any(n.startswith(f"{pipeline.SELFTEST_ROOT}-t6") for n in server.folders)
    assert "/".join(mixed) in server.folders


def test_selftest_cleanup_after_failure_removes_only_its_own_folder(live, monkeypatch):
    server, imap, graph, clock = live
    other = (f"{pipeline.SELFTEST_ROOT}-other", pipeline.SELFTEST_LEAF)
    _put_real_mail(server, imap, other)
    monkeypatch.setattr(gs.GraphStore, "resolve_folder", lambda self, path: (_ for _ in ()).throw(gs.GraphError("x")))
    pipeline.run_selftest(imap, graph, run_id="t8", say=quiet, sleep=clock.sleep)
    assert "/".join(other) in server.folders
    assert not any(n.startswith(f"{pipeline.SELFTEST_ROOT}-t8") for n in server.folders)


def test_selftest_catches_a_server_that_stamps_today(live):
    server, imap, graph, clock = live
    real_store = server.store
    server.store = lambda folder, msg, flags=(), msg_time=None: real_store(folder, msg, flags, None)
    checks = {c.name: c for c in pipeline.run_selftest(imap, graph, run_id="t3", say=quiet, sleep=clock.sleep)}
    assert not checks["original received date kept (2019-06-08)"].ok


def test_selftest_catches_a_duplicate_after_drop(live, monkeypatch):
    server, imap, graph, clock = live
    monkeypatch.setattr(im.ImapStore, "_refresh_presence", lambda self, folder: None)  # break the guard
    checks = {c.name: c for c in pipeline.run_selftest(imap, graph, run_id="t4", say=quiet, sleep=clock.sleep)}
    assert not checks["exactly one copy of each message"].ok


# --- prune --------------------------------------------------------------------------------------

def test_replan_with_new_exclusion_then_prune(world, export):
    state, server, imap = world["state"], world["server"], world["imap"]
    pipeline.upload_run(state, imap, say=quiet)
    pipeline.tag_run(state, world["graph"], colors={}, say=quiet)
    in_archive = {im.parse_proton_id(m) for _, m, _, _ in server.folders["Archive"]}
    assert "a1" in in_archive

    # the user decides mail labeled Important must not be migrated after all (a1 carries it)
    rows, _ = pipeline.build_plan_rows(export.dir, pipeline.PlanSettings(exclude_labels=("Important",)))
    replan = state.apply_plan(rows)
    assert replan.to_prune == ["a1"]
    assert state.rows_to_prune() == {("Archive",): ["a1"]}

    report = pipeline.prune_run(state, imap, say=quiet)
    assert (report.removed, report.not_found) == (1, [])
    assert {im.parse_proton_id(m) for _, m, _, _ in server.folders["Archive"]} == in_archive - {"a1"}
    assert [im.parse_proton_id(m) for _, m, _, _ in server.folders["Deleted Items"]] == ["a1"]
    row = state.conn.execute("SELECT stage, skip_reason, imap_uid FROM messages WHERE proton_id='a1'").fetchone()
    assert tuple(row) == ("skipped", "excluded-label", None)
    assert pipeline.prune_run(state, imap, say=quiet).removed == 0   # second run: nothing left

    # and verify's folder counts no longer expect it
    assert state.in_mailbox_counts().get(("Archive",)) == len(in_archive) - 1


# --- CLI exit codes and Next: lines (one table for every command) -------------------------------

@pytest.fixture
def cli_world(export, tmp_path, monkeypatch):
    from proton2m365 import cli

    state_path = tmp_path / "state.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(exclude_labels=("work@example.org",)),
                         state_path=state_path)
    server, clock = FakeServer(), FakeClock()
    imap = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server), sleep=clock.sleep,
                        pacer=im.Pacer(10**6, 10**12, clock=clock.clock, sleep=clock.sleep))
    imap.connect()
    linked = LinkedGraph(server)
    graph = gs.GraphStore(lambda: "tok", sleep=clock.sleep,
                          client=httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(linked.handler)))

    signed_in = {"username": "alice@example.com", "home_account_id": "oid-a.tid-work"}

    class FakeAuth:
        @property
        def username(self):
            return signed_in["username"]

        @property
        def home_account_id(self):
            return signed_in["home_account_id"]

        @property
        def is_personal(self):
            return signed_in["home_account_id"].endswith("." + cli.auth_mod.CONSUMER_TENANT)

    class Conns:
        def __init__(self, args, recorded=None):
            self.auth = FakeAuth()

        def imap_store(self, args):
            return imap

        def graph(self):
            return graph

        def close(self):
            pass

    monkeypatch.setattr(cli, "_Connections", Conns)
    monkeypatch.setattr(cli.auth_mod, "CONFIG_DIR", tmp_path / "config")  # mailbox locks stay in the test
    monkeypatch.chdir(tmp_path)  # verify writes verify-report.csv to the current directory

    def run(*argv):
        return cli.main([*argv, "--state", str(state_path)])

    return {"run": run, "linked": linked, "export": export, "state_path": state_path, "cli": cli,
            "signed_in": signed_in}


def _next_line(out: str) -> str:
    return [line for line in out.splitlines() if line.startswith("Next: ")][-1]


def test_cli_happy_path_exit_codes_and_next_lines(cli_world, capsys):
    run = cli_world["run"]
    assert run("upload") == 0
    assert "proton2m365 tag --state" in _next_line(capsys.readouterr().out)
    assert run("verify", "--sample", "1") == 11                      # uploaded, not tagged yet
    capsys.readouterr()
    assert run("tag") == 0
    assert "verify --all" in _next_line(capsys.readouterr().out)
    assert run("verify", "--sample", "1") == 10                      # SAMPLE OK
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("VERIFY SAMPLE OK: ")      # the outcome comes first
    assert run("verify", "--all") == 0
    out = capsys.readouterr().out
    assert "VERIFY PASSED: all 6 messages are in the mailbox and checked" in out
    assert _next_line(out).startswith("Next: nothing left to do")
    assert run("status") == 0
    assert _next_line(capsys.readouterr().out).startswith("Next: ")


def test_cli_upload_with_rows_left_is_incomplete_not_1(cli_world, capsys):
    run, export = cli_world["run"], cli_world["export"]
    assert run("upload", "--limit", "2") == 11
    (export.dir / "a2.eml").unlink()                                  # unreadable at upload time
    assert run("upload") == 11
    out = capsys.readouterr().out
    # An unreadable file is refused for good: retrying cannot help, so Next: offers --exclude-file.
    assert "unreadable" in out and "--exclude-file <id>" in _next_line(out)


def test_cli_upload_interrupted_is_13(cli_world, monkeypatch):
    real = pipeline.upload_run

    def interrupted(*args, **kwargs):
        r = real(*args, **{**kwargs, "limit": 1})
        r.stopped = "interrupted (Ctrl-C); re-run upload to continue"
        return r

    monkeypatch.setattr(pipeline, "upload_run", interrupted)
    assert cli_world["run"]("upload") == 13


def test_cli_tag_with_unmatched_rows_is_incomplete(cli_world):
    run = cli_world["run"]
    run("upload")
    cli_world["linked"].hidden = {"z1"}
    assert run("tag") == 11


def test_cli_verify_mismatch_is_12(cli_world, capsys):
    run = cli_world["run"]
    run("upload")
    run("tag")
    with open_state(cli_world["state_path"]) as st:
        gid = next(r.graph_id for r in st.rows_to_verify() if r.proton_id == "a1")
    cli_world["linked"].override[gid] = {"isRead": False}
    capsys.readouterr()
    assert run("verify", "--all") == 12
    assert "VERIFY FAILED" in capsys.readouterr().out


def test_cli_sign_in_error_is_2_not_an_outcome(cli_world, monkeypatch):
    cli = cli_world["cli"]

    class Broken:
        def __init__(self, args, recorded=None):
            raise cli.auth_mod.AuthError("no")

    monkeypatch.setattr(cli, "_Connections", Broken)
    assert cli_world["run"]("verify", "--all") == 2


def test_cli_selftest_failure_is_12(monkeypatch, cli_world, capsys):
    cli = cli_world["cli"]
    monkeypatch.setattr(pipeline, "run_selftest",
                        lambda *a, **k: [pipeline.Check("x", True), pipeline.Check("y", False, "bad")])
    assert cli.main(["selftest"]) == 12
    monkeypatch.setattr(pipeline, "run_selftest", lambda *a, **k: [pipeline.Check("x", True)])
    assert cli.main(["selftest"]) == 0
    monkeypatch.setattr(pipeline, "run_selftest",
                        lambda *a, **k: [pipeline.Check("x", True), pipeline.Check("z", False, "r", ran=False)])
    assert cli.main(["selftest"]) == 12                                  # not run never counts as passed
    assert "1/2 checks passed, 1 not run." in capsys.readouterr().out


def test_help_lists_the_exit_codes(capsys):
    from proton2m365 import cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "exit codes:" in out and "13  interrupted" in out


# --- account binding -----------------------------------------------------------------------------

def _account(state_path):
    with open_state(state_path, write=False) as st:
        raw = st.get_meta("account")
    return json.loads(raw) if raw else None


def test_first_write_binds_the_state_to_the_signed_in_account(cli_world, capsys):
    run = cli_world["run"]
    assert run("verify", "--sample", "1") == 11            # reading an empty state binds nothing
    assert _account(cli_world["state_path"]) is None
    assert run("upload") == 0
    assert _account(cli_world["state_path"]) == {"home_account_id": "oid-a.tid-work",
                                                 "username": "alice@example.com"}
    assert "Mailbox: alice@example.com" in capsys.readouterr().out


@pytest.mark.parametrize("command", [("upload",), ("tag",), ("prune",), ("verify", "--all")])
def test_every_online_command_refuses_another_account(cli_world, capsys, command):
    run = cli_world["run"]
    run("upload")
    cli_world["signed_in"].update(username="bob@example.com", home_account_id="oid-b.tid-work")
    capsys.readouterr()
    assert run(*command) == 2
    err = capsys.readouterr().err
    assert "belongs to alice@example.com" in err and "--account alice@example.com" in err


def test_old_state_with_mail_needs_bind_account(cli_world, capsys):
    run = cli_world["run"]
    run("upload")
    with open_state(cli_world["state_path"]) as st, st.conn:
        st.conn.execute("DELETE FROM meta WHERE key = 'account'")   # as written before accounts were recorded
    capsys.readouterr()
    assert run("verify", "--all") == 2
    assert "6 messages are already in a mailbox" in capsys.readouterr().err
    assert run("tag") == 2                                         # writing commands are refused too
    assert run("verify", "--sample", "1", "--bind-account") == 11      # bound; tag still to do
    assert _account(cli_world["state_path"])["username"] == "alice@example.com"
    assert run("tag") == 0


def test_two_state_files_cannot_write_to_one_mailbox_at_once(cli_world, tmp_path, capsys):
    from proton2m365.locks import FileLock

    cli = cli_world["cli"]
    name = __import__("hashlib").sha256(b"oid-a.tid-work").hexdigest()[:16]
    with FileLock(tmp_path / "config" / "locks" / f"mailbox-{name}.lock"):   # another run, any state file
        assert cli_world["run"]("upload") == 2
        assert "another proton2m365 run is writing to alice@example.com" in capsys.readouterr().err
        assert cli_world["run"]("verify", "--sample", "1") == 11           # reading is not blocked
    assert cli_world["run"]("upload") == 0


# --- size preflight for personal mailboxes ------------------------------------------------------

@pytest.fixture
def personal(cli_world, monkeypatch):
    cli = cli_world["cli"]
    cli_world["signed_in"].update(username="carol@example.org",
                                  home_account_id="oid-c." + cli.auth_mod.CONSUMER_TENANT)
    monkeypatch.setattr(cli, "PERSONAL_LIMIT_BYTES", 100)    # the fixture export is a few hundred bytes
    return cli_world


def test_personal_mailbox_over_the_limit_stops_before_any_write(personal, capsys):
    run = personal["run"]
    assert run("upload") == 2
    out, err = capsys.readouterr()
    assert "Size: this run" in out and "15 GB" in err and "--yes" in err and "Nothing was uploaded" in err
    assert _account(personal["state_path"]) is None                     # a refused preflight binds nothing
    with open_state(personal["state_path"], write=False) as st:
        assert st.stage_counts().get("appended", 0) == 0
    assert run("upload", "--limit", "2", "--yes") == 11                 # --yes stores the acknowledgement
    assert run("upload") == 0                                           # not bigger: no second question


def test_personal_mailbox_asks_again_when_the_total_grows(personal, monkeypatch):
    run, cli = personal["run"], personal["cli"]
    assert run("upload", "--limit", "1", "--yes") == 11
    with open_state(personal["state_path"]) as st:
        st.record_meta(cli.SIZE_ACK_META, "101")                        # acknowledged less than the new total
    assert run("upload") == 2


def test_work_mailbox_prints_size_and_proceeds(cli_world, monkeypatch, capsys):
    monkeypatch.setattr(cli_world["cli"], "PERSONAL_LIMIT_BYTES", 1000)
    assert cli_world["run"]("upload") == 0
    assert "Size: this run" in capsys.readouterr().out


def test_retry_failed_counts_failed_bytes(personal, capsys):
    run = personal["run"]
    assert run("upload", "--retry-failed") == 0                         # nothing failed: 0 GB this run
    assert "this run 0.00 GB" in capsys.readouterr().out


# --- migration receipt -------------------------------------------------------------------------

def _receipt(cli_world):
    path = cli_world["state_path"].with_name("state.receipt.json")
    return path, json.loads(path.read_text())


def test_receipt_accounts_for_every_export_file(cli_world, capsys):
    run, export = cli_world["run"], cli_world["export"]
    (export.dir / "stray.eml").write_bytes(b"x")                       # an orphan .eml
    run("upload")
    run("tag")
    capsys.readouterr()
    assert run("verify", "--all") == 11                                # the orphan keeps it INCOMPLETE
    out = capsys.readouterr().out
    path, doc = _receipt(cli_world)
    assert path.read_text().startswith('{"proton2m365_receipt": 1')
    assert owner_only(path)
    outcomes = {e["id"]: e["outcome"] for e in doc["entries"]}
    assert outcomes["stray.eml"] == "orphan-eml"
    assert sorted(o for o in outcomes.values() if o.startswith("skipped:")) == ["skipped:draft"]
    assert sum(o == "migrated-verified" for o in outcomes.values()) == 6
    assert doc["mailbox"] == "alice@example.com" and doc["outcome"] == "INCOMPLETE"
    assert "export_dir" not in doc["selection"]["settings"]           # no local paths in the receipt
    assert doc["selection"]["folders"]["Archive"]["messages"] == 2
    txt = path.with_name("state.receipt.txt").read_text()
    assert doc["run_id"] in txt and "Export files not migrated:\n  stray.eml (orphan-eml)" in txt
    assert "Content: not compared" in txt and f"Receipt: {path}" in txt
    assert f"Receipt: {path.resolve()}" in out and "Summary: " in out


def test_receipt_is_cumulative_and_names_each_stage(cli_world, capsys):
    run = cli_world["run"]
    run("upload")
    run("tag")
    run("verify", "--all")
    assert run("verify", "--sample", "0") == 0                         # nothing re-checked, still PASSED
    _, doc = _receipt(cli_world)
    assert {e["outcome"] for e in doc["entries"]} == {"migrated-verified", "skipped:draft"}
    assert all(e["checks"] == "pass" for e in doc["entries"] if e["outcome"] == "migrated-verified")


def test_receipt_outcomes_for_unfinished_work(cli_world):
    run, linked = cli_world["run"], cli_world["linked"]
    run("upload", "--limit", "4")
    linked.hidden = {"a1"}
    run("tag")                                                          # a1 not visible yet: stays appended
    run("verify", "--all")
    _, doc = _receipt(cli_world)
    outcomes = {e["id"]: e["outcome"] for e in doc["entries"]}
    assert outcomes["a1"] == "waiting:tag"
    assert sorted(o for o in outcomes.values() if o == "planned") == ["planned", "planned"]


def test_receipt_records_a_failed_check(cli_world):
    run = cli_world["run"]
    run("upload")
    run("tag")
    with open_state(cli_world["state_path"]) as st:
        gid = next(r.graph_id for r in st.rows_to_verify() if r.proton_id == "a1")
    cli_world["linked"].override[gid] = {"isRead": False}
    assert run("verify", "--all") == 12
    _, doc = _receipt(cli_world)
    a1 = next(e for e in doc["entries"] if e["id"] == "a1")
    assert a1["outcome"] == "failed:verify" and a1["checks"] == "fail:read"


def test_unsaved_receipt_keeps_the_outcome_code_except_passed(cli_world, tmp_path, capsys):
    run = cli_world["run"]
    run("upload")
    run("tag")
    nowhere = tmp_path / "no such folder"                 # cannot be written on any system
    assert run("verify", "--all", "--receipt", str(nowhere / "r.json")) == 14      # would have PASSED
    err = capsys.readouterr().err
    assert "could not write" in err and "r.json" in err
    with open_state(cli_world["state_path"]) as st:
        gid = next(r.graph_id for r in st.rows_to_verify() if r.proton_id == "a1")
    cli_world["linked"].override[gid] = {"isRead": False}
    assert run("verify", "--all", "--receipt", str(nowhere / "r.json")) == 12      # FAILED stays FAILED
    assert not nowhere.exists()                                                     # no temp files left


def test_check_publish_recognises_a_receipt(cli_world):
    import importlib.util

    run = cli_world["run"]
    run("upload")
    run("tag")
    run("verify", "--all")
    path, _ = _receipt(cli_world)
    spec = importlib.util.spec_from_file_location("cp_for_receipt", Path(__file__).resolve().parents[1]
                                                  / "scripts" / "check_publish.py")
    cp = importlib.util.module_from_spec(spec)
    sys.modules["cp_for_receipt"] = cp
    spec.loader.exec_module(cp)
    assert cp._RECEIPT.match(path.read_bytes())


def test_help_lists_exit_14(capsys):
    from proton2m365 import cli

    with pytest.raises(SystemExit):
        cli.main(["verify", "--help"])
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "14  verify passed but its receipt could not be saved" in capsys.readouterr().out


# --- DX: Next: carries the options in use, resume lines, --json ---------------------------------

def test_next_line_repeats_the_sign_in_options_in_use(cli_world, capsys):
    run = cli_world["run"]
    assert run("upload", "--client-id", "cid-1", "--tenant", "organizations", "--device-code") == 0
    line = _next_line(capsys.readouterr().out)
    assert line.startswith("Next: proton2m365 tag --state ")
    assert "--client-id cid-1 --tenant organizations --device-code" in line


def test_upload_says_categories_come_next_and_resume_line_on_stop(cli_world, capsys, monkeypatch):
    run = cli_world["run"]
    real = pipeline.upload_run

    def interrupted(*args, **kwargs):
        r = real(*args, **{**kwargs, "limit": 2})
        r.stopped = "interrupted (Ctrl-C); re-run upload to continue"
        return r

    monkeypatch.setattr(pipeline, "upload_run", interrupted)
    monkeypatch.setattr(sys, "argv", ["proton2m365", "upload"])
    assert run("upload") == 13
    out = capsys.readouterr().out
    assert "Stopped safely after 2 messages. Run `proton2m365 upload` again to resume; nothing will be sent twice." in out
    monkeypatch.setattr(pipeline, "upload_run", real)
    assert run("upload") == 0
    assert "Mail is in the mailbox; categories and flags are not applied yet." in capsys.readouterr().out


def test_verify_and_status_json(cli_world, capsys):
    run = cli_world["run"]
    run("upload")
    run("tag")
    capsys.readouterr()
    assert run("verify", "--all", "--json") == 0
    out, err = capsys.readouterr()
    result = json.loads(out)                                             # stdout is exactly one JSON object
    assert result["outcome"] == "PASSED" and result["migrated"] == 6 and result["artifacts"]["errors"] == {}
    assert "VERIFY PASSED" in err                                        # prose went to stderr
    assert run("status", "--json") == 0
    status = json.loads(capsys.readouterr().out)
    # status does not read the export, so it cannot certify PASSED: it points at verify --all
    assert status["verified"] == 6 and status["next"].startswith("proton2m365 verify --all")


def test_json_error_object(cli_world, capsys, tmp_path):
    code = cli_world["cli"].main(["verify", "--json", "--state", str(tmp_path / "missing.sqlite")])
    out, _ = capsys.readouterr()
    assert code == 2
    err = json.loads(out)["error"]
    assert err["code"] == "P2M-STATE-MISSING" and err["retryable"] is False and "no state at" in err["message"]


# --- robustness ----------------------------------------------------------------------------------

def test_interrupt_after_append_before_state_update_never_sends_twice(world, monkeypatch):
    st, server = world["state"], world["server"]
    real = type(st).mark_appended
    calls = []

    def dies_once(self, pid, uid):
        calls.append(pid)
        if len(calls) == 2:
            raise KeyboardInterrupt  # the APPEND for this message landed; the state update did not
        return real(self, pid, uid)

    monkeypatch.setattr(type(st), "mark_appended", dies_once)
    r = pipeline.upload_run(st, world["imap"], say=quiet)
    assert r.stopped and "interrupted" in r.stopped
    monkeypatch.setattr(type(st), "mark_appended", real)
    pipeline.upload_run(st, world["imap"], say=quiet)
    for folder, msgs in server.folders.items():
        ids = [im.parse_proton_id(m) for _, m, _, _ in msgs]
        assert len(ids) == len(set(ids)), (folder, ids)        # nothing appended twice
    assert st.stage_counts()["appended"] == 6


@pytest.mark.parametrize("command", [("upload",), ("tag",), ("prune",), ("status",), ("doctor",)])
def test_every_command_prints_one_json_object(cli_world, capsys, command):
    run = cli_world["run"]
    run("upload")
    capsys.readouterr()
    code = run(*command, "--json")
    out = capsys.readouterr().out
    doc = json.loads(out)                                   # exactly one JSON object on stdout
    assert doc["command"] == command[0] and doc["exit_code"] == code
    if command[0] in ("upload", "tag", "prune"):
        assert "next" in doc


def test_plan_json(tmp_path, capsys):
    from proton2m365 import cli

    export = ExportBuilder(tmp_path / "one", labels=USER_LABELS)
    export.add("a1")
    assert cli.main(["plan", str(export.dir), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["upload_count"] == 1 and doc["folders"] == {"Inbox": 1} and doc["next"] == "proton2m365 upload"


def test_verify_all_reports_a_message_uploaded_twice(cli_world, capsys):
    run = cli_world["run"]
    run("upload")
    run("tag")
    server = cli_world["linked"].s
    uid, raw, flags, when = next(m for m in server.folders["Archive"] if im.parse_proton_id(m[1]) == "a1")
    server.store("Archive", raw, flags, when)                     # a second copy of a1
    capsys.readouterr()
    assert run("verify", "--all") == 12
    out = capsys.readouterr().out
    assert "1 messages have more than one copy in the mailbox: a1" in out


def test_receipt_page_is_private_escaped_and_lists_what_is_unresolved(cli_world, capsys):
    run, export = cli_world["run"], cli_world["export"]
    (export.dir / "a&b.eml").write_bytes(b"x")                           # an orphan ('<' is not a legal name on Windows)
    run("upload")
    run("tag")
    capsys.readouterr()
    assert run("verify", "--all") == 11
    out = capsys.readouterr().out
    page = cli_world["state_path"].with_name("state.receipt.html")
    assert f"Receipt page: {page.resolve()}" in out
    html = page.read_text()
    assert owner_only(page)
    assert "<strong>INCOMPLETE</strong>" in html and "a&amp;b.eml" in html and "a&b.eml" not in html
    assert "Archive</td><td class=n>2</td>" in html and "Content: not compared" in html
    _, doc = _receipt(cli_world)
    assert doc["run_id"] in html


def test_verify_content_compares_and_reports(cli_world, capsys):
    run, linked = cli_world["run"], cli_world["linked"]
    run("upload")
    run("tag")
    capsys.readouterr()
    assert run("verify", "--all", "--content") == 0
    txt = cli_world["state_path"].with_name("state.receipt.txt").read_text()
    assert "Content: compared on 6 messages this run" in txt
    with open_state(cli_world["state_path"]) as st:
        gid = next(r.graph_id for r in st.rows_to_verify() if r.proton_id == "a1")
    linked.mime_override[gid] = b"From: a@example.com\r\nContent-Type: text/plain\r\n\r\nsomething else\r\n"
    capsys.readouterr()
    assert run("verify", "--all", "--content") == 12
    assert "a1: content: text" in capsys.readouterr().out


def test_upload_takes_and_releases_the_mailbox_lease(cli_world):
    from proton2m365.lease import LEASE_FOLDER

    run = cli_world["run"]
    assert run("upload") == 0
    server = cli_world["linked"].s
    assert "/".join(LEASE_FOLDER) in server.folders and server.folders["/".join(LEASE_FOLDER)] == []


def test_upload_refused_while_another_computer_holds_the_lease(cli_world, capsys):
    from proton2m365.lease import MailboxLease

    run = cli_world["run"]
    server = cli_world["linked"].s
    other = im.ImapStore("me@x", lambda: "tok", client_factory=lambda: FakeClient(server),
                         pacer=im.Pacer(10**6, 10**12))
    other.connect()
    MailboxLease(other, holder="elsewhere").acquire()
    capsys.readouterr()
    assert run("upload") == 2
    assert "another computer is uploading into this mailbox" in capsys.readouterr().err
    assert run("upload", "--break-lease") == 0


# --- migrate: the whole thing in one resumable command -------------------------------------------

def _migrate_args(cli_world, *extra):
    cli = cli_world["cli"]
    args = cli.build_parser().parse_args(["migrate", str(cli_world["export"].dir),
                                          "--state", str(cli_world["state_path"]), *extra])
    cli._resolve_settings(args)
    return args


@pytest.fixture
def passing_selftest(monkeypatch):
    monkeypatch.setattr(pipeline, "run_selftest", lambda *a, **k: [pipeline.Check("x", True)])


def test_migrate_runs_every_step_and_ends_with_the_receipt(cli_world, capsys, passing_selftest):
    cli = cli_world["cli"]
    code = cli.cmd_migrate(_migrate_args(cli_world, "--yes"))
    out = capsys.readouterr().out
    assert code == 0
    for n, title in enumerate(["check the export", "plan", "selftest (first time only)", "upload", "tag",
                               "verify everything"], 1):
        assert f"== Step {n}/6: {title} ==" in out
    assert "VERIFY PASSED" in out and cli_world["state_path"].with_name("state.receipt.json").exists()
    code = cli.cmd_migrate(_migrate_args(cli_world, "--yes"))      # again: nothing to do, still PASSED
    out = capsys.readouterr().out
    assert code == 0 and "Already passed." in out and "Nothing to upload." in out


def test_migrate_asks_first_and_does_nothing_on_no(cli_world, capsys, passing_selftest):
    cli = cli_world["cli"]
    asked = []
    code = cli.cmd_migrate(_migrate_args(cli_world), ask=lambda q: asked.append(q) or "no")
    assert code == 0 and "Upload 6 messages" in asked[0]
    assert "Nothing uploaded" in capsys.readouterr().out
    with open_state(cli_world["state_path"], write=False) as st:
        assert st.stage_counts()["planned"] == 6


def test_migrate_without_a_terminal_needs_yes(cli_world, capsys):
    assert cli_world["cli"].cmd_migrate(_migrate_args(cli_world)) == 2
    assert "--yes" in capsys.readouterr().err


def test_migrate_stops_when_selftest_fails(cli_world, capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "run_selftest", lambda *a, **k: [pipeline.Check("x", False, "no")])
    assert cli_world["cli"].cmd_migrate(_migrate_args(cli_world, "--yes")) == 12
    with open_state(cli_world["state_path"], write=False) as st:
        assert st.stage_counts()["planned"] == 6                    # nothing uploaded


def test_migrate_resumes_after_an_interrupted_upload(cli_world, capsys, passing_selftest, monkeypatch):
    cli = cli_world["cli"]
    real = pipeline.upload_run

    def interrupted(*args, **kwargs):
        r = real(*args, **{**kwargs, "limit": 2})
        r.stopped = "interrupted (Ctrl-C); re-run upload to continue"
        return r

    monkeypatch.setattr(pipeline, "upload_run", interrupted)
    assert cli.cmd_migrate(_migrate_args(cli_world, "--yes")) == 13
    monkeypatch.setattr(pipeline, "upload_run", real)
    assert cli.cmd_migrate(_migrate_args(cli_world, "--yes")) == 0


def test_migrate_retries_tag_while_graph_catches_up(cli_world, capsys, passing_selftest):
    cli, linked = cli_world["cli"], cli_world["linked"]
    linked.hidden = {"a1"}
    waits = []

    def wait(seconds):
        waits.append(seconds)
        linked.hidden = set()                                       # Graph sees it a minute later

    assert cli.cmd_migrate(_migrate_args(cli_world, "--yes"), wait=wait) == 0
    assert waits == [cli.TAG_RETRY_WAIT]


# --- top-up: a newer export of the same mailbox (v0.2.0) --------------------------------------------------

def _newer_export(world, tmp_path, name="newer"):
    """A copy of the world's export in another folder, standing in for a later Proton export."""
    import shutil

    root = tmp_path / name
    shutil.copytree(world["export"].dir, root / world["export"].dir.name)
    builder = ExportBuilder.__new__(ExportBuilder)
    builder.dir = root / world["export"].dir.name
    return builder


def _topup(world, builder, **kwargs):
    return pipeline.plan_into(world["state"], builder.dir,
                              pipeline.PlanSettings(exclude_labels=("work@example.org",)), **kwargs)


def test_topup_uploads_only_new_messages_and_passes(world, tmp_path):
    _migrate(world)
    newer = _newer_export(world, tmp_path)
    newer.add("n1", labels=("0", "5"))
    newer.add("n2", labels=("6", "5"))
    report = _topup(world, newer)
    assert report.replan.new == 2 and not report.replan.missing_from_export
    up = pipeline.upload_run(world["state"], world["imap"], say=quiet)
    assert up.sent == 2
    pipeline.tag_run(world["state"], world["graph"], say=quiet)
    r = pipeline.verify_run(world["state"], world["graph"], check_all=True, content=True,
                            export=pipeline.export_accounting(world["state"], newer.dir))
    assert r.status.outcome == Outcome.PASSED, (r.mismatches, r.status)


def test_topup_with_missing_messages_needs_confirmation_and_writes_nothing(world, tmp_path):
    _migrate(world)
    newer = _newer_export(world, tmp_path)
    (newer.dir / "a1.eml").unlink()
    (newer.dir / "a1.metadata.json").unlink()
    before = json.loads(world["state"].get_meta("plan_settings"))["export_dir"]
    with pytest.raises(pipeline.ConfirmNeeded, match="P2M-CONFIRM-NEEDED: 1 of this state"):
        _topup(world, newer)
    assert json.loads(world["state"].get_meta("plan_settings"))["export_dir"] == before
    assert world["state"].missing_source_ids() == []


def test_topup_accept_missing_keeps_the_message_and_passes_by_presence(world, tmp_path):
    _migrate(world)
    newer = _newer_export(world, tmp_path)
    (newer.dir / "a1.eml").unlink()
    (newer.dir / "a1.metadata.json").unlink()
    _topup(world, newer, accept_missing=True)
    st = world["state"]
    assert st.missing_source_ids() == ["a1"]
    assert st.stage_counts()["tagged"] == 6           # its stage is kept
    r = pipeline.verify_run(st, world["graph"], check_all=True, content=True,
                            export=pipeline.export_accounting(st, newer.dir))
    assert r.status.outcome == Outcome.PASSED, (r.mismatches, r.status)
    assert "missing from the newer export" in r.status.sentence
    assert r.content_checked == 5                     # a1 is checked by presence only


def test_topup_message_back_in_a_later_export_is_present_again(world, tmp_path):
    _migrate(world)
    newer = _newer_export(world, tmp_path)
    for suffix in (".eml", ".metadata.json"):
        (newer.dir / f"a1{suffix}").unlink()
    _topup(world, newer, accept_missing=True)
    _topup(world, _newer_export(world, tmp_path, "latest"))   # a1 is back
    assert world["state"].missing_source_ids() == []


def test_topup_never_uploads_a_missing_planned_message(world, tmp_path):
    newer = _newer_export(world, tmp_path)
    for suffix in (".eml", ".metadata.json"):
        (newer.dir / f"a1{suffix}").unlink()
    _topup(world, newer, accept_missing=True)
    up = pipeline.upload_run(world["state"], world["imap"], say=quiet)
    assert up.sent == 5 and world["server"].copies("Archive", "a1") == 0
    pipeline.tag_run(world["state"], world["graph"], say=quiet)
    r = pipeline.verify_run(world["state"], world["graph"], check_all=True,
                            export=pipeline.export_accounting(world["state"], newer.dir))
    assert r.status.outcome == Outcome.PASSED
    assert any("never uploaded" in reason for reason in r.status.reasons)


def test_topup_of_a_different_mailbox_is_refused(world, tmp_path):
    other = _newer_export(world, tmp_path, "other")
    for f in list(other.dir.glob("*.eml")) + list(other.dir.glob("*.metadata.json")):
        f.unlink()
    other.add("x1")
    with pytest.raises(pe.ExportError, match="P2M-EXPORT-CHANGED: .* shares no message"):
        _topup(world, other)


def test_topup_then_old_export_deleted_content_check_uses_the_new_files(world, tmp_path):
    import shutil

    _migrate(world)
    newer = _newer_export(world, tmp_path)
    _topup(world, newer)
    shutil.rmtree(world["export"].dir)
    r = pipeline.verify_run(world["state"], world["graph"], check_all=True, content=True,
                            export=pipeline.export_accounting(world["state"], newer.dir))
    assert r.status.outcome == Outcome.PASSED, (r.mismatches, r.status)


def test_upload_skips_a_file_changed_since_plan(world):
    (world["export"].dir / "a1.eml").write_bytes(eml_bytes(message_id="<a1@x>", subject="edited later"))
    up = pipeline.upload_run(world["state"], world["imap"], say=quiet)
    assert up.source_changed == ["a1"] and world["server"].copies("Archive", "a1") == 0
    progress = world["state"].progress()
    assert progress.planned == 1 and progress.source_changed == 1
    status = migration_status(progress)
    assert status.next_command is None and "plan" in status.next_hint


def test_receipt_marks_messages_missing_from_the_newer_export(world, tmp_path):
    from proton2m365 import receipt

    newer = _newer_export(world, tmp_path)
    for suffix in (".eml", ".metadata.json"):
        (newer.dir / f"a1{suffix}").unlink()
    _topup(world, newer, accept_missing=True)
    entry = next(e for e in receipt.entries(world["state"], pipeline.export_accounting(world["state"], newer.dir))
                 if e["id"] == "a1")
    assert entry["outcome"] == "missing-from-newer-export-never-uploaded"
    assert entry["source"] == "missing-from-newer-export"
