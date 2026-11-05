"""
Person hub: mirror Organized's congregation roster into RapidPro contacts
and SiYuan person pages — one identity across all three systems.

Sources, first non-empty wins:
  1. Organized backend `get_persons` (available once Data sharing is on)
  2. newest publishers JSON in /library/hourglass (fsr unified export or a
     raw Hourglass export) — the interim path while server sync is off

What it does per person WITH a phone number:
  - RapidPro: get-or-create the contact by URN — whatsapp: scheme checked
    FIRST (inbound WhatsApp creates contacts under it, so matching it is
    what prevents roster/chat duplicates), then tel:. Both schemes end up
    on the contact. Keeps the name current, tags with the `organized_uid`
    field and the "Congregation" group.
    It NEVER sends a message — outbound consent is enforced elsewhere
    (organized-backend's opt-in gate); this only makes identities known.
  - SiYuan: upsert a /people/<Name> page in the Wiki notebook (skip-if-
    unchanged via a custom-hash attr, same pattern as the KG renderer).

Graceful when no source exists yet: logs how to feed it and exits 0 (this
is an expected state, not a failure worth an OnFailure email).

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.person_sync
"""

import glob
import hashlib
import json
import os
import re
import sys

from loguru import logger

WIKI_NOTEBOOK = os.getenv("PERSON_SYNC_NOTEBOOK", "Wiki")
GROUP_NAME = "Congregation"
FIELD_KEY = "organized_uid"
HOURGLASS_DIR = "/library/hourglass"
DEFAULT_COUNTRY = os.getenv("PERSON_SYNC_COUNTRY_PREFIX", "509")  # Haiti


def _die(msg: str) -> None:
    logger.error(f"Person sync ABORTED: {msg}")
    sys.exit(1)


def normalize_phone(raw: str) -> str | None:
    """First phone in the field → E.164-ish, or None if unusable.

    Organized/Hourglass store '3712-3456' or '15550103456' or several
    numbers separated by ';'. Local 8-digit numbers get the country prefix.
    """
    if not raw:
        return None
    first = str(raw).split(";")[0]
    digits = re.sub(r"\D", "", first)
    # Numbers stored as dialed ("001 849 …") carry the 00 international
    # prefix; RapidPro strips it when normalizing tel: URNs, so keeping it
    # here makes every run see the URN as missing and re-update forever.
    if digits.startswith("00") and len(digits) > 10:
        digits = digits[2:]
    if len(digits) == 8:
        digits = DEFAULT_COUNTRY + digits
    if len(digits) < 10 or len(digits) > 15:
        return None
    return "+" + digits


def _persons_from_backend() -> list[dict]:
    from app.graph.tools.organized import query_organized_api

    try:
        raw = query_organized_api({"action": "get_persons"})
        data = json.loads(raw)
    except Exception as e:
        logger.warning(f"Person sync: backend query failed: {e}")
        return []
    persons = data.get("persons", [])
    return [p for p in persons if not p.get("archived")]


def _persons_from_hourglass() -> list[dict]:
    """Newest publishers-bearing JSON in /library/hourglass, mapped to the
    same shape get_persons returns."""
    candidates = sorted(
        glob.glob(f"{HOURGLASS_DIR}/*.json"),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pubs = data.get("publishers")
        if not pubs:
            continue
        logger.info(f"Person sync: using {path} ({len(pubs)} publisher(s))")
        return [
            {
                "person_uid": p.get("uuid") or str(p.get("id", "")),
                "display_name": f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
                "firstname": p.get("firstname", ""),
                "lastname": p.get("lastname", ""),
                "phone": p.get("cellphone") or p.get("homephone") or "",
                "email": p.get("email") or "",
            }
            for p in pubs
        ]
    return []


# ── RapidPro ─────────────────────────────────────────────────────────────────

def _ensure_group(rp) -> str | None:
    result = rp("GET", "groups.json")
    for g in result.get("results", []):
        if g["name"] == GROUP_NAME:
            return g["uuid"]
    created = rp("POST", "groups.json", json={"name": GROUP_NAME})
    return created.get("uuid")


def _ensure_field(rp) -> None:
    result = rp("GET", "fields.json")
    keys = [f.get("key") for f in result.get("results", [])]
    if FIELD_KEY not in keys:
        rp("POST", "fields.json", json={"name": "Organized UID", "type": "text"})


def _sync_contact(rp, person: dict, phone: str, group_uuid: str) -> str:
    """Returns 'created' | 'updated' | 'ok' | 'skipped' | 'error'."""
    wa_urn = f"whatsapp:{phone.lstrip('+')}"
    tel_urn = f"tel:{phone}"

    # whatsapp: first — inbound chat creates contacts under that scheme, so
    # matching it merges the roster into the person's existing conversation
    # contact instead of minting a tel:-only duplicate.
    results = []
    for urn in (wa_urn, tel_urn):
        existing = rp("GET", "contacts.json", params={"urn": urn})
        results = existing.get("results", [])
        if results:
            break

    payload = {
        "name": canonical_name(person),
        "fields": {FIELD_KEY: person.get("person_uid") or ""},
        "groups": [group_uuid] if group_uuid else [],
    }

    if results:
        contact = results[0]
        # A contact with a `kind` field is a post in a contact directory (see
        # contact_sync): its name is the position and must not follow whoever
        # happens to hold that SIM. Renaming it here would also duplicate it on
        # every phone that already imported the directory.
        if str((contact.get("fields", {}) or {}).get("kind") or "").strip():
            logger.warning(
                f"Person sync: {phone} belongs to a directory post "
                f"('{contact.get('name')}') — skipping '{person['display_name']}'"
            )
            return "skipped"
        # Household phone sharing: if this URN already belongs to a DIFFERENT
        # Organized person, leave it alone — otherwise two people sharing one
        # number would rename the contact back and forth on every run.
        owner_uid = (contact.get("fields", {}) or {}).get(FIELD_KEY)
        if owner_uid and owner_uid != payload["fields"][FIELD_KEY]:
            logger.warning(
                f"Person sync: {phone} already linked to another person "
                f"(uid={owner_uid}) — skipping '{person['display_name']}'"
            )
            return "skipped"
        # Ensure both URN schemes end up on the contact (POST replaces the
        # set, so carry the existing ones along).
        current_urns = list(contact.get("urns", []))
        missing_urns = [u for u in (wa_urn, tel_urn) if u not in current_urns]
        needs_update = (
            contact.get("name") != payload["name"]
            or owner_uid != payload["fields"][FIELD_KEY]
            or bool(missing_urns)
        )
        if not needs_update:
            return "ok"
        if missing_urns:
            payload["urns"] = current_urns + missing_urns
        # Preserve whatever groups the contact already has.
        payload["groups"] = [g["uuid"] for g in contact.get("groups", [])]
        if group_uuid and group_uuid not in payload["groups"]:
            payload["groups"].append(group_uuid)
        result = rp(
            "POST", f"contacts.json?uuid={contact['uuid']}", json=payload
        )
        return "error" if result.get("error") else "updated"

    payload["urns"] = [wa_urn, tel_urn]
    result = rp("POST", "contacts.json", json=payload)
    return "error" if result.get("error") else "created"


# ── SiYuan person wiki ───────────────────────────────────────────────────────
#
# One doc per person, structured as a real wiki page:
#   > ⚙️ facts blockquote   — machine-owned, attr custom-person-facts=<uid>,
#                             updated IN PLACE (never a doc rewrite)
#   ## 🧠 Memwa             — Hermes-writable durable facts (never touched)
#   ## 📝 Nòt               — the operator's hand notes (never touched)
# Docs are found by the custom-organized-uid doc attr (rename-safe); the doc
# title is the person's full name and follows roster renames.

_PRIVILEGE_LABELS = {"elder": "Ansyen", "ms": "Sèvitè ministeryèl"}
_ENROLLMENT_LABELS = {
    "FR": "Pyonye pèmanan",
    "AP": "Pyonye oksilyè",
    "FS": "Pyonye espesyal",
    "FMF": "Misyonè",
}
_ROLE_LABELS = {
    "weekend_meeting.speaker.part_1": "Oratè — diskou piblik",
    "weekend_meeting.speaker.part_2": "Oratè (2yèm pati)",
    "weekend_meeting.speaker.substitute": "Oratè ranplasan",
    "weekend_meeting.chairman": "Prezidan (wikenn)",
    "weekend_meeting.wt_study.reader": "Lektè Toudegad",
    "weekend_meeting.wt_study.conductor": "Kondiktè Toudegad",
    "weekend_meeting.opening_prayer": "Priyè kòmansman (wikenn)",
    "weekend_meeting.closing_prayer": "Priyè fen (wikenn)",
    "midweek_meeting.chairman.main_hall": "Prezidan (lasemèn)",
    "midweek_meeting.opening_prayer": "Priyè kòmansman (lasemèn)",
    "midweek_meeting.closing_prayer": "Priyè fen (lasemèn)",
    "midweek_meeting.tgw_talk": "Diskou Trezò",
    "midweek_meeting.tgw_gems": "Trezò espirityèl",
    "midweek_meeting.tgw_bible_reading": "Lekti Labib",
    "midweek_meeting.lc_cbs.conductor": "Kondiktè Etid Biblik",
    "midweek_meeting.lc_cbs.reader": "Lektè Etid Biblik",
}

_MEMWA_SECTION = "## 🧠 Memwa\n\n- ‍"
_NOT_SECTION = "## 📝 Nòt\n\n- ‍"


def canonical_name(person: dict) -> str:
    """'Firstname Lastname' — the wiki/page/contact name. Falls back to the
    JW-style display name ('M. Jean') when name parts are missing."""
    full = f"{person.get('firstname', '')} {person.get('lastname', '')}".strip()
    return full or person.get("display_name", "")


def _date_only(iso: str) -> str:
    return (iso or "").split("T")[0]


def _role_label(role: str) -> str:
    if role in _ROLE_LABELS:
        return _ROLE_LABELS[role]
    # Unmapped slot — show something readable rather than nothing.
    return role.replace("_meeting", "").replace(".", " › ").replace("_", " ")


def _facts_markdown(person: dict, phone: str | None) -> tuple[str, str]:
    """Render the machine facts blockquote. Returns (markdown, hash)."""
    lines = ["> ⚙️ **Fich Organized** — otomatik (person_sync), pa modifye"]

    contact_bits = []
    if phone:
        contact_bits.append(f"📱 {phone}")
    if person.get("email"):
        contact_bits.append(f"✉️ {person['email']}")
    if contact_bits:
        lines.append("> " + " · ".join(contact_bits))
    if person.get("address"):
        lines.append(f"> 🏠 {person['address']}")

    date_bits = []
    if person.get("birth_date"):
        date_bits.append(f"🎂 {_date_only(person['birth_date'])}")
    if person.get("baptized_date"):
        date_bits.append(f"💧 batize {_date_only(person['baptized_date'])}")
    if date_bits:
        lines.append("> " + " · ".join(date_bits))

    role_bits = []
    for pr in person.get("privileges") or []:
        label = _PRIVILEGE_LABELS.get(pr["kind"], pr["kind"])
        since = f" (depi {pr['start_date']})" if pr.get("start_date") else ""
        role_bits.append(f"{label}{since}")
    for en in person.get("enrollments") or []:
        label = _ENROLLMENT_LABELS.get(en["kind"], en["kind"])
        since = f" (depi {en['start_date']})" if en.get("start_date") else ""
        role_bits.append(f"{label}{since}")
    if role_bits:
        lines.append("> 🎖️ " + " · ".join(role_bits))

    group = person.get("group")
    if group:
        role = {"overseer": " — siveyan", "assistant": " — asistan"}.get(
            group.get("role"), "")
        lines.append(f"> 👥 {group['label']}{role}")

    for a in person.get("upcoming") or []:
        lines.append(f"> 📅 {a['week']} — {_role_label(a['role'])}")

    md = "\n".join(lines)
    return md, hashlib.sha256(md.encode()).hexdigest()[:16]


def _sql(stmt: str) -> list[dict]:
    from app.hooks.siyuan_tools import _siyuan_request

    r = _siyuan_request("query/sql", {"stmt": stmt}, quiet=True)
    return r.get("data") or []


def _find_block_retry(root_id: str, block_type: str) -> str | None:
    """First block of a type in a doc — with retries (SQL index is async)."""
    import time

    for _ in range(4):
        rows = _sql(
            f"SELECT id FROM blocks WHERE root_id='{root_id}' "
            f"AND type='{block_type}' ORDER BY created LIMIT 1"
        )
        if rows:
            return rows[0]["id"]
        time.sleep(3)
    return None


def _legacy_notes(doc_id: str) -> str | None:
    """Hand content from a v1 page's '## Notes' section, or None if empty."""
    from app.hooks.siyuan_tools import siyuan_read_doc

    md = siyuan_read_doc(doc_id, full=True) or ""
    m = re.search(r"\n## Notes\n(.*)\Z", md, re.DOTALL)
    if not m:
        return None
    body = m.group(1).strip()
    return body if body and body != "- ‍" else None


def _doc_attrs(person: dict) -> dict:
    return {
        "custom-type": "person",
        "custom-organized-uid": person.get("person_uid") or "",
        "custom-person-privileges": ",".join(
            [p["kind"] for p in person.get("privileges") or []]
            + [e["kind"] for e in person.get("enrollments") or []]
        ),
        "custom-person-group": (person.get("group") or {}).get("label", ""),
    }


def _sync_pages(persons: list[dict]) -> tuple[int, int]:
    from app.hooks.siyuan_tools import (
        _siyuan_request,
        get_notebook_id,
        siyuan_set_attrs,
    )

    nb_id = get_notebook_id(WIKI_NOTEBOOK)
    if not nb_id:
        logger.warning(
            f"Person sync: notebook '{WIKI_NOTEBOOK}' not reachable — "
            "skipping SiYuan pages this run"
        )
        return 0, 0

    # Bulk maps (one SQL each — used as lookups only; the hash check below
    # reads live attrs so the async SQL index can't cause spurious rewrites):
    # uid -> doc id (oldest wins if duplicated), doc -> its blockquote (the
    # facts block: both page generations have exactly one), docs still in the
    # v1 layout (H1 in body), doc titles.
    uid_doc: dict[str, str] = {}
    for row in _sql(
        "SELECT a.value AS uid, a.block_id AS id, b.created FROM attributes a "
        "JOIN blocks b ON b.id = a.block_id AND b.type='d' "
        f"WHERE a.name='custom-organized-uid' AND b.box='{nb_id}' "
        "ORDER BY b.created LIMIT 1000"
    ):
        uid_doc.setdefault(row["uid"], row["id"])
    doc_quote: dict[str, str] = {}
    for row in _sql(
        f"SELECT root_id, id FROM blocks WHERE box='{nb_id}' AND type='b' "
        "AND hpath LIKE '/people/%' ORDER BY created LIMIT 2000"
    ):
        doc_quote.setdefault(row["root_id"], row["id"])
    v1_docs = {
        row["root_id"]
        for row in _sql(
            f"SELECT root_id FROM blocks WHERE box='{nb_id}' AND type='h' "
            "AND subtype='h1' AND hpath LIKE '/people/%' LIMIT 1000"
        )
    }
    doc_titles = {
        row["id"]: row["content"]
        for row in _sql(
            f"SELECT id, content FROM blocks WHERE type='d' AND box='{nb_id}' "
            "AND hpath LIKE '/people/%' LIMIT 1000"
        )
    }

    written = skipped = 0
    seen_titles: set[str] = set()
    for person in persons:
        uid = person.get("person_uid") or ""
        title = re.sub(r"[/\\\n#]", " ", canonical_name(person)).strip()
        if not title or not uid:
            continue
        if title in seen_titles:
            title = f"{title} ({uid[:8]})"
        seen_titles.add(title)

        facts_md, content_hash = _facts_markdown(
            person, normalize_phone(person.get("phone", ""))
        )
        doc_id = uid_doc.get(uid)

        if doc_id:
            # Follow roster renames: the doc title tracks the full name.
            if doc_titles.get(doc_id, "") not in ("", title):
                _siyuan_request(
                    "filetree/renameDocByID", {"id": doc_id, "title": title},
                    quiet=True,
                )
            facts_id = None if doc_id in v1_docs else doc_quote.get(doc_id)
            if facts_id:
                # Live attr read (attr/getBlockAttrs is not the async SQL
                # index) so the skip check is correct immediately after a
                # write — never rewrite an unchanged block.
                attrs = _siyuan_request(
                    "attr/getBlockAttrs", {"id": facts_id}, quiet=True
                )
                if (attrs.get("data") or {}).get("custom-hash") == content_hash:
                    skipped += 1
                    continue
                _siyuan_request("block/updateBlock", {
                    "id": facts_id, "dataType": "markdown", "data": facts_md,
                })
                siyuan_set_attrs(facts_id, {
                    "custom-person-facts": uid, "custom-hash": content_hash,
                })
                siyuan_set_attrs(doc_id, _doc_attrs(person))
                written += 1
                continue
            # v1 page (H1 layout) — one-time restructure, carrying any
            # hand-written Notes into the new 📝 Nòt section. v2 docs never
            # take this path (their whole body would be reset), so Memwa/Nòt
            # edits are safe.
            notes = _legacy_notes(doc_id)
            body = "\n\n".join([
                facts_md,
                _MEMWA_SECTION,
                f"## 📝 Nòt\n\n{notes}" if notes else _NOT_SECTION,
            ])
            _siyuan_request("block/updateBlock", {
                "id": doc_id, "dataType": "markdown", "data": body,
            })
        else:
            body = "\n\n".join([facts_md, _MEMWA_SECTION, _NOT_SECTION])
            result = _siyuan_request("filetree/createDocWithMd", {
                "notebook": nb_id, "path": f"/people/{title}", "markdown": body,
            })
            doc_id = result.get("data")
            if not doc_id:
                logger.warning(f"Person sync: could not create page for {title}")
                continue

        facts_id = _find_block_retry(doc_id, "b")
        if facts_id:
            siyuan_set_attrs(facts_id, {
                "custom-person-facts": uid, "custom-hash": content_hash,
            })
        siyuan_set_attrs(doc_id, _doc_attrs(person))
        written += 1
    return written, skipped


def _sync_index(persons: list[dict]) -> None:
    """Render a roster index into the /people parent doc: headline counts +
    a live query-embed of every person page."""
    from app.hooks.siyuan_tools import (
        _siyuan_request,
        get_notebook_id,
        siyuan_set_attrs,
    )

    nb_id = get_notebook_id(WIKI_NOTEBOOK)
    if not nb_id:
        return
    ids = _siyuan_request(
        "filetree/getIDsByHPath", {"notebook": nb_id, "path": "/people"},
        quiet=True,
    ).get("data") or []
    if not ids:
        return

    active = [p for p in persons if not p.get("archived")]
    kinds: dict[str, int] = {}
    for p in active:
        for pr in p.get("privileges") or []:
            kinds[pr["kind"]] = kinds.get(pr["kind"], 0) + 1
        for en in p.get("enrollments") or []:
            kinds[en["kind"]] = kinds.get(en["kind"], 0) + 1
    with_phone = sum(
        1 for p in active if normalize_phone(p.get("phone", ""))
    )
    labels = {**_PRIVILEGE_LABELS, **_ENROLLMENT_LABELS}
    counts = " · ".join(
        f"{labels.get(k, k)}: {n}" for k, n in sorted(kinds.items())
    )
    body = "\n\n".join([
        "> ⚙️ **Endèks otomatik** (person_sync) — "
        f"👥 {len(active)} moun · 📱 {with_phone} ak telefòn"
        + (f"\n> 🎖️ {counts}" if counts else ""),
        "{{select * from blocks where type='d' and "
        "ial like '%custom-type=\"person\"%' order by content limit 500}}",
    ])
    body_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
    attrs = _siyuan_request(
        "attr/getBlockAttrs", {"id": ids[0]}, quiet=True
    )
    if (attrs.get("data") or {}).get("custom-hash") == body_hash:
        return
    _siyuan_request("block/updateBlock", {
        "id": ids[0], "dataType": "markdown", "data": body,
    })
    siyuan_set_attrs(ids[0], {
        "custom-type": "person-index", "custom-hash": body_hash,
    })


def main() -> None:
    from app.graph.tools.rapidpro import _rp_api as rp

    persons = _persons_from_backend()
    source = "backend"
    if not persons:
        persons = _persons_from_hourglass()
        source = "hourglass"
    if not persons:
        logger.warning(
            "Person sync: no person source yet — enable Data sharing in "
            "Organized and sync, or drop an fsr unified/Hourglass export "
            f"JSON into {HOURGLASS_DIR}/. Nothing to do."
        )
        return

    with_phone = [
        (p, normalize_phone(p.get("phone", "")))
        for p in persons
    ]
    with_phone = [(p, ph) for p, ph in with_phone if ph]

    group_uuid = _ensure_group(rp)
    _ensure_field(rp)

    counts = {"created": 0, "updated": 0, "ok": 0, "skipped": 0, "error": 0}
    for person, phone in with_phone:
        counts[_sync_contact(rp, person, phone, group_uuid)] += 1

    written, skipped = _sync_pages(persons)
    _sync_index(persons)

    logger.info(
        f"Person sync OK (source={source}): {len(persons)} person(s), "
        f"{len(with_phone)} with phone → contacts "
        f"{counts['created']} created / {counts['updated']} updated / "
        f"{counts['ok']} unchanged / {counts['skipped']} shared-phone skip(s) / "
        f"{counts['error']} error(s); "
        f"SiYuan pages {written} written / {skipped} unchanged"
    )
    if counts["error"]:
        _die(f"{counts['error']} contact sync error(s) — see log above")


if __name__ == "__main__":
    main()
