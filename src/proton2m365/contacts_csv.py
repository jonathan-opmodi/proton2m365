"""`proton2m365 contacts-csv`: Proton Contacts' vCard export -> the CSV Outlook imports, plus what did not fit.

Outlook on the web imports contacts only from CSV, with fixed columns: 3 email addresses, one mobile number, two
home and two business numbers, one "other" number, three faxes, a pager, home/business/other addresses and one web
page. This reads Proton's .vcf (vCard 2.1, 3.0 or 4.0), fills those columns as fully as they allow (a second
mobile goes to "Other Phone", a second home address to "Other"), and lists every value that has no column, so
nothing is dropped without the user knowing:

    contacts.vcf --parse (unfold, groups, params, escapes, quoted-printable)--> cards
                 --map (columns, overflow to spare columns)--> CSV rows + losses per contact
                 --> contacts.csv (UTF-8 with BOM, as Outlook writes it) + contacts.losses.txt (owner-only)

Nothing leaves the computer. Importing the same CSV twice into Outlook creates duplicates (Outlook does not
match contacts), so the command says so.
"""

from __future__ import annotations

import csv
import io
import os
import quopri
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = [
    "Title", "First Name", "Middle Name", "Last Name", "Suffix", "Nickname",
    "E-mail Address", "E-mail 2 Address", "E-mail 3 Address",
    "Mobile Phone", "Home Phone", "Home Phone 2", "Business Phone", "Business Phone 2", "Other Phone",
    "Pager", "Business Fax", "Home Fax", "Other Fax",
    "Company", "Department", "Job Title",
    "Home Street", "Home City", "Home State", "Home Postal Code", "Home Country/Region",
    "Business Street", "Business City", "Business State", "Business Postal Code", "Business Country/Region",
    "Other Street", "Other City", "Other State", "Other Postal Code", "Other Country/Region",
    "Birthday", "Anniversary", "Web Page", "Notes", "Categories",
]

# Properties that describe the card itself, not the person: never reported as lost.
_STRUCTURAL = {"BEGIN", "END", "VERSION", "PRODID", "UID", "REV", "FN", "N", "KIND", "SOURCE", "CLIENTPIDMAP",
               "X-ABLABEL", "X-ABUID", "X-PM-LABEL"}


class VCardError(Exception):
    pass


@dataclass
class Prop:
    name: str
    params: dict[str, list[str]]
    value: str
    group: str | None = None

    def types(self) -> set[str]:
        out = set()
        for key in ("TYPE", ""):  # vCard 2.1 writes bare types: TEL;CELL;VOICE:...
            for v in self.params.get(key, []):
                out |= {t.strip().lower() for t in v.split(",") if t.strip()}
        return out

    def preferred(self) -> bool:
        return "pref" in self.types() or "PREF" in self.params


@dataclass
class Contact:
    name: str
    row: dict[str, str] = field(default_factory=dict)
    losses: list[tuple[str, str]] = field(default_factory=list)  # (what, value)


@dataclass
class Conversion:
    contacts: list[Contact] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, why): contact groups, empty cards

    @property
    def with_losses(self) -> list[Contact]:
        return [c for c in self.contacts if c.losses]


# --- reading ----------------------------------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper().split(":", 1)[0]:
            lines[-1] = lines[-1][:-1] + raw  # quoted-printable soft line break (vCard 2.1)
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_line(line: str) -> Prop:
    head, sep, value = line.partition(":")
    if not sep:
        raise VCardError("P2M-VCARD: a line is not a vCard property (no ':'); is this a .vcf file?")
    # a ':' inside a quoted parameter value belongs to the head
    while head.count('"') % 2:
        more, sep, value = value.partition(":")
        head += ":" + more
    pieces = _split_unquoted(head, ";")
    name = pieces[0]
    group = None
    if "." in name:
        group, name = name.rsplit(".", 1)
    params: dict[str, list[str]] = {}
    for p in pieces[1:]:
        key, eq, val = p.partition("=")
        if not eq:
            params.setdefault("", []).append(key)
        else:
            params.setdefault(key.upper(), []).append(val.strip('"'))
    encoding = ",".join(params.get("ENCODING", [])).upper()
    if "QUOTED-PRINTABLE" in encoding:
        charset = (params.get("CHARSET") or ["utf-8"])[0]
        try:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except LookupError:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
    return Prop(name.upper(), params, value, group)


def read_cards(text: str) -> list[list[Prop]]:
    cards, current = [], None
    for line in _unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VCARD"):
            current = []
        elif upper.startswith("END:VCARD"):
            if current is not None:
                cards.append(current)
            current = None
        elif current is not None:
            current.append(_parse_line(line))
    if current is not None:
        raise VCardError("P2M-VCARD: the file ends inside a contact (no END:VCARD); export the contacts again")
    return cards


def _text(value: str) -> str:
    """Unescape a vCard text value."""
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _components(value: str, sep: str = ";") -> list[str]:
    parts, buf, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf.append(value[i:i + 2])
            i += 2
            continue
        if ch == sep:
            parts.append(_text("".join(buf)))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append(_text("".join(buf)))
    return parts


# --- mapping --------------------------------------------------------------------------------------

def _date(value: str) -> str | None:
    """vCard date -> M/D/YYYY, or None when there is no year (Outlook needs one)."""
    v = value.strip()
    if v.startswith("--"):
        return None
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", v)
    if not m:
        return None
    year, month, day = (int(x) for x in m.groups())
    return f"{month}/{day}/{year}"


def _tel_value(prop: Prop) -> str:
    value = _text(prop.value).strip()
    return value[4:] if value.lower().startswith("tel:") else value


def _put(row: dict[str, str], columns: list[str], value: str) -> bool:
    for col in columns:
        if not row.get(col):
            row[col] = value
            return True
    return False


def convert_card(props: list[Prop]) -> Contact | None:
    by_name: dict[str, list[Prop]] = {}
    for p in props:
        by_name.setdefault(p.name, []).append(p)
    kind = _text(by_name.get("KIND", [Prop("KIND", {}, "")])[0].value).lower()
    fn = _text(by_name["FN"][0].value).strip() if "FN" in by_name else ""
    contact = Contact(name=fn)
    row = contact.row
    if kind == "group":
        return None
    if "N" in by_name:
        last, first, middle, prefix, suffix = (_components(by_name["N"][0].value) + [""] * 5)[:5]
        row.update({"Last Name": last, "First Name": first, "Middle Name": middle, "Title": prefix, "Suffix": suffix})
    if not any(row.get(k) for k in ("First Name", "Last Name")) and fn and "@" not in fn:
        row["First Name"] = fn  # a name with no structured parts
    if not contact.name:
        contact.name = " ".join(x for x in (row.get("First Name"), row.get("Last Name")) if x) or "(no name)"
    lost = contact.losses

    if "NICKNAME" in by_name:
        nicknames = [n for p in by_name["NICKNAME"] for n in _components(p.value, ",") if n]
        if nicknames:
            row["Nickname"] = nicknames[0]
            lost += [("nickname", n) for n in nicknames[1:]]

    emails = sorted(by_name.get("EMAIL", []), key=lambda p: not p.preferred())
    for i, p in enumerate(emails):
        value = _text(p.value).strip()
        if i < 3:
            row[["E-mail Address", "E-mail 2 Address", "E-mail 3 Address"][i]] = value
        else:
            lost.append(("email address beyond the 3 Outlook keeps", value))

    for p in sorted(by_name.get("TEL", []), key=lambda p: not p.preferred()):
        types, value = p.types(), _tel_value(p)
        if "fax" in types:
            order = (["Home Fax"] if "home" in types else ["Business Fax"] if "work" in types else []) + \
                    ["Business Fax", "Home Fax", "Other Fax"]
        elif "pager" in types:
            order = ["Pager", "Other Phone"]
        elif "cell" in types or "mobile" in types:
            order = ["Mobile Phone", "Other Phone"]
        elif "home" in types:
            order = ["Home Phone", "Home Phone 2", "Other Phone"]
        elif "work" in types:
            order = ["Business Phone", "Business Phone 2", "Other Phone"]
        else:
            order = ["Other Phone", "Home Phone", "Business Phone", "Mobile Phone"]
        if not _put(row, order, value):
            lost.append(("phone number with no free Outlook column", value))

    for p in by_name.get("ADR", []):
        types = p.types()
        parts = (_components(p.value) + [""] * 7)[:7]
        po_box, extended, street, city, region, postal, country = parts
        # one line: Outlook's contact edit form drops line breaks in a street ("Apt 4" + "1 Main St" -> "Apt 41 Main St")
        street_full = ", ".join(x.replace("\n", ", ") for x in (po_box, extended, street) if x)
        prefixes = (["Home"] if "home" in types else ["Business"] if "work" in types else []) + ["Other", "Home", "Business"]
        target = next((pre for pre in prefixes if not any(row.get(f"{pre} {f}") for f in
                                                            ("Street", "City", "Postal Code"))), None)
        if target is None:
            lost.append(("postal address with no free Outlook column",
                         ", ".join(x for x in (street_full, city, region, postal, country) if x)))
            continue
        row.update({f"{target} Street": street_full, f"{target} City": city, f"{target} State": region,
                    f"{target} Postal Code": postal, f"{target} Country/Region": country})

    if "ORG" in by_name:
        org = _components(by_name["ORG"][0].value)
        row["Company"] = org[0]
        row["Department"] = "; ".join(x for x in org[1:] if x)
        lost += [("organization", _text(p.value)) for p in by_name["ORG"][1:]]
    if "TITLE" in by_name:
        row["Job Title"] = _text(by_name["TITLE"][0].value)
        lost += [("job title", _text(p.value)) for p in by_name["TITLE"][1:]]
    for key, column in (("BDAY", "Birthday"), ("ANNIVERSARY", "Anniversary")):
        for i, p in enumerate(by_name.get(key, [])):
            value = _date(p.value)
            if value and i == 0:
                row[column] = value
            else:
                lost.append((f"{column.lower()} (Outlook needs a full date)" if not value else column.lower(),
                             p.value))
    urls = [_text(p.value).strip() for p in by_name.get("URL", [])]
    if urls:
        row["Web Page"] = urls[0]
        lost += [("web page beyond the one Outlook keeps", u) for u in urls[1:]]
    notes = [_text(p.value) for p in by_name.get("NOTE", [])]
    if notes:
        row["Notes"] = "\n\n".join(notes)
    categories = [c.strip() for p in by_name.get("CATEGORIES", []) for c in _components(p.value, ",") if c.strip()]
    if categories:
        row["Categories"] = ";".join(dict.fromkeys(categories))

    handled = {"NICKNAME", "EMAIL", "TEL", "ADR", "ORG", "TITLE", "BDAY", "ANNIVERSARY", "URL", "NOTE",
               "CATEGORIES"} | _STRUCTURAL
    for p in props:
        if p.name in handled:
            continue
        if p.name == "PHOTO":
            lost.append(("photo (a CSV cannot hold one)", ""))
        elif p.name == "LOGO":
            lost.append(("logo (a CSV cannot hold one)", ""))
        else:
            label = p.name.lower().removeprefix("x-").replace("-", " ")
            lost.append((f"{label} (no Outlook column)", _text(p.value)[:200]))
    if not any(row.values()):
        return None
    return contact


def load(path: Path) -> str:
    """The file's text: UTF-8 (with or without a BOM), else Windows-1252, which old vCard 2.1 files use."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VCardError(f"P2M-VCARD: cannot read {path}: {exc.strerror or exc}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def convert(text: str) -> Conversion:
    result = Conversion()
    cards = read_cards(text)
    if not cards:
        raise VCardError("P2M-VCARD: no contacts found (no BEGIN:VCARD); is this Proton's contacts export (.vcf)?")
    for props in cards:
        contact = convert_card(props)
        if contact is None:
            names = [p for p in props if p.name == "FN"]
            name = _text(names[0].value) if names else "(no name)"
            is_group = any(p.name == "KIND" and _text(p.value).lower() == "group" for p in props)
            result.skipped.append((name, "contact group (Proton exports its members' groups as categories)"
                                   if is_group else "no name, email, phone or address"))
            continue
        result.contacts.append(contact)
    return result


def csv_text(conversion: Conversion) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for c in conversion.contacts:
        writer.writerow({col: c.row.get(col, "") for col in COLUMNS})
    return out.getvalue()


def losses_text(conversion: Conversion, source: str) -> str:
    lines = [f"Values from {source} that Outlook's contact CSV has no place for.",
             "Add them by hand in Outlook if you need them (open the contact, Edit).", ""]
    for c in conversion.with_losses:
        lines.append(c.name)
        lines += [f"  - {what}: {value}" if value else f"  - {what}" for what, value in c.losses]
        lines.append("")
    if conversion.skipped:
        lines.append("Not converted:")
        lines += [f"  - {name}: {why}" for name, why in conversion.skipped]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str, *, bom: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8-sig" if bom else "utf-8", newline="")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def write(conversion: Conversion, csv_path: Path, losses_path: Path | None, source: str) -> None:
    _write_private(csv_path, csv_text(conversion), bom=True)
    if losses_path is not None:
        _write_private(losses_path, losses_text(conversion, source))
