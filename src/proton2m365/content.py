"""verify --content: compare what is in the mailbox with the export file, after decoding.

Upload changes a message on purpose (an added X-Proton-Id header, renamed read-receipt and follow-up
headers, long lines re-encoded) and Exchange rewrites headers and transfer encodings, so the bytes never
match. What must match is what a person reads and keeps:

    attachments   the decoded bytes of every attachment (SHA-256), as a multiset with their file names
    text          every text/plain part, decoded, NFC, LF line endings, trailing whitespace removed
    html-text     the visible text of every text/html part, plus every link target; reported as its own
                  check and never presumed harmless
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
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
                self.links.append(href.strip())

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


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
            c.attachments[(filename or "", hashlib.sha256(data).hexdigest())] += 1
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
