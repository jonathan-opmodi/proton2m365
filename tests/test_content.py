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


# --- the four differences a real Exchange mailbox showed (none of them lost content) ----------------

def test_folded_or_encoded_attachment_names_match_their_unfolded_form():
    data = b"%PDF-1.4 same bytes"
    folded = mime(attachment=("x", data)).replace(b"filename=x", b'filename="Board Minutes\r\n August 2020.pdf"')
    plain = mime(attachment=("x", data)).replace(b"filename=x", b'filename="Board Minutes August 2020.pdf"')
    encoded = mime(attachment=("x", data)).replace(
        b"filename=x", b'filename="=?utf-8?Q?Board_Minutes_August_2020.pdf?="')
    assert content.compare(folded, plain) == []
    assert content.compare(encoded, plain) == []


def _tnef(files):
    """A minimal TNEF stream: signature, key, then per file a rendering, a title and the data."""
    import struct

    def attr(level, attr_id, value):
        return bytes([level]) + struct.pack("<II", attr_id, len(value)) + value + struct.pack("<H", sum(value) & 0xFFFF)

    out = struct.pack("<IH", 0x223E9F78, 1) + attr(1, 0x00089006, b"\x01\x00\x00\x00")
    for name, data in files:
        out += attr(2, 0x00069002, b"\x00" * 14) + attr(2, 0x00018010, name.encode() + b"\0") + attr(2, 0x0006800F, data)
    return out


def test_winmail_dat_counts_as_the_files_inside_it():
    rich_only = mime(attachment=("winmail.dat", _tnef([])))
    assert content.compare(rich_only, mime()) == []                     # only formatting inside: nothing lost
    with_file = mime(attachment=("winmail.dat", _tnef([("budget.xlsx", b"PK cells")])))
    assert content.compare(with_file, mime(attachment=("budget.xlsx", b"PK cells"))) == []
    lost = content.compare(with_file, mime())                          # Exchange dropped the file: reported
    assert lost and lost[0][0] == "content: attachments" and "budget.xlsx" in lost[0][1]


def test_unreadable_winmail_dat_is_compared_as_it_is():
    assert content.tnef_attachments(b"not tnef") is None
    bad = mime(attachment=("winmail.dat", b"not tnef"))
    assert content.compare(bad, mime())[0][0] == "content: attachments"


def test_links_compare_after_decoding():
    a = mime(html='<p><a href="https://example.com/r?e=%7B%22a%22:1%7D&amp;x=1">here</a></p>')
    b = mime(html="<p><a href='https://example.com/r?e={\"a\":1}&x=1'>here</a></p>")
    assert content.compare(a, b) == []


def test_latin1_label_with_windows_curly_quotes_reads_like_exchange_does():
    source = mime(html="<p>If you don\x92t</p>").replace(b"text/html; charset=utf-8", b"text/html; charset=iso-8859-1")
    source = source.replace("\x92".encode(), b"\x92")
    exchange = mime(html="<p>If you don’t</p>")
    assert content.compare(source, exchange) == []


def test_c1_controls_from_a_latin1_step_read_as_windows_1252():
    # Declared UTF-8, but the sender stored U+0092 (a Windows-1252 apostrophe that went through Latin-1).
    source = mime(html="<p>If you don\u0092t</p>")
    exchange = mime(html="<p>If you don\u2019t</p>")
    assert content.compare(source, exchange) == []
