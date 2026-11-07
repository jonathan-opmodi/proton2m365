from __future__ import annotations

import re

import json
import sqlite3

import pytest

from proton2m365 import pipeline
from proton2m365 import proton_export as pe
from proton2m365.cli import main

from .conftest import ExportBuilder, eml_bytes

R, S, IMP = pe.FLAG_RECEIVED, pe.FLAG_SENT, pe.FLAG_IMPORTED

USER_LABELS = [
    {"ID": "auto=", "Name": "Alerts", "Path": "Alerts", "ParentID": "", "Type": 3},
    {"ID": "amzn=", "Name": "Amazon", "Path": "Alerts/Amazon", "ParentID": "auto=", "Type": 3},
    {"ID": "slash=", "Name": "A/B", "Path": "A/B", "ParentID": "", "Type": 3},
    {"ID": "files=", "Name": "files", "Path": "files", "ParentID": "", "Type": 3},
    {"ID": "arch=", "Name": "Archive", "Path": "Archive", "ParentID": "", "Type": 3},
    {"ID": "sub=", "Name": "Inbox", "Path": "Alerts/Inbox", "ParentID": "auto=", "Type": 3},
    {"ID": "imp=", "Name": "Important", "Path": "Important", "ParentID": "", "Type": 1},
    {"ID": "gm=", "Name": "work@example.org", "Path": "work@example.org", "ParentID": "", "Type": 1},
    {"ID": "comma=", "Name": "Bills, 2024; paid", "Path": "Bills, 2024; paid", "ParentID": "", "Type": 1},
]


@pytest.fixture
def labels(tmp_path):
    b = ExportBuilder(tmp_path, labels=USER_LABELS)
    return pe.load_labels(b.dir / "labels.json")


def meta(*labels, flags=R, unread=False, time=1_560_000_000):
    return pe.MessageMeta("p1", tuple(labels), flags, time, unread)


# --- folders ----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label_id", "segments"),
    [
        ("auto=", ("Alerts",)),
        ("amzn=", ("Alerts", "Amazon")),
        ("slash=", ("A∕B",)),                           # '/' in a name never splits the path
        ("files=", ("files (Proton)",)),                     # reserved root name, any case
        ("arch=", ("Archive (Proton)",)),                    # clashes with Exchange's Archive
        ("sub=", ("Alerts", "Inbox")),         # only the root level is reserved
    ],
)
def test_folder_segments(labels, label_id, segments):
    assert pe.folder_segments(label_id, labels) == segments


def test_folder_segments_nfc(tmp_path):
    b = ExportBuilder(tmp_path, labels=[{"ID": "u=", "Name": "Café", "Path": "Café", "Type": 3}])
    labels = pe.load_labels(b.dir / "labels.json")
    assert pe.folder_segments("u=", labels) == ("Café",)


def test_folder_segments_parent_cycle_terminates(tmp_path):
    b = ExportBuilder(tmp_path, labels=[
        {"ID": "a=", "Name": "A", "Path": "A", "ParentID": "b=", "Type": 3},
        {"ID": "b=", "Name": "B", "Path": "B", "ParentID": "a=", "Type": 3},
    ])
    labels = pe.load_labels(b.dir / "labels.json")
    assert pe.folder_segments("a=", labels) == ("B", "A")


@pytest.mark.parametrize(
    ("message_labels", "kwargs", "expected"),
    [
        (("5", "6", "15", "21"), {}, ("Archive",)),
        (("0", "5"), {}, ("Inbox",)),
        (("0", "7", "2"), {}, ("Inbox",)),                   # sent to self: Inbox wins
        (("7", "2", "5"), {}, ("Sent Items",)),
        (("2",), {}, ("Sent Items",)),                        # All Sent alone still means sent
        (("5", "21"), {}, ("Proton Unfiled",)),
        (("6", "amzn="), {}, ("Alerts", "Amazon")),  # user folder beats system folder
        (("3", "amzn="), {}, ("Alerts", "Amazon")),  # trash not included: normal routing
        (("3", "amzn="), {"include_trash": True}, ("Proton Trash",)),
        (("4", "0"), {"include_spam": True}, ("Proton Spam",)),
        (("3", "4"), {"include_trash": True, "include_spam": True}, ("Proton Trash",)),
    ],
)
def test_target_folder(labels, message_labels, kwargs, expected):
    assert pe.target_folder(meta(*message_labels), labels, **kwargs)[0] == expected


def test_target_folder_multiple_user_folders_is_deterministic(labels):
    folder, multiple = pe.target_folder(meta("slash=", "amzn="), labels)
    assert folder == ("Alerts", "Amazon") and multiple is True  # lowest path wins
    assert pe.target_folder(meta("amzn=", "slash="), labels)[0] == folder


# --- categories -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("message_labels", "kwargs", "expected"),
    [
        (("0", "imp=", "gm="), {}, ("Important", "work@example.org")),
        (("0", "imp=", "gm="), {"excluded_ids": {"gm="}}, ("Important",)),
        (("0", "20", "21", "22", "24", "15"), {}, ()),       # numeric labels never become categories
        (("0", "amzn="), {}, ()),                            # folders are not categories
        (("0", "10"), {}, ("Starred",)),
        (("0", "10", "imp="), {"starred_as_flag": True}, ("Important",)),
        (("comma=",), {}, ("Bills 2024 paid",)),
    ],
)
def test_categories_for(labels, message_labels, kwargs, expected):
    assert pe.categories_for(meta(*message_labels), labels, **kwargs) == expected


@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        (["Important"], {"imp="}),
        (["important"], {"imp="}),                            # case-insensitive
        (["gm="], {"gm="}),                                   # by ID
        (["  work@example.org "], {"gm="}),
        (["Important", "work@example.org"], {"imp=", "gm="}),
    ],
)
def test_resolve_label_selectors(labels, selectors, expected):
    assert pe.resolve_label_selectors(selectors, labels) == expected


@pytest.mark.parametrize("bad", ["Nope", "Amazon", "21", "Starred"])   # unknown, a folder, numeric, system
def test_resolve_label_selectors_rejects(labels, bad):
    with pytest.raises(pe.ExportError, match="matches no label") as exc:
        pe.resolve_label_selectors([bad], labels)
    assert "Important" in str(exc.value)                      # lists what is available


# --- message id and date ----------------------------------------------------------------------

def test_synthesized_message_id_is_stable_and_safe():
    a = pe.synthesized_message_id("ab+c/d==é x")
    assert a == pe.synthesized_message_id("ab+c/d==é x")
    assert a.startswith("<proton-") and a.endswith("@proton2m365.invalid>")
    assert " " not in a and "é" not in a


def test_internal_date_prefers_proton_time():
    facts = pe.EmlFacts(size=1, message_id=None, max_line=1, signed=False, date=1_000)
    assert pe.internal_date(meta(time=2_000), facts) == 2_000
    assert pe.internal_date(pe.MessageMeta("p", (), R, None, False), facts) == 1_000
    no_date = pe.EmlFacts(size=1, message_id=None, max_line=1, signed=False)
    assert pe.internal_date(pe.MessageMeta("p", (), R, None, False), no_date) is None


def test_read_eml_facts_date_and_sha256(tmp_path):
    import hashlib
    raw = eml_bytes(extra_headers="Date: Mon, 08 Oct 2018 16:43:06 +0000\r\n")
    p = tmp_path / "m.eml"
    p.write_bytes(raw)
    facts = pe.read_eml_facts(p, with_sha256=True)
    assert facts.date == 1539016986
    assert facts.sha256 == hashlib.sha256(raw).hexdigest()
    assert pe.read_eml_facts(p).sha256 is None


# --- plan end to end --------------------------------------------------------------------------

@pytest.fixture
def export(tmp_path) -> ExportBuilder:
    b = ExportBuilder(tmp_path, labels=USER_LABELS)
    b.add("arch1", labels=("5", "6", "15", "21", "gm="), flags=R | IMP)
    b.add("arch2", labels=("5", "6", "22", "gm=", "imp="), flags=R | IMP, unread=True)
    b.add("inbox1", labels=("0", "10"), flags=R)
    b.add("sent1", labels=("7", "2"), flags=S)
    b.add("amzn1", labels=("amzn=", "6"), flags=R)
    b.add("draft1", labels=("8", "1"), flags=0)
    b.add("trash1", labels=("3",), flags=R)
    b.add("spam1", labels=("4",), flags=R)
    b.add("nomid", labels=("0",), flags=R, eml=eml_bytes(message_id=None))
    b.add("long1", labels=("0",), flags=R, eml=eml_bytes(message_id="<long1@x>", body=b"q" * 12_000 + b"\r\n"))
    return b


def _rows(state_path):
    conn = sqlite3.connect(state_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["proton_id"]: r for r in conn.execute("SELECT * FROM messages")}
    finally:
        conn.close()


def test_plan_writes_rows(export, tmp_path):
    state = tmp_path / "state.sqlite"
    report = pipeline.plan_export(export.dir, pipeline.PlanSettings(exclude_labels=("work@example.org",)),
                                  state_path=state)
    rows = _rows(state)
    assert len(rows) == 10
    assert json.loads(rows["arch1"]["target_folder"]) == ["Archive"]
    assert json.loads(rows["arch1"]["categories"]) == []
    assert json.loads(rows["arch2"]["categories"]) == ["Important"]
    assert rows["arch2"]["seen"] == 0 and rows["arch1"]["seen"] == 1
    assert json.loads(rows["inbox1"]["categories"]) == ["Starred"] and rows["inbox1"]["flagged"] == 0
    assert json.loads(rows["sent1"]["target_folder"]) == ["Sent Items"]
    assert json.loads(rows["amzn1"]["target_folder"]) == ["Alerts", "Amazon"]
    assert rows["draft1"]["stage"] == "skipped" and rows["draft1"]["skip_reason"] == "draft"
    assert rows["trash1"]["skip_reason"] == "trash" and rows["spam1"]["skip_reason"] == "spam"
    assert rows["nomid"]["synth_message_id"] == 1 and rows["nomid"]["message_id"].endswith("@proton2m365.invalid>")
    assert rows["long1"]["needs_reencode"] == 1
    assert rows["arch1"]["internal_date"] == 1_560_000_000
    assert len(rows["arch1"]["sha256"]) == 64
    assert {r["stage"] for pid, r in rows.items()
            if pid not in ("draft1", "trash1", "spam1", "arch1", "arch2")} == {"planned"}
    # every message carrying an excluded label is skipped, not just the category
    assert {rows[p]["skip_reason"] for p in ("arch1", "arch2")} == {"excluded-label"}
    assert {rows[p]["stage"] for p in ("arch1", "arch2")} == {"skipped"}

    assert report.upload_count == 5
    assert report.excluded == {"work@example.org": 2}
    assert report.dropped_numeric == {"21": 1, "22": 1}
    assert report.replan.new == 10


def test_plan_is_idempotent(export, tmp_path):
    state = tmp_path / "state.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=state)
    again = pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=state)
    assert (again.replan.new, again.replan.updated, again.replan.unchanged) == (0, 0, 10)


def test_plan_include_trash_then_exclude_again(export, tmp_path):
    state = tmp_path / "state.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(include_trash=True), state_path=state)
    assert _rows(state)["trash1"]["stage"] == "planned"
    assert json.loads(_rows(state)["trash1"]["target_folder"]) == ["Proton Trash"]
    report = pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=state)
    assert _rows(state)["trash1"]["stage"] == "skipped"
    assert ("trash1", "planned", "skipped") in report.replan.reclassified


def test_dry_run_writes_nothing(export, tmp_path):
    state = tmp_path / "state.sqlite"
    report = pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=None)
    assert report.replan is None and report.upload_count == 7
    assert not state.exists()


def test_bad_exclude_label_fails_before_writing(export, tmp_path):
    state = tmp_path / "state.sqlite"
    with pytest.raises(pe.ExportError):
        pipeline.plan_export(export.dir, pipeline.PlanSettings(exclude_labels=("Nope",)), state_path=state)
    assert not _rows(state) if state.exists() else True


def test_case_collision_reported(tmp_path):
    b = ExportBuilder(tmp_path, labels=[
        {"ID": "w1=", "Name": "Work", "Path": "Work", "Type": 3},
        {"ID": "w2=", "Name": "work", "Path": "work", "Type": 3},
    ])
    b.add("a", labels=("w1=",))
    b.add("b", labels=("w2=",))
    report = pipeline.plan_export(b.dir, pipeline.PlanSettings(), state_path=None)
    assert report.case_collisions == [["Work", "work"]]


def test_cli_plan(export, tmp_path, capsys):
    state = tmp_path / "s.sqlite"
    code = main(["plan", str(export.dir), "--state", str(state),
                 "--exclude-label", "work@example.org", "--exclude-label", "important"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Upload 5 of 10 messages" in out
    assert "Alerts/Amazon" in out
    assert re.search(r"work@example.org +2  excluded", out) and re.search(r"Important +1  excluded", out)
    assert "Excluding a label leaves out whole messages" in out
    assert "Dropped unnamed Proton labels (never categories): 21 (1), 22 (1)" in out
    assert "Left out: 2 excluded-label, 1 draft, 1 spam, 1 trash" in out
    assert "State: 10 new, 0 updated, 0 unchanged" in out


def test_cli_plan_unknown_label(export, tmp_path, capsys):
    assert main(["plan", str(export.dir), "--state", str(tmp_path / "s.sqlite"), "--exclude-label", "Nope"]) == 2
    assert "matches no label" in capsys.readouterr().err


def test_cli_plan_starred_as_flag(export, tmp_path, capsys, monkeypatch):
    assert main(["plan", str(export.dir), "--dry-run", "--starred-as-flag"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing written" in out and "Flagged (starred): 1" in out
