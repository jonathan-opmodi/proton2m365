# Security

## What leaves your computer

Only calls to Microsoft: sign-in (login.microsoftonline.com), Microsoft Graph and Outlook IMAP. proton2m365
sends no telemetry and talks to no other service.

## What stays on your computer, and holds mailbox data

| File | Holds | Protection |
|---|---|---|
| token cache (`~/.config/proton2m365/token_cache.bin`, or the OS secret store) | sign-in tokens | macOS Keychain, Windows DPAPI or Linux libsecret; otherwise an owner-only file (0600 on macOS and Linux) |
| state file (`state.sqlite` by default) and its `.bak-v*` upgrade backups | message IDs, folder and category names, the mailbox address | your file permissions |
| `<state>.receipt.json`, `.receipt.txt`, `.receipt.html`, `verify-report.csv` | the same, per message | owner-only (0600) where the OS supports it; on Windows they inherit your profile's permissions |
| `crash-*.log` | a traceback, which can include addresses | owner-only (0600) |

`doctor --issue` prints a report with names and addresses replaced by hashes; attach that to issues, never the
files above.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository rather than a public issue. Support is
best effort; expect a reply within a couple of weeks.
