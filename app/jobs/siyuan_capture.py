"""
✍️ SiYuan inbound capture — capture from where you actually live.

WhatsApp is one capture surface; SiYuan is the other (you're in it all day,
synced to desktop + mobile). This job polls a single Life doc — "✍️ Inbox" —
for verb-prefixed lines you've written there, routes each through the SAME
pipeline as WhatsApp (macro_capture), and marks it done with a ✅ and a
`custom-captured` stamp so it's never ingested twice.

Loop-safe by construction:
  - the Inbox is never a mirror target (WhatsApp captures mirror into
    📥 Capture / 🎫 Tickets / …, never here), and
  - ingestion runs with mirror=False, so nothing is written back to SiYuan
    except the ✅ mark on the line you wrote.
Verb detection reads the block's *markdown*, where the intro's backtick-quoted
examples (`` `note:` ``) don't start with a verb — so the intro is skipped, and
so is any prose line without a verb (left untouched for you to fix).

Idempotent: dedup by the `custom-captured` block attr (survives a lost state
file) + a bounded fast-path state file + a ✅ prefix guard. Safe to run often.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.siyuan_capture
"""

import json
import os
import sys
from datetime import datetime

from loguru import logger

LIFE_NOTEBOOK = os.getenv("COCKPIT_NOTEBOOK", "Life")
INBOX_PATH = "/✍️ Inbox"
STATE_FILE = os.getenv(
    "SIYUAN_CAPTURE_STATE_FILE",
    "/opt/iiab/ai-gateway/data/siyuan_capture_state.json",
)

# Intro is a blockquote of prose; examples are backtick-quoted so verb
# detection (which reads markdown) never mistakes them for real captures.
_INTRO = (
    "> ✍️ **Inbox** — write one capture per line; it syncs to the cockpit.\n"
    "> Use a verb at the start: `ticket:` · `note:` · `person: Jean | …` · "
    "`ministry: 2h` · `business:` · `family:` · `study:` · "
    "`remind: tomorrow 9am …`\n"
    "> A ✅ appears once a line is captured — leave it or delete it."
)


def _die(msg: str) -> None:
    logger.error(f"SiYuan capture ABORTED: {msg}")
    sys.exit(1)


def _load_seen() -> set:
    try:
        d = json.loads(open(STATE_FILE).read())
        return set(d.get("seen", []))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        # bound the state file — the block attr is the durable dedup, this is
        # just a fast path, so keep only the most-recent ids.
        open(STATE_FILE, "w").write(json.dumps({"seen": sorted(seen)[-1000:]}))
    except OSError as e:
        logger.warning(f"SiYuan capture: cannot persist state: {e}")


def _ensure_inbox(nb_id: str) -> str | None:
    from app.hooks.siyuan_tools import _siyuan_request
    ids = _siyuan_request(
        "filetree/getIDsByHPath", {"notebook": nb_id, "path": INBOX_PATH},
        quiet=True,
    ).get("data") or []
    if ids:
        return ids[0]
    res = _siyuan_request("filetree/createDocWithMd", {
        "notebook": nb_id, "path": INBOX_PATH, "markdown": _INTRO,
    })
    doc_id = res.get("data")
    if doc_id:
        logger.info(f"SiYuan capture: created {LIFE_NOTEBOOK}{INBOX_PATH} → {doc_id}")
    return doc_id


def main() -> None:
    from app.graph.tools.capture import macro_capture, split_verb
    from app.hooks.siyuan_tools import (
        _siyuan_request, get_notebook_id, siyuan_set_attrs,
    )

    nb_id = get_notebook_id(LIFE_NOTEBOOK)
    if not nb_id:
        _die(f"SiYuan notebook '{LIFE_NOTEBOOK}' unreachable")

    doc_id = _ensure_inbox(nb_id)
    if not doc_id:
        _die("could not resolve/create the ✍️ Inbox doc")

    seen = _load_seen()
    rows = _siyuan_request("query/sql", {"stmt":
        f"SELECT id, markdown, ial FROM blocks WHERE root_id='{doc_id}' "
        "AND type='p' ORDER BY created"}, quiet=True).get("data") or []

    ingested = 0
    for row in rows:
        bid = row.get("id")
        md = (row.get("markdown") or "").strip()
        ial = row.get("ial") or ""
        if not bid or bid in seen or "custom-captured" in ial:
            continue
        if md.startswith("✅"):
            seen.add(bid)               # already processed, remember it
            continue
        kind, _rest = split_verb(md)
        if not kind:
            continue                    # prose / backticked example → leave alone
        result = macro_capture({"text": md, "mirror": False})
        if result.strip().startswith("🤔"):
            # macro_capture needs more (e.g. `remind:` with no time, `close:`
            # with no id). Leave the line plain — the missing ✅ is the signal to
            # fix or delete it — and DON'T mark it, so a correction re-ingests.
            logger.debug(f"SiYuan capture: line not yet valid, left as-is: "
                         f"{md[:60]!r} ({result[:50]!r})")
            continue
        # success → feedback + idempotency mark on the very line you wrote
        _siyuan_request("block/updateBlock", {
            "id": bid, "dataType": "markdown", "data": f"✅ {md}",
        })
        siyuan_set_attrs(bid, {
            "captured": datetime.now().isoformat(timespec="seconds"),
            "capture-kind": kind,
        })
        seen.add(bid)
        ingested += 1
        logger.info(f"SiYuan capture: ingested [{kind}] {md[:60]!r} → {result[:60]!r}")

    _save_seen(seen)
    logger.info(f"SiYuan capture OK: {ingested} new line(s) from "
                f"{LIFE_NOTEBOOK}{INBOX_PATH}")


if __name__ == "__main__":
    main()
