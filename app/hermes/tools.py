import logging
import os
from tools.registry import registry
from app.hermes.engine import _current_urn
import app.hermes.schemas as schemas
from app.graph.tools import rapidpro, mocks, talkprep, forms, upload, system, config
from app.graph.tools import organized
from app.graph.tools import capture
import app.plugins.social.schemas as social_schemas
import app.plugins.social.tools as social

logger = logging.getLogger(__name__)

# ── MemPalace tools (new in V2) ─────────────────────────────────────────────

def search_memory(args: dict, **kw) -> str:
    """Search the user's memory palace for relevant past conversations."""
    from mempalace.mcp_server import tool_search

    query = args.get("query", "")
    result = tool_search(query=query, wing=_palace_wing(), limit=5)
    return str(result)


def store_memory(args: dict, **kw) -> str:
    """Store a fact or note in the user's memory palace."""
    from mempalace.mcp_server import tool_add_drawer

    content = args.get("content", "")
    room = args.get("room", "general")

    result = tool_add_drawer(content=content, wing=_palace_wing(), room=room)
    return str(result)


def recall_memory(args: dict, **kw) -> str:
    """Get the status and overview of the memory palace."""
    # tool_status() is palace-GLOBAL (wing names, counts) — stats-level
    # exposure of the operator's second brain. Content reads are already
    # wing-scoped; scope the overview to the operator too.
    if not _is_admin_caller():
        return ("Memwa ou yo estoke an prive nan pwòp espas pa ou. "
                "Rezime jeneral la se pou operatè a sèlman.")
    from mempalace.mcp_server import tool_status
    result = tool_status()
    return str(result)


def _sanitize_wing_tail(value: str) -> str:
    """Map a URN tail to mempalace's safe name charset (non-alnum -> '_')."""
    return "".join(c if c.isalnum() else "_" for c in value)


def _palace_wing() -> str:
    """Derive the tenant-scoped wing name for the current URN.

    Single source of truth for wing naming — search/store/diary all call it:
    1. Sanitize the URN tail to mempalace's safe charset (non-alphanumeric
       -> '_'); without this, email URNs ('@', '.') are rejected, silently
       breaking per-tenant memory.
    2. Identity unification — URNs belonging to the same person share ONE
       wing. Admin identities come from ADMIN_PHONE (comma-separated phone
       and/or email); any of them resolves to the canonical admin wing (the
       first ADMIN_PHONE entry), so a person reachable on both WhatsApp and
       email has a single shared memory ("one identity, one memory").
       Generalizes to any multi-URN identity listed together.
    """
    urn = _current_urn.get()
    if not urn:
        return "default"
    safe = _sanitize_wing_tail(urn.split(":")[-1].lstrip("+"))

    admin_ids = [_sanitize_wing_tail(a.strip())
                 for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]
    if admin_ids and safe in admin_ids:
        return f"wing_{admin_ids[0]}"
    return f"wing_{safe}"


def _is_admin_caller() -> bool:
    """True only when the current URN resolves to the canonical admin wing.

    The knowledge graph is the admin's private second brain and is a SINGLE
    GLOBAL store (entities are not namespaced per wing). So it must never be
    read or written from a non-admin (e.g. customer support/sales) session,
    or the admin's personal/business facts would bleed across users — and
    other users' facts would pollute the admin's graph. Mirrors the per-wing
    ownership check already used for drawers (_verify_drawer_ownership)."""
    admin_ids = [_sanitize_wing_tail(a.strip())
                 for a in os.getenv("ADMIN_PHONE", "").split(",") if a.strip()]
    return bool(admin_ids) and _palace_wing() == f"wing_{admin_ids[0]}"


_KG_ADMIN_ONLY = "The knowledge graph is private to the admin and is unavailable in this session."


def diary_write(args: dict, **kw) -> str:
    """Write an agent diary entry (reflection journal)."""
    from mempalace.mcp_server import tool_diary_write
    result = tool_diary_write(
        agent_name="hermes",
        entry=args.get("entry", ""),
        topic=args.get("topic", "general"),
        wing=_palace_wing(),
    )
    return str(result)


def diary_read(args: dict, **kw) -> str:
    """Read recent agent diary entries."""
    from mempalace.mcp_server import tool_diary_read
    result = tool_diary_read(
        agent_name="hermes",
        last_n=args.get("last_n", 10),
        wing=_palace_wing(),
    )
    return str(result)


def kg_query(args: dict, **kw) -> str:
    """Query knowledge graph for entity relationships."""
    if not _is_admin_caller():
        return _KG_ADMIN_ONLY
    from mempalace.mcp_server import tool_kg_query
    result = tool_kg_query(
        entity=args.get("entity", ""),
        as_of=args.get("as_of"),
        direction=args.get("direction", "both"),
    )
    return str(result)


def kg_add(args: dict, **kw) -> str:
    """Add a relationship triple to the knowledge graph."""
    if not _is_admin_caller():
        return _KG_ADMIN_ONLY
    from mempalace.mcp_server import tool_kg_add
    result = tool_kg_add(
        subject=args.get("subject", ""),
        predicate=args.get("predicate", ""),
        object=args.get("object", ""),
        source_closet=args.get("source", "hermes"),
    )
    return str(result)


def kg_invalidate(args: dict, **kw) -> str:
    """Mark a knowledge graph fact as outdated/incorrect."""
    if not _is_admin_caller():
        return _KG_ADMIN_ONLY
    from mempalace.mcp_server import tool_kg_invalidate
    result = tool_kg_invalidate(
        subject=args.get("subject", ""),
        predicate=args.get("predicate", ""),
        object=args.get("object", ""),
    )
    return str(result)


def _verify_drawer_ownership(drawer_id: str, wing: str) -> dict | None:
    """Fetch a drawer and verify it belongs to the caller's wing.

    Returns None if ownership is confirmed, or an error dict to return
    immediately if the drawer is missing or belongs to another tenant.
    """
    from mempalace.mcp_server import _get_collection

    col = _get_collection()
    if not col:
        return {"success": False, "error": "Palace not initialized"}
    existing = col.get(ids=[drawer_id], include=["metadatas"])
    if not existing["ids"]:
        return {"success": False, "error": f"Drawer not found: {drawer_id}"}
    meta = existing.get("metadatas", [{}])[0] or {}
    drawer_wing = meta.get("wing", "")
    if drawer_wing != wing:
        return {"success": False, "error": "Access denied: drawer belongs to another user"}
    return None


def update_memory(args: dict, **kw) -> str:
    """Update an existing memory drawer's content or room."""
    from mempalace.mcp_server import tool_update_drawer

    drawer_id = args.get("drawer_id", "")
    wing = _palace_wing()

    # Verify ownership before allowing mutation
    err = _verify_drawer_ownership(drawer_id, wing)
    if err:
        return str(err)

    # Only pass fields the user explicitly wants to change.
    # Don't pass wing= (that would be a move, not a guard).
    result = tool_update_drawer(
        drawer_id=drawer_id,
        content=args.get("content"),
        room=args.get("room"),
    )
    return str(result)


def delete_memory(args: dict, **kw) -> str:
    """Delete a memory drawer by its ID."""
    from mempalace.mcp_server import tool_delete_drawer

    drawer_id = args.get("drawer_id", "")
    wing = _palace_wing()

    # Verify ownership before allowing deletion
    err = _verify_drawer_ownership(drawer_id, wing)
    if err:
        return str(err)

    result = tool_delete_drawer(drawer_id=drawer_id)
    return str(result)


SEARCH_MEMORY_SCHEMA = {
    "name": "search_memory",
    "description": "Search the user's memory palace for relevant past conversations.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }
}

STORE_MEMORY_SCHEMA = {
    "name": "store_memory",
    "description": "Store a fact or note in the user's memory palace.",
    "parameters": {
        "type": "object",
        "properties": {"content": {"type": "string"}, "room": {"type": "string"}},
        "required": ["content"]
    }
}

RECALL_MEMORY_SCHEMA = {
    "name": "recall_memory",
    "description": "Get the status and overview of the memory palace.",
    "parameters": {"type": "object", "properties": {}}
}

DIARY_WRITE_SCHEMA = {
    "name": "diary_write",
    "description": (
        "Write an agent diary entry — your reflection journal. Use after sessions to record "
        "what happened, what you learned, and what matters. Entries are scoped to the current user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry": {"type": "string", "description": "The diary entry content."},
            "topic": {"type": "string", "description": "Topic tag (e.g. 'crm', 'talkprep', 'general')."},
        },
        "required": ["entry"]
    }
}

DIARY_READ_SCHEMA = {
    "name": "diary_read",
    "description": "Read recent diary entries to recall what you've been working on and reflecting about.",
    "parameters": {
        "type": "object",
        "properties": {
            "last_n": {"type": "integer", "description": "Number of recent entries to return (default 10)."},
        },
    }
}

KG_QUERY_SCHEMA = {
    "name": "kg_query",
    "description": (
        "Query the knowledge graph for relationships about an entity. Returns triples like "
        "'Marie works-with Jean' or 'Project-X uses Python'. Use for relationship questions "
        "that SiYuan page attributes can't answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "The entity name to query."},
            "direction": {"type": "string", "enum": ["both", "outgoing", "incoming"], "description": "Relationship direction."},
        },
        "required": ["entity"]
    }
}

KG_ADD_SCHEMA = {
    "name": "kg_add",
    "description": (
        "Add a relationship triple to the knowledge graph. Format: subject-predicate-object. "
        "Example: kg_add(subject='Marie', predicate='works-with', object='Jean'). "
        "Use for relationships between entities. For entity properties, use SiYuan attrs instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string", "description": "Relationship verb (e.g. 'works-with', 'lives-in', 'manages')."},
            "object": {"type": "string"},
        },
        "required": ["subject", "predicate", "object"]
    }
}

KG_INVALIDATE_SCHEMA = {
    "name": "kg_invalidate",
    "description": "Mark a knowledge graph triple as outdated or incorrect. The fact remains in history but is no longer active.",
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
        },
        "required": ["subject", "predicate", "object"]
    }
}

UPDATE_MEMORY_SCHEMA = {
    "name": "update_memory",
    "description": (
        "Update an existing memory drawer. Use to correct or refine a stored fact. "
        "You need the drawer_id from a prior search_memory result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drawer_id": {"type": "string", "description": "The drawer ID to update."},
            "content": {"type": "string", "description": "New content (replaces existing)."},
            "room": {"type": "string", "description": "Move to a different room."},
        },
        "required": ["drawer_id"]
    }
}

DELETE_MEMORY_SCHEMA = {
    "name": "delete_memory",
    "description": (
        "Delete a memory drawer permanently. Use when a stored fact is wrong, "
        "obsolete, or the user asks to forget something. Requires drawer_id "
        "from a prior search_memory result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drawer_id": {"type": "string", "description": "The drawer ID to delete."},
        },
        "required": ["drawer_id"]
    }
}

# ── SiYuan read tool handlers ───────────────────────────────────────────────

def _handle_siyuan_search(args: dict, **kw) -> str:
    """Search the SiYuan knowledge wiki."""
    import json
    from app.hooks.siyuan_tools import siyuan_search

    results = siyuan_search(
        query=args.get("query", ""),
        notebook=args.get("notebook"),
    )
    if not results:
        return json.dumps({"message": "No results found.", "results": []})
    return json.dumps({"results": results}, ensure_ascii=False)


def _handle_siyuan_read(args: dict, **kw) -> str:
    """Read a SiYuan document by block ID."""
    import json
    from app.hooks.siyuan_tools import siyuan_read_doc

    content = siyuan_read_doc(doc_id=args.get("doc_id", ""))
    if content is None:
        return json.dumps({"error": "Document not found or unreadable."})
    return json.dumps({"content": content}, ensure_ascii=False)


def _handle_siyuan_list_docs(args: dict, **kw) -> str:
    """List documents in a SiYuan notebook path."""
    import json
    from app.hooks.siyuan_tools import siyuan_list_docs
    results = siyuan_list_docs(
        notebook=args.get("notebook", ""),
        path=args.get("path", "/"),
    )
    return json.dumps({"documents": results}, ensure_ascii=False)


def _handle_siyuan_create_notebook(args: dict, **kw) -> str:
    """Create a new SiYuan notebook."""
    import json
    from app.hooks.siyuan_tools import siyuan_create_notebook
    nb_id = siyuan_create_notebook(name=args.get("name", ""))
    if nb_id:
        return json.dumps({"notebook_id": nb_id})
    return json.dumps({"error": "Failed to create notebook."})


def _handle_siyuan_create_doc(args: dict, **kw) -> str:
    """Create a new document in a SiYuan notebook."""
    import json
    from app.hooks.siyuan_tools import siyuan_create_doc, siyuan_doc_url
    doc_id = siyuan_create_doc(
        notebook=args.get("notebook", ""),
        path=args.get("path", ""),
        markdown=args.get("markdown", ""),
    )
    if doc_id:
        return json.dumps({"doc_id": doc_id, "url": siyuan_doc_url(doc_id)})
    return json.dumps({"error": "Failed to create document."})


def _handle_siyuan_update_block(args: dict, **kw) -> str:
    """Update a block's content."""
    import json
    from app.hooks.siyuan_tools import siyuan_update_block
    ok = siyuan_update_block(
        block_id=args.get("block_id", ""),
        markdown=args.get("markdown", ""),
    )
    return json.dumps({"success": ok})


def _handle_siyuan_append_block(args: dict, **kw) -> str:
    """Append content to a SiYuan page."""
    import json
    from app.hooks.siyuan_tools import siyuan_append_block, siyuan_doc_url
    block_id = siyuan_append_block(
        notebook=args.get("notebook", ""),
        path=args.get("path", ""),
        markdown=args.get("markdown", ""),
    )
    if block_id:
        return json.dumps({"block_id": block_id, "url": siyuan_doc_url(block_id)})
    return json.dumps({"error": "Failed to append block."})


def _handle_siyuan_set_attrs(args: dict, **kw) -> str:
    """Set custom attributes on a block."""
    import json
    from app.hooks.siyuan_tools import siyuan_set_attrs
    ok = siyuan_set_attrs(
        block_id=args.get("block_id", ""),
        attrs=args.get("attrs", {}),
    )
    return json.dumps({"success": ok})


def _handle_siyuan_get_attrs(args: dict, **kw) -> str:
    """Get attributes of a block."""
    import json
    from app.hooks.siyuan_tools import siyuan_get_attrs
    attrs = siyuan_get_attrs(block_id=args.get("block_id", ""))
    return json.dumps({"attrs": attrs}, ensure_ascii=False)


def _handle_siyuan_sql_query(args: dict, **kw) -> str:
    """Execute a read-only SQL query."""
    import json
    from app.hooks.siyuan_tools import siyuan_sql_query
    results = siyuan_sql_query(sql=args.get("sql", ""))
    return json.dumps({"results": results}, ensure_ascii=False)


def _handle_siyuan_delete_block(args: dict, **kw) -> str:
    """Delete a block."""
    import json
    from app.hooks.siyuan_tools import siyuan_delete_block
    ok = siyuan_delete_block(block_id=args.get("block_id", ""))
    return json.dumps({"success": ok})


def _handle_siyuan_rename_doc(args: dict, **kw) -> str:
    """Rename a document."""
    import json
    from app.hooks.siyuan_tools import siyuan_rename_doc
    ok = siyuan_rename_doc(
        doc_id=args.get("doc_id", ""),
        title=args.get("title", ""),
    )
    return json.dumps({"success": ok})


def _handle_siyuan_init_wiki(args: dict, **kw) -> str:
    """Bootstrap a new LLM Wiki in SiYuan."""
    import json
    from app.hooks.siyuan_tools import siyuan_init_wiki, siyuan_doc_url
    result = siyuan_init_wiki(
        name=args.get("name", "Wiki"),
        domain=args.get("domain", "General knowledge"),
        tags=args.get("tags", ""),
    )
    # Inject clickable URLs for each created document
    for key in ("schema_id", "index_id", "log_id"):
        if result.get(key):
            result[f"{key.replace('_id', '_url')}"] = siyuan_doc_url(result[key])
    if result.get("directories"):
        result["directory_urls"] = {
            path: siyuan_doc_url(doc_id)
            for path, doc_id in result["directories"].items()
            if doc_id
        }
    return json.dumps(result, ensure_ascii=False)


def _handle_siyuan_get_backlinks(args: dict, **kw) -> str:
    """Get backlinks (inbound references) for a document."""
    import json
    from app.hooks.siyuan_tools import siyuan_get_backlinks
    results = siyuan_get_backlinks(doc_id=args.get("doc_id", ""))
    return json.dumps({"backlinks": results, "count": len(results)}, ensure_ascii=False)


def _handle_siyuan_get_children(args: dict, **kw) -> str:
    """Get child blocks of a block."""
    import json
    from app.hooks.siyuan_tools import siyuan_get_children
    children = siyuan_get_children(block_id=args.get("block_id", ""))
    return json.dumps({"children": children}, ensure_ascii=False)


def _handle_siyuan_get_hpath(args: dict, **kw) -> str:
    """Resolve block ID to human-readable path."""
    import json
    from app.hooks.siyuan_tools import siyuan_get_hpath, siyuan_doc_url
    block_id = args.get("block_id", "")
    hpath = siyuan_get_hpath(block_id=block_id)
    if hpath:
        return json.dumps({"hpath": hpath, "url": siyuan_doc_url(block_id)})
    return json.dumps({"error": "Block not found."})


def _handle_siyuan_remove_doc(args: dict, **kw) -> str:
    """Delete a document permanently."""
    import json
    from app.hooks.siyuan_tools import siyuan_remove_doc
    ok = siyuan_remove_doc(doc_id=args.get("doc_id", ""))
    return json.dumps({"success": ok})


def _handle_siyuan_upsert_page(args: dict, **kw) -> str:
    """Create or update a wiki page by path."""
    import json
    from app.hooks.siyuan_tools import siyuan_upsert_page, siyuan_doc_url
    page_id = siyuan_upsert_page(
        notebook=args.get("notebook", ""),
        path=args.get("path", ""),
        markdown=args.get("markdown", ""),
    )
    if page_id:
        return json.dumps({
            "page_id": page_id,
            "url": siyuan_doc_url(page_id),
            "message": f"Page upserted at {args.get('path', '')}",
        })
    return json.dumps({"error": "Failed to upsert page."})


def _handle_siyuan_lint(args: dict, **kw) -> str:
    """Run wiki quality rules and return a structured report."""
    import json
    from app.hooks.siyuan_tools import siyuan_lint
    report = siyuan_lint(notebook=args.get("notebook", ""))
    return json.dumps(report, ensure_ascii=False)


def _handle_crm_lookup_enriched(args: dict, **kw) -> str:
    """Layer 2 contact lookup enriched with SiYuan person page data.

    After RapidPro returns contact basics, searches the SiYuan wiki for a
    matching /people/ page. If no page exists and admin is looking up the
    contact, auto-creates a stub person page.
    """
    import json
    from app.graph.tools.rapidpro import crm_lookup_contact
    from app.hooks.siyuan_tools import (
        siyuan_search, siyuan_read_doc, siyuan_upsert_page,
        siyuan_set_attrs, siyuan_doc_url,
    )

    # Step 1: Standard RapidPro lookup
    rp_result = crm_lookup_contact(args, **kw)

    # Step 2: Try to find a matching SiYuan person page
    phone = args.get("phone", "").strip().lstrip("+").lstrip("0")
    if not phone:
        return rp_result

    # Try to parse contact name from RapidPro result
    contact_name = ""
    try:
        # RapidPro lookup returns formatted text, try to extract name
        if "👤 *" in rp_result:
            contact_name = rp_result.split("👤 *")[1].split("*")[0].strip()
    except (IndexError, AttributeError):
        pass

    # Step 2: SiYuan enrichment (best-effort — fails gracefully if SiYuan is down)
    try:
        wiki_info = ""
        search_query = contact_name if contact_name and contact_name != "_(no name)_" else phone
        if search_query:
            results = siyuan_search(search_query, limit=3)
            person_page = None
            for r in results:
                hpath = r.get("hpath", "")
                if "/people/" in hpath:
                    person_page = r
                    break

            if person_page:
                doc_id = person_page.get("id", "")
                page_content = siyuan_read_doc(doc_id)
                page_url = siyuan_doc_url(doc_id)
                wiki_info = (
                    f"\n\n📓 *Wiki Page:*\n"
                    f"{page_url}\n"
                    f"{page_content[:300] if page_content else '(empty)'}"
                )
            elif contact_name and contact_name != "_(no name)_":
                # Auto-create a stub person page
                from datetime import date
                today = date.today().isoformat()
                slug = contact_name.lower().replace(" ", "-")
                stub_md = (
                    f"# {contact_name}\n\n"
                    f"📱 whatsapp:{phone}\n\n"
                    f"*Auto-created from CRM lookup on {today}.*\n"
                )
                # Try to create in the first wiki notebook that has /people/
                page_id = siyuan_upsert_page("Life", f"/people/{slug}", stub_md)
                if not page_id:
                    for nb_name in ("Wiki", "IIAB", "PersonalCRM"):
                        page_id = siyuan_upsert_page(nb_name, f"/people/{slug}", stub_md)
                        if page_id:
                            break

                if page_id:
                    siyuan_set_attrs(page_id, {
                        "type": "person",
                        "tags": "auto-created",
                        "last-contact": today,
                        "created": today,
                        "updated": today,
                    })
                    wiki_info = (
                        f"\n\n📓 *Wiki Page Created:*\n"
                        f"{siyuan_doc_url(page_id)}\n"
                        f"_(auto-created stub — add birthday, notes, circle)_"
                    )

        return rp_result + wiki_info
    except Exception:
        # SiYuan unreachable or error — return unenriched result
        logger.debug("SiYuan enrichment skipped (service unavailable or error)")
        return rp_result


def _handle_siyuan_dashboard(args: dict, **kw) -> str:
    """Generate an on-demand dashboard from SiYuan wiki data."""
    import json
    from app.hooks.siyuan_tools import siyuan_dashboard
    result = siyuan_dashboard(
        dashboard_type=args.get("dashboard_type", ""),
        notebook=args.get("notebook", "Life"),
    )
    # Return the formatted dashboard text directly if available
    if "dashboard" in result:
        return result["dashboard"]
    return json.dumps(result, ensure_ascii=False)


# ── Tool registration ───────────────────────────────────────────────────────

_registered = False

def register_all_tools() -> None:
    """Register all V2 native tools globally via hermes_agent."""
    global _registered
    if _registered:
        return

    # RapidPro Tools
    registry.register("fetch_dossier", "rapidpro", schemas.FETCH_DOSSIER, rapidpro.fetch_dossier)
    registry.register("start_flow", "rapidpro", schemas.START_FLOW, rapidpro.start_flow)
    registry.register("start_crm_ops", "rapidpro", schemas.START_CRM_OPS, rapidpro.start_crm_ops)
    registry.register("send_crm_help", "rapidpro", schemas.SEND_CRM_HELP, rapidpro.send_crm_help)

    # Organized (congregation schedule/assignments query API)
    registry.register("organized_schedule", "organized", schemas.ORGANIZED_SCHEDULE, organized.macro_get_schedule)
    registry.register("organized_next_week", "organized", schemas.ORGANIZED_NEXT_WEEK, organized.macro_get_next_week)
    registry.register("organized_my_assignments", "organized", schemas.ORGANIZED_MY_ASSIGNMENTS, organized.macro_get_my_assignments)
    registry.register("organized_search_persons", "organized", schemas.ORGANIZED_SEARCH_PERSONS, organized.macro_search_persons)
    registry.register("organized_visiting_speakers", "organized", schemas.ORGANIZED_VISITING_SPEAKERS, organized.macro_get_visiting_speakers)
    registry.register("organized_events", "organized", schemas.ORGANIZED_EVENTS, organized.macro_get_events)
    registry.register("organized_upcoming_talks", "organized", schemas.ORGANIZED_UPCOMING_TALKS, organized.macro_upcoming_talks)
    registry.register("organized_my_next_talk", "organized", schemas.ORGANIZED_MY_NEXT_TALK, organized.macro_my_next_talk)
    # The retail line: a shop's box on WhatsApp (toolset "retail"; the shop comes from the caller's binding)
    from app.graph.tools import retail as retail_tools
    for _name in retail_tools.READ_TOOLS + retail_tools.CHANGE_TOOLS:
        registry.register(_name, "retail", getattr(schemas, _name.upper()), getattr(retail_tools, _name))
    from app.graph.tools import retail_dialog as retail_dialog_tool
    registry.register("retail_dialog", "retail", schemas.RETAIL_DIALOG, retail_dialog_tool.retail_dialog)

    # ERPNext read-only business view (operator persona only via toolset "erpnext")
    from app.graph.tools import erpnext as erpnext_tools
    registry.register("erpnext_sales_summary", "erpnext", schemas.ERPNEXT_SALES_SUMMARY, erpnext_tools.erpnext_sales_summary)
    registry.register("organized_menu", "organized", schemas.ORGANIZED_MENU, organized.macro_organized_menu)
    registry.register("organized_sources", "organized", schemas.ORGANIZED_SOURCES, organized.macro_get_sources)
    registry.register("organized_field_group", "organized", schemas.ORGANIZED_FIELD_GROUP, organized.macro_get_field_group)
    registry.register("organized_attendance", "organized", schemas.ORGANIZED_ATTENDANCE, organized.macro_get_attendance)
    registry.register("organized_field_report", "organized", schemas.ORGANIZED_FIELD_REPORT, organized.macro_get_field_report)
    registry.register("organized_speakers_congregations", "organized", schemas.ORGANIZED_SPEAKERS_CONGREGATIONS, organized.macro_get_speakers_congregations)
    registry.register("organized_cong_report", "organized", schemas.ORGANIZED_CONG_REPORT, organized.macro_get_cong_report)
    registry.register("organized_branch_report", "organized", schemas.ORGANIZED_BRANCH_REPORT, organized.macro_get_branch_report)
    registry.register("organized_delegated_reports", "organized", schemas.ORGANIZED_DELEGATED_REPORTS, organized.macro_get_delegated_reports)
    registry.register("organized_cong_analysis", "organized", schemas.ORGANIZED_CONG_ANALYSIS, organized.macro_get_cong_analysis)
    registry.register("organized_bible_studies", "organized", schemas.ORGANIZED_BIBLE_STUDIES, organized.macro_get_bible_studies)
    registry.register("organized_notifications", "organized", schemas.ORGANIZED_NOTIFICATIONS, organized.macro_get_notifications)

    # Email delivery (cross-channel: reply by email from any chat session)
    from app.graph.tools import email as email_tools
    registry.register("send_email", "email", schemas.SEND_EMAIL, email_tools.send_email)

    # Team contact sync — authorization is `Team: *` group membership, so this
    # is reachable by every granted tier (see access.TIER_TOOLSETS).
    from app.graph.tools import contact_sync as contact_sync_tools
    registry.register("contact_sync", "contacts", schemas.CONTACT_SYNC, contact_sync_tools.macro_sync)
    # "flow": no persona is given this toolset, so the assistant never sees the
    # menu tool; the tools API admits it as public (it runs as the caller and only
    # ever serves them their own directory).
    registry.register("contact_sync_dialog", "flow", schemas.CONTACT_SYNC_DIALOG, contact_sync_tools.macro_sync_dialog)
    # Operator-only (config toolset): pushes pending updates to every member.
    from app.jobs import contact_sync_notify
    registry.register("contact_sync_push", "config", schemas.CONTACT_SYNC_PUSH, contact_sync_notify.macro_sync_push)
    from app.graph.tools import ict_requests
    registry.register("ict_close", "config", schemas.ICT_CLOSE, ict_requests.macro_ict_close)
    registry.register("contact_check", "config", schemas.CONTACT_CHECK, ict_requests.macro_contact_check)

    # CRM Layer 2 Direct Commands (ADR-011 T2)
    registry.register("crm_list_groups", "rapidpro", schemas.CRM_LIST_GROUPS, rapidpro.crm_list_groups)
    registry.register("crm_lookup_contact", "rapidpro", schemas.CRM_LOOKUP_CONTACT, _handle_crm_lookup_enriched)
    registry.register("crm_org_info", "rapidpro", schemas.CRM_ORG_INFO, rapidpro.crm_org_info)
    registry.register("crm_create_group", "rapidpro", schemas.CRM_CREATE_GROUP, rapidpro.crm_create_group)

    # Mocks Tools
    registry.register("check_stock", "mocks", schemas.CHECK_STOCK, mocks.check_stock)
    registry.register("order_delivery", "mocks", schemas.ORDER_DELIVERY, mocks.order_delivery)
    registry.register("schedule_viewing", "mocks", schemas.SCHEDULE_VIEWING, mocks.schedule_viewing)

    # Forms Tools
    registry.register("submit_form", "forms", schemas.SUBMIT_FORM, forms.submit_form)

    # Upload Tools
    registry.register("upload_jwpub", "upload", schemas.UPLOAD_JWPUB, upload.upload_jwpub)

    # TalkPrep Tools
    registry.register("get_talkprep_help", "talkprep", schemas.GET_TALKPREP_HELP, talkprep.get_talkprep_help)
    registry.register("talkmaster_status", "talkprep", schemas.TALKMASTER_STATUS, talkprep.talkmaster_status)
    registry.register("select_active_talk", "talkprep", schemas.SELECT_ACTIVE_TALK, talkprep.select_active_talk)
    registry.register("list_publications", "talkprep", schemas.LIST_PUBLICATIONS, talkprep.list_publications)
    registry.register("list_topics", "talkprep", schemas.LIST_TOPICS, talkprep.list_topics)
    registry.register("import_talk", "talkprep", schemas.IMPORT_TALK, talkprep.import_talk)
    registry.register("create_revision", "talkprep", schemas.CREATE_REVISION, talkprep.create_revision)
    registry.register("challenge_section", "talkprep", schemas.CHALLENGE_SECTION, talkprep.challenge_section)
    registry.register("develop_section", "talkprep", schemas.DEVELOP_SECTION, talkprep.develop_section)
    registry.register("set_golden_thread", "talkprep", schemas.SET_GOLDEN_THREAD, talkprep.set_golden_thread)
    registry.register("evaluate_talk", "talkprep", schemas.EVALUATE_TALK, talkprep.evaluate_talk)
    registry.register("get_evaluation_scores", "talkprep", schemas.GET_EVALUATION_SCORES, talkprep.get_evaluation_scores)
    registry.register("rehearsal_cue", "talkprep", schemas.REHEARSAL_CUE, talkprep.rehearsal_cue)
    registry.register("rehearse_audio", "talkprep", schemas.REHEARSE_AUDIO, talkprep.rehearse_audio)
    registry.register("export_talk_summary", "talkprep", schemas.EXPORT_TALK_SUMMARY, talkprep.export_talk_summary)
    registry.register("cost_report", "talkprep", schemas.COST_REPORT, talkprep.cost_report)
    registry.register("generate_anki_deck", "talkprep", schemas.GENERATE_ANKI_DECK, talkprep.generate_anki_deck)
    registry.register("push_to_siyuan", "talkprep", schemas.PUSH_TO_SIYUAN, talkprep.push_to_siyuan)

    # MemPalace Tools
    registry.register("search_memory", "mempalace", SEARCH_MEMORY_SCHEMA, search_memory)
    registry.register("store_memory", "mempalace", STORE_MEMORY_SCHEMA, store_memory)
    registry.register("recall_memory", "mempalace", RECALL_MEMORY_SCHEMA, recall_memory)
    registry.register("diary_write", "mempalace", DIARY_WRITE_SCHEMA, diary_write)
    registry.register("diary_read", "mempalace", DIARY_READ_SCHEMA, diary_read)
    registry.register("kg_query", "mempalace", KG_QUERY_SCHEMA, kg_query)
    registry.register("kg_add", "mempalace", KG_ADD_SCHEMA, kg_add)
    registry.register("kg_invalidate", "mempalace", KG_INVALIDATE_SCHEMA, kg_invalidate)
    registry.register("update_memory", "mempalace", UPDATE_MEMORY_SCHEMA, update_memory)
    registry.register("delete_memory", "mempalace", DELETE_MEMORY_SCHEMA, delete_memory)

    # SiYuan Read Tools (close the write-only gap)
    registry.register("siyuan_search", "siyuan", schemas.SIYUAN_SEARCH, _handle_siyuan_search)
    registry.register("siyuan_read", "siyuan", schemas.SIYUAN_READ, _handle_siyuan_read)

    # SiYuan Wiki Tools (LLM Wiki pattern — Karpathy's compounding KB)
    registry.register("siyuan_list_docs", "siyuan", schemas.SIYUAN_LIST_DOCS, _handle_siyuan_list_docs)
    registry.register("siyuan_create_notebook", "siyuan", schemas.SIYUAN_CREATE_NOTEBOOK, _handle_siyuan_create_notebook)
    registry.register("siyuan_create_doc", "siyuan", schemas.SIYUAN_CREATE_DOC, _handle_siyuan_create_doc)
    registry.register("siyuan_update_block", "siyuan", schemas.SIYUAN_UPDATE_BLOCK, _handle_siyuan_update_block)
    registry.register("siyuan_append_block", "siyuan", schemas.SIYUAN_APPEND_BLOCK, _handle_siyuan_append_block)
    registry.register("siyuan_set_attrs", "siyuan", schemas.SIYUAN_SET_ATTRS, _handle_siyuan_set_attrs)
    registry.register("siyuan_get_attrs", "siyuan", schemas.SIYUAN_GET_ATTRS, _handle_siyuan_get_attrs)
    registry.register("siyuan_sql_query", "siyuan", schemas.SIYUAN_SQL_QUERY, _handle_siyuan_sql_query)
    registry.register("siyuan_delete_block", "siyuan", schemas.SIYUAN_DELETE_BLOCK, _handle_siyuan_delete_block)
    registry.register("siyuan_rename_doc", "siyuan", schemas.SIYUAN_RENAME_DOC, _handle_siyuan_rename_doc)
    registry.register("siyuan_init_wiki", "siyuan", schemas.SIYUAN_INIT_WIKI, _handle_siyuan_init_wiki)

    # SiYuan Wiki Navigation & Maintenance (gap-closing)
    registry.register("siyuan_get_backlinks", "siyuan", schemas.SIYUAN_GET_BACKLINKS, _handle_siyuan_get_backlinks)
    registry.register("siyuan_get_children", "siyuan", schemas.SIYUAN_GET_CHILDREN, _handle_siyuan_get_children)
    registry.register("siyuan_get_hpath", "siyuan", schemas.SIYUAN_GET_HPATH, _handle_siyuan_get_hpath)
    registry.register("siyuan_remove_doc", "siyuan", schemas.SIYUAN_REMOVE_DOC, _handle_siyuan_remove_doc)
    registry.register("siyuan_upsert_page", "siyuan", schemas.SIYUAN_UPSERT_PAGE, _handle_siyuan_upsert_page)
    registry.register("siyuan_lint", "siyuan", schemas.SIYUAN_LINT, _handle_siyuan_lint)
    registry.register("siyuan_dashboard", "siyuan", schemas.SIYUAN_DASHBOARD, _handle_siyuan_dashboard)

    # System Operations (ADR-011 migration)
    registry.register("macro_reset", "system", schemas.MACRO_RESET, system.macro_reset)
    registry.register("macro_debug", "system", schemas.MACRO_DEBUG, system.macro_debug)
    registry.register("macro_noai", "system", schemas.MACRO_NOAI, system.macro_noai)
    registry.register("macro_noai_global", "system", schemas.MACRO_NOAI_GLOBAL, system.macro_noai_global)
    registry.register("macro_noai_status", "system", schemas.MACRO_NOAI_STATUS, system.macro_noai_status)
    registry.register("macro_enableai", "system", schemas.MACRO_ENABLEAI, system.macro_enableai)
    registry.register("macro_enableai_global", "system", schemas.MACRO_ENABLEAI_GLOBAL, system.macro_enableai_global)
    registry.register("macro_reload", "system", schemas.MACRO_RELOAD, system.macro_reload)
    registry.register("macro_health", "system", schemas.MACRO_HEALTH, system.macro_health)
    registry.register("macro_skills", "system", schemas.MACRO_SKILLS, system.macro_skills)
    registry.register("macro_flow", "system", schemas.MACRO_FLOW, system.macro_flow)

    # Config Operations (ADR-011 migration)
    registry.register("macro_persona", "config", schemas.MACRO_PERSONA, config.macro_persona)
    registry.register("macro_channel", "config", schemas.MACRO_CHANNEL, config.macro_channel)
    registry.register("macro_elevate", "config", schemas.MACRO_ELEVATE, config.macro_elevate)
    from app.graph.tools import access as access_tools
    registry.register("macro_access", "config", schemas.MACRO_ACCESS, access_tools.macro_access)
    registry.register("macro_global", "config", schemas.MACRO_GLOBAL, config.macro_global)
    registry.register("macro_label", "config", schemas.MACRO_LABEL, config.macro_label)

    # Capture — operator-only one-line jots to the right sink (toolset never
    # granted to a tier; only the operator's assistant persona carries it).
    registry.register("macro_capture", "capture", schemas.MACRO_CAPTURE, capture.macro_capture)

    # People — shepherd view over the operator's `moun:` jots (read-only,
    # same operator-only capture toolset).
    from app.graph.tools import people as people_tools
    registry.register("macro_people", "capture", schemas.MACRO_PEOPLE, people_tools.macro_people)

    # Settings — operator tunes their own cockpit from chat (config toolset,
    # never granted to a tier).
    from app.graph.tools import settings as settings_tools
    registry.register("macro_settings", "config", schemas.MACRO_SETTINGS, settings_tools.macro_settings)
    # The Settings list on WhatsApp (flow-called, operator only; checks its caller itself too)
    registry.register("settings_dialog", "config", schemas.SETTINGS_DIALOG, settings_tools.settings_dialog)

    # Social-Code Simulation Tools (ADR-014)
    registry.register("sim_update_mood", "social", social_schemas.SIM_UPDATE_MOOD, social.sim_update_mood)
    registry.register("sim_update_trust", "social", social_schemas.SIM_UPDATE_TRUST, social.sim_update_trust)
    registry.register("sim_update_dossier", "social", social_schemas.SIM_UPDATE_DOSSIER, social.sim_update_dossier)
    registry.register("sim_assess_boredom", "social", social_schemas.SIM_ASSESS_BOREDOM, social.sim_assess_boredom)
    registry.register("sim_trigger_distraction", "social", social_schemas.SIM_TRIGGER_DISTRACTION, social.sim_trigger_distraction)
    registry.register("sim_grade_response", "social", social_schemas.SIM_GRADE_RESPONSE, social.sim_grade_response)
    registry.register("sim_get_scenario", "social", social_schemas.SIM_GET_SCENARIO, social.sim_get_scenario)
    registry.register("sim_drill_grade", "social", social_schemas.SIM_DRILL_GRADE, social.sim_drill_grade)
    registry.register("sim_freetext", "social", social_schemas.SIM_FREETEXT, social.sim_freetext)
    registry.register("sim_set_language", "social", social_schemas.SIM_SET_LANGUAGE, social.sim_set_language)
    registry.register("sim_session_summary", "social", social_schemas.SIM_SESSION_SUMMARY, social.sim_session_summary)
    registry.register("sim_toggle_ai", "social", social_schemas.SIM_TOGGLE_AI, social.sim_toggle_ai)

    _registered = True
    logger.info("Registered 78 native Hermes-compatible tools globally.")

    # Confirm before change: a model turn may only *propose* a change; the person's
    # Wi/Non tap runs it. Flows and matched verbs (the tools API) are not held.
    from app.graph.tools.confirm import guard_registry
    logger.info(f"Confirm-before-change guards {guard_registry(registry)} change tools.")

def get_hermes_tools() -> dict:
    """Return the global registry dict if anything needs to introspect it."""
    if not _registered:
        register_all_tools()
    return registry._tools
