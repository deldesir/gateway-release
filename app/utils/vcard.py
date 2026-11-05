"""vCard 3.0 generation — pure, deterministic, no I/O.

vCard 3.0 (RFC 2426) rather than 4.0: broad phone compatibility matters more
here than modern syntax, since the files are imported by whatever Contacts app
each team member happens to run.

Two rules make this module testable and safe to re-import on a phone:

  - **Deterministic.** Same contacts in, byte-identical file out. `rev` is a
    caller-supplied timestamp, never `now()`, and contacts are sorted before
    rendering. Callers hash the output to decide whether anything changed.
  - **No UID.** `Contact.uid` is the ledger's key (RapidPro's contact uuid) and
    stays out of the card: phones show a `UID` as a stray field and do not use
    it to recognise a re-import, so it bought nothing and looked like noise.

No `PHOTO` — these files ride WhatsApp and email, so they stay small.
"""

from __future__ import annotations

from dataclasses import dataclass

VCARD_VERSION = "3.0"

_CRLF = "\r\n"
# RFC 2426 §2.6: lines SHOULD be folded to <=75 octets, continuations begin
# with a single space. Folding is by octet, not character — see _fold().
_FOLD_LIMIT = 75


@dataclass(frozen=True)
class Contact:
    """One directory entry. Immutable and hashable so tests can compare sets."""

    uid: str
    name: str
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    org: str = ""
    title: str = ""
    # Sub-units under `org`, outermost first — ORG is a structured value, so a
    # directory tree ("Org/Region/Site") lands as "Org;Region;Site".
    org_units: tuple[str, ...] = ()
    note: str = ""
    # Catalog code behind `title`. Never rendered — it lets a caller re-title a
    # card in another language without re-reading the source record.
    function: str = ""
    # Never rendered: they let a caller pick cards (by kind, by an older label)
    # without re-reading the source record.
    kind: str = ""
    alias: str = ""
    # True when `name` is not a person's name — a post, a vehicle, a switchboard.
    # Guessing a family name out of "Head Of Mission" files it under "Mission".
    single_name: bool = False

    @property
    def display_name(self) -> str:
        """FN is mandatory and must be non-empty — fall back to any identifier."""
        for candidate in (self.name.strip(), *self.phones, *self.emails, self.uid):
            if candidate:
                return candidate
        return "Unknown"

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.display_name.casefold(), self.uid)


def _escape(value: str) -> str:
    """Escape a TEXT value per RFC 2426 §5. Backslash first, or we double-escape."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold one content line to <=75 octets per line.

    Folds on octet boundaries without splitting a multi-byte character, which a
    naive character-count fold would do to accented names.
    """
    if len(line.encode("utf-8")) <= _FOLD_LIMIT:
        return line

    pieces: list[str] = []
    buf = bytearray()
    limit = _FOLD_LIMIT
    for char in line:
        encoded = char.encode("utf-8")
        if len(buf) + len(encoded) > limit:
            pieces.append(buf.decode("utf-8"))
            buf = bytearray()
            # continuation lines spend one octet on the leading space
            limit = _FOLD_LIMIT - 1
        buf += encoded
    pieces.append(buf.decode("utf-8"))
    return (_CRLF + " ").join(pieces)


def _structured_name(name: str, single: bool = False) -> str:
    """Derive N (Family;Given;Additional;Prefix;Suffix) from a display name.

    RapidPro stores one display string, so this is a best guess; FN stays
    authoritative. "Sánchez, Efraín" (comma form) is read as Family, Given;
    otherwise the last token is taken as the family name. With `single`, the
    whole string stays in one slot, so phones sort and show it as written.
    """
    cleaned = name.strip()
    if not cleaned:
        return ";;;;"
    if single:
        return f"{_escape(cleaned)};;;;"

    if "," in cleaned:
        family, _, given = cleaned.partition(",")
        return f"{_escape(family.strip())};{_escape(given.strip())};;;"

    parts = cleaned.split()
    if len(parts) == 1:
        return f"{_escape(parts[0])};;;;"
    family = parts[-1]
    given = " ".join(parts[:-1])
    return f"{_escape(family)};{_escape(given)};;;"


def build_vcard(contact: Contact, rev: str = "") -> str:
    """Render a single VCARD block. `rev` is an ISO basic UTC stamp (20260811T143000Z)."""
    lines = [
        "BEGIN:VCARD",
        f"VERSION:{VCARD_VERSION}",
        f"FN:{_escape(contact.display_name)}",
        f"N:{_structured_name(contact.name, contact.single_name)}",
    ]

    if contact.org:
        units = ";".join(_escape(unit) for unit in (contact.org, *contact.org_units) if unit)
        lines.append(f"ORG:{units}")
    if contact.title:
        lines.append(f"TITLE:{_escape(contact.title)}")

    lines.extend(f"TEL;TYPE=CELL:{_escape(phone)}" for phone in contact.phones if phone)
    lines.extend(
        f"EMAIL;TYPE=INTERNET:{_escape(email)}" for email in contact.emails if email
    )

    if contact.note:
        lines.append(f"NOTE:{_escape(contact.note)}")
    if rev:
        lines.append(f"REV:{rev}")

    lines.append("END:VCARD")
    return _CRLF.join(_fold(line) for line in lines)


def build_vcf(contacts: list[Contact], rev: str = "") -> str:
    """Render a multi-entry .vcf file. Sorted, so the output is hash-stable.

    A single file with many VCARD blocks is the portable way to hand over a
    directory; every mainstream Contacts app imports all blocks in one pass.
    """
    ordered = sorted(contacts, key=lambda c: c.sort_key)
    blocks = [build_vcard(contact, rev=rev) for contact in ordered]
    if not blocks:
        return ""
    return _CRLF.join(blocks) + _CRLF
