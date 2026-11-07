from __future__ import annotations

import pytest

from proton2m365.status import (
    EXIT_CODES, CheckFacts, ExportAccounting, Outcome, Progress, migration_status,
)

DONE = Progress(verified=5, skipped=2)
EXPORT_OK = ExportAccounting()


@pytest.mark.parametrize(("progress", "export", "checks", "outcome", "code", "next_command"), [
    # PASSED only with everything verified and the export fully accounted for
    (DONE, EXPORT_OK, None, Outcome.PASSED, 0, None),
    (Progress(skipped=3), EXPORT_OK, None, Outcome.PASSED, 0, None),
    (Progress(), EXPORT_OK, None, Outcome.PASSED, 0, None),
    # SAMPLE OK: nothing wrong, nothing left to do, some rows never checked
    (Progress(verified=3, unchecked=2), EXPORT_OK, None, Outcome.SAMPLE_OK, 10, "proton2m365 verify --all"),
    # INCOMPLETE beats SAMPLE OK; Next: follows D7's order
    (Progress(planned=1, unchecked=4), EXPORT_OK, None, Outcome.INCOMPLETE, 11, "proton2m365 upload"),
    (Progress(upload_failed=1, appended=1), EXPORT_OK, None, Outcome.INCOMPLETE, 11,
     "proton2m365 upload --retry-failed"),
    (Progress(appended=1, tag_failed=1), EXPORT_OK, None, Outcome.INCOMPLETE, 11, "proton2m365 tag"),
    (Progress(tag_failed=1, waiting_prune=1), EXPORT_OK, None, Outcome.INCOMPLETE, 11,
     "proton2m365 tag --retry-failed"),
    (Progress(verified=1, waiting_prune=1), EXPORT_OK, None, Outcome.INCOMPLETE, 11, "proton2m365 prune"),
    (DONE, ExportAccounting(unresolved=("x.eml",)), None, Outcome.INCOMPLETE, 11, None),
    (DONE, ExportAccounting(missing_from_export=("p1",)), None, Outcome.INCOMPLETE, 11, None),
    (DONE, ExportAccounting(available=False), None, Outcome.INCOMPLETE, 11, None),
    # FAILED beats everything
    (Progress(planned=3, verified=1), EXPORT_OK, CheckFacts(mismatched=1), Outcome.FAILED, 12, None),
    (Progress(verified=4, failed_check=1), EXPORT_OK, None, Outcome.FAILED, 12, None),
    (DONE, EXPORT_OK, CheckFacts(folders_short=("Inbox",)), Outcome.FAILED, 12, None),
])
def test_outcome_table(progress, export, checks, outcome, code, next_command):
    s = migration_status(progress, export, checks)
    assert (s.outcome, s.exit_code, s.next_command) == (outcome, code, next_command)


def test_unresolved_files_are_named_and_come_first():
    s = migration_status(Progress(planned=2), ExportAccounting(unresolved=("a.eml", "b.metadata.json")))
    assert "a.eml" in s.reasons[0] and "b.metadata.json" in s.reasons[0]
    assert s.next_command is None and "plan" in s.next_hint
    assert any("planned" in r for r in s.reasons)


def test_without_export_accounting_nothing_can_pass():
    assert migration_status(DONE, None).outcome == Outcome.SAMPLE_OK


def test_next_command_repeats_a_non_default_state_quoted():
    s = migration_status(Progress(planned=1), EXPORT_OK, state_arg="my state.sqlite")
    assert s.next_command == "proton2m365 upload --state 'my state.sqlite'"


def test_passed_sentences():
    assert migration_status(DONE, EXPORT_OK).sentence == "all 5 messages are in the mailbox and checked"
    assert migration_status(Progress(skipped=3), EXPORT_OK).sentence == \
        "nothing to migrate; all 3 messages are intentionally skipped"


def test_exit_code_table_has_no_duplicates_and_no_outcome_on_1_or_2():
    codes = [c for c, _ in EXIT_CODES]
    assert len(codes) == len(set(codes))
    outcome_codes = {migration_status(p, EXPORT_OK, c).exit_code for p, c in [
        (DONE, None), (Progress(unchecked=1), None), (Progress(planned=1), None), (DONE, CheckFacts(mismatched=1))]}
    assert outcome_codes == {0, 10, 11, 12}


def test_unresolved_drift_keeps_it_incomplete():
    s = migration_status(Progress(verified=3, drift=1), EXPORT_OK)
    assert s.outcome == Outcome.INCOMPLETE and "--accept-drift" in s.next_hint
