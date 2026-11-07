"""verify --content: what a person reads and keeps must match after decoding."""

from __future__ import annotations

import base64
import quopri

from proton2m365 import content
from proton2m365.imap_store import prepare_message


def mime(text="Hello Ana,\nsee you soon.\n", html=None, attachment=None, *, cte="8bit", extra=""):
    boundary = "b1"
    parts = [f"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: {cte}\r\n\r\n"
             + (quopri.encodestring(text.encode()).decode() if cte == "quoted-printable" else text)]
    if html is not None:
        parts.append(f"Content-Type: text/html; charset=utf-8\r\n\r\n{html}")
    if attachment is not None:
        name, data = attachment
        parts.append(f"Content-Type: application/octet-stream\r\nContent-Disposition: attachment; filename={name}\r\n"
                     f"Content-Transfer-Encoding: base64\r\n\r\n{base64.b64encode(data).decode()}")
    body = "".join(f"--{boundary}\r\n{p}\r\n" for p in parts) + f"--{boundary}--\r\n"
    return (f"From: a@example.com\r\nSubject: hi\r\n{extra}MIME-Version: 1.0\r\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n{body}').encode()


def test_same_content_through_different_encodings_and_headers_matches():
    src = mime(html="<p>Hello <b>Ana</b> <a href='https://example.com/x'>link</a></p>", attachment=("a.pdf", b"%PDF1"))
    up = mime(text="Hello Ana,\r\nsee you soon.   \r\n", cte="quoted-printable",
              html="<html><head><style>p{}</style></head><body><p>Hello  <b>Ana</b>\n<a href=\"https://example.com/x\">link</a></p></body></html>",
              attachment=("a.pdf", b"%PDF1"), extra="X-Proton-Id: p1\r\nX-MS-Exchange: yes\r\n")
    assert content.compare(src, up) == []


def test_upload_preparation_itself_never_counts_as_a_difference():
    src = mime(text="café " + "word " * 3000 + "\n", html="<p>x</p>")      # long line: re-encoded on upload
    up = prepare_message(src, proton_id="p1", needs_reencode=True, synthesized_message_id=None)
    assert content.compare(src, up) == []


def test_a_changed_attachment_is_reported():
    problems = content.compare(mime(attachment=("a.pdf", b"one")), mime(attachment=("a.pdf", b"two")))
    assert [p[0] for p in problems] == ["content: attachments"]


def test_changed_text_is_reported_with_where_it_differs():
    problems = content.compare(mime(text="pay 100 euros"), mime(text="pay 900 euros"))
    assert problems[0][0] == "content: text" and "100" in problems[0][1] and "900" in problems[0][2]


def test_html_text_and_link_changes_are_their_own_check():
    a = mime(html="<p>Click <a href='https://example.com/real'>here</a></p>")
    b = mime(html="<p>Click <a href='https://example.org/other'>here</a></p>")
    assert [p[0] for p in content.compare(a, b)] == ["html-text-differs"]
    c = mime(html="<p>Click here now</p>")
    assert content.compare(mime(html="<p>Click here</p>"), c)[0][0] == "html-text-differs"


def test_unicode_forms_are_equal():
    assert content.compare(mime(text="José\n"), mime(text="José\n")) == []
