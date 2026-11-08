# Error codes

Every predictable error starts with a `P2M-` code and exits with code 2 (see the README's exit-code table).
If the fix below does not help, open an issue with `proton2m365 doctor --issue`.

## P2M-USAGE
With `--json`: the command line was not understood (an unknown option or a missing argument). The usage
message is on stderr. Run the command with `--help`.

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

## P2M-LEASE-LOST
This run was paused longer than its mailbox lease (for example the computer slept) and another computer took
over the mailbox meanwhile, so it stopped before writing anything else. Nothing is lost: when the other run has
finished, run the same command again to resume.

## P2M-MAILBOX-BUSY
Another proton2m365 run is uploading into the same mailbox, on this computer or another one (proton2m365 keeps
a small lease message in the folder `_proton2m365_lease` while it works). Wait for it to finish. If no other run
is active (one crashed), the lease expires within 15 minutes, or run again with `--break-lease`.

## P2M-SIZE
A personal mailbox would go over 14 GB (free Outlook.com mailboxes hold 15 GB). Exclude labels or folders and
`plan` again, add storage, or run again with `--yes` if the mailbox has room.

## P2M-SAMPLE
The state was planned from `sample-export`. Upload it only into a test mailbox, with `--sample-ok`.

## P2M-SETUP-BLOCKED
Your organization does not let Microsoft Graph Command Line Tools sign in, so `setup` could not register the app.
It wrote an Azure CLI script (`proton2m365-setup.sh`, or `.ps1` on Windows) in the current folder that creates the
same app: run it as an administrator, then run the `proton2m365 setup --use-app ...` command it prints. Or register
the app by hand: [setup-own-app.md](setup-own-app.md).

## P2M-SETUP-CONFIG
`setup` could not write `~/.config/proton2m365/config.toml` (the message names the path and the reason) and printed
the lines to put in it yourself; or `setup --use-app` was given something that is not a client ID, or no `--tenant`.

## P2M-SETUP-IMAP
The tenant has no Office 365 Exchange Online service (needed for IMAP) and `setup` could not add it, or the tenant's
Microsoft Graph or Exchange service does not offer a permission proton2m365 needs. Check the tenant has Exchange
Online mailboxes.

## P2M-SETUP-LOCKED
Another `proton2m365 setup` is running on this computer. Wait for it to finish, then run `setup` again.

## P2M-SETUP-PERSONAL
You signed in to `setup` with a personal Microsoft account (Outlook.com, Microsoft 365 Personal or Family). Only an
organization's administrator can register an app with `setup`. For a personal account, register the app by hand:
[setup-own-app.md](setup-own-app.md), route B.

## P2M-SETUP-PROPAGATION
The app was created, but Microsoft had not made it visible yet when `setup` stopped waiting. Run `setup` again in a
few minutes: it finds the same app and continues (it never creates a second one).

## P2M-SETUP-ROLE
Your account may not register apps or grant consent in this tenant. Run `setup` as a Global Administrator, Privileged
Role Administrator, Application Administrator or Cloud Application Administrator, or have one run the Azure CLI
script `setup` wrote.

## P2M-STATE-MISSING
No state file at that path. Check `--state` (the message lists state files nearby), or run `plan` first: only
`plan` creates a state file.

## P2M-STATE-LOCKED
Another proton2m365 run is using this state file. Wait for that process to finish; if none is running, run the
command again.

## P2M-STATE-NEWER
The state file was written by a newer proton2m365. Upgrade (`pipx upgrade proton2m365`).

## P2M-CONTACTS-EXISTS
`contacts-csv` would replace a CSV or loss report from an earlier run. If you have not imported that CSV into
Outlook yet, run again with `--force`. If you have, keep it: importing contacts twice creates duplicates.

## P2M-CONTACTS-WRITE
`contacts-csv` could not write the CSV or the loss report (the folder is read-only, or the disk is full). Choose
another place with `-o`.

## P2M-CONFIRM-NEEDED
The command would change something you have to agree to first, and could not ask (no terminal, or `--json`).
The message says what and how many. For `plan`: messages planned from an earlier export are missing from this
one; if they were deleted in Proton or left out on purpose, run `plan` again with `--accept-missing` (they stay
in the mailbox and are listed in the receipt); if the export is incomplete, export again from Proton. For
`dedupe`: run it with `--dry-run` to see the copies, then with `--yes`.

## P2M-EXPORT-CHANGED
The export this state was planned from is missing or different. Put it back where it was, or `plan` again with
its new location. A newer export of the same mailbox is accepted by `plan` (and `migrate <new export>`): it
must share messages with the state; an export sharing none is a different mailbox and needs its own `--state`.

## P2M-VCARD
`contacts-csv` could not read the file as contacts: it cannot be opened, has no `BEGIN:VCARD`, ends in the
middle of a contact, or has a line that is not a vCard property. Export the contacts from Proton again and
pass the `.vcf` file.

## P2M-EXPORT-FORMAT
The export does not look like the Proton Export Tool format proton2m365 knows. Please open an issue with
`doctor --issue`; `--allow-unknown-format` tries anyway.

## P2M-IMAP-UIDVALIDITY
The server renumbered a folder (its UIDVALIDITY changed) between reading it and moving messages, so `dedupe`
moved nothing. Run it again; it reads the folder afresh.

## P2M-IMAP-NO-MOVE
The mail server does not support IMAP MOVE, which `prune` needs. Exchange Online and Outlook.com support it.

## P2M-ERROR
An error without a more specific code (for example an unexpected reply from Microsoft Graph or IMAP). The
message says what failed. Run the command again; if it keeps failing, open an issue with `doctor --issue`.

## P2M-MAP-NO-MATCH
A `--map-folder` or `--map-label` (or a `[map]` entry in config.toml) matches no folder or category in this
export. The message lists the folders and categories `plan` found; use one of those names as written there.
