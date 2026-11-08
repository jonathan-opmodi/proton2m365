"""Where a migration stands: one outcome, one exit code and one next command, computed in one place.

verify, status and every command's "Next:" line read from `migration_status`, so they cannot disagree.
The function is pure: it takes counts (from State) and accounting facts (from the export and a verify run).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum

# Exit codes, the same for every command (README and --help print EXIT_CODES).
EXIT_OK = 0
EXIT_ERROR = 1          # unexpected error (a crash); never used for an outcome
EXIT_USAGE = 2          # usage, configuration, sign-in, state lock, Graph/IMAP errors
EXIT_MAILBOX_FULL = 3
EXIT_SAMPLE_OK = 10
EXIT_INCOMPLETE = 11    # work remains, including upload/tag runs that left rows to retry
EXIT_FAILED = 12        # a checked message does not match, a folder is short, or a selftest check failed
EXIT_INTERRUPTED = 13   # stopped early (Ctrl-C, connection lost); safe to run the same command again
EXIT_NOT_SAVED = 14     # verify would have PASSED, but the receipt could not be written

EXIT_CODES: tuple[tuple[int, str], ...] = (
    (EXIT_OK, "done (verify: PASSED)"),
    (EXIT_ERROR, "unexpected error"),
    (EXIT_USAGE, "usage, configuration, sign-in or connection error"),
    (EXIT_MAILBOX_FULL, "mailbox full"),
    (EXIT_SAMPLE_OK, "verify: SAMPLE OK (some messages not checked yet)"),
    (EXIT_INCOMPLETE, "incomplete: messages still need work"),
    (EXIT_FAILED, "failed: a check did not match"),
    (EXIT_INTERRUPTED, "interrupted; run the same command again to resume"),
    (EXIT_NOT_SAVED, "verify passed but its receipt could not be saved (other outcomes keep their code)"),
)


class Outcome(StrEnum):
    PASSED = "PASSED"
    SAMPLE_OK = "SAMPLE OK"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


_EXIT = {Outcome.PASSED: EXIT_OK, Outcome.SAMPLE_OK: EXIT_SAMPLE_OK,
         Outcome.INCOMPLETE: EXIT_INCOMPLETE, Outcome.FAILED: EXIT_FAILED}


@dataclass(frozen=True)
class Progress:
    """Row counts from state. Rows the plan now skips but that are still in the mailbox count only as waiting_prune."""

    planned: int = 0
    upload_failed: int = 0
    upload_rejected: int = 0  # of upload_failed: refused for good (rejected by the server, unreadable file)
    appended: int = 0
    tag_failed: int = 0
    unchecked: int = 0       # tagged, never passed a check
    failed_check: int = 0    # tagged, latest check did not match
    verified: int = 0
    waiting_prune: int = 0
    skipped: int = 0
    drift: int = 0           # uploaded messages whose folder, date or content the current plan changes
    missing_source: int = 0  # never uploaded and missing from the newest export (accepted with --accept-missing)
    missing_in_mailbox: int = 0  # in the mailbox, missing from the newest export (still checked by presence)
    source_changed: int = 0  # planned, but the export file changed since plan (plan again reads it)

    @property
    def in_mailbox(self) -> int:
        return self.appended + self.tag_failed + self.unchecked + self.failed_check + self.verified

    @property
    def total(self) -> int:
        return (self.planned + self.upload_failed + self.in_mailbox + self.waiting_prune + self.skipped
                + self.missing_source)


@dataclass(frozen=True)
class ExportAccounting:
    """Export files that are not accounted for by state, as names a user can find on disk."""

    available: bool = True                                  # the export folder could be read
    unresolved: tuple[str, ...] = ()                        # orphans, partial files, files plan could not read
    unplanned_pairs: tuple[str, ...] = ()                   # ids of complete pairs plan could not read
    excluded_files: tuple[str, ...] = ()                    # unresolved files excluded with plan --exclude-file
    missing_from_export: tuple[str, ...] = ()               # state rows whose files are no longer in the export


@dataclass(frozen=True)
class CheckFacts:
    """What a verify run found this time."""

    mismatched: int = 0
    folders_short: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()  # ids with more than one copy in the mailbox (verify --all)


@dataclass(frozen=True)
class MigrationStatus:
    outcome: Outcome
    exit_code: int
    sentence: str
    next_command: str | None           # the one command to run next, or None
    next_hint: str                     # what to tell the user on the Next: line
    reasons: list[str] = field(default_factory=list)


def migration_status(
    progress: Progress, export: ExportAccounting | None = None, checks: CheckFacts | None = None,
    *, prefix: str = "proton2m365", state_arg: str | None = None, extra: tuple[str, ...] = (),
) -> MigrationStatus:
    """Outcome by precedence FAILED > INCOMPLETE > SAMPLE OK > PASSED (the first that applies wins).

    `export` is None when the caller did not look at the export (status, upload, tag); then export
    accounting cannot make anything PASSED, but it does not block the next command either.
    """
    checks = checks or CheckFacts()

    def cmd(*words: str) -> str:
        """The command to run next, repeating the options this run actually used (state, sign-in)."""
        parts = list(words)
        if state_arg:
            parts += ["--state", state_arg]
        parts += list(extra) if words[0] != "plan" else []
        return " ".join([prefix, *(p if p.startswith("<") else shlex.quote(p) for p in parts)])

    work = []  # (condition, reason, command) in Next: precedence order
    if export is not None and not export.available:
        work.append(("the export folder is not readable; export accounting was not done",
                     "make the export folder available again (or run `plan` with its new location)", None))
    if export is not None and export.unresolved:
        work.append((f"{_count(len(export.unresolved), 'export file')} not planned: " + _names(export.unresolved),
                     "repair each file named above (or export it again from Proton) and run `plan` again, or "
                     "leave it out on purpose with " + cmd("plan", "<export>", "--exclude-file", "<name>")
                     + "; never delete export files", None))
    if export is not None and export.missing_from_export:
        work.append((f"{_count(len(export.missing_from_export), 'planned message')} no longer in the export: "
                     + _names(export.missing_from_export), "check the export folder, then run `plan` again", None))
    if progress.source_changed:
        work.append((f"{progress.source_changed:,} export files changed after they were planned (not uploaded)",
                     "check the export is the one you meant, then " + cmd("plan", "<export>"), None))
    if progress.planned - progress.source_changed > 0:
        work.append((f"{progress.planned:,} planned, not uploaded yet", None, cmd("upload")))
    if progress.upload_failed - progress.upload_rejected:
        work.append((f"{progress.upload_failed - progress.upload_rejected:,} failed to upload (worth retrying)",
                     None, cmd("upload", "--retry-failed")))
    if progress.upload_rejected:
        work.append((f"{progress.upload_rejected:,} were refused for good (rejected by the server or unreadable; "
                     "retrying will not help)",
                     "fix each message named in the receipt, or leave it out on purpose with "
                     + cmd("plan", "<export>", "--exclude-file", "<id>"), None))
    if progress.appended:
        work.append((f"{progress.appended:,} uploaded, not tagged yet", None, cmd("tag")))
    if progress.tag_failed:
        work.append((f"{progress.tag_failed:,} failed to tag", None, cmd("tag", "--retry-failed")))
    if progress.drift:
        work.append((f"{progress.drift:,} uploaded messages differ from the current plan (folder, date or "
                     "content); they were not changed in the mailbox",
                     "undo that plan change, or keep the mailbox as it is with "
                     + cmd("plan", "<export>", "--accept-drift"), None))
    if progress.waiting_prune:
        work.append((f"{progress.waiting_prune:,} uploaded messages the plan now skips are waiting for prune",
                     None, cmd("prune")))
    reasons = [w[0] for w in work]
    notes = []
    if progress.missing_source:
        notes.append(f"{progress.missing_source:,} messages missing from the newer export were never uploaded "
                     "(accepted with plan --accept-missing; listed in the receipt)")
    if progress.missing_in_mailbox:
        notes.append(f"{progress.missing_in_mailbox:,} messages in the mailbox are missing from the newer export "
                     "(checked by presence only; listed in the receipt)")

    if checks.mismatched or progress.failed_check or checks.folders_short or checks.duplicated:
        failed = max(checks.mismatched, progress.failed_check)
        why = []
        if checks.duplicated:
            why.append(f"{len(checks.duplicated):,} messages have more than one copy in the mailbox: "
                       + _names(checks.duplicated) + " (run `dedupe` to move the extra copies to Deleted Items)")
        if failed:
            why.append(f"{failed:,} checked messages do not match")
        if checks.folders_short:
            why.append(f"{len(checks.folders_short):,} folders have fewer messages than planned: "
                       + _names(checks.folders_short))
        return MigrationStatus(
            Outcome.FAILED, EXIT_FAILED, "; ".join(why), None,
            "look at the mismatches in the report, fix them in Outlook or re-run tag, then " + cmd("verify"),
            why + reasons)

    if work:
        _, hint, command = work[0]
        remaining = (progress.total - progress.verified - progress.skipped - progress.unchecked
                     - progress.missing_source)
        sentence = f"nothing is wrong yet; {remaining:,} messages still need work" if remaining else \
            "nothing is wrong yet; some export files still need attention"
        return MigrationStatus(Outcome.INCOMPLETE, EXIT_INCOMPLETE, sentence, command, hint or command or "",
                               reasons + notes)

    if progress.unchecked or export is None:
        if progress.unchecked:
            reasons.append(f"{progress.unchecked:,} messages in the mailbox have not been checked yet")
        if export is None:
            reasons.append("the export was not compared in this run")
        return MigrationStatus(
            Outcome.SAMPLE_OK, EXIT_SAMPLE_OK,
            f"checked messages are correct; {progress.unchecked:,} not yet checked", cmd("verify", "--all"),
            cmd("verify", "--all"), reasons + notes)

    if not progress.verified:
        sentence = "nothing to migrate" if not progress.skipped else \
            f"nothing to migrate; all {progress.skipped:,} messages are intentionally skipped"
    else:
        sentence = f"all {progress.verified:,} messages are in the mailbox and checked"
    if progress.missing_source or progress.missing_in_mailbox:
        sentence += "; " + "; ".join(notes)
    return MigrationStatus(Outcome.PASSED, EXIT_OK, sentence, None,
                           "nothing left to do; keep your Proton export until you are satisfied", reasons + notes)


def next_line(s: MigrationStatus) -> str:
    return f"Next: {s.next_hint}"


def _names(items: tuple[str, ...] | list[str], limit: int = 10) -> str:
    items = list(items)
    shown = ", ".join(items[:limit])
    return shown + (f" ...and {len(items) - limit:,} more" if len(items) > limit else "")


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun} is" if n == 1 else f"{n:,} {noun}s are"
