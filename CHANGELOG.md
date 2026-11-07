# Changelog

All notable changes to proton2m365. Versions follow the git tags (`vX.Y.Z`).

## v0.1.0 (2026-11-07)

First public release. Microsoft 365 work and school accounts are supported; Outlook.com and Microsoft 365
Personal/Family are experimental.

- Move a Proton Mail Export Tool backup into Microsoft 365 over IMAP and Microsoft Graph: received dates, read
  state, folders (from Proton's metadata) and labels as categories are kept; drafts, Trash and Spam are left out
  unless included; stopping and resuming never uploads a message twice.
- `migrate EXPORT` runs the whole thing in one resumable command; `scan`, `plan`, `upload`, `tag`, `verify` and
  `prune` remain available one at a time, each ending with a `Next:` line.
- `verify` ends with one outcome (PASSED, SAMPLE OK, INCOMPLETE, FAILED) and writes a receipt
  (`.receipt.html`, `.txt`, `.json`) that accounts for every export file; `--all` also finds duplicate copies,
  and `--content` compares text, attachments, HTML text and links with the export.
- Bring your own sign-in app (docs/setup-own-app.md); device-code sign-in over SSH; each state file is bound to
  one mailbox; a lease in the mailbox keeps two computers from uploading into it at once.
- `plan --exclude-label`, `--exclude-file`, `--map-folder` and `--map-label`, saved and reused by a re-plan.
- Adaptive upload pacing, `selftest`, `doctor --issue`, `sample-export`, `--json` on every command, stable
  `P2M-` error codes and exit codes, and tested on Linux, macOS and Windows.
