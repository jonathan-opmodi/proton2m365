"""contacts-csv: vCard 2.1/3.0/4.0 -> Outlook's contact CSV, with every value that has no column reported."""

from __future__ import annotations

import csv
import io
import json
import os
import stat
import sys

import pytest

from proton2m365 import cli
from proton2m365 import contacts_csv as cc


def vcf(*cards: str) -> str:
    return "\r\n".join(cards) + "\r\n"


ALICE_30 = "\r\n".join([
    "BEGIN:VCARD",
    "VERSION:3.0",
    "FN:Dr. Alice B. Example Jr.",
    "N:Example;Alice;B.;Dr.;Jr.",
    "NICKNAME:Al,Ally",
    "EMAIL;TYPE=INTERNET:alice.other@example.com",
    "EMAIL;TYPE=INTERNET,PREF:alice@example.com",
    "TEL;TYPE=CELL:+1 555 0100",
    "TEL;TYPE=HOME,VOICE:+1 555 0101",
    "TEL;TYPE=WORK:+1 555 0102",
    "TEL;TYPE=WORK,FAX:+1 555 0103",
    "TEL;TYPE=PAGER:+1 555 0104",
    "ADR;TYPE=HOME:;Apt 4;1 Main St;Springfield;IL;62701;USA",
    "ADR;TYPE=WORK:;;2 Office Rd;Shelbyville;IL;62565;USA",
    "ORG:Example Corp;Research;Lab 3",
    "TITLE:Chief Tester",
    "BDAY:1980-02-29",
    "URL:https://example.com/alice",
    "URL:https://example.org/alice",
    "NOTE:Line one\\nLine two\\, with comma",
    "CATEGORIES:Friends,Work",
    "PHOTO;ENCODING=b;TYPE=JPEG:AAAA",
    "X-SPOUSE:Bob",
    "END:VCARD",
])


def one(text: str) -> cc.Contact:
    result = cc.convert(text)
    assert len(result.contacts) == 1, result
    return result.contacts[0]


def test_vcard_30_fills_every_outlook_column():
    c = one(vcf(ALICE_30))
    r = c.row
    assert (r["Title"], r["First Name"], r["Middle Name"], r["Last Name"], r["Suffix"]) == \
        ("Dr.", "Alice", "B.", "Example", "Jr.")
    assert r["Nickname"] == "Al"
    assert r["E-mail Address"] == "alice@example.com"  # PREF first
    assert r["E-mail 2 Address"] == "alice.other@example.com"
    assert r["Mobile Phone"] == "+1 555 0100"
    assert r["Home Phone"] == "+1 555 0101"
    assert r["Business Phone"] == "+1 555 0102"
    assert r["Business Fax"] == "+1 555 0103"
    assert r["Pager"] == "+1 555 0104"
    assert r["Home Street"] == "Apt 4\n1 Main St"
    assert (r["Home City"], r["Home State"], r["Home Postal Code"], r["Home Country/Region"]) == \
        ("Springfield", "IL", "62701", "USA")
    assert r["Business City"] == "Shelbyville"
    assert (r["Company"], r["Department"], r["Job Title"]) == ("Example Corp", "Research; Lab 3", "Chief Tester")
    assert r["Birthday"] == "2/29/1980"
    assert r["Web Page"] == "https://example.com/alice"
    assert r["Notes"] == "Line one\nLine two, with comma"
    assert r["Categories"] == "Friends;Work"
    lost = dict(c.losses)
    assert lost["nickname"] == "Ally"
    assert lost["web page beyond the one Outlook keeps"] == "https://example.org/alice"
    assert "photo (a CSV cannot hold one)" in lost
    assert lost["spouse (no Outlook column)"] == "Bob"


def test_vcard_21_bare_types_quoted_printable_and_folding():
    card = "\r\n".join([
        "BEGIN:VCARD",
        "VERSION:2.1",
        "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:M=C3=BCller;J=C3=BCrgen",
        "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:J=C3=BCrgen M=C3=BC=",
        "ller",
        "TEL;CELL;VOICE:0170 1234567",
        "TEL;HOME:030 1234",
        "NOTE:a long note that was",
        " folded onto two lines",
        "END:VCARD",
    ])
    c = one(vcf(card))
    assert c.name == "Jürgen Müller"
    assert (c.row["First Name"], c.row["Last Name"]) == ("Jürgen", "Müller")
    assert c.row["Mobile Phone"] == "0170 1234567"
    assert c.row["Home Phone"] == "030 1234"
    assert c.row["Notes"] == "a long note that wasfolded onto two lines"
    assert c.losses == []


def test_vcard_40_uris_pref_param_groups_and_quoted_params():
    card = "\r\n".join([
        "BEGIN:VCARD",
        "VERSION:4.0",
        "FN:Carol Example",
        "N:Example;Carol;;;",
        "item1.EMAIL;PREF=1:carol@example.com",
        "item1.X-ABLABEL:main",
        "EMAIL:carol2@example.com",
        'TEL;VALUE=uri;TYPE="voice,cell":tel:+1-555-0110',
        'TEL;VALUE=uri;TYPE="work,voice";LABEL="a:b":tel:+1-555-0111',
        "ANNIVERSARY:20050611",
        "BDAY:--0412",
        "X-PM-LABEL:ignored",
        "END:VCARD",
    ])
    c = one(vcf(card))
    assert c.row["E-mail Address"] == "carol@example.com"
    assert c.row["Mobile Phone"] == "+1-555-0110"
    assert c.row["Business Phone"] == "+1-555-0111"
    assert c.row["Anniversary"] == "6/11/2005"
    assert "Birthday" not in c.row
    assert c.losses == [("birthday (Outlook needs a full date)", "--0412")]


def test_overflow_goes_to_spare_columns_then_to_the_loss_list():
    lines = ["BEGIN:VCARD", "VERSION:3.0", "FN:Dan Example", "N:Example;Dan;;;"]
    lines += [f"EMAIL:dan{i}@example.com" for i in range(5)]
    lines += [f"TEL;TYPE=CELL:+1 555 020{i}" for i in range(3)]
    lines += ["ADR;TYPE=HOME:;;1 A St;X;;;", "ADR;TYPE=HOME:;;2 B St;Y;;;", "ADR;TYPE=HOME:;;3 C St;Z;;;",
              "ADR;TYPE=HOME:;;4 D St;W;;;", "END:VCARD"]
    c = one(vcf("\r\n".join(lines)))
    assert [c.row[k] for k in ("E-mail Address", "E-mail 2 Address", "E-mail 3 Address")] == \
        ["dan0@example.com", "dan1@example.com", "dan2@example.com"]
    assert c.row["Mobile Phone"] == "+1 555 0200"
    assert c.row["Other Phone"] == "+1 555 0201"  # the second mobile
    assert c.row["Home Street"] == "1 A St"
    assert c.row["Other Street"] == "2 B St"
    assert c.row["Business Street"] == "3 C St"
    lost = [what for what, _ in c.losses]
    assert lost.count("email address beyond the 3 Outlook keeps") == 2
    assert lost.count("phone number with no free Outlook column") == 1
    assert ("postal address with no free Outlook column", "4 D St, W") in c.losses


def test_groups_and_empty_cards_are_skipped_with_a_reason():
    group = "BEGIN:VCARD\r\nVERSION:4.0\r\nKIND:group\r\nFN:Team\r\nMEMBER:urn:uuid:1\r\nEND:VCARD"
    empty = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:x\r\nEND:VCARD"
    email_only = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:eve@example.com\r\nEMAIL:eve@example.com\r\nEND:VCARD"
    result = cc.convert(vcf(group, empty, email_only))
    assert [c.row.get("E-mail Address") for c in result.contacts] == ["eve@example.com"]
    assert result.contacts[0].row.get("First Name") is None  # an address is not a first name
    assert [name for name, _ in result.skipped] == ["Team", "(no name)"]
    assert "contact group" in result.skipped[0][1]


@pytest.mark.parametrize("text, words", [
    ("hello", "no contacts found"),
    ("BEGIN:VCARD\r\nFN:x\r\n", "ends inside a contact"),
    ("BEGIN:VCARD\r\nnot a property\r\nEND:VCARD\r\n", "not a vCard property"),
])
def test_bad_files_are_named_errors(text, words):
    with pytest.raises(cc.VCardError, match=words) as info:
        cc.convert(text)
    assert str(info.value).startswith("P2M-VCARD:")


def test_load_falls_back_to_windows_1252(tmp_path):
    p = tmp_path / "old.vcf"
    p.write_bytes(b"BEGIN:VCARD\r\nVERSION:2.1\r\nN:Ren\xe9e;Z\r\nEND:VCARD\r\n")
    assert one(cc.load(p)).row["Last Name"] == "Renée"


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_writes_bom_csv_and_owner_only_loss_report(tmp_path, capsys):
    src = tmp_path / "contacts.vcf"
    src.write_text(vcf(ALICE_30), encoding="utf-8", newline="")
    code, out, err = run(capsys, "contacts-csv", str(src))
    assert code == 0
    csv_path, report = tmp_path / "contacts.csv", tmp_path / "contacts.losses.txt"
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert list(rows[0].keys()) == cc.COLUMNS
    assert rows[0]["E-mail Address"] == "alice@example.com"
    text = report.read_text(encoding="utf-8")
    assert "Bob" in text and "Dr. Alice B. Example Jr." in text
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(report).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(csv_path).st_mode) == 0o600
    assert "Bob" not in out and "Bob" not in err  # values stay in the owner-only file
    assert "duplicates" in out


def test_cli_refuses_to_overwrite_without_force_and_drops_a_stale_report(tmp_path, capsys):
    src = tmp_path / "c.vcf"
    src.write_text(vcf(ALICE_30), encoding="utf-8")
    assert run(capsys, "contacts-csv", str(src))[0] == 0
    code, _, err = run(capsys, "contacts-csv", str(src))
    assert code == 2 and "P2M-CONTACTS-EXISTS" in err
    src.write_text(vcf("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Z\r\nN:Z;;;;\r\nEND:VCARD"), encoding="utf-8")
    code, out, _ = run(capsys, "contacts-csv", str(src), "--force")
    assert code == 0 and "nothing was left out" in out
    assert not (tmp_path / "c.losses.txt").exists()


def test_cli_json_and_output_name(tmp_path, capsys):
    src = tmp_path / "c.vcf"
    src.write_text(vcf(ALICE_30), encoding="utf-8")
    code, out, _ = run(capsys, "contacts-csv", str(src), "-o", str(tmp_path / "out.csv"), "--json")
    doc = json.loads(out)
    assert code == 0 and doc["command"] == "contacts-csv" and doc["json_version"] == 1
    assert doc["contacts"] == 1 and doc["contacts_with_losses"] == 1
    assert doc["losses_report"].endswith("out.losses.txt")

    code, out, _ = run(capsys, "contacts-csv", str(tmp_path / "missing.vcf"), "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "P2M-VCARD"


def test_cli_will_not_write_over_the_vcf(tmp_path, capsys):
    src = tmp_path / "c.vcf"
    src.write_text(vcf(ALICE_30), encoding="utf-8")
    code, _, err = run(capsys, "contacts-csv", str(src), "-o", str(src))
    assert code == 2 and "replace the .vcf" in err
