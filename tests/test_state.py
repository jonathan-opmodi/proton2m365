from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from proton2m365.state import SCHEMA_VERSION, PlanRow, StateError, StateLocked, open_state

from .conftest import owner_only


def row(pid="p1", *, folder=("Inbox",), cats=(), seen=True, flagged=False, skip=None, sha="a" * 64, path=None):
    return PlanRow(
        proton_id=pid, eml_path=path or f"/x/{pid}.eml", sha256=sha, size=10, target_folder=folder,
        categories=cats, seen=seen, flagged=flagged, internal_date=1_000, message_id=f"<{pid}@x>",
        synth_message_id=False, needs_reencode=False, skip_reason=skip,
    )


def set_stage(state, pid, stage):
    with state.conn:
        state.conn.execute("UPDATE messages SET stage = ? WHERE proton_id = ?", (stage, pid))


def stage_of(state, pid):
    return state.conn.execute("SELECT stage FROM messages WHERE proton_id = ?", (pid,)).fetchone()[0]


@pytest.fixture
def state(tmp_path):
    with open_state(tmp_path / "state.sqlite", create=True) as s:
        yield s


def test_insert_planned_and_skipped(state):
    r = state.apply_plan([row("a"), row("b", skip="draft")])
    assert r.new == 2
    assert stage_of(state, "a") == "planned" and stage_of(state, "b") == "skipped"


@pytest.mark.parametrize(
    ("start", "skip", "expected"),
    [
        ("planned", "trash", "skipped"),
        ("skipped", None, "planned"),
        ("upload_failed", "oversize", "skipped"),
        ("upload_failed", None, "upload_failed"),   # waits for --retry-failed
        ("planned", None, "planned"),
    ],
)
def test_reclassify_rows_not_in_mailbox(state, start, skip, expected):
    state.apply_plan([row("a")])
    set_stage(state, "a", start)
    state.apply_plan([row("a", skip=skip)])
    assert stage_of(state, "a") == expected


def test_not_uploaded_rows_take_new_folder(state):
    state.apply_plan([row("a", folder=("Inbox",))])
    r = state.apply_plan([row("a", folder=("Archive",))])
    assert r.updated == 1 and not r.drift
    assert state.conn.execute("SELECT target_folder FROM messages").fetchone()[0] == '["Archive"]'


@pytest.mark.parametrize("stage", ["appended", "tagged", "tag_failed", "verified"])
def test_uploaded_rows_never_move_back_or_change_folder(state, stage):
    state.apply_plan([row("a", folder=("Inbox",))])
    set_stage(state, "a", stage)
    r = state.apply_plan([row("a", folder=("Archive",), skip="trash", seen=False, sha="b" * 64)])
    assert stage_of(state, "a") == stage
    assert state.conn.execute("SELECT target_folder FROM messages").fetchone()[0] == '["Inbox"]'
    assert {d.field for d in r.drift} >= {"target_folder", "seen", "sha256"}
    assert r.to_prune == ["a"]                       # now skipped: `prune` removes it from the mailbox
    assert state.rows_to_prune() == {("Inbox",): ["a"]}
    assert state.rows_to_tag() == [] and state.rows_to_verify() == []  # nothing else touches it
    state.mark_pruned("a")
    assert stage_of(state, "a") == "skipped" and state.rows_to_prune() == {}


@pytest.mark.parametrize("stage", ["appended", "tag_failed", "tagged", "verified"])
def test_category_and_flag_changes_always_apply_at_the_next_tag(state, stage):
    state.apply_plan([row("a", cats=("Important",))])
    set_stage(state, "a", stage)
    r = state.apply_plan([row("a", cats=(), flagged=True)])
    stored = state.conn.execute("SELECT categories, flagged, stage FROM messages").fetchone()
    assert tuple(stored)[:2] == ("[]", 1) and {d.field for d in r.applied_to_uploaded} == {"categories", "flagged"}
    # A tagged or verified row goes back to appended, so tag applies the change and verify checks it again.
    assert stored[2] == ("appended" if stage in ("tagged", "verified") else stage)
    assert not r.drift and state.unresolved_drift() == []


def test_folder_and_date_drift_is_unresolved_until_accepted(state):
    state.apply_plan([row("a"), row("b")])
    set_stage(state, "a", "verified")
    moved = PlanRow(**{**row("a", folder=("Archive",)).__dict__, "internal_date": 2_000})
    r = state.apply_plan([moved, row("b")])
    assert {d.field for d in r.drift} == {"target_folder", "internal_date"}
    assert r.unresolved_drift == ["a"] and state.progress().drift == 1
    assert state.conn.execute("SELECT target_folder FROM messages WHERE proton_id = 'a'").fetchone()[0] == '["Inbox"]'
    r = state.apply_plan([moved, row("b")], accept_drift=True)
    assert r.unresolved_drift == [] and state.progress().drift == 0
    r = state.apply_plan([moved, row("b")])                                 # accepted stays accepted
    assert r.unresolved_drift == []
    newer = PlanRow(**{**moved.__dict__, "target_folder": ("Other",)})
    assert state.apply_plan([newer, row("b")]).unresolved_drift == ["a"]  # a different change asks again


def test_moved_export_updates_path_silently(state):
    state.apply_plan([row("a")])
    set_stage(state, "a", "tagged")
    r = state.apply_plan([row("a", path="/moved/a.eml")])
    assert not r.drift and r.updated == 1
    assert state.conn.execute("SELECT eml_path FROM messages").fetchone()[0] == "/moved/a.eml"


def test_missing_from_export_is_reported_not_deleted(state):
    state.apply_plan([row("a"), row("b")])
    r = state.apply_plan([row("a")])
    assert r.missing_from_export == ["b"]
    assert state.stage_counts() == {"planned": 2}


def test_settings_recorded(state):
    state.apply_plan([row("a")], settings={"include_trash": True})
    assert state.get_meta("plan_settings") == '{"include_trash": true}'


def test_read_only_open_requires_existing_state(tmp_path):
    with pytest.raises(StateError, match="no state at .*missing.sqlite"):
        with open_state(tmp_path / "missing.sqlite", write=False):
            pass


def test_second_writer_is_refused(tmp_path):
    path = tmp_path / "state.sqlite"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import os, sys, time
            from proton2m365.state import open_state
            with open_state({str(path)!r}, create=True):
                print(os.getpid(), flush=True)
                time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        reported = holder.stdout.readline().strip()   # the real interpreter's PID (a launcher on Windows)
        with pytest.raises(StateLocked) as exc:
            with open_state(path):
                pass
        assert exc.value.holder == reported
    finally:
        holder.kill()
        holder.wait()
    with open_state(path):  # released when the holder dies
        pass


# --- upload / tag transitions ----------------------------------------------------------------------

from proton2m365.state import MAX_TAG_ATTEMPTS, MAX_UPLOAD_ATTEMPTS  # noqa: E402


def test_rows_to_upload_and_retry_failed_are_disjoint(state):
    state.apply_plan([row("a"), row("b"), row("c", skip="draft")])
    set_stage(state, "b", "upload_failed")
    assert [r.proton_id for r in state.rows_to_upload()] == ["a"]
    assert [r.proton_id for r in state.rows_to_upload(retry_failed=True)] == ["b"]
    r = state.rows_to_upload()[0]
    assert r.target_folder == ("Inbox",) and r.seen is True and r.message_id == "<a@x>"


def test_mark_appended_and_tagged(state):
    state.apply_plan([row("a")])
    state.mark_appended("a", 42)
    assert stage_of(state, "a") == "appended"
    assert [r.imap_uid for r in state.rows_to_tag()] == [42]
    state.mark_tagged("a", "AAMk")
    assert stage_of(state, "a") == "tagged"
    assert state.rows_to_tag() == []


def test_transitions_refuse_wrong_stage(state):
    state.apply_plan([row("a", skip="trash")])
    with pytest.raises(StateError, match="cannot move from skipped"):
        state.mark_appended("a", 1)
    with pytest.raises(StateError, match="cannot move from skipped"):
        state.mark_tagged("a", "g")


def test_upload_failures_count_up_to_failed(state):
    state.apply_plan([row("a")])
    for _ in range(MAX_UPLOAD_ATTEMPTS - 1):
        assert state.record_upload_failure("a", "timeout") == "planned"
    assert state.record_upload_failure("a", "timeout") == "upload_failed"
    assert state.reset_attempts("upload_failed") == 1
    assert state.rows_to_upload(retry_failed=True)[0].upload_attempts == 0


def test_permanent_upload_failure_fails_at_once(state):
    state.apply_plan([row("a")])
    assert state.record_upload_failure("a", "BAD too big", permanent=True) == "upload_failed"


def test_tag_misses_count_up_to_tag_failed(state):
    state.apply_plan([row("a")])
    state.mark_appended("a", 1)
    for _ in range(MAX_TAG_ATTEMPTS - 1):
        assert state.record_tag_miss("a", "graph-unmatched") == "appended"
    assert state.record_tag_miss("a", "graph-unmatched") == "tag_failed"
    assert [r.proton_id for r in state.rows_to_tag(retry_failed=True)] == ["a"]
    state.mark_tagged("a", "g")  # tag_failed rows can still be tagged by --retry-failed
    assert stage_of(state, "a") == "tagged"


def test_upload_folders_and_folder_ids(state):
    state.apply_plan([row("a", folder=("Archive",)), row("b", folder=("X", "Y")), row("c", folder=("Z",), skip="spam")])
    assert state.upload_folders() == {("Archive",), ("X", "Y")}
    assert state.folder_id(("X", "Y")) is None
    state.set_folder_id(("X", "Y"), "fid1")
    state.set_folder_id(("X", "Y"), "fid2")
    assert state.folder_id(("X", "Y")) == "fid2"


# --- verification evidence and schema upgrade --------------------------------------------------

def test_failed_check_clears_verified_and_is_remembered(state):
    state.apply_plan([row("a"), row("b")])
    set_stage(state, "a", "tagged")
    set_stage(state, "b", "tagged")
    state.mark_verified("a", checked_at=100)
    state.mark_check_failed("a", ["read", "received date", "read"], checked_at=200)
    assert stage_of(state, "a") == "tagged"
    assert state.failed_check_ids() == {"a"}
    got = state.conn.execute("SELECT check_result, checked_at FROM messages WHERE proton_id = 'a'").fetchone()
    assert tuple(got) == ("fail:read; received date", 200)
    state.mark_verified("a", checked_at=300)
    assert stage_of(state, "a") == "verified" and state.failed_check_ids() == set()


def _make_v1(path, *, verified=("a",), planned=("b",)):
    """A state file as proton2m365 schema 1 wrote it (no verification columns)."""
    with open_state(path, create=True) as s:
        s.apply_plan([row(p) for p in (*verified, *planned)])
        for pid in verified:
            set_stage(s, pid, "verified")
        with s.conn:
            s.conn.execute("ALTER TABLE messages DROP COLUMN check_result")
            s.conn.execute("ALTER TABLE messages DROP COLUMN checked_at")
            s.conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")


def _columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(messages)")}


def test_v1_state_upgrades_with_backup(tmp_path):
    path = tmp_path / "state.sqlite"
    _make_v1(path)
    with open_state(path) as s:
        assert {"check_result", "checked_at"} <= _columns(s.conn)
        assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert "source_present" in _columns(s.conn)
        assert s.conn.execute("SELECT check_result FROM messages WHERE proton_id = 'a'").fetchone()[0] == "pass"
        assert s.failed_check_ids() == set()
    backup = tmp_path / "state.sqlite.bak-v1"
    assert backup.exists() and owner_only(backup)
    import sqlite3
    b = sqlite3.connect(backup)
    assert b.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
    assert b.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    b.close()


def test_upgrade_backup_includes_rows_still_in_the_wal(tmp_path):
    import sqlite3
    path = tmp_path / "state.sqlite"
    _make_v1(path, verified=(), planned=("b",))
    writer = sqlite3.connect(path)  # stays open, so its WAL is not checkpointed into the main file
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    with writer:
        writer.execute("UPDATE messages SET stage = 'verified' WHERE proton_id = 'b'")
    with open_state(path):
        pass
    writer.close()
    b = sqlite3.connect(tmp_path / "state.sqlite.bak-v1")
    assert b.execute("SELECT stage FROM messages WHERE proton_id = 'b'").fetchone()[0] == "verified"
    b.close()


def test_read_only_open_does_not_upgrade(tmp_path):
    path = tmp_path / "state.sqlite"
    _make_v1(path)
    with open_state(path, write=False) as s:
        assert s.get_meta("schema_version") == "1"
        assert "check_result" not in _columns(s.conn)
    assert not (tmp_path / "state.sqlite.bak-v1").exists()


def test_progress_reads_old_state_without_upgrading_it(tmp_path):
    path = tmp_path / "state.sqlite"
    _make_v1(path)
    with open_state(path, write=False) as s:
        p = s.progress()
    assert (p.verified, p.planned, p.failed_check) == (1, 1, 0)


def test_progress_separates_waiting_prune_and_failed_checks(state):
    state.apply_plan([row("a"), row("b"), row("c"), row("d", skip="draft")])
    for pid in "abc":
        set_stage(state, pid, "tagged")
    state.mark_check_failed("b", ["read"], checked_at=1)
    with state.conn:
        state.conn.execute("UPDATE messages SET skip_reason = 'excluded-label' WHERE proton_id = 'c'")
    p = state.progress()
    assert (p.unchecked, p.failed_check, p.waiting_prune, p.skipped) == (1, 1, 1, 1)


def test_newer_state_is_refused_and_left_unchanged(tmp_path):
    path = tmp_path / "state.sqlite"
    with open_state(path, create=True) as s:
        s.apply_plan([row("a")])
        with s.conn:
            s.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    before = path.read_bytes()
    with pytest.raises(StateError, match="schema version 99"):
        with open_state(path):
            pass
    assert path.read_bytes() == before


def test_an_interrupted_backup_is_made_again(tmp_path):
    import sqlite3
    path = tmp_path / "state.sqlite"
    _make_v1(path)
    (tmp_path / "state.sqlite.bak-v1").write_bytes(b"half a database")   # an earlier upgrade was cut off
    with open_state(path):
        pass
    b = sqlite3.connect(tmp_path / "state.sqlite.bak-v1")
    assert b.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    b.close()
