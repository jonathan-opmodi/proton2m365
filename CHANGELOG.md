# Changelog

All notable changes to proton2m365. Versions follow the git tags (`vX.Y.Z`).

## Unreleased (v0.1.0)

First public release.

- Plan, upload, tag, verify and prune a Proton Mail Export Tool backup into Microsoft 365 or Outlook.com
  over IMAP and Microsoft Graph, keeping received dates, read state, folders, and labels as categories.
- `verify` ends with one outcome (PASSED, SAMPLE OK, INCOMPLETE, FAILED) and writes a receipt that accounts
  for every export file.
- Sign-in for work and personal Microsoft accounts, device code over SSH, account binding per state file.
- Adaptive upload pacing, `selftest`, `doctor`, `sample-export`, stable error codes and exit codes.
