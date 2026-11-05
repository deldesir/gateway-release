"""
KG→Wiki renderer: promote MemPalace knowledge-graph facts into the SiYuan Wiki.

For every entity that is the subject of current (non-invalidated) triples,
upsert a page under Wiki:/entities/<name> in the established format:

    🧠 **From memory** — auto-rendered, do not edit by hand
    • <predicate> **<object>** · 90%          (block-ref when the object has its own page)
    *sources: <drawer ids>*
    *rendered YYYY-MM-DD HH:MM*

    ## Notes                                   (hand-written notes are preserved)

Unchanged pages are skipped via a custom-hash attribute. A run summary is
appended to the Wiki Log doc:

    ## [YYYY-MM-DD] render | KG→Wiki: N page(s), M fact-set(s)

This job is deliberately LOUD: any precondition failure (SiYuan auth, missing
Wiki notebook, missing/empty KG) exits non-zero so the systemd unit shows red
instead of silently rendering nothing.

Run: /opt/iiab/ai-gateway/.venv/bin/python -m app.jobs.kg_wiki_render
"""

import hashlib
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.hooks.siyuan_tools import (
    _siyuan_request,
    get_notebook_id,
    siyuan_append_block,
    siyuan_get_attrs,
    siyuan_read_doc,
    siyuan_set_attrs,
    siyuan_upsert_page,
)

WIKI_NOTEBOOK = "Wiki"
ENTITIES_ROOT = "/entities"
LOG_PATH = "/Log"

_NOTES_DEFAULT = "## Notes\n\n- ‍"


def _die(msg: str) -> None:
    logger.error(f"KG→Wiki render ABORTED: {msg}")
    sys.exit(1)


def _load_fact_sets() -> dict[str, list[dict]]:
    """Return {subject display name: [fact, ...]} for all current triples."""
    from mempalace.knowledge_graph import DEFAULT_KG_PATH, KnowledgeGraph

    if not Path(DEFAULT_KG_PATH).exists():
        _die(f"knowledge graph not found at {DEFAULT_KG_PATH} (set MEMPALACE_KG_PATH)")

    kg = KnowledgeGraph()
    conn = kg._conn()
    rows = conn.execute(
        """
        SELECT s.name AS subject, t.predicate, o.name AS object,
               t.confidence, t.source_drawer_id
        FROM triples t
        JOIN entities s ON t.subject = s.id
        JOIN entities o ON t.object = o.id
        WHERE t.valid_to IS NULL
        ORDER BY s.name, t.extracted_at
        """
    ).fetchall()
    kg.close()

    fact_sets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        fact_sets[r["subject"]].append(
            {
                "predicate": r["predicate"],
                "object": r["object"],
                "confidence": r["confidence"] if r["confidence"] is not None else 1.0,
                "source": r["source_drawer_id"] or "",
            }
        )
    return dict(fact_sets)


def _safe_title(name: str) -> str:
    return name.replace("/", "-").strip() or "unnamed"


def _page_id_by_hpath(nb_id: str, hpath: str) -> str | None:
    result = _siyuan_request("filetree/getIDsByHPath", {"notebook": nb_id, "path": hpath})
    ids = result.get("data") or []
    return ids[0] if ids else None


def _existing_notes(page_id: str) -> str:
    """Extract a hand-edited '## Notes' section from the current page, if any."""
    md = siyuan_read_doc(page_id, full=True) or ""
    m = re.search(r"\n(## Notes\n.*?)(?=\n\[\^|\Z)", md, re.DOTALL)
    if not m:
        return _NOTES_DEFAULT
    notes = m.group(1).rstrip()
    return notes if notes.strip() != "## Notes" else _NOTES_DEFAULT


def _render_body(
    facts: list[dict], page_ids: dict[str, str], notes: str, now: datetime
) -> tuple[str, str]:
    """Return (markdown body, stable content hash).

    The hash excludes the 'rendered <timestamp>' line and the Notes section so
    a page only counts as changed when its facts change.
    """
    lines = ["🧠 **From memory** — auto-rendered, do not edit by hand  "]
    for f in facts:
        pred = f["predicate"].replace("_", " ")
        obj = f["object"]
        pct = int(round((f["confidence"] or 1.0) * 100))
        ref_id = page_ids.get(obj.lower())
        obj_md = f'(({ref_id} "{obj}"))' if ref_id else f"**{obj}**"
        lines.append(f"• {pred} {obj_md} · {pct}%  ")

    sources = sorted({f["source"] for f in facts if f["source"]})
    if sources:
        lines.append(f"*sources: {', '.join(sources)}*  ")

    content_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    lines.append(f"*rendered {now:%Y-%m-%d %H:%M}*")
    body = "\n".join(lines) + "\n\n" + notes + "\n"
    return body, content_hash


def main() -> None:
    now = datetime.now()

    # ── Preconditions (loud) ─────────────────────────────────────────────
    nb_id = get_notebook_id(WIKI_NOTEBOOK)
    if not nb_id:
        _die(
            f"notebook '{WIKI_NOTEBOOK}' not reachable — check SiYuan is running, "
            "SIYUAN_API_URL / SIYUAN_ACCESS_AUTH_CODE, and that the notebook is open"
        )

    fact_sets = _load_fact_sets()
    if not fact_sets:
        _die("knowledge graph has no current triples — likely a KG path misconfig")

    logger.info(f"KG→Wiki: {len(fact_sets)} fact-set(s) to render")

    # ── Pass 1: ensure every entity page exists, build the ref map ──────
    page_ids: dict[str, str] = {}
    for name in fact_sets:
        hpath = f"{ENTITIES_ROOT}/{_safe_title(name)}"
        pid = _page_id_by_hpath(nb_id, hpath)
        if not pid:
            pid = siyuan_upsert_page(WIKI_NOTEBOOK, hpath, "")
            if not pid:
                _die(f"could not create page {hpath}")
        page_ids[name.lower()] = pid

    # ── Pass 2: render bodies (with cross-refs), skip unchanged ─────────
    pages_written = 0
    for name, facts in fact_sets.items():
        pid = page_ids[name.lower()]
        attrs = siyuan_get_attrs(pid)
        notes = _existing_notes(pid)
        body, content_hash = _render_body(facts, page_ids, notes, now)

        if attrs.get("custom-hash") == content_hash:
            continue

        if siyuan_upsert_page(WIKI_NOTEBOOK, f"{ENTITIES_ROOT}/{_safe_title(name)}", body) is None:
            _die(f"failed to write page for '{name}'")

        avg_conf = sum(f["confidence"] or 1.0 for f in facts) / len(facts)
        sources = sorted({f["source"] for f in facts if f["source"]})
        siyuan_set_attrs(
            pid,
            {
                "type": "entity",
                "confidence": f"{avg_conf:.2f}",
                "sources": ", ".join(sources),
                "created": attrs.get("custom-created") or f"{now:%Y-%m-%d}",
                "updated": f"{now:%Y-%m-%d}",
                "hash": content_hash,
            },
        )
        pages_written += 1

    # ── Log entry ────────────────────────────────────────────────────────
    entry = (
        f"## [{now:%Y-%m-%d}] render | KG→Wiki: "
        f"{pages_written} page(s), {len(fact_sets)} fact-set(s)"
    )
    if siyuan_append_block(WIKI_NOTEBOOK, LOG_PATH, entry) is None:
        _die("failed to append the run summary to the Wiki Log doc")

    logger.info(
        f"KG→Wiki render OK: {pages_written} page(s) written, "
        f"{len(fact_sets) - pages_written} unchanged, {len(fact_sets)} fact-set(s)"
    )


if __name__ == "__main__":
    main()
