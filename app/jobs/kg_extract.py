"""
→KG extraction: promote facts from MemPalace drawers AND operator captures
into the knowledge graph.

The KG otherwise only grows when the admin explicitly calls kg_add in chat —
in practice it goes stale. Worse, it only ever learned from what the operator
*said* (drawers), never from what they *did* (captures.jsonl). This job feeds
BOTH into the same extractor:
  - MemPalace drawers in the admin wing (chat memory), and
  - durable captures (person / note / family / study / business) — so the
    things you log accrete into durable knowledge instead of evaporating.
Facts are added with provenance (source_drawer_id = the drawer id, or
"capture:<ts>"). The nightly KG→Wiki render (03:30) then publishes them, so
schedule this just before it (03:15).

State lives in data/kg_extract_state.json (processed + failed drawer ids, plus
processed capture timestamps), so each source is paid for once. Loud on
precondition failures.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.kg_extract
"""

import json
import os
import sys
from pathlib import Path

from loguru import logger

STATE_PATH = Path("/opt/iiab/ai-gateway/data/kg_extract_state.json")
BATCH_LIMIT = 15          # drawers per run — bounds nightly LLM cost
MAX_FAILURES = 2          # give up on a drawer after N failed extractions
MIN_CONTENT_CHARS = 80    # skip trivial drawers

# Captures worth distilling: the durable, narrative kinds. Work tickets,
# reminders, ministry tallies and bookkeeping kinds are excluded (transient
# or purely numeric — no lasting facts to learn).
CAPTURE_KINDS = {"person", "note", "family", "study", "business"}
CAPTURE_BATCH_LIMIT = 20  # captures per run
CAPTURE_MIN_CHARS = 20    # skip one-word jots

_EXTRACT_PROMPT = """You are a knowledge-graph fact extractor for a personal memory system.

From the text below, extract DURABLE facts as subject-predicate-object triples:
people, places, organizations, projects, roles, ownership, relationships,
stable preferences, dates of life events. Skip small talk, transient states,
opinions about the moment, and anything you are not confident is a lasting fact.

Rules:
- subject/object: concise entity names (e.g. "Jean", "ai-gateway", "Pòtoprens");
  predicate: short snake_case relation (e.g. "lives_in", "owns", "works_on").
- confidence: 0.5-1.0 (only include facts at 0.5+).
- 0 to 8 triples. An empty list is a good answer for chatty content.

Respond with JSON: {"triples": [{"subject": "...", "predicate": "...", "object": "...", "confidence": 0.9}]}

--- TEXT ---
%s
--- END TEXT ---
"""


def _die(msg: str) -> None:
    logger.error(f"KG extract ABORTED: {msg}")
    sys.exit(1)


def _admin_wing() -> str:
    admin = [a.strip() for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]
    if not admin:
        _die("ADMIN_PHONE not set — cannot resolve the admin wing")
    tail = "".join(c if c.isalnum() else "_" for c in admin[0].split(":")[-1].lstrip("+"))
    return f"wing_{tail}"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"done": [], "failed": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _triple_exists(kg, subject: str, predicate: str, obj: str) -> bool:
    conn = kg._conn()
    row = conn.execute(
        "SELECT 1 FROM triples WHERE subject=? AND predicate=? AND object=? "
        "AND valid_to IS NULL LIMIT 1",
        (kg._entity_id(subject), predicate.lower().replace(" ", "_"), kg._entity_id(obj)),
    ).fetchone()
    return row is not None


def _process_text(kg, text: str, source_id: str, adapter: str,
                  completion_json) -> tuple[int, int] | None:
    """Extract triples from one text and add the new ones. Returns
    (added, duplicates), or None if the LLM extraction itself failed (the
    caller decides whether to retry or mark done)."""
    try:
        raw = completion_json(_EXTRACT_PROMPT % text[:6000], temperature=0.2)
        triples = (json.loads(raw) or {}).get("triples", [])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"KG extract: extraction failed for {source_id}: {e}")
        return None
    added = dupes = 0
    for t in triples:
        s, p, o = t.get("subject"), t.get("predicate"), t.get("object")
        conf = float(t.get("confidence") or 0)
        if not (s and p and o) or conf < 0.5:
            continue
        if _triple_exists(kg, s, p, o):
            dupes += 1
            continue
        kg.add_triple(
            s, p, o,
            confidence=round(conf, 2),
            source_drawer_id=source_id,
            adapter_name=adapter,
        )
        added += 1
    return added, dupes


def main() -> None:
    from mempalace.knowledge_graph import DEFAULT_KG_PATH, KnowledgeGraph
    from mempalace.mcp_server import tool_get_drawer, tool_list_drawers
    from talkmaster.llm import completion_json

    if not Path(DEFAULT_KG_PATH).exists():
        _die(f"knowledge graph not found at {DEFAULT_KG_PATH} (set MEMPALACE_KG_PATH)")

    wing = _admin_wing()
    state = _load_state()
    done = set(state.get("done", []))
    failed: dict = state.get("failed", {})

    # Enumerate all drawer ids in the admin wing
    drawers, offset = [], 0
    while True:
        page = tool_list_drawers(wing=wing, limit=100, offset=offset)
        if isinstance(page, dict) and page.get("error"):
            _die(f"could not list drawers: {page['error']}")
        batch = page.get("drawers", [])
        drawers.extend(batch)
        offset += len(batch)
        if offset >= page.get("total", 0) or not batch:
            break

    def _drawer_id(d: dict) -> str:
        return d.get("drawer_id") or d.get("id") or ""

    pending = [
        d for d in drawers
        if _drawer_id(d)
        and _drawer_id(d) not in done
        and failed.get(_drawer_id(d), 0) < MAX_FAILURES
    ]

    # Captures the operator logged but the KG hasn't distilled yet.
    from app.graph.tools.capture import read_captures
    cap_done = set(state.get("captures_done", []))
    pending_caps = [
        r for r in read_captures(kinds=CAPTURE_KINDS)
        if r.get("ts") and r["ts"] not in cap_done
        and len((r.get("text") or "").strip()) >= CAPTURE_MIN_CHARS
    ]
    logger.info(
        f"KG extract: {len(drawers)} drawer(s) in {wing} ({len(pending)} pending, "
        f"up to {BATCH_LIMIT}); {len(pending_caps)} capture(s) pending "
        f"(up to {CAPTURE_BATCH_LIMIT})"
    )
    if not pending and not pending_caps:
        logger.info("KG extract: nothing new to process")
        return

    kg = KnowledgeGraph()
    added = skipped_dupes = processed = 0

    for d in pending[:BATCH_LIMIT]:
        did = _drawer_id(d)
        record = tool_get_drawer(did)
        content = ""
        if isinstance(record, dict):
            content = record.get("content") or record.get("document") or ""
        if len(content) < MIN_CONTENT_CHARS:
            done.add(did)
            processed += 1
            continue

        res = _process_text(kg, content, did, "kg_extract", completion_json)
        if res is None:
            failed[did] = failed.get(did, 0) + 1
            logger.warning(f"KG extract: drawer {did} failed "
                           f"({failed[did]}/{MAX_FAILURES})")
            continue
        a, dup = res
        added += a
        skipped_dupes += dup
        done.add(did)
        failed.pop(did, None)
        processed += 1

    # Captures → KG: what you DID feeds the second brain, not only what you said.
    cap_added = cap_processed = 0
    for r in pending_caps[:CAPTURE_BATCH_LIMIT]:
        ts = r["ts"]
        text = (r.get("text") or "").strip()
        res = _process_text(kg, text, f"capture:{ts}", "capture_kg", completion_json)
        if res is None:
            continue                 # transient LLM failure → retry next run
        a, dup = res
        cap_added += a
        skipped_dupes += dup
        cap_done.add(ts)
        cap_processed += 1

    kg.close()
    state["done"] = sorted(done)
    state["failed"] = failed
    state["captures_done"] = sorted(cap_done)
    _save_state(state)
    logger.info(
        f"KG extract OK: {processed} drawer(s) + {cap_processed} capture(s) "
        f"processed, {added + cap_added} new triple(s) "
        f"({cap_added} from captures), {skipped_dupes} duplicate(s) skipped, "
        f"{len(pending) - processed} drawer(s) still pending"
    )


if __name__ == "__main__":
    main()
