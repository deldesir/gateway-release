"""
🕊️ Person touches — put your shepherd notes on the person's own page.

The Phase-1 bridge between the two brains. For each person you've LOGGED (a
`person:` capture) that unambiguously matches exactly ONE roster page, this
writes a single machine-owned "🕊️ Shepherd (auto)" blockquote onto that
person's /people page — last contact + your recent touches — so opening the
page shows Organized's ⚙️ facts AND your own notes in one place.

Safe by construction:
  - the block is DISJOINT from person_sync's ⚙️ facts block and from the
    Hermes/operator-owned 🧠 Memwa / 📝 Nòt sections. Each job finds only its
    own block by a custom attr, so neither clobbers the other; the facts block
    stays the oldest blockquote (person_sync's anchor), ours is inserted after.
  - a logged name matching MORE THAN ONE roster person is ambiguous and is
    skipped — never risk writing spiritual notes onto the wrong page.
  - read-only recompute, idempotent via a content hash (unchanged → skip).

A silent no-op until you actually log people, so it costs nothing on an empty
ledger. Read-only w.r.t. the roster; the only writes are our own block.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.person_touches
"""

import hashlib
import os
import sys
from datetime import datetime

from loguru import logger

WIKI_NOTEBOOK = os.getenv("PERSON_SYNC_NOTEBOOK", "Wiki")
TOUCH_ATTR = "shepherd-touch"          # custom-shepherd-touch=<person_uid>
MAX_TOUCHES = 5


def _die(msg: str) -> None:
    logger.error(f"Person touches ABORTED: {msg}")
    sys.exit(1)


def _render(uid: str, days_since, count: int, touches: list[dict]) -> tuple[str, str]:
    plural = "es" if count != 1 else ""
    lines = [f"> 🕊️ **Shepherd** — auto, from your captures ({count} touch{plural})"]
    if days_since is not None:
        lines.append(f"> Last contact: **{days_since}d** ago")
    for t in touches[:MAX_TOUCHES]:
        day = (t.get("ts") or "")[:10]
        lines.append(f"> - `{day}` — {t.get('text', '')}")
    md = "\n".join(lines)
    return md, hashlib.sha256((uid + "\n" + md).encode()).hexdigest()[:16]


def _append_op_id(result: dict) -> str | None:
    data = result.get("data")
    if data and isinstance(data, list) and data:
        return data[0].get("doOperations", [{}])[0].get("id")
    return None


def main() -> None:
    from app.graph.tools.people import (
        _keys_match, _person_key, _roster_persons, people_index, person_history,
    )
    from app.hooks.siyuan_tools import (
        _siyuan_request, get_notebook_id, siyuan_get_attrs, siyuan_set_attrs,
    )
    from app.jobs.person_sync import canonical_name

    now = datetime.now()
    nb_id = get_notebook_id(WIKI_NOTEBOOK)
    if not nb_id:
        _die(f"SiYuan notebook '{WIKI_NOTEBOOK}' unreachable")

    logged = people_index(now)
    if not logged:
        logger.info("Person touches: no person captures yet — nothing to do")
        return

    persons = _roster_persons()
    if not persons:
        logger.info("Person touches: roster unavailable — nothing to do")
        return

    # roster person_uid → doc id (oldest wins), same map person_sync builds.
    uid_doc: dict[str, str] = {}
    for row in _siyuan_request("query/sql", {"stmt":
        "SELECT a.value AS uid, a.block_id AS id, b.created FROM attributes a "
        "JOIN blocks b ON b.id=a.block_id AND b.type='d' "
        f"WHERE a.name='custom-organized-uid' AND b.box='{nb_id}' "
        "ORDER BY b.created LIMIT 2000"}, quiet=True).get("data") or []:
        uid_doc.setdefault(row["uid"], row["id"])

    roster = [
        {"uid": p.get("person_uid") or "", "key": _person_key(canonical_name(p))}
        for p in persons if p.get("person_uid")
    ]

    written = skipped = ambiguous = unmatched = deferred = 0
    for lp in logged:
        matches = [r for r in roster if _keys_match(lp["key"], r["key"])]
        if not matches:
            unmatched += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            logger.info(f"Person touches: '{lp['name']}' matches "
                        f"{len(matches)} roster people — skipping (ambiguous)")
            continue
        uid = matches[0]["uid"]
        doc_id = uid_doc.get(uid)
        if not doc_id:
            deferred += 1              # page not created yet; retry next run
            continue

        touches = person_history(lp["name"], limit=MAX_TOUCHES, now=now)
        md, content_hash = _render(uid, lp["days_since"], lp["count"], touches)

        rows = _siyuan_request("query/sql", {"stmt":
            f"SELECT id FROM blocks WHERE root_id='{doc_id}' AND type='b' "
            f"AND ial LIKE '%custom-{TOUCH_ATTR}=%' LIMIT 1"},
            quiet=True).get("data") or []
        block_id = rows[0]["id"] if rows else None

        if block_id:
            if siyuan_get_attrs(block_id).get("custom-hash") == content_hash:
                skipped += 1
                continue
            _siyuan_request("block/updateBlock",
                            {"id": block_id, "dataType": "markdown", "data": md})
        else:
            # insert right after the ⚙️ facts blockquote (oldest 'b'), so the
            # page reads facts → shepherd → Memwa → Nòt; fall back to appending.
            facts = _siyuan_request("query/sql", {"stmt":
                f"SELECT id FROM blocks WHERE root_id='{doc_id}' AND type='b' "
                "AND ial LIKE '%custom-person-facts=%' ORDER BY created LIMIT 1"},
                quiet=True).get("data") or []
            if facts:
                res = _siyuan_request("block/insertBlock", {
                    "previousID": facts[0]["id"], "dataType": "markdown", "data": md})
            else:
                res = _siyuan_request("block/appendBlock", {
                    "parentID": doc_id, "dataType": "markdown", "data": md})
            block_id = _append_op_id(res)
            if not block_id:
                logger.warning(f"Person touches: could not write block for '{lp['name']}'")
                continue

        siyuan_set_attrs(block_id, {
            TOUCH_ATTR: uid, "hash": content_hash, "type": "auto",
        })
        written += 1

    logger.info(
        f"Person touches OK: {written} written, {skipped} unchanged, "
        f"{ambiguous} ambiguous, {deferred} awaiting page, "
        f"{unmatched} not in roster (of {len(logged)} logged)"
    )


if __name__ == "__main__":
    main()
