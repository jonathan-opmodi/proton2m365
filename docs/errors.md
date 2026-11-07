# Error codes

Every predictable error starts with a `P2M-` code and exits with code 2 (see the README's exit-code table).
If the fix below does not help, open an issue with `proton2m365 doctor --issue`.

## P2M-AUTH-NO-APP
proton2m365 has no sign-in app of its own: register one (a one-time setup, about 10 minutes, see
[setup-own-app.md](setup-own-app.md)) and give its client ID and tenant with `--client-id` and `--tenant`,
`PROTON2M365_CLIENT_ID` and `PROTON2M365_TENANT`, or `client_id` and `tenant` in
`~/.config/proton2m365/config.toml`.

## P2M-AUTH-TENANT
The app is registered for one organization (or for personal accounts only), so sign-in must say which: pass
`--tenant` with your tenant ID or domain, or `--tenant consumers` for a personal-accounts app (or set `tenant` in
config.toml).

## P2M-AUTH-CONSENT
The app's permissions have not been granted yet. In the Entra admin center open the app, **API permissions**, and
select **Grant admin consent** ([setup-own-app.md](setup-own-app.md), step 3), or have your admin open the
admin-consent link in the message.

## P2M-AUTH-TIMEOUT
No browser finished signing in within 5 minutes. Run the command again with `--device-code`.

## P2M-AUTH-TWO-ACCOUNTS
Device-code sign-in asked twice (Graph, then IMAP) and two different accounts answered. Run the command again
and sign in to the same mailbox both times.

## P2M-AUTH-WHICH-ACCOUNT
Several accounts are signed in. Add `--account <address>`.

## P2M-AUTH-FAILED
Microsoft refused the sign-in. The message includes Microsoft's `AADSTS` code, where the client ID came from and
which authority was used. Conditional Access and disabled accounts are common causes; ask your admin.

## P2M-AUTH-TOKEN
A token could not be refreshed. Run the command again to sign in. If IMAP keeps failing for a work account,
IMAP may be disabled for the mailbox (`Set-CASMailbox <address> -ImapEnabled $true`, an admin task) or the IMAP
permission was not granted to the app.

## P2M-ACCOUNT-MISMATCH
This state file belongs to another mailbox. Use `--account <that address>`, or a different `--state` for this
mailbox.

## P2M-ACCOUNT-UNBOUND
The state file was made before proton2m365 recorded mailboxes and already has mail in one. If the signed-in
mailbox is the right one, run the command again with `--bind-account`.

## P2M-MAILBOX-BUSY
Another proton2m365 run is uploading into the same mailbox, on this computer or another one (proton2m365 keeps
a small lease message in the folder `_proton2m365_lease` while it works). Wait for it to finish. If no other run
is active (one crashed), the lease expires within 15 minutes, or run again with `--break-lease`.

## P2M-SIZE
A personal mailbox would go over 14 GB (free Outlook.com mailboxes hold 15 GB). Exclude labels or folders and
`plan` again, add storage, or run again with `--yes` if the mailbox has room.

## P2M-SAMPLE
The state was planned from `sample-export`. Upload it only into a test mailbox, with `--sample-ok`.

## P2M-STATE-MISSING
No state file at that path. Check `--state` (the message lists state files nearby), or run `plan` first: only
`plan` creates a state file.

## P2M-STATE-LOCKED
Another proton2m365 run is using this state file. Wait for that process to finish; if none is running, run the
command again.

## P2M-STATE-NEWER
The state file was written by a newer proton2m365. Upgrade (`pipx upgrade proton2m365`).

## P2M-EXPORT-CHANGED
The export this state was planned from is missing or different. Put it back where it was, or `plan` again with
its new location.

## P2M-EXPORT-FORMAT
The export does not look like the Proton Export Tool format proton2m365 knows. Please open an issue with
`doctor --issue`; `--allow-unknown-format` tries anyway.

## P2M-IMAP-NO-MOVE
The mail server does not support IMAP MOVE, which `prune` needs. Exchange Online and Outlook.com support it.

## P2M-ERROR
An error without a more specific code (for example an unexpected reply from Microsoft Graph or IMAP). The
message says what failed. Run the command again; if it keeps failing, open an issue with `doctor --issue`.

## P2M-MAP-NO-MATCH
A `--map-folder` or `--map-label` (or a `[map]` entry in config.toml) matches no folder or category in this
export. The message lists the folders and categories `plan` found; use one of those names as written there.
