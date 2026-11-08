# Changelog

All notable changes to proton2m365. Versions follow the git tags (`vX.Y.Z`).

## Unreleased (v0.3.0)

- `proton2m365 setup` registers the sign-in app in a work or school tenant in one administrator sign-in (through
  Microsoft Graph Command Line Tools, kept in memory only), grants admin consent and writes config.toml. Running it
  again updates the same app; `--remove` deletes it; `--dry-run` shows what it would do. When the tenant blocks that
  sign-in, it writes an Azure CLI script instead, finished with `setup --use-app`.
- Sign-in no longer crashes when the macOS Keychain refuses writes (no keychain, or a locked one over SSH): it uses
  the owner-only cache file instead.

## v0.2.0 (2026-11-07)

- A newer export of the same mailbox can be planned into the same state: `migrate NEWER-EXPORT` uploads only
  new messages. Messages the newer export no longer has need `plan --accept-missing`; they stay in the mailbox,
  are checked by presence only and are listed in the receipt. An export sharing no message is refused.
- `dedupe` moves extra copies of uploaded messages to Deleted Items, keeping one (`--dry-run`, `--yes`,
  `--folder`); the kept copies are tagged and checked again.
- Long commands keep the computer awake and notify when they end (`--allow-sleep`, `--no-notify`,
  `PROTON2M365_ALLOW_SLEEP`, `PROTON2M365_NO_NOTIFY`).
- `upload` refuses an export file that changed after `plan` read it.
- Safer under failure: a Graph create is never repeated blindly after an uncertain error; a run that slept
  past its mailbox lease while another computer took over stops (`P2M-LEASE-LOST`); an interrupted state
  backup is made again.
- The state file moves to schema 3 (a backup is kept); v0.1 refuses it. See docs/upgrading.md.

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
