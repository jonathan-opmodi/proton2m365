# proton2m365

Move a **Proton Mail Export Tool** backup into **Microsoft 365** or **Outlook.com**, without Proton Mail
Bridge or a paid Proton plan, and get a receipt that accounts for every message.

- Keeps each message's original received date and read/unread state.
- Puts mail in the right folder (Inbox, Sent Items, Archive, your own folders) using Proton's metadata.
- Turns Proton labels into Outlook categories, and stars into a category or a flag.
- Can stop and resume at any time without uploading anything twice.
- Ends with `verify`: one outcome (PASSED, SAMPLE OK, INCOMPLETE or FAILED) and a receipt file listing every
  export file as migrated, intentionally skipped (and why), or unresolved.

Status: Microsoft 365 work and school accounts are tested live. **Outlook.com and Microsoft 365
Personal/Family are experimental** until the live checks in [docs/how-it-works.md](docs/how-it-works.md#outlookcom)
have passed. Support is best effort through GitHub issues. Want contacts and calendar, Gmail, or simpler
personal-account support? [Open a "Want" issue](https://github.com/jonathan-opmodi/proton2m365/issues/new/choose)
(or thumbs-up an existing one): those counts decide what comes next.

## Prerequisites

- Python 3.12 or newer, and either [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/).
  - macOS: `brew install pipx` (or `brew install uv`)
  - Windows: install Python from python.org, then `py -m pip install --user pipx` (or `winget install astral-sh.uv`)
  - Linux: your distribution's `pipx` package (or the uv installer)
- A Proton Mail export made with Proton's **Export Tool** (the folder that contains `labels.json` and pairs of
  `.eml` and `.metadata.json` files). Unzip it first.
- The mailbox you are moving into: a Microsoft 365 work or school account, or an Outlook.com account.
- **Your own sign-in app** for migrating (not needed to try it). proton2m365 does not ship one, so your mail is only
  ever reached through an app you control. Registering it is a one-time setup, see
  [docs/setup-own-app.md](docs/setup-own-app.md):
  - work or school account: if you are an administrator, `proton2m365 setup` registers it in one sign-in (about a
    minute); otherwise an administrator registers it by hand, about 10 minutes;
  - personal account (Outlook.com, Microsoft 365 Family): Microsoft lets only organizations register apps, so
    you first create a free Azure account (it asks for a card to verify you; the app itself costs nothing),
    about 20 minutes.

## Try it (no sign-in, nothing written to any mailbox)

```sh
pipx install proton2m365
proton2m365 sample-export ./sample
proton2m365 scan ./sample
proton2m365 plan --dry-run ./sample
```

With uv instead of pipx, run each command as `uvx proton2m365 ...` and skip the install line.

`plan --dry-run` shows what would be uploaded: 34 of the sample's 40 messages, which folders they would go
to, which labels become categories, and the 6 that are skipped and why. Try it on your own export next; it
still writes nothing anywhere.

## Migrate your export

First register your sign-in app: as a tenant administrator, run `proton2m365 setup`; otherwise follow
[docs/setup-own-app.md](docs/setup-own-app.md) and put its client ID and tenant in
`~/.config/proton2m365/config.toml`. Then:

```sh
proton2m365 selftest                       # live checks with synthetic messages; cleans up after itself
proton2m365 scan /path/to/export
proton2m365 plan /path/to/export           # writes ./state.sqlite; add --exclude-label "Name" to leave mail out
proton2m365 upload                         # hours for a large export; safe to stop and run again
proton2m365 tag                            # applies categories and flags
proton2m365 verify --all                   # the outcome, and the receipt
```

Or all of it in one command, which asks before uploading and can be run again to continue where it stopped:

```sh
proton2m365 migrate /path/to/export        # add --exclude-label "Name" to leave mail out
```

Long commands (`upload`, `tag`, `verify --all`, `prune`, `dedupe`, `migrate`) keep the computer from falling
asleep while they run (closing a laptop lid still sleeps it) and show a desktop notification when they end;
`--allow-sleep` and `--no-notify` turn these off.

Every command ends with a `Next:` line naming the command to run next. `verify` writes three receipt files next
to the state file: `state.receipt.html` (open it in a browser, or print it), `state.receipt.txt` and the full
per-message `state.receipt.json`. Keep your Proton export until you have read the receipt.

### Signing in

The first online command opens your browser. Choose the mailbox you are moving **into**, and approve the
permissions your app asks for. Over SSH or without a display, proton2m365 uses a device code instead (it may ask twice:
once for Microsoft Graph, once for IMAP). Each state file remembers its mailbox and refuses any other.

## What you will see in Outlook

| From Proton | In Outlook |
|---|---|
| Inbox, Sent, Archive | Inbox, Sent Items, Archive |
| your folders (`Projects/Alpha`) | the same folders, created for you |
| labels | categories (with the nearest Outlook color) |
| starred | a "Starred" category, or a flag with `plan --starred-as-flag` |
| received date, read/unread | kept |
| drafts, Trash, Spam | skipped (Trash and Spam can be included with `--include-trash`, `--include-spam`) |

Want it somewhere else? `plan --map-folder "Projects=Clients"` moves a folder (and its subfolders), and
`--map-label "Work=Office"` renames a category (`"Work="` drops it). The same mappings can live in
`~/.config/proton2m365/config.toml` under `[map.folders]` and `[map.labels]`.

Each uploaded message carries an added `X-Proton-Id` header, which is how proton2m365 finds it again. While
`upload`, `prune` or `dedupe` runs, a folder `_proton2m365_lease` holds one small message so that a second computer does
not upload the same mail; it is emptied when the run ends.

## What is not preserved

Proton's conversation grouping, snooze and scheduled sends, and labels you exclude. Messages larger than the
mailbox's size limit (35 MB by default) are skipped and listed. `verify` checks dates, read state, flags,
categories, drafts and folders; `verify --content` also downloads the checked messages and compares their text,
attachments, HTML text and links with the export (`verify --all --content` downloads the whole mailbox). The
receipt says which of these were done.

## A newer export later

Exported again from Proton after migrating (new mail arrived, or you exported before you were done)? Run
`migrate` with the new export and the same state file:

```sh
proton2m365 migrate /path/to/newer-export  # same --state as before
```

Only messages that are new since the first export are uploaded; labels you changed in Proton are applied by
`tag`. If some messages planned from the first export are missing from the newer one (deleted in Proton, or
an export that stopped early), `plan` stops and says how many: if that is expected, run it again with
`--accept-missing`. Those messages stay in the mailbox, are checked by presence only, and are listed in the
receipt. An export that shares no message with the state is a different mailbox and needs its own `--state`.

## Extra copies

If `verify --all` reports messages with more than one copy in the mailbox (for example after another tool
was used too), `proton2m365 dedupe --dry-run` lists them and `proton2m365 dedupe` moves the extra copies to
Deleted Items, keeping one of each. It asks first (`--yes` skips the question; without a terminal it is
required) and `--folder "Archive"` limits it to one folder. Then run `tag` and `verify` again.

## Calendar and contacts

They go through Outlook's own import. Calendars upload as they are (`.ics`); contacts need converting first,
which `proton2m365 contacts-csv contacts.vcf` does offline, listing anything Outlook's contact CSV has no place
for. [docs/calendar-and-contacts.md](docs/calendar-and-contacts.md) has the steps and what each import leaves out.

## If it stops

Run the same command again: nothing that already arrived is sent twice. `proton2m365 status` shows where
things stand without signing in. To start over, delete the state file (and its `.receipt.*` files) and run
`plan` again; mail already uploaded is detected by its `X-Proton-Id` header.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | done (verify: PASSED) |
| 1 | unexpected error: a crash log was written; attach `proton2m365 doctor --issue` output to an issue |
| 2 | usage, configuration, sign-in or connection error (the message starts with a `P2M-` code, see [docs/errors.md](docs/errors.md)) |
| 3 | mailbox full |
| 10 | verify: SAMPLE OK, some messages not checked yet (`verify --all`) |
| 11 | incomplete: messages still need work (the `Next:` line says which command) |
| 12 | failed: a check did not match, or a selftest check failed |
| 13 | interrupted: run the same command again to resume |
| 14 | verify would have PASSED, but its receipt could not be saved |

## Why not Thunderbird or a paid EML converter?

They can move `.eml` files, and that may be all you need. As far as we know none of them reads Proton's
metadata files, so Proton's folders, labels and read state are not carried over, and nothing tells you
afterwards whether every message arrived. proton2m365 exists for those things.
Tools built on Proton Mail Bridge (imapsync and others) need a paid Proton plan and a running Bridge.

## More

- [How it works](docs/how-it-works.md): export format, folder routing, why IMAP plus Graph
- [Calendar and contacts](docs/calendar-and-contacts.md): Outlook's import, and what it leaves out
- [Errors](docs/errors.md): every `P2M-` code and what to do
- [Upgrading](docs/upgrading.md): pinning a version, resuming across versions
- [Using your own Entra app](docs/setup-own-app.md)
- [Security](SECURITY.md): what stays on your computer
- [Contributing](CONTRIBUTING.md)

MIT licensed. Not affiliated with Proton AG or Microsoft.
