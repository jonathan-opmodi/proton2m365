"""verify --content: compare what is in the mailbox with the export file, after decoding.

Upload changes a message on purpose (an added X-Proton-Id header, renamed read-receipt and follow-up
headers, long lines re-encoded) and Exchange rewrites headers and transfer encodings, so the bytes never
match. What must match is what a person reads and keeps:

    attachments   the decoded bytes of every attachment (SHA-256), as a multiset with their file names
                  (decoded and unfolded); a winmail.dat (Outlook's TNEF) counts as the files inside it,
                  because Exchange unpacks it on arrival
    text          every text/plain part, decoded, NFC, LF line endings, trailing whitespace removed
    html-text     the visible text of every text/html part, plus every link target (entity- and
                  percent-decoded); reported as its own check and never presumed harmless

Text declared as ISO-8859-1 or ASCII is read as Windows-1252, as browsers and Exchange do (mail that says
latin-1 but contains curly quotes is common).
"""

from __future__ import annotations

import hashlib
import html
import re
import struct
import unicodedata
from email.header import decode_header, make_header
from urllib.parse import unquote
from collections import Counter
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from html.parser import HTMLParser


@dataclass
class Content:
    text: str = ""
    html_text: str = ""
    links: list[str] = field(default_factory=list)
    attachments: Counter = field(default_factory=Counter)  # (file name, sha256) -> count


class _HtmlText(HTMLParser):
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(unquote(html.unescape(href.strip())))

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


# C1 control characters (U+0080-U+009F) in decoded text are almost always Windows-1252 punctuation that went
# through a Latin-1 step (U+0092 for a curly apostrophe). Browsers and Exchange show them as the Windows-1252
# character, so the comparison does too.
def _c1_table() -> dict[int, str]:
    table = {}
    for code in range(0x80, 0xA0):
        try:
            table[code] = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            pass
    return table


C1_AS_CP1252 = _c1_table()
LATIN1_ALIASES = {"iso-8859-1", "iso8859-1", "latin-1", "latin1", "l1", "us-ascii", "ascii", "iso_8859-1"}
TNEF_SIGNATURE = 0x223E9F78


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = (part.get_content_charset() or "utf-8").lower()
    if charset in LATIN1_ALIASES:
        charset = "cp1252"  # what browsers and Exchange actually use for these labels
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    return text.translate(C1_AS_CP1252)


def _filename(raw_name: str | None) -> str:
    """A file name as a person sees it: encoded words decoded, folded lines joined, spaces collapsed."""
    if not raw_name:
        return ""
    try:
        name = str(make_header(decode_header(raw_name)))
    except Exception:  # noqa: BLE001 - a malformed encoded word: compare it as written
        name = raw_name
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip()


def tnef_attachments(data: bytes) -> list[tuple[str, bytes]] | None:
    """The files inside a winmail.dat (TNEF), or None when it is not readable TNEF."""
    if len(data) < 6 or struct.unpack_from("<I", data, 0)[0] != TNEF_SIGNATURE:
        return None
    pos, files, current = 6, [], None
    try:
        while pos + 9 <= len(data):
            level = data[pos]
            attr, length = struct.unpack_from("<II", data, pos + 1)
            value = data[pos + 9:pos + 9 + length]
            pos += 9 + length + 2  # value plus its 2-byte checksum
            if level != 2:
                continue
            kind = attr & 0xFFFF
            if kind == 0x9002:            # attAttachRendData: a new attachment starts
                current = {"name": "", "data": None}
                files.append(current)
            elif current is not None and kind == 0x8010:   # attAttachTitle
                current["name"] = value.rstrip(b"\0").decode("cp1252", errors="replace")
            elif current is not None and kind == 0x800F:   # attAttachData
                current["data"] = value
    except struct.error:
        return None
    return [(f["name"], f["data"]) for f in files if f["data"] is not None]


def read(raw: bytes) -> Content:
    msg = message_from_bytes(raw, policy=policy.compat32)
    c = Content()
    texts, htmls = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        filename = part.get_filename()
        ctype = part.get_content_type()
        if disposition == "attachment" or (filename and ctype not in ("text/plain", "text/html")):
            data = part.get_payload(decode=True) or b""
            inner = tnef_attachments(data) if _filename(filename).lower() == "winmail.dat" or \
                ctype == "application/ms-tnef" else None
            if inner is not None:
                for name, blob in inner:
                    c.attachments[(_filename(name), hashlib.sha256(blob).hexdigest())] += 1
                continue
            c.attachments[(_filename(filename), hashlib.sha256(data).hexdigest())] += 1
        elif ctype == "text/plain":
            texts.append(_norm_text(_part_text(part)))
        elif ctype == "text/html":
            parser = _HtmlText()
            parser.feed(_part_text(part))
            htmls.append(re.sub(r"\s+", " ", unicodedata.normalize("NFC", "".join(parser.parts))).strip())
            c.links += parser.links
    c.text = "\n\n".join(t for t in texts if t)
    c.html_text = " ".join(h for h in htmls if h)
    return c


def compare(source: bytes, uploaded: bytes) -> list[tuple[str, str, str]]:
    """(check, expected, actual) for each difference; empty when the content matches."""
    a, b = read(source), read(uploaded)
    problems = []
    if a.attachments != b.attachments:
        problems.append(("content: attachments", _attachments(a), _attachments(b)))
    if a.text != b.text:
        problems.append(("content: text", _excerpt(a.text, b.text), _excerpt(b.text, a.text)))
    if a.html_text != b.html_text or sorted(a.links) != sorted(b.links):
        what = "links differ" if a.html_text == b.html_text else _excerpt(a.html_text, b.html_text)
        got = ", ".join(sorted(set(b.links) - set(a.links))[:3]) if a.html_text == b.html_text else \
            _excerpt(b.html_text, a.html_text)
        problems.append(("html-text-differs", what, got))
    return problems


def _attachments(c: Content) -> str:
    return ", ".join(f"{name or '(unnamed)'} {sha[:8]}" for (name, sha), n in sorted(c.attachments.items())
                     for _ in range(n)) or "(none)"


def _excerpt(text: str, other: str, width: int = 40) -> str:
    """Around the first difference, so a report shows where the two part ways."""
    i = next((k for k, (x, y) in enumerate(zip(text, other)) if x != y), min(len(text), len(other)))
    start = max(0, i - width // 2)
    return ("..." if start else "") + text[start:start + width].replace("\n", "\\n") + ("..." if start + width < len(text) else "")
