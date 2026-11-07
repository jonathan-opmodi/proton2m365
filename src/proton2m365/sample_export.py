"""A synthetic Proton export, so anyone can try proton2m365 without touching real mail.

`proton2m365 sample-export <dir>` writes 40 messages: 34 to upload (Inbox 10, Sent Items 6, Archive 10,
Projects/Alpha 8; 8 unread; categories Work on 6 and Starred on 2; one long line, one without a Message-ID,
one with an attachment) and 6 that plan skips (2 drafts, 2 in Trash, 2 in Spam). With --with-orphan it adds
one .eml that has no metadata. The tests build their exports with the same ExportBuilder.
"""

from __future__ import annotations

import json
from pathlib import Path

SAMPLE_MARKER = "SAMPLE-EXPORT.txt"  # plan sees it and marks the state as synthetic
SAMPLE_ROOT = "user@example.com"

SYSTEM_LABELS = [
    {"ID": "0", "Name": "Inbox", "Path": "Inbox", "Type": 3},
    {"ID": "3", "Name": "Trash", "Path": "Trash", "Type": 3},
    {"ID": "4", "Name": "Spam", "Path": "Spam", "Type": 3},
    {"ID": "6", "Name": "Archive", "Path": "Archive", "Type": 3},
    {"ID": "7", "Name": "Sent", "Path": "Sent", "Type": 3},
    {"ID": "8", "Name": "Drafts", "Path": "Drafts", "Type": 3},
    {"ID": "10", "Name": "Starred", "Path": "Starred", "Type": 3},
]
SAMPLE_LABELS = [
    {"ID": "proj=", "Name": "Projects", "Path": "Projects", "ParentID": "", "Type": 3},
    {"ID": "alpha=", "Name": "Alpha", "Path": "Projects/Alpha", "ParentID": "proj=", "Type": 3},
    {"ID": "work=", "Name": "Work", "Path": "Work", "ParentID": "", "Type": 1, "Color": "#E74856"},
]
RECEIVED, SENT = 1, 2


def eml_bytes(*, message_id: str | None = "<m@example.com>", content_type: str = "text/plain",
              body: bytes = b"hello\r\n", extra_headers: str = "", subject: str = "hi",
              date: str | None = None) -> bytes:
    headers = f"From: a@example.com\r\nTo: b@example.com\r\nSubject: {subject}\r\n"
    if date:
        headers += f"Date: {date}\r\n"
    if message_id is not None:
        headers += f"Message-ID: {message_id}\r\n"
    headers += f"Content-Type: {content_type}\r\n{extra_headers}"
    return headers.encode() + b"\r\n" + body


class ExportBuilder:
    """Writes a fake Proton export folder: labels.json plus <id>.eml / <id>.metadata.json pairs."""

    def __init__(self, root: Path, labels: list[dict] | None = None, wrapped: bool = True,
                 system_labels: list[dict] | None = None):
        self.dir = Path(root) / SAMPLE_ROOT / "mail_20261101_120000"
        self.dir.mkdir(parents=True)
        items = (SYSTEM_LABELS if system_labels is None else system_labels) + (labels or [])
        doc = {"Version": 1, "Payload": items} if wrapped else items
        (self.dir / "labels.json").write_text(json.dumps(doc), newline="\n")

    def add(self, pid: str, *, labels=("0",), flags: int = 1, time: int = 1_560_000_000, unread: bool = False,
            eml: bytes | None = None, metadata: bool = True, write_eml: bool = True) -> None:
        if write_eml:
            (self.dir / f"{pid}.eml").write_bytes(eml if eml is not None else eml_bytes(message_id=f"<{pid}@x>"))
        if metadata:
            payload = {"ID": pid, "LabelIDs": list(labels), "Flags": flags, "Time": time, "Unread": int(unread)}
            (self.dir / f"{pid}.metadata.json").write_text(json.dumps({"Version": 1, "Payload": payload}), newline="\n")


def _attachment_eml(pid: str) -> bytes:
    boundary = "sample-boundary"
    body = (f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nSee the attached file.\r\n"
            f"--{boundary}\r\nContent-Type: text/plain; name=notes.txt\r\n"
            "Content-Disposition: attachment; filename=notes.txt\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            "U2FtcGxlIGF0dGFjaG1lbnQK\r\n"
            f"--{boundary}--\r\n").encode()
    return eml_bytes(message_id=f"<{pid}@sample.invalid>", content_type=f'multipart/mixed; boundary="{boundary}"',
                     body=body, subject="Sample with an attachment")


def write_sample_export(root: Path, *, with_orphan: bool = False) -> Path:
    """Write the sample under root (which must be empty or missing). Returns the export folder."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty; choose an empty or new folder")
    b = ExportBuilder(root, labels=SAMPLE_LABELS)
    (b.dir / SAMPLE_MARKER).write_text("Synthetic sample export written by proton2m365 sample-export. "
                                       "Not real mail: upload it only into a test mailbox.\n", newline="\n")
    day, n = 86_400, 0

    def add(kind: str, labels, flags, *, unread=False, eml=None):
        nonlocal n
        n += 1
        pid = f"sample-{n:02d}-{kind}"
        b.add(pid, labels=labels, flags=flags, unread=unread, time=1_577_836_800 + n * day,
              eml=eml or eml_bytes(message_id=f"<{pid}@sample.invalid>", subject=f"Sample {kind} {n}"))
        return pid

    unread_left = 8
    for i in range(10):  # Inbox: Work on 3, Starred on 1, 4 unread, the long line and the missing Message-ID
        labels = ["0"] + (["work="] if i < 3 else []) + (["10"] if i == 3 else [])
        eml = None
        if i == 4:
            eml = eml_bytes(message_id=None, subject="Sample without a Message-ID")
        if i == 5:
            eml = eml_bytes(message_id="<sample-long@sample.invalid>", subject="Sample with a long line",
                            body=b"<p>" + b"a very long line " * 700 + b"</p>\r\n")
        add("inbox", labels, RECEIVED, unread=i >= 6, eml=eml)
    unread_left -= 4
    for i in range(6):   # Sent Items
        add("sent", ["7"] + (["work="] if i < 2 else []), SENT)
    for i in range(10):  # Archive: 2 unread, Starred on 1, the attachment
        add("archive", ["6"] + (["10"] if i == 0 else []), RECEIVED, unread=i in (1, 2),
            eml=_attachment_eml("sample-attach") if i == 9 else None)
    unread_left -= 2
    for i in range(8):   # Projects/Alpha: Work on 1, 2 unread
        add("alpha", ["alpha="] + (["work="] if i == 0 else []), RECEIVED, unread=i < unread_left)
    for _ in range(2):
        add("draft", ["8"], 0)
    for _ in range(2):
        add("trash", ["3"], RECEIVED)
    for _ in range(2):
        add("spam", ["4"], RECEIVED)
    if with_orphan:
        (b.dir / "sample-orphan.eml").write_bytes(eml_bytes(message_id="<orphan@sample.invalid>"))
    return b.dir
