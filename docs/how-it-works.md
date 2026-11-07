# How it works

## The export

Proton's Export Tool writes one folder per mailbox: `labels.json` (folders and labels) and, for each message,
`<id>.eml` (the message) and `<id>.metadata.json` (its labels, flags, time and read state). `scan` reads it
without changing anything; `plan` decides, for each message, its folder, categories, flag and whether to skip it,
and writes that to the state file.

## Routing

The first matching rule wins: Trash and Spam when you include them (`Proton Trash`, `Proton Spam`); your own
folders (the first path alphabetically when a message sits in several, which `plan` reports); Inbox (a message
sent to yourself carries both Inbox and Sent, and the received copy belongs in Inbox); Sent Items; Archive;
otherwise `Proton Unfiled`. Drafts are skipped only when both Proton's Drafts label and its flags agree; Trash
and Spam are skipped unless included; messages over `--max-size` are skipped and listed.

## Why IMAP plus Microsoft Graph

IMAP APPEND is the only way into Exchange Online that keeps a message's original received date (Graph's
import stamps the import time, and EWS is switched off). Graph then sets categories and flags, which IMAP
cannot. Each message gets an `X-Proton-Id` header; that is how upload knows what already arrived and how tag
and verify find each message again.

## Pacing

Uploads start at 2 messages a second, speed up while Exchange accepts them, and halve when Exchange pushes
back (a throttle reply, a dropped connection, or an upload far slower than usual). `--max-msgs-per-sec` and
`--max-bytes-per-sec` set the ceiling.

## Outlook.com

Personal accounts sign in through the same app. Until `selftest` has passed on a fresh Outlook.com account
(browser and device code), Outlook.com support is experimental: please report what `selftest` says.

## Adding a destination

Everything Microsoft-specific sits behind two interfaces in `src/proton2m365/targets.py`: a `MailStore` (put
messages into folders and find them again by their `X-Proton-Id` header, over IMAP) and an `Organizer` (apply
categories and flags, and read messages back for `verify`). Export reading, planning, state, pacing, verify's
outcome and the receipt do not change. A new destination (Fastmail, iCloud, Gmail) is one module that provides
both, plus an entry in `TARGETS`; it then appears under `--target`. Only `microsoft365` exists today.
