from __future__ import annotations

import json
from pathlib import Path

import pytest

from proton2m365 import proton_export as pe
from proton2m365.cli import main, render_scan

from .conftest import ExportBuilder, eml_bytes

R, S, IMP = pe.FLAG_RECEIVED, pe.FLAG_SENT, pe.FLAG_IMPORTED


# --- draft truth table ------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("in_drafts", "rs", "imported", "expected"),
    [
        (True, False, False, pe.DraftVerdict.SKIP_DRAFT),
        (True, False, True, pe.DraftVerdict.SKIP_DRAFT),
        (True, True, False, pe.DraftVerdict.KEEP_DISAGREEMENT),
        (True, True, True, pe.DraftVerdict.KEEP_DISAGREEMENT),
        (False, False, True, pe.DraftVerdict.KEEP_IMPORTED),
        (False, False, False, pe.DraftVerdict.KEEP_DISAGREEMENT),
        (False, True, False, pe.DraftVerdict.KEEP),
        (False, True, True, pe.DraftVerdict.KEEP),
    ],
)
def test_draft_truth_table(in_drafts, rs, imported, expected):
    assert pe.classify_draft_bits(in_drafts, rs, imported) is expected


@pytest.mark.parametrize(
    ("labels", "flags", "expected"),
    [
        (("8",), 0, pe.DraftVerdict.SKIP_DRAFT),           # Drafts label
        (("1",), 0, pe.DraftVerdict.SKIP_DRAFT),           # All Drafts label counts too
        (("0",), R, pe.DraftVerdict.KEEP),
        (("7",), S, pe.DraftVerdict.KEEP),
        (("0",), IMP, pe.DraftVerdict.KEEP_IMPORTED),      # imported mail without Received bit is kept
        (("8",), R | IMP, pe.DraftVerdict.KEEP_DISAGREEMENT),
    ],
)
def test_classify_draft_reads_labels_and_flags(labels, flags, expected):
    meta = pe.MessageMeta("x", labels, flags, 0, False)
    assert pe.classify_draft(meta) is expected


# --- skip reasons -----------------------------------------------------------------------------

def _facts(size=100, max_line=10, signed=False):
    return pe.EmlFacts(size=size, message_id="<a@b>", max_line=max_line, signed=signed)


@pytest.mark.parametrize(
    ("labels", "flags", "facts", "kwargs", "expected"),
    [
        (("0",), R, _facts(), {}, None),
        (("8",), 0, _facts(size=10**9), {}, pe.SkipReason.DRAFT),                 # draft wins over oversize
        (("3", "8"), 0, _facts(), {"include_trash": True}, pe.SkipReason.DRAFT),  # draft wins over included trash
        (("3",), R, _facts(), {}, pe.SkipReason.TRASH),
        (("3",), R, _facts(), {"include_trash": True}, None),
        (("4",), R, _facts(), {}, pe.SkipReason.SPAM),
        (("3", "4"), R, _facts(), {}, pe.SkipReason.TRASH),                       # trash before spam
        (("0",), R, _facts(size=101), {"max_size": 100}, pe.SkipReason.OVERSIZE),
        (("0",), R, _facts(size=100), {"max_size": 100}, None),                   # limit is inclusive
        (("0",), R, _facts(max_line=20_000), {}, None),                           # unsigned long line: re-encoded
        (("0",), R, _facts(max_line=20_000, signed=True), {}, pe.SkipReason.LONG_LINE_SIGNED),
        (("0",), R, _facts(max_line=10_000, signed=True), {}, None),              # at the limit is fine
    ],
)
def test_default_skip_reason(labels, flags, facts, kwargs, expected):
    meta = pe.MessageMeta("x", labels, flags, 0, False)
    kwargs = {"max_size": 10**6, **kwargs}
    assert pe.default_skip_reason(meta, facts, **kwargs) == expected


# --- parsing --------------------------------------------------------------------------------------

def test_labels_json_bare_list_and_kinds(tmp_path):
    b = ExportBuilder(tmp_path, labels=[{"ID": "abc=", "Name": "Work", "Path": "Work", "Type": 3},
                                        {"ID": "def=", "Name": "Receipts", "Path": "Receipts", "Type": 1}],
                      wrapped=False)
    labels = pe.load_labels(b.dir / "labels.json")
    assert labels["0"].kind == "system"
    assert labels["abc="].kind == "folder"
    assert labels["def="].kind == "label"


def test_labels_json_garbage_raises(tmp_path):
    p = tmp_path / "labels.json"
    p.write_text("{not json")
    with pytest.raises(pe.ExportError):
        pe.load_labels(p)


@pytest.mark.parametrize(
    ("eml", "message_id", "signed", "long"),
    [
        (eml_bytes(), "<m@example.com>", False, False),
        (eml_bytes(message_id=None), None, False, False),
        (eml_bytes(message_id="   "), None, False, False),
        (eml_bytes(content_type='multipart/signed; protocol="application/pgp-signature"; boundary=x'), "<m@example.com>", True, False),
        (eml_bytes(content_type="application/pkcs7-mime; smime-type=enveloped-data"), "<m@example.com>", True, False),
        (eml_bytes(body=b"-----BEGIN PGP SIGNED MESSAGE-----\r\nHash: SHA256\r\n\r\nhi\r\n"), "<m@example.com>", True, False),
        (eml_bytes(body=b"<p>" + b"x" * 12_000 + b"</p>\r\n"), "<m@example.com>", False, True),
        (eml_bytes(body=b"y" * 10_000 + b"\n"), "<m@example.com>", False, False),   # LF-only, exactly at the limit
    ],
)
def test_read_eml_facts(tmp_path, eml, message_id, signed, long):
    p = tmp_path / "m.eml"
    p.write_bytes(eml)
    facts = pe.read_eml_facts(p)
    assert facts.message_id == message_id
    assert facts.signed is signed
    assert (facts.max_line > pe.LONG_LINE_LIMIT) is long
    assert facts.size == len(eml)


def test_header_only_pgp_text_in_header_is_not_signed(tmp_path):
    p = tmp_path / "m.eml"
    p.write_bytes(eml_bytes(extra_headers="X-Note: -----BEGIN PGP MESSAGE-----\r\n"))
    assert pe.read_eml_facts(p).signed is False


# --- locating the export ------------------------------------------------------------------------

def test_resolve_export_dir_from_account_root(export):
    assert pe.resolve_export_dir(export.dir.parent.parent) == export.dir


def test_resolve_export_dir_refuses_to_guess(tmp_path):
    ExportBuilder(tmp_path)
    second = tmp_path / "user@example.com" / "mail_20261102_090000"
    second.mkdir()
    (second / "labels.json").write_text("[]")
    with pytest.raises(pe.ExportError, match="found 2 Proton exports"):
        pe.resolve_export_dir(tmp_path)


def test_resolve_export_dir_none(tmp_path):
    with pytest.raises(pe.ExportError, match="no labels.json"):
        pe.resolve_export_dir(tmp_path)


# --- inventory --------------------------------------------------------------------------------

@pytest.fixture
def mixed(tmp_path) -> ExportBuilder:
    b = ExportBuilder(tmp_path, labels=[
        {"ID": "work=", "Name": "Work", "Path": "Work", "Type": 3},
        {"ID": "cli=", "Name": "Clients", "Path": "Work/Clients", "ParentID": "work=", "Type": 3},
        {"ID": "rcpt=", "Name": "Receipts", "Path": "Receipts", "Type": 1, "Color": "#ff0000"},
    ])
    b.add("inbox1", labels=("0", "10"), flags=R, time=1_400_000_000)           # starred
    b.add("inbox2", labels=("0", "rcpt="), flags=R | IMP, time=1_700_000_000)
    b.add("sent1", labels=("7",), flags=S)
    b.add("work1", labels=("cli=",), flags=R)
    b.add("draft1", labels=("8",), flags=0)
    b.add("odd1", labels=("0",), flags=0)                                        # disagreement
    b.add("imp1", labels=("6",), flags=IMP)                                      # imported, kept
    b.add("trash1", labels=("3",), flags=R)
    b.add("spam1", labels=("4",), flags=R)
    b.add("nomid", labels=("0",), flags=R, eml=eml_bytes(message_id=None))
    b.add("dupA", labels=("0",), flags=R, eml=eml_bytes(message_id="<same@x>"))
    b.add("dupB", labels=("0",), flags=R, eml=eml_bytes(message_id="<same@x>"))
    b.add("long1", labels=("0",), flags=R, eml=eml_bytes(message_id="<long1@x>", body=b"z" * 11_000 + b"\r\n"))
    b.add("long2", labels=("0",), flags=R,
          eml=eml_bytes(message_id="<long2@x>", content_type="multipart/signed; boundary=b", body=b"z" * 11_000 + b"\r\n"))
    b.add("big1", labels=("0",), flags=R, eml=eml_bytes(message_id="<big1@x>", body=b"a\r\n" * 600))
    b.add("unk1", labels=("0", "ghost="), flags=R)
    b.add("orphanE", metadata=False)
    b.add("orphanM", write_eml=False)
    (b.dir / "broken.metadata.json").write_text("{nope")
    (b.dir / "broken.eml").write_bytes(eml_bytes())
    (b.dir / "half.eml.part").write_bytes(b"")
    return b


def test_inventory_counts(mixed):
    inv = pe.build_inventory(mixed.dir, max_size=1_000)
    assert inv.message_count == 16
    assert inv.label_counts["0"] == 10
    assert inv.label_counts["cli="] == 1
    assert inv.starred == 1
    assert inv.unknown_label_ids == {"ghost=": 1}
    assert inv.earliest == 1_400_000_000 and inv.latest == 1_700_000_000
    assert inv.draft_disagreements == ["odd1"]
    assert inv.draft_table[(True, False, False)] == 1
    assert inv.draft_table[(False, False, True)] == 1
    assert inv.missing_message_id == ["nomid"]
    assert inv.duplicate_message_ids == {"<same@x>": ["dupA", "dupB"]}
    assert inv.long_line == ["long1"]
    assert inv.long_line_signed == ["long2"]
    assert [pid for pid, _ in inv.oversize] == ["big1", "long1", "long2"]
    assert inv.orphan_eml == ["orphanE"]
    assert inv.orphan_metadata == ["orphanM"]
    assert [name for name, _ in inv.bad_metadata] == ["broken.metadata.json"]
    assert inv.partial_files == ["half.eml.part"]


def test_inventory_default_upload_totals(mixed):
    inv = pe.build_inventory(mixed.dir, max_size=1_000)
    assert inv.skip_counts == {
        pe.SkipReason.DRAFT: 1, pe.SkipReason.TRASH: 1, pe.SkipReason.SPAM: 1, pe.SkipReason.OVERSIZE: 3,
    }
    assert inv.upload_count == 16 - 6
    assert inv.upload_count + sum(inv.skip_counts.values()) == inv.message_count


def test_inventory_signed_long_line_counts_as_skip_when_not_oversize(mixed):
    inv = pe.build_inventory(mixed.dir, max_size=10**6)
    assert inv.skip_counts[pe.SkipReason.LONG_LINE_SIGNED] == 1
    assert pe.SkipReason.OVERSIZE not in inv.skip_counts


def test_empty_eml_reported_as_partial(export):
    export.add("empty", eml=b"")
    inv = pe.build_inventory(export.dir)
    assert "empty.eml" in inv.partial_files


def test_export_tool_log_errors(export):
    (export.dir.parent / "export.log").write_text(
        "starting export\nmessage abc: download failed: timeout\nall good\nFATAL: disk full\n"
    )
    inv = pe.build_inventory(export.dir)
    assert inv.log_error_count == 2
    assert [n for _, n, _ in inv.log_errors] == [2, 4]


def test_no_log_found(export):
    inv = pe.build_inventory(export.dir)
    assert inv.logs_checked == []
    assert "No Export Tool log found" in render_scan(inv)


def test_scan_is_read_only(mixed):
    before = {p.name: p.stat().st_mtime_ns for p in mixed.dir.iterdir()}
    pe.build_inventory(mixed.dir)
    after = {p.name: p.stat().st_mtime_ns for p in mixed.dir.iterdir()}
    assert before == after


# --- rendering and CLI --------------------------------------------------------------------------

def test_render_sidebar_order_and_tree(mixed):
    text = render_scan(pe.build_inventory(mixed.dir, max_size=1_000))
    system = text[text.index("System"):text.index("Folders")]
    assert system.index("Inbox") < system.index("Drafts") < system.index("Sent") < system.index("Starred")
    assert system.index("Starred") < system.index("Spam") < system.index("Trash")
    folders = text[text.index("Folders"):text.index("Labels")]
    assert "    Work " in folders and "      Clients" in folders
    assert "Receipts" in text[text.index("Labels"):]
    assert "skip as draft" in text and "keep, report disagreement" in text
    assert "upload 10 messages" in text


def test_cli_scan_text_and_exit_code(mixed, capsys):
    assert main(["scan", str(mixed.dir.parent.parent), "--max-size", "0.001"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Proton export: {mixed.dir}")


def test_cli_scan_json(mixed, capsys):
    assert main(["scan", str(mixed.dir), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["message_count"] == 16
    assert data["label_counts"]["10"] == {"name": "Starred", "kind": "system", "count": 1}
    assert data["duplicate_message_ids"] == {"<same@x>": ["dupA", "dupB"]}


def test_cli_scan_bad_folder(tmp_path, capsys):
    assert main(["scan", str(tmp_path)]) == 2
    assert "no labels.json" in capsys.readouterr().err


# --- export completeness (from the real Export Tool layout) --------------------------------------

def _tool_log(export: ExportBuilder, found: int, *, mention: bool = True, name: str = "20261106_174252_export.log"):
    logs = export.dir.parent.parent / "logs"
    logs.mkdir(exist_ok=True)
    where = f' export-dir="{export.dir}"' if mention else ""
    (logs / name).write_text(
        f'time="x" level="info" msg="Starting" export="mail"{where}\n'
        f'time="x" level="info" msg="Found {found} Messages for download" export="mail"\n'
    )


def test_log_in_tool_logs_dir_gives_expected_count(export):
    for i in range(3):
        export.add(f"m{i}")
    _tool_log(export, found=5)
    inv = pe.build_inventory(export.dir)
    assert inv.expected_count == 5
    assert inv.missing_vs_log == 2
    assert "2 are not in this folder" in render_scan(inv)


def test_log_complete(export):
    export.add("m0")
    _tool_log(export, found=1)
    inv = pe.build_inventory(export.dir)
    assert inv.missing_vs_log == 0
    assert "all are present" in render_scan(inv)


def test_unreadable_messages_count_as_present_for_log_compare(export):
    export.add("m0")
    (export.dir / "bad.metadata.json").write_text("{nope")
    (export.dir / "bad.eml").write_bytes(eml_bytes())
    _tool_log(export, found=2)
    assert pe.build_inventory(export.dir).missing_vs_log == 0


def test_logs_for_other_exports_are_ignored(export):
    export.add("m0")
    _tool_log(export, found=1)
    _tool_log(export, found=999, mention=False, name="20250101_000000_export.log")
    (export.dir.parent.parent / "logs" / "20250101_000000_export.log").write_text(
        'msg="Found 999 Messages for download" export-dir="/elsewhere"\nlevel="error" msg="boom"\n')
    inv = pe.build_inventory(export.dir)
    assert inv.expected_count == 1
    assert inv.log_error_count == 0
    assert [p.name for p in inv.logs_checked] == ["20261106_174252_export.log"]


def test_recently_modified_warns_and_old_does_not(export):
    import os
    export.add("m0")
    assert "Is the Export Tool still running?" in render_scan(pe.build_inventory(export.dir))
    old = 1_700_000_000
    os.utime(export.dir, (old, old))
    assert "still running" not in render_scan(pe.build_inventory(export.dir))


def test_leftover_temp_dir_reported_only_when_not_empty(export):
    export.add("m0")
    (export.dir / "temp").mkdir()
    assert pe.build_inventory(export.dir).leftover_dirs == []
    (export.dir / "temp" / "chunk").write_bytes(b"x")
    inv = pe.build_inventory(export.dir)
    assert inv.leftover_dirs == ["temp"]
    assert "non-empty working folders" in render_scan(inv)
