# Calendar and contacts

proton2m365 moves mail. Calendars and contacts go through Outlook's own import, which handles most of it; this
page says how, and what that import leaves out. Every gap below was checked by importing synthetic test data
into Outlook on the web (November 2026).

## Calendar

Outlook imports Proton's calendar file (`.ics`) directly. Nothing needs converting.

1. In Proton Calendar, open the calendar's settings and export it. You get one `.ics` file per calendar.
2. In Outlook on the web, open Calendar, choose **Add calendar**, then **Upload from file**.
3. Pick the `.ics` file and choose the calendar to import into. A new, empty calendar makes the result easy to
   check, and easy to delete and redo.

Uploading the same file again is safe: Outlook recognises events it already has and adds no copies.

### What comes through

- One-off, all-day and multi-day events.
- Repeating events, including ones that cross a daylight-saving change, skipped occurrences and occurrences
  moved to another time (they stay one series).
- Events in other time zones, and descriptions with accents or emoji.

### What does not

Outlook does not warn about any of these.

| In Proton | In Outlook |
|---|---|
| An event that repeats on a list of specific dates (`RDATE`) | Not imported at all |
| An event that repeats every hour or every few hours (`FREQ=HOURLY`) | Not imported at all |
| More than one reminder | Only the first reminder |
| A meeting you were invited to (it has an organizer and attendees) | Arrives as an invitation from the organizer: you cannot edit it, and Accept or Decline sends the organizer an email |
| A link attached to an event | A blocked `.url` file instead of the link |

To find the events that will be dropped before you upload, search the `.ics` for the two markers. On macOS or
Linux:

```sh
grep -n -e '^RDATE' -e 'FREQ=HOURLY' calendar.ics
```

On Windows (PowerShell):

```powershell
Select-String -Path calendar.ics -Pattern '^RDATE', 'FREQ=HOURLY'
```

Each line it prints belongs to an event you need to recreate by hand in Outlook. Open the file in a text editor
and look upward from that line for `SUMMARY:` to see the event's name.

## Contacts

Outlook on the web imports contacts only from a CSV file, with fixed columns, and Proton exports a vCard
(`.vcf`). `proton2m365 contacts-csv` converts one into the other on your computer (no sign-in, nothing sent
anywhere) and lists every value that does not fit.

1. In Proton Contacts, export all contacts. You get one `.vcf` file.
2. Convert it:

   ```sh
   proton2m365 contacts-csv contacts.vcf
   ```

   This writes `contacts.csv` next to it (`-o` chooses another name). If anything did not fit, it also writes
   `contacts.losses.txt`, which only you can read: each contact with the values Outlook has no place for.
3. In Outlook on the web, open People, choose **Manage contacts**, then **Import contacts**, and pick
   `contacts.csv`.

**Import it once.** Outlook does not match contacts it already has: importing the same file again creates a
second copy of every contact. To redo an import, delete the imported contacts first.

### What fits

Outlook's CSV has room for: name (with prefix, middle name, suffix), one nickname, 3 email addresses, a mobile
number, 2 home and 2 work numbers, one other number, home, work and other fax, a pager, home, work and other
addresses, company, department, job title, birthday, anniversary, one web page, notes and categories (Proton's
contact groups).

When a contact has more of something than Outlook has columns for, `contacts-csv` uses a spare column before
giving up: a second mobile number goes to "Other Phone", a second home address to "Other".

### What does not fit

These go to `contacts.losses.txt` so you can add them by hand:

- a 4th and later email address; phone numbers once every phone column is used; a 4th address;
- a second web page, second nickname, second organization;
- a birthday or anniversary without a year (Outlook needs one);
- photos;
- custom fields and anything else Outlook has no column for (gender, language, related people, and so on).

Contact groups themselves are not contacts and are not converted; their names come through as categories on
each member.

Outlook also shows every imported email address as "Other email", whatever type it had in Proton.

### Why not import contacts and calendars directly?

It would need extra permissions on your mailbox, and Outlook's own import already handles calendars well. If
you need a direct import, say so in a ["Want" issue](https://github.com/jonathan-opmodi/proton2m365/issues/new/choose):
those counts decide what gets built.
