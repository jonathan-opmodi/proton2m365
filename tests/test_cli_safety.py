"""sample-export, state-file safety, re-plan selection, export fingerprint and format detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proton2m365 import cli, pipeline
from proton2m365 import proton_export as pe
from proton2m365.sample_export import SAMPLE_MARKER, write_sample_export
from proton2m365.state import open_state

from .conftest import ExportBuilder


def main(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- sample-export (A1) ------------------------------------------------------------------------

def test_sample_export_scans_clean_with_40_messages(tmp_path, capsys):
    code, out, _ = main(capsys, "sample-export", str(tmp_path / "sample"))
    assert code == 0 and "40 messages" in out
    export_dir = pe.resolve_export_dir(tmp_path / "sample")
    listing = pe.list_export(export_dir)
    assert (len(listing.paired), listing.orphan_eml, listing.orphan_metadata) == (40, [], [])
    assert (export_dir / SAMPLE_MARKER).exists()
    assert pe.detect_format(export_dir) == "export-tool-v1"


def test_sample_plan_has_exactly_the_documented_counts(tmp_path):
    export_dir = write_sample_export(tmp_path / "sample")
    r = pipeline.plan_export(export_dir, pipeline.PlanSettings(), state_path=None)
    assert dict(r.folders) == {("Inbox",): 10, ("Sent Items",): 6, ("Archive",): 10, ("Projects", "Alpha"): 8}
    assert dict(r.skips) == {"draft": 2, "trash": 2, "spam": 2}
    assert dict(r.categories) == {"Work": 6, "Starred": 2}
    assert r.upload_count == 34 and len(r.reencode) == 1 and len(r.synthesized_ids) == 1
    rows = pipeline.build_plan_rows(export_dir, pipeline.PlanSettings())[0]
    assert sum(1 for row in rows if row.skip_reason is None and not row.seen) == 8


def test_sample_with_orphan_and_refusing_a_non_empty_folder(tmp_path, capsys):
    export_dir = write_sample_export(tmp_path / "s", with_orphan=True)
    assert pe.list_export(export_dir).orphan_eml == ["sample-orphan"]
    code, _, err = main(capsys, "sample-export", str(tmp_path / "s"))
    assert code == 2 and "not empty" in err


def test_sample_state_needs_sample_ok_to_upload(tmp_path, capsys, monkeypatch):
    export_dir = write_sample_export(tmp_path / "sample")
    state = tmp_path / "sample.sqlite"
    pipeline.plan_export(export_dir, pipeline.PlanSettings(), state_path=state)

    class Conns:
        def __init__(self, args, recorded=None):
            self.auth = type("A", (), {"username": "t@example.com", "home_account_id": "o.t", "is_personal": False})()

        def close(self):
            pass

    monkeypatch.setattr(cli, "_Connections", Conns)
    code, _, err = main(capsys, "upload", "--state", str(state))
    assert code == 2 and "synthetic sample" in err and "--sample-ok" in err


# --- state-file safety (X7) --------------------------------------------------------------------

def test_only_plan_creates_a_state_file(tmp_path, capsys):
    (tmp_path / "other.sqlite").write_bytes(b"")
    code, _, err = main(capsys, "status", "--state", str(tmp_path / "typo.sqlite"))
    assert code == 2 and "no state at" in err and "--state" in err and "other.sqlite" in err
    assert not (tmp_path / "typo.sqlite").exists()
    code, _, err = main(capsys, "verify", "--state", str(tmp_path / "typo.sqlite"))
    assert code == 2 and not (tmp_path / "typo.sqlite").exists()


def test_state_path_from_environment_and_config(tmp_path, capsys, monkeypatch, export):
    export.add("a1")
    env_state = tmp_path / "env.sqlite"
    monkeypatch.setenv("PROTON2M365_STATE", str(env_state))
    assert main(capsys, "plan", str(export.dir))[0] == 0
    assert env_state.exists()
    monkeypatch.delenv("PROTON2M365_STATE")
    from proton2m365 import auth

    auth.DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    auth.DEFAULT_CONFIG.write_text(f'state = "{tmp_path / "cfg.sqlite"}"\n')
    assert main(capsys, "plan", str(export.dir))[0] == 0
    assert (tmp_path / "cfg.sqlite").exists()


# --- re-plan keeps the selection (X8) -----------------------------------------------------------

LABELS = [{"ID": "n=", "Name": "News", "Path": "News", "ParentID": "", "Type": 1}]


def test_replan_reuses_the_saved_selection(tmp_path, capsys):
    export = ExportBuilder(tmp_path, labels=LABELS)
    export.add("a1", labels=("0", "n="))
    export.add("t1", labels=("3",))
    assert main(capsys, "plan", str(export.dir), "--exclude-label", "News", "--starred-as-flag")[0] == 0
    code, out, _ = main(capsys, "plan", str(export.dir), "--include-trash")
    assert code == 0 and "Reusing the selection saved in this state file" in out and "--exclude-label News" in out
    with open_state(tmp_path / "state.sqlite", write=False) as st:
        saved = json.loads(st.get_meta("plan_settings"))
    assert saved["exclude_labels"] == ["News"] and saved["include_trash"] and saved["starred_as_flag"]
    assert main(capsys, "plan", str(export.dir), "--reset-selection")[0] == 0
    with open_state(tmp_path / "state.sqlite", write=False) as st:
        saved = json.loads(st.get_meta("plan_settings"))
    assert saved["exclude_labels"] == [] and not saved["include_trash"] and not saved["starred_as_flag"]


# --- export fingerprint (EN6) --------------------------------------------------------------------

@pytest.fixture
def planned(tmp_path, export):
    export.add("a1")
    pipeline.plan_export(export.dir, pipeline.PlanSettings(), state_path=tmp_path / "state.sqlite")
    return export


def test_verify_refuses_a_missing_export(planned, tmp_path):
    (planned.dir / "labels.json").rename(planned.dir / "labels.moved")
    with open_state(tmp_path / "state.sqlite") as st, pytest.raises(pe.ExportError, match="P2M-EXPORT-CHANGED"):
        pipeline.check_export_unchanged(st)


def test_verify_refuses_a_different_export(planned, tmp_path):
    (planned.dir / "labels.json").write_text(json.dumps({"Version": 1, "Payload": []}))
    with open_state(tmp_path / "state.sqlite") as st, pytest.raises(pe.ExportError, match="not the one"):
        pipeline.check_export_unchanged(st)


def test_same_export_passes(planned, tmp_path):
    with open_state(tmp_path / "state.sqlite") as st:
        assert pipeline.check_export_unchanged(st) == planned.dir


# --- export format detection ------------------------------------------------------------------

def test_unknown_format_is_refused_unless_allowed(export, capsys, tmp_path):
    export.add("a1")
    (export.dir / "labels.json").write_text(json.dumps({"Version": 2, "Labels": []}))
    code, _, err = main(capsys, "scan", str(export.dir))
    assert code == 2 and "P2M-EXPORT-FORMAT" in err and "doctor --issue" in err
    code, _, err = main(capsys, "plan", str(export.dir), "--allow-unknown-format", "--dry-run")
    assert "continuing because of --allow-unknown-format" in err


def test_unknown_metadata_shape_is_refused(export):
    export.add("a1")
    (export.dir / "a1.metadata.json").write_text(json.dumps({"Version": 1, "Payload": {"Id": "a1"}}))
    with pytest.raises(pe.UnknownExportFormat):
        pe.detect_format(export.dir)


def test_plan_records_the_format_and_doctor_shows_it(export, tmp_path, capsys):
    export.add("a1")
    assert main(capsys, "plan", str(export.dir))[0] == 0
    with open_state(tmp_path / "state.sqlite", write=False) as st:
        assert json.loads(st.get_meta("plan_settings"))["export_format"] == "export-tool-v1"
    code, out, _ = main(capsys, "doctor")
    assert code == 0 and '"export_format": "export-tool-v1"' in out


def test_unwrapped_labels_are_a_known_format(tmp_path):
    b = ExportBuilder(tmp_path, wrapped=False)
    assert pe.detect_format(b.dir) == "unwrapped-v0"


# --- exclude-file and permanent failures (6a, X10) ----------------------------------------------

def test_exclude_file_skips_a_planned_message_and_accounts_for_an_orphan(tmp_path, export, capsys):
    export.add("a1")
    export.add("a2")
    (export.dir / "broken.eml").write_bytes(b"x")                       # an orphan .eml
    assert main(capsys, "plan", str(export.dir), "--exclude-file", "a2.eml", "--exclude-file", "broken")[0] == 0
    with open_state(tmp_path / "state.sqlite") as st:
        assert st.skip_counts() == {"excluded-by-user": 1}
        acct = pipeline.export_accounting(st, export.dir)
    assert acct.unresolved == () and acct.excluded_files == ("broken.eml",)
    code, out, _ = main(capsys, "plan", str(export.dir))                 # a re-plan keeps both
    assert "--exclude-file a2.eml" in out and "--exclude-file broken" in out


def test_rejected_rows_point_to_exclude_file_and_transient_ones_to_retry():
    from proton2m365.status import ExportAccounting, Progress, migration_status

    s = migration_status(Progress(verified=3, upload_failed=2, upload_rejected=2), ExportAccounting())
    assert s.next_command is None and "--exclude-file <id>" in s.next_hint
    s = migration_status(Progress(verified=3, upload_failed=2, upload_rejected=1), ExportAccounting())
    assert s.next_command == "proton2m365 upload --retry-failed"


# --- settings from flag, environment or config.toml (X12) ---------------------------------------

def test_settings_come_from_flag_then_environment_then_config(monkeypatch):
    from proton2m365 import auth

    auth.DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    auth.DEFAULT_CONFIG.write_text('account = "cfg@example.com"\nmax_msgs_per_sec = 3\n')
    monkeypatch.setenv("PROTON2M365_MAX_BYTES_PER_SEC", "500000")
    args = cli.build_parser().parse_args(["upload", "--max-msgs-per-sec", "1"])
    cli._resolve_settings(args)
    assert (args.max_msgs_per_sec, args.max_bytes_per_sec, args.account) == (1.0, 500000.0, "cfg@example.com")
    assert args.setting_sources == {"state": "default", "account": "config.toml", "max_msgs_per_sec": "flag",
                                    "max_bytes_per_sec": "environment"}


def test_doctor_shows_where_settings_came_from(capsys, monkeypatch):
    monkeypatch.setenv("PROTON2M365_STATE", "elsewhere.sqlite")
    code, out, _ = main(capsys, "doctor")
    assert code == 0 and "Settings (where each came from):" in out and "  state: environment" in out
