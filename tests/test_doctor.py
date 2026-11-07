"""doctor: everything an issue needs, nothing private. Private-looking values are assembled at runtime."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from proton2m365 import auth as auth_mod
from proton2m365 import cli, doctor, pipeline
from proton2m365.state import open_state

from .conftest import ExportBuilder

AT = "@"
ADDRESS = "dana.private" + AT + "example.com"
TENANT_ID = "1234abcd-0000-4000-8000-00000000beef"
HOME = "/Users/" + "danaprivate"
LABELS = [
    {"ID": "sec=", "Name": "Secret Project", "Path": "Secret Project", "ParentID": "", "Type": 1, "Color": "#ff0000"},
    {"ID": "fold=", "Name": "Private Folder", "Path": "Private Folder", "ParentID": "", "Type": 3},
]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.delenv("PROTON2M365_CLIENT_ID", raising=False)
    export = ExportBuilder(tmp_path, labels=LABELS)
    export.add("m1", labels=("0", "sec="))
    export.add("m2", labels=("fold=",))
    state_path = tmp_path / "state.sqlite"
    pipeline.plan_export(export.dir, pipeline.PlanSettings(exclude_labels=("Secret Project",)), state_path=state_path)
    with open_state(state_path) as st, st.conn:
        st.conn.execute("UPDATE meta SET value = ? WHERE key = 'plan_settings'", (json.dumps(
            {**json.loads(st.get_meta("plan_settings")), "export_dir": f"{HOME}/Downloads/{ADDRESS}/mail_1"}),))
        st.conn.execute("INSERT INTO meta(key, value) VALUES('account', ?)",
                        (json.dumps({"home_account_id": f"oid.{TENANT_ID}", "username": ADDRESS}),))
        st.conn.execute("UPDATE messages SET stage = 'upload_failed', last_error = ? WHERE proton_id = 'm2'",
                        (f"APPEND rejected: Subject 'Quarterly numbers for {ADDRESS}' too large",))
    monkeypatch.chdir(tmp_path)
    return state_path


def run(capsys, *argv):
    code = cli.main(["doctor", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_issue_report_has_the_template_headings_and_nothing_private(seeded, capsys):
    code, out, _ = run(capsys, "--state", str(seeded), "--issue")
    assert code == 0
    for heading in doctor.ISSUE_HEADINGS:
        assert f"### {heading}" in out
    for private in (ADDRESS, "dana.private", TENANT_ID, "danaprivate", "Secret Project", "Private Folder",
                    "Quarterly", str(seeded.parent)):
        assert private not in out, private
    assert "upload_failed errors: 1 rejected by the server" in out
    assert re.search(r"bound to account: <address [0-9a-f]{8}>", out)
    assert "Account:\n  not run:" in out and "Mailbox:\n  not run" in out


def test_show_names_reveals_them_but_not_with_issue(seeded, capsys):
    code, out, _ = run(capsys, "--state", str(seeded), "--show-names")
    assert code == 0 and ADDRESS in out and "Secret Project" in out
    code, _, err = run(capsys, "--state", str(seeded), "--issue", "--show-names")
    assert code == 2 and "always redacts" in err


def test_hashes_are_stable_per_install_and_differ_between_installs(tmp_path):
    a = doctor.Redactor(doctor.install_salt(tmp_path / "a"))
    assert a(ADDRESS) == doctor.Redactor(doctor.install_salt(tmp_path / "a"))(ADDRESS)
    assert a(ADDRESS) != doctor.Redactor(doctor.install_salt(tmp_path / "b"))(ADDRESS)
    assert ((tmp_path / "a" / "doctor-salt").stat().st_mode & 0o777) == 0o600


def test_doctor_never_signs_in_interactively(seeded, capsys, monkeypatch):
    calls = []

    class FakeAuth:
        def __init__(self, config, **kwargs):
            self.config, self.account = config, None

        def sign_in_silent(self, recorded=None):
            calls.append(("silent", recorded))
            return None

        def sign_in(self, **kwargs):
            raise AssertionError("doctor must not sign in interactively")

    monkeypatch.setattr(auth_mod, "Auth", FakeAuth)
    code, out, _ = run(capsys, "--state", str(seeded), "--client-id", "cid")
    assert code == 0 and calls == [("silent", f"oid.{TENANT_ID}")]
    assert "not run: no cached sign-in" in out


def test_issue_template_uses_the_doctor_headings():
    template = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text()
    assert re.findall(r"^### (.+)$", template, re.M) == list(doctor.ISSUE_HEADINGS)
    assert "doctor --issue" in template


@pytest.mark.parametrize(("text", "kind"), [
    ("cannot read /x/y.eml: No such file", "unreadable export file"),
    ("NO [OVERQUOTA] mailbox is full", "mailbox full"),
    ("Server Unavailable. 15 Suggested Backoff Time: 300 milliseconds", "throttled by the server"),
    ("connection reset by peer", "connection problem"),
    ("BAD Command Argument Error. 11", "rejected by the server"),
    ("fail:read", "check did not match"),
    ("something else entirely", "other error"),
])
def test_error_text_is_reduced_to_a_kind(text, kind):
    assert doctor.error_kind(text) == kind


def _parser_running(func):
    parser = cli.build_parser()
    for sub in parser._subparsers._group_actions[0].choices.values():
        if sub.prog.endswith(" status"):
            sub.set_defaults(func=func)
    return parser


def test_crash_is_logged_privately_and_doctor_shows_only_package_frames(seeded, capsys, monkeypatch):
    def boom(args):
        raise ValueError(f"bad value for {ADDRESS} in {HOME}/x")

    parser = _parser_running(boom)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert cli.main(["status", "--state", str(seeded)]) == 1
    err = capsys.readouterr().err
    assert "unexpected error ValueError" in err and "doctor --issue" in err
    log = next(seeded.parent.glob("crash-*.log"))
    assert (log.stat().st_mode & 0o777) == 0o600
    report = doctor.render(doctor.build_report(
        state=None, state_path=seeded, auth=None, auth_error=None, online_checks=None,
        redact=doctor.Redactor(b"s"), crash_dirs=doctor.crash_dirs(seeded, auth_mod.CONFIG_DIR), version="t"))
    assert "ValueError at proton2m365/cli.py:" in report
    assert ADDRESS not in report and "danaprivate" not in report and "tests/" not in report


def test_crash_log_falls_back_to_the_config_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth_mod, "CONFIG_DIR", tmp_path / "config")
    parser = _parser_running(lambda args: 1 / 0)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert cli.main(["status", "--state", str(tmp_path / "no" / "such" / "state.sqlite")]) == 1
    assert list((tmp_path / "config").glob("crash-*.log"))


def test_ctrl_c_is_exit_13(monkeypatch, capsys):
    def interrupted(args):
        raise KeyboardInterrupt

    parser = _parser_running(interrupted)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert cli.main(["status"]) == 13
