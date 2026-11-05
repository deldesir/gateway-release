"""
SiYuan wiki tools — read/write to SiYuan Note for persistent knowledge.

SiYuan is a local-first note-taking server running alongside IIAB.
These tools allow Hermes to:
  1. Look up existing wiki pages (notebook/page resolution)
  2. Create or update pages (upsert semantics)
  3. Append content blocks to existing pages

Authentication uses SiYuan's accessAuthCode feature (token-based).
All requests go to the local SiYuan instance via HTTP API.
"""

import logging
import os
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_SIYUAN_URL = os.getenv("SIYUAN_API_URL", "http://localhost:6806")
_SIYUAN_AUTH_CODE = os.getenv("SIYUAN_ACCESS_AUTH_CODE", "")
# Public-facing SiYuan URL for deep-links in WhatsApp messages.
# Set to the externally-reachable URL, e.g., "http://box.lan/siyuan"
_SIYUAN_WEB_URL = os.getenv("SIYUAN_WEB_URL", "http://box.lan/siyuan")

# Notebook ID cache: {notebook_name: notebook_id}
_notebook_map: Dict[str, str] = {}

# Persistent HTTP client with session cookie for SiYuan auth.
# SiYuan 3.6.x uses session-based auth: login via /api/system/loginAuth
# with the access auth code, then use the session cookie for all requests.
# Header-based "Authorization: Token <code>" is NOT supported.
_client: Optional[httpx.Client] = None
_auth_ok: bool = False


def siyuan_doc_url(block_id: str) -> str:
    """Build a clickable deep-link URL for a SiYuan block/document.

    Returns a URL like: http://box.lan/siyuan/stage/#blocks/20260429...
    The user can click this in WhatsApp to jump directly to the page.
    """
    return f"{_SIYUAN_WEB_URL}/stage/#blocks/{block_id}"


def _get_client() -> httpx.Client:
    """Return a persistent httpx.Client, authenticating on first use."""
    global _client, _auth_ok

    if _client is None:
        _client = httpx.Client(timeout=10)

    if not _auth_ok and _SIYUAN_AUTH_CODE:
        try:
            resp = _client.post(
                f"{_SIYUAN_URL}/api/system/loginAuth",
                json={"authCode": _SIYUAN_AUTH_CODE},
            )
            data = resp.json()
            if data.get("code") == 0:
                _auth_ok = True
                logger.info("SiYuan session auth OK")
            else:
                logger.warning(f"SiYuan login failed: {data.get('msg')}")
        except Exception as e:
            logger.error(f"SiYuan login error: {e}")

    return _client


# ── HTTP client ──────────────────────────────────────────────────────────────

def _siyuan_request(endpoint: str, payload: dict, quiet: bool = False) -> dict:
    """
    Make a synchronous POST request to SiYuan's HTTP API.

    SiYuan's API is JSON-RPC-style: POST to /api/<endpoint> with a JSON body.
    Auth is via session cookie obtained from /api/system/loginAuth.

    quiet: existence probes expect misses ("block not found"); log those at
    debug so nightly renders don't fill the journal with warnings for pages
    that are about to be created.
    """
    url = f"{_SIYUAN_URL}/api/{endpoint}"
    client = _get_client()

    try:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            log = logger.debug if quiet else logger.warning
            log(f"SiYuan API error: {data.get('msg')} (endpoint={endpoint})")
        return data
    except httpx.HTTPError as e:
        logger.error(f"SiYuan request failed: {endpoint} → {e}")
        return {"code": -1, "msg": str(e), "data": None}


# ── Health ───────────────────────────────────────────────────────────────────

def siyuan_health() -> Dict[str, object]:
    """Actively verify SiYuan auth + version (a bare 200 on /api/system/version
    can succeed even when auth is broken, hiding a dead integration).

    Returns:
        {auth_ok, kernel_version, prerelease, notebooks, error}
    """
    global _auth_ok
    info: Dict[str, object] = {
        "auth_ok": False,
        "kernel_version": "",
        "prerelease": False,
        "notebooks": 0,
        "error": "",
    }
    if not _SIYUAN_URL:
        info["error"] = "SIYUAN_API_URL not set"
        return info

    # Force a fresh login so a stale/expired session can't mask a failure.
    _auth_ok = False
    try:
        client = _get_client()
        if _SIYUAN_AUTH_CODE and not _auth_ok:
            info["error"] = "loginAuth failed (check SIYUAN_ACCESS_AUTH_CODE)"
            return info

        resp = client.post(f"{_SIYUAN_URL}/api/system/version", json={})
        data = resp.json()
        if data.get("code") != 0:
            info["error"] = f"version check failed: {data.get('msg')}"
            return info
        version = str(data.get("data") or "")

        nb = client.post(f"{_SIYUAN_URL}/api/notebook/lsNotebooks", json={}).json()
        if nb.get("code") != 0:
            info["error"] = f"auth rejected: {nb.get('msg')}"
            info["kernel_version"] = version
            return info

        info.update(
            auth_ok=True,
            kernel_version=version,
            prerelease=any(tag in version for tag in ("-rc", "-alpha", "-beta", "-dev")),
            notebooks=len((nb.get("data") or {}).get("notebooks", [])),
        )
    except Exception as e:
        info["error"] = str(e)
    return info


# ── Notebook resolution ─────────────────────────────────────────────────────

def _init_notebook_map() -> None:
    """
    Populate the notebook name→ID cache from SiYuan.

    Called once during app lifespan startup. If SiYuan is unreachable,
    the cache stays empty and wiki operations gracefully no-op.
    """
    global _notebook_map

    if not _SIYUAN_URL:
        logger.info("SiYuan disabled (no SIYUAN_API_URL)")
        return

    result = _siyuan_request("notebook/lsNotebooks", {})
    notebooks = (result.get("data") or {}).get("notebooks", [])

    _notebook_map = {nb["name"]: nb["id"] for nb in notebooks if not nb.get("closed")}
    logger.info(f"SiYuan: {len(_notebook_map)} notebooks cached: {list(_notebook_map.keys())}")


def get_notebook_id(name: str) -> Optional[str]:
    """
    Get notebook ID by name, with lazy init.

    Returns None if the notebook doesn't exist or SiYuan is unreachable.
    """
    if not _notebook_map:
        _init_notebook_map()
    return _notebook_map.get(name)


# ── Page operations ──────────────────────────────────────────────────────────

def siyuan_upsert_page(
    notebook: str,
    path: str,
    markdown: str,
) -> Optional[str]:
    """
    Create or update a SiYuan page.

    Args:
        notebook: Notebook name (resolved to ID internally)
        path: Page path within the notebook (e.g. "/Daily/2024-01-15")
        markdown: Markdown content for the page

    Returns:
        Page block ID if successful, None otherwise
    """
    nb_id = get_notebook_id(notebook)
    if not nb_id:
        logger.warning(f"Notebook '{notebook}' not found — skipping upsert")
        return None

    # Check if page exists (a miss here is normal — we create the page below).
    # Must resolve by human-readable hpath: getHPathByPath expects ID-based
    # file paths, so probing it with an hpath always missed and every changed
    # write minted a duplicate doc at the same path.
    search_result = _siyuan_request("filetree/getIDsByHPath", {
        "notebook": nb_id,
        "path": path,
    }, quiet=True)
    block_ids = search_result.get("data") or []
    if block_ids:
        block_id = block_ids[0]
        _siyuan_request("block/updateBlock", {
            "id": block_id,
            "dataType": "markdown",
            "data": markdown,
        })
        logger.debug(f"SiYuan: updated page {notebook}{path}")
        return block_id

    # Page doesn't exist — create it
    result = _siyuan_request("filetree/createDocWithMd", {
        "notebook": nb_id,
        "path": path,
        "markdown": markdown,
    })

    page_id = result.get("data")
    if page_id:
        logger.debug(f"SiYuan: created page {notebook}{path} → {page_id}")
    return page_id


def siyuan_append_block(
    notebook: str,
    path: str,
    markdown: str,
) -> Optional[str]:
    """
    Append a markdown block to an existing SiYuan page.

    If the page doesn't exist, creates it first.

    Args:
        notebook: Notebook name
        path: Page path within the notebook
        markdown: Markdown content to append

    Returns:
        Block ID of the appended content, or None on failure
    """
    nb_id = get_notebook_id(notebook)
    if not nb_id:
        logger.warning(f"Notebook '{notebook}' not found — skipping append")
        return None

    # Find the page's root block ID
    search_result = _siyuan_request("filetree/getIDsByHPath", {
        "notebook": nb_id,
        "path": path,
    })
    block_ids = search_result.get("data", [])

    if not block_ids:
        # Page doesn't exist — create it with the content
        return siyuan_upsert_page(notebook, path, markdown)

    # Append to existing page
    parent_id = block_ids[0]
    result = _siyuan_request("block/appendBlock", {
        "parentID": parent_id,
        "dataType": "markdown",
        "data": markdown,
    })

    block_data = result.get("data")
    if block_data and isinstance(block_data, list) and block_data:
        block_id = block_data[0].get("doOperations", [{}])[0].get("id")
        logger.debug(f"SiYuan: appended block to {notebook}{path} → {block_id}")
        return block_id

    return None


def siyuan_create_daily_note(notebook: str) -> Optional[str]:
    """Resolve (creating if needed) TODAY's daily note in a notebook.

    filetree/createDailyNote is idempotent per day — calling it for an
    existing note returns the same document. Some kernel versions omit the
    id in the response, and the SQL index that could recover it lags writes
    by a few seconds, so fall back to polling the index for the
    custom-dailynote-YYYYMMDD attribute the kernel stamps on daily notes.

    Returns the daily-note document id, or None when SiYuan is unreachable.
    """
    import time
    from datetime import datetime

    nb_id = get_notebook_id(notebook)
    if not nb_id:
        logger.warning(f"Notebook '{notebook}' not found — no daily note")
        return None
    result = _siyuan_request("filetree/createDailyNote", {"notebook": nb_id})
    data = result.get("data") or {}
    if isinstance(data, dict) and data.get("id"):
        return data["id"]
    stamp = datetime.now().strftime("%Y%m%d")
    stmt = (
        "SELECT id FROM blocks WHERE type='d' AND box='{nb}' "
        "AND ial LIKE '%custom-dailynote-{stamp}%' LIMIT 1"
    ).format(nb=nb_id, stamp=stamp)
    for attempt in range(3):
        rows = _siyuan_request("query/sql", {"stmt": stmt}).get("data") or []
        if rows:
            return rows[0]["id"]
        if attempt < 2:
            time.sleep(3)
    logger.warning(f"Daily note for {notebook} not resolvable (index lag?)")
    return None


def _block_op_id(result: dict) -> Optional[str]:
    """Extract the new block id from an insert/prepend/append response."""
    block_data = result.get("data")
    if block_data and isinstance(block_data, list) and block_data:
        return block_data[0].get("doOperations", [{}])[0].get("id")
    return None


def siyuan_prepend_block(parent_id: str, markdown: str) -> Optional[str]:
    """Prepend a markdown block inside a parent block/document."""
    result = _siyuan_request("block/prependBlock", {
        "parentID": parent_id,
        "dataType": "markdown",
        "data": markdown,
    })
    return _block_op_id(result)


def siyuan_insert_block_after(previous_id: str, markdown: str) -> Optional[str]:
    """Insert a markdown block immediately after an existing block."""
    result = _siyuan_request("block/insertBlock", {
        "previousID": previous_id,
        "dataType": "markdown",
        "data": markdown,
    })
    return _block_op_id(result)


# ── Read operations ──────────────────────────────────────────────────────────

def siyuan_search(query: str, notebook: str = None, limit: int = 5) -> list:
    """
    Full-text search across SiYuan blocks.

    Args:
        query: Search query text
        notebook: Optional notebook name to scope search
        limit: Maximum results to return (default 5)

    Returns:
        List of result dicts with id, content preview, and path
    """
    payload = {"query": query}

    result = _siyuan_request("search/fullTextSearchBlock", payload)
    blocks = (result.get("data") or {}).get("blocks", [])

    # Filter by notebook if specified
    if notebook:
        nb_id = get_notebook_id(notebook)
        if nb_id:
            blocks = [b for b in blocks if b.get("box") == nb_id]

    # Return top N results, truncated
    results = []
    for b in blocks[:limit]:
        content = b.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."
        results.append({
            "id": b.get("id", ""),
            "content": content,
            "path": b.get("hPath", b.get("path", "")),
            "notebook": b.get("box", ""),
        })

    return results


def siyuan_read_doc(doc_id: str, full: bool = False) -> Optional[str]:
    """
    Read a SiYuan document's markdown content by block ID.

    Uses the export API which returns clean markdown regardless of
    how the document was authored internally.

    Args:
        doc_id: Block ID of the document root
        full: return the COMPLETE document. The default truncates at 8000
              chars for LLM-tool safety — any read-modify-WRITE caller must
              pass full=True or it will bake the truncation (and its marker)
              back into the document.

    Returns:
        Markdown content string, or None if not found
    """
    result = _siyuan_request("export/exportMdContent", {"id": doc_id})

    if result.get("code") != 0:
        logger.warning(f"SiYuan read failed: {result.get('msg')}")
        return None

    content = (result.get("data") or {}).get("content", "")

    # Truncate to prevent blowing the context window (LLM tool calls only)
    if not full and len(content) > 8000:
        content = content[:8000] + "\n\n...(truncated — document exceeds 8000 chars)"

    return content


# ── Document tree operations ─────────────────────────────────────────────────

def siyuan_list_docs(notebook: str, path: str = "/") -> list:
    """
    List documents in a notebook path.

    Args:
        notebook: Notebook name (resolved to ID internally)
        path: Path within the notebook (default "/" for root)

    Returns:
        List of dicts with id, name, and subFileCount
    """
    nb_id = get_notebook_id(notebook)
    if not nb_id:
        return []

    result = _siyuan_request("filetree/listDocsByPath", {
        "notebook": nb_id,
        "path": path,
    })
    files = (result.get("data") or {}).get("files", [])
    return [
        {
            "id": f.get("id", ""),
            "name": f.get("name", "").replace(".sy", ""),
            "subFileCount": f.get("subFileCount", 0),
        }
        for f in files
    ]


def siyuan_create_notebook(name: str) -> Optional[str]:
    """
    Create a new SiYuan notebook.

    Args:
        name: Human-readable notebook name

    Returns:
        Notebook ID if successful, None otherwise
    """
    global _notebook_map
    result = _siyuan_request("notebook/createNotebook", {"name": name})
    nb = (result.get("data") or {}).get("notebook", {})
    nb_id = nb.get("id")
    if nb_id:
        _notebook_map[name] = nb_id
        logger.info(f"SiYuan: created notebook '{name}' → {nb_id}")
    return nb_id


def siyuan_create_doc(notebook: str, path: str, markdown: str) -> Optional[str]:
    """
    Create a new SiYuan document with markdown content.

    Unlike siyuan_upsert_page, this always creates (fails if path exists).

    Args:
        notebook: Notebook name
        path: Document path (e.g. "/entities/transformer-architecture")
        markdown: Markdown content

    Returns:
        Document block ID if successful, None otherwise
    """
    nb_id = get_notebook_id(notebook)
    if not nb_id:
        logger.warning(f"Notebook '{notebook}' not found — skipping create")
        return None

    result = _siyuan_request("filetree/createDocWithMd", {
        "notebook": nb_id,
        "path": path,
        "markdown": markdown,
    })
    doc_id = result.get("data")
    if doc_id:
        logger.debug(f"SiYuan: created doc {notebook}{path} → {doc_id}")
    return doc_id


def siyuan_update_block(block_id: str, markdown: str) -> bool:
    """
    Replace a block's content with new markdown.

    Args:
        block_id: ID of the block to update
        markdown: New markdown content

    Returns:
        True if successful
    """
    result = _siyuan_request("block/updateBlock", {
        "id": block_id,
        "dataType": "markdown",
        "data": markdown,
    })
    ok = result.get("code") == 0
    if ok:
        logger.debug(f"SiYuan: updated block {block_id}")
    return ok


def siyuan_set_attrs(block_id: str, attrs: Dict[str, str]) -> bool:
    """
    Set custom attributes on a block.

    SiYuan requires custom attributes to be prefixed with 'custom-'.
    This function auto-prefixes keys that don't already have the prefix.

    Args:
        block_id: Block ID
        attrs: Dict of attribute key→value pairs

    Returns:
        True if successful
    """
    # Auto-prefix custom- for convenience
    prefixed = {}
    for k, v in attrs.items():
        key = k if k.startswith("custom-") else f"custom-{k}"
        prefixed[key] = str(v)

    result = _siyuan_request("attr/setBlockAttrs", {
        "id": block_id,
        "attrs": prefixed,
    })
    ok = result.get("code") == 0
    if ok:
        logger.debug(f"SiYuan: set {len(prefixed)} attrs on {block_id}")
    return ok


def siyuan_get_attrs(block_id: str) -> Dict[str, str]:
    """
    Get all attributes of a block.

    Args:
        block_id: Block ID

    Returns:
        Dict of attribute key→value pairs
    """
    result = _siyuan_request("attr/getBlockAttrs", {"id": block_id})
    return result.get("data") or {}


def siyuan_sql_query(sql: str) -> list:
    """
    Execute a read-only SQL query against SiYuan's block database.

    SECURITY: Only SELECT statements are allowed. Any other statement
    type (INSERT, UPDATE, DELETE, DROP, ALTER, etc.) is rejected.

    Args:
        sql: SQL SELECT statement

    Returns:
        List of result rows (dicts), or error message
    """
    # Validate: must be a SELECT statement
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        logger.warning(f"SiYuan SQL rejected (non-SELECT): {sql[:80]}")
        return [{"error": "Only SELECT statements are allowed."}]

    # Extra guard: reject dangerous keywords even inside SELECT
    _DANGEROUS = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "ATTACH"}
    tokens = set(stripped.split())
    if tokens & _DANGEROUS:
        logger.warning(f"SiYuan SQL rejected (dangerous keyword): {sql[:80]}")
        return [{"error": "Statement contains disallowed keywords."}]

    result = _siyuan_request("query/sql", {"stmt": sql})
    data = result.get("data")
    if data is None:
        return [{"error": result.get("msg", "Query failed")}]

    # Truncate large result sets
    if isinstance(data, list) and len(data) > 50:
        data = data[:50]
        data.append({"_truncated": f"Results capped at 50 rows"})

    return data


def siyuan_delete_block(block_id: str) -> bool:
    """
    Delete a block by ID.

    Args:
        block_id: Block ID to delete

    Returns:
        True if successful
    """
    result = _siyuan_request("block/deleteBlock", {"id": block_id})
    ok = result.get("code") == 0
    if ok:
        logger.debug(f"SiYuan: deleted block {block_id}")
    return ok


def siyuan_rename_doc(doc_id: str, title: str) -> bool:
    """
    Rename a document.

    Args:
        doc_id: Document root block ID
        title: New title

    Returns:
        True if successful
    """
    result = _siyuan_request("filetree/renameDocByID", {
        "id": doc_id,
        "title": title,
    })
    ok = result.get("code") == 0
    if ok:
        logger.debug(f"SiYuan: renamed doc {doc_id} → '{title}'")
    return ok


# ── Wiki navigation & maintenance ────────────────────────────────────────────

def siyuan_get_backlinks(doc_id: str) -> list:
    """
    Get all blocks that reference a given document (backlinks).

    Queries the SiYuan `refs` table to find inbound references.
    Essential for wiki lint: pages with zero backlinks are orphans.

    Args:
        doc_id: Document root block ID to find backlinks for

    Returns:
        List of dicts with source block info (id, content, path)
    """
    sql = (
        f"SELECT r.block_id, r.content, r.path, b.root_id "
        f"FROM refs r "
        f"LEFT JOIN blocks b ON r.block_id = b.id "
        f"WHERE r.def_block_root_id = '{doc_id}' "
        f"LIMIT 50"
    )
    result = _siyuan_request("query/sql", {"stmt": sql})
    data = result.get("data")
    if not data:
        return []

    return [
        {
            "block_id": row.get("block_id", ""),
            "content": (row.get("content", "") or "")[:200],
            "path": row.get("path", ""),
            "root_id": row.get("root_id", ""),
        }
        for row in data
    ]


def siyuan_get_children(block_id: str) -> list:
    """
    Get child blocks of a given block.

    Returns the immediate children with their types, useful for
    navigating the block tree to find specific sections for targeted
    updates (e.g., finding the "Entities" heading in the Index doc).

    Args:
        block_id: Parent block ID (usually a document root)

    Returns:
        List of dicts with id, type, and subType
    """
    result = _siyuan_request("block/getChildBlocks", {"id": block_id})
    data = result.get("data")
    if not data:
        return []

    return [
        {
            "id": child.get("id", ""),
            "type": child.get("type", ""),
            "subType": child.get("subType", ""),
        }
        for child in data
    ]


def siyuan_get_hpath(block_id: str) -> Optional[str]:
    """
    Get the human-readable path for a block ID.

    Converts opaque IDs like "20260429041444-wppl57p" to readable paths
    like "/entities/kubernetes".

    Args:
        block_id: Block ID

    Returns:
        Human-readable path string, or None if not found
    """
    result = _siyuan_request("filetree/getHPathByID", {"id": block_id})
    if result.get("code") != 0:
        return None
    return result.get("data")


def siyuan_remove_doc(doc_id: str) -> bool:
    """
    Delete a document permanently.

    Use for archiving superseded wiki pages. This is irreversible.

    Args:
        doc_id: Document root block ID

    Returns:
        True if successful
    """
    result = _siyuan_request("filetree/removeDocByID", {"id": doc_id})
    ok = result.get("code") == 0
    if ok:
        logger.info(f"SiYuan: removed document {doc_id}")
    return ok


def siyuan_lint(notebook: str) -> Dict[str, Any]:
    """
    Run 10 wiki quality rules against a SiYuan notebook.

    Returns a structured lint report with errors, warnings, and info items.

    Args:
        notebook: Notebook name (resolved to ID internally)

    Returns:
        Dict with errors, warnings, info lists, and a summary string.
    """
    from datetime import date, timedelta

    nb_id = get_notebook_id(notebook)
    if not nb_id:
        return {"error": f"Notebook '{notebook}' not found"}

    errors = []
    warnings = []
    info = []

    today = date.today()
    stale_cutoff = (today - timedelta(days=90)).isoformat()

    # ── Rule 2: broken-ref (error) ─────────────────────────────────────
    broken_refs = siyuan_sql_query(
        f"SELECT r.block_id, r.def_block_id FROM refs r "
        f"LEFT JOIN blocks b ON r.def_block_id = b.id "
        f"WHERE r.box = '{nb_id}' AND b.id IS NULL"
    )
    for ref in broken_refs:
        errors.append({
            "rule": "broken-ref",
            "block_id": ref.get("block_id", ""),
            "detail": f"Reference to deleted block {ref.get('def_block_id', '?')}",
        })

    # ── Rule 4: duplicate-title (error) ────────────────────────────────
    dupe_rows = siyuan_sql_query(
        f"SELECT content, COUNT(*) as cnt FROM blocks "
        f"WHERE type='d' AND box='{nb_id}' "
        f"GROUP BY LOWER(content) HAVING cnt > 1"
    )
    for row in dupe_rows:
        errors.append({
            "rule": "duplicate-title",
            "detail": f'"{row.get("content", "?")}" appears {row.get("cnt", "?")} times',
        })

    # ── Rule 6: low-confidence (warning) ───────────────────────────────
    low_conf = siyuan_sql_query(
        f"SELECT b.id, b.content, a.value FROM blocks b "
        f"JOIN attributes a ON b.id = a.block_id "
        f"WHERE a.name = 'custom-confidence' AND CAST(a.value AS REAL) < 0.5 "
        f"AND b.box = '{nb_id}'"
    )
    for row in low_conf:
        warnings.append({
            "rule": "low-confidence",
            "block_id": row.get("id", ""),
            "detail": f'"{row.get("content", "?")}" has confidence {row.get("value", "?")}',
        })

    # ── Rule 7: contradicted-page (warning) ────────────────────────────
    contradicted = siyuan_sql_query(
        f"SELECT b.id, b.content, a.value FROM blocks b "
        f"JOIN attributes a ON b.id = a.block_id "
        f"WHERE a.name = 'custom-contradicted-by' AND a.value != '' "
        f"AND b.box = '{nb_id}'"
    )
    for row in contradicted:
        warnings.append({
            "rule": "contradicted-page",
            "block_id": row.get("id", ""),
            "detail": f'"{row.get("content", "?")}" contradicted by {row.get("value", "?")}',
        })

    # ── Rules 1,3,5,8,9,10: iterate documents ─────────────────────────
    all_docs = siyuan_sql_query(
        f"SELECT id, content, hpath FROM blocks "
        f"WHERE type='d' AND box='{nb_id}' ORDER BY hpath"
    )

    # Skip meta and directory-level docs for certain rules
    skip_titles = {"Schema", "Index", "Log"}
    skip_hpath_prefixes = ("/raw/",)

    # Cross-link minimum thresholds (from Schema)
    min_links = {
        "entity": 2, "person": 1, "concept": 1, "synthesis": 3,
        "overview": 5, "summary": 1, "habit": 1, "goal": 2,
        "query": 1, "journal": 0, "raw": 0,
    }

    # Build index content set for Rule 10
    index_doc = None
    for doc in all_docs:
        if doc.get("content") == "Index":
            index_doc = doc
            break
    index_content = ""
    if index_doc:
        index_md = siyuan_read_doc(index_doc["id"])
        if index_md:
            index_content = index_md

    for doc in all_docs:
        doc_id = doc.get("id", "")
        title = doc.get("content", "")
        hpath = doc.get("hpath", "")
        attrs = None  # Reset per-doc to prevent leaking between iterations

        # Skip Schema, Index, Log, and raw/ docs for orphan/attr checks
        is_meta = title in skip_titles
        is_raw = any(hpath.startswith(p) for p in skip_hpath_prefixes)
        is_dir_stub = hpath.count("/") == 1 and title.lower() in (
            "entities", "concepts", "syntheses", "summaries", "journal",
            "people", "circles", "habits", "goals", "queries",
            "raw", "articles", "papers", "transcripts",
        )

        if is_meta or is_dir_stub:
            continue

        # Fetch attrs once for non-raw docs (used by rules 3, 8, 9)
        if not is_raw:
            attrs = siyuan_get_attrs(doc_id)

        # Rule 1: orphan-page
        if not is_raw:
            backlinks = siyuan_get_backlinks(doc_id)
            if len(backlinks) == 0:
                warnings.append({
                    "rule": "orphan-page",
                    "block_id": doc_id,
                    "detail": f'"{title}" has 0 inbound links',
                })

        # Rule 3: missing-attrs
        if attrs is not None:
            required = ["custom-type", "custom-tags", "custom-created", "custom-updated"]
            missing = [k for k in required if k not in attrs or not attrs[k]]
            if missing:
                warnings.append({
                    "rule": "missing-attrs",
                    "block_id": doc_id,
                    "detail": f'"{title}" missing: {", ".join(missing)}',
                })

        # Rule 5: empty-page
        body = siyuan_read_doc(doc_id)
        if body and len(body.strip()) < 50:
            warnings.append({
                "rule": "empty-page",
                "block_id": doc_id,
                "detail": f'"{title}" body is only {len(body.strip())} chars',
            })

        # Rule 8: stale-page
        if attrs is not None:
            updated = attrs.get("custom-updated", "")
            if updated and updated < stale_cutoff:
                info.append({
                    "rule": "stale-page",
                    "block_id": doc_id,
                    "detail": f'"{title}" last updated {updated}',
                })

        # Rule 9: cross-link-minimum
        if attrs is not None:
            page_type = attrs.get("custom-type", "")
            if page_type in min_links:
                required_links = min_links[page_type]
                if required_links > 0:
                    ref_count_rows = siyuan_sql_query(
                        f"SELECT COUNT(*) as cnt FROM refs "
                        f"WHERE block_id = '{doc_id}' OR def_block_id = '{doc_id}'"
                    )
                    actual = ref_count_rows[0].get("cnt", 0) if ref_count_rows else 0
                    if actual < required_links:
                        warnings.append({
                            "rule": "cross-link-minimum",
                            "block_id": doc_id,
                            "detail": f'"{title}" ({page_type}) has {actual} links, needs {required_links}',
                        })

        # Rule 10: index-completeness
        if not is_meta and not is_dir_stub and index_content:
            if doc_id not in index_content and title not in index_content:
                info.append({
                    "rule": "index-completeness",
                    "block_id": doc_id,
                    "detail": f'"{title}" not listed in Index',
                })

    summary = (
        f"## Lint Report — {today.isoformat()}\n"
        f"Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(info)}"
    )

    logger.info(f"SiYuan lint: {notebook} → {len(errors)}E {len(warnings)}W {len(info)}I")
    return {
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "summary": summary,
        "total": len(errors) + len(warnings) + len(info),
    }


def siyuan_dashboard(dashboard_type: str, notebook: str = "Life") -> Dict[str, Any]:
    """
    Generate an on-demand dashboard view from SiYuan wiki data.

    Supported types:
      - crm_health: People directory stats, contact freshness
      - recent_contacts: People sorted by last-contact date
      - wiki_quality: Formatted lint report
      - habits: Active habits, streaks, compliance
      - goals: Goal status breakdown

    Args:
        dashboard_type: One of "crm_health", "recent_contacts",
                        "wiki_quality", "habits", "goals"
        notebook: Notebook name (default "Life")

    Returns:
        Dict with "dashboard" (formatted markdown string) and "data" (raw dict).
    """
    from datetime import date, timedelta

    nb_id = get_notebook_id(notebook)
    if not nb_id:
        return {"error": f"Notebook '{notebook}' not found"}

    today = date.today()
    generators = {
        "crm_health": lambda: _dashboard_crm_health(nb_id, today),
        "recent_contacts": lambda: _dashboard_recent_contacts(nb_id, today),
        "wiki_quality": lambda: _dashboard_wiki_quality(notebook),
        "habits": lambda: _dashboard_habits(nb_id, today),
        "goals": lambda: _dashboard_goals(nb_id, today),
    }

    gen = generators.get(dashboard_type)
    if not gen:
        return {
            "error": f"Unknown dashboard type '{dashboard_type}'",
            "available": list(generators.keys()),
        }

    result = gen()
    logger.info(f"SiYuan dashboard: {dashboard_type} for {notebook}")
    return result


def _dashboard_crm_health(nb_id: str, today) -> Dict[str, Any]:
    """CRM Health dashboard: people directory stats and contact freshness."""
    from datetime import timedelta

    # All person pages
    persons = siyuan_sql_query(
        f"SELECT b.id, b.content, a_lc.value as last_contact, "
        f"a_bd.value as birthday, a_ci.value as circle "
        f"FROM blocks b "
        f"LEFT JOIN attributes a_lc ON b.id = a_lc.block_id AND a_lc.name = 'custom-last-contact' "
        f"LEFT JOIN attributes a_bd ON b.id = a_bd.block_id AND a_bd.name = 'custom-birthday' "
        f"LEFT JOIN attributes a_ci ON b.id = a_ci.block_id AND a_ci.name = 'custom-circle' "
        f"WHERE b.box = '{nb_id}' AND b.type = 'd' AND b.hpath LIKE '%/people/%'"
    )

    total = len(persons)
    has_birthday = sum(1 for p in persons if p.get("birthday"))
    has_circle = sum(1 for p in persons if p.get("circle"))

    # Contact freshness
    cutoff_7 = (today - timedelta(days=7)).isoformat()
    cutoff_30 = (today - timedelta(days=30)).isoformat()
    cutoff_90 = (today - timedelta(days=90)).isoformat()
    contacted_7d = sum(1 for p in persons if (p.get("last_contact") or "") >= cutoff_7)
    contacted_30d = sum(1 for p in persons if (p.get("last_contact") or "") >= cutoff_30)
    stale_90d = sum(1 for p in persons if p.get("last_contact") and p["last_contact"] < cutoff_90)
    never_contacted = sum(1 for p in persons if not p.get("last_contact"))

    # Circle breakdown
    circles: Dict[str, int] = {}
    for p in persons:
        c = p.get("circle", "") or "uncategorized"
        circles[c] = circles.get(c, 0) + 1
    circle_lines = "\n".join(
        f"  • {name}: {count}" for name, count in sorted(circles.items(), key=lambda x: -x[1])
    )

    dashboard = (
        f"📊 *CRM Health — {today.isoformat()}*\n\n"
        f"👥 *People Directory*\n"
        f"  Total: *{total}*\n"
        f"  With birthday: {has_birthday} ({_pct(has_birthday, total)})\n"
        f"  With circle: {has_circle} ({_pct(has_circle, total)})\n\n"
        f"📞 *Contact Freshness*\n"
        f"  Last 7 days: *{contacted_7d}*\n"
        f"  Last 30 days: {contacted_30d}\n"
        f"  Stale (90+ days): ⚠️ {stale_90d}\n"
        f"  Never contacted: {never_contacted}\n\n"
        f"🔵 *Circles*\n{circle_lines}"
    )

    return {
        "dashboard": dashboard,
        "data": {
            "total_people": total,
            "has_birthday": has_birthday,
            "has_circle": has_circle,
            "contacted_7d": contacted_7d,
            "contacted_30d": contacted_30d,
            "stale_90d": stale_90d,
            "never_contacted": never_contacted,
            "circles": circles,
        },
    }


def _dashboard_recent_contacts(nb_id: str, today) -> Dict[str, Any]:
    """Recent Contacts dashboard: people sorted by last-contact date."""
    persons = siyuan_sql_query(
        f"SELECT b.id, b.content, a.value as last_contact "
        f"FROM blocks b "
        f"JOIN attributes a ON b.id = a.block_id AND a.name = 'custom-last-contact' "
        f"WHERE b.box = '{nb_id}' AND b.type = 'd' AND b.hpath LIKE '%/people/%' "
        f"AND a.value != '' "
        f"ORDER BY a.value DESC"
    )

    # Also find people never contacted
    never = siyuan_sql_query(
        f"SELECT b.id, b.content FROM blocks b "
        f"LEFT JOIN attributes a ON b.id = a.block_id AND a.name = 'custom-last-contact' "
        f"WHERE b.box = '{nb_id}' AND b.type = 'd' AND b.hpath LIKE '%/people/%' "
        f"AND (a.value IS NULL OR a.value = '')"
    )

    lines = []
    for i, p in enumerate(persons[:15]):
        name = p.get("content", "?")
        lc = p.get("last_contact", "?")
        days_ago = (today - _parse_date(lc)).days if _parse_date(lc) else "?"
        emoji = "🟢" if isinstance(days_ago, int) and days_ago <= 7 else (
            "🟡" if isinstance(days_ago, int) and days_ago <= 30 else "🔴"
        )
        lines.append(f"  {emoji} *{name}* — {lc} ({days_ago}d ago)")

    never_names = [p.get("content", "?") for p in never[:10]]

    dashboard = (
        f"📋 *Recent Contacts — {today.isoformat()}*\n\n"
        + "\n".join(lines)
    )
    if never_names:
        dashboard += (
            f"\n\n⚪ *Never Contacted ({len(never)}):*\n"
            f"  {', '.join(never_names)}"
            + ("..." if len(never) > 10 else "")
        )

    return {
        "dashboard": dashboard,
        "data": {
            "recent": [{"name": p.get("content"), "last_contact": p.get("last_contact")} for p in persons[:15]],
            "never_contacted_count": len(never),
        },
    }


def _dashboard_wiki_quality(notebook: str) -> Dict[str, Any]:
    """Wiki Quality dashboard: formatted lint report."""
    report = siyuan_lint(notebook)
    if "error" in report:
        return {"dashboard": f"❌ {report['error']}", "data": report}

    lines = [f"🔍 *Wiki Quality — {notebook}*\n"]

    if report["errors"]:
        lines.append(f"❌ *Errors ({len(report['errors'])}):*")
        for e in report["errors"][:5]:
            lines.append(f"  • [{e['rule']}] {e['detail']}")

    if report["warnings"]:
        lines.append(f"\n⚠️ *Warnings ({len(report['warnings'])}):*")
        for w in report["warnings"][:8]:
            lines.append(f"  • [{w['rule']}] {w['detail']}")

    if report["info"]:
        lines.append(f"\nℹ️ *Info ({len(report['info'])}):*")
        for i in report["info"][:5]:
            lines.append(f"  • [{i['rule']}] {i['detail']}")

    if report["total"] == 0:
        lines.append("✅ *All checks passed!*")

    return {
        "dashboard": "\n".join(lines),
        "data": report,
    }


def _dashboard_habits(nb_id: str, today) -> Dict[str, Any]:
    """Habits dashboard: active habits, streaks, compliance."""
    habits = siyuan_sql_query(
        f"SELECT b.id, b.content, "
        f"a_st.value as streak, a_fr.value as frequency, a_lc.value as last_check "
        f"FROM blocks b "
        f"LEFT JOIN attributes a_st ON b.id = a_st.block_id AND a_st.name = 'custom-streak' "
        f"LEFT JOIN attributes a_fr ON b.id = a_fr.block_id AND a_fr.name = 'custom-frequency' "
        f"LEFT JOIN attributes a_lc ON b.id = a_lc.block_id AND a_lc.name = 'custom-last-check' "
        f"WHERE b.box = '{nb_id}' AND b.type = 'd' AND b.hpath LIKE '%/habits/%'"
    )

    if not habits:
        return {
            "dashboard": "📅 *Habits*\n\nNo habits tracked yet. Create pages in `/habits/` to get started.",
            "data": {"total": 0},
        }

    lines = [f"📅 *Habit Tracker — {today.isoformat()}*\n"]
    active = 0
    total_streak = 0

    for h in habits:
        name = h.get("content", "?")
        streak = int(h.get("streak") or 0)
        freq = h.get("frequency", "daily") or "daily"
        last_check = h.get("last_check", "")

        total_streak += streak

        # Check if on track
        if last_check:
            days_since = (today - _parse_date(last_check)).days if _parse_date(last_check) else 999
        else:
            days_since = 999

        if freq == "daily" and days_since <= 1:
            emoji = "🔥"
            active += 1
        elif freq == "weekly" and days_since <= 7:
            emoji = "🔥"
            active += 1
        elif days_since <= 2:
            emoji = "🟡"
        else:
            emoji = "❌"

        streak_bar = "🟩" * min(streak, 10) + ("..." if streak > 10 else "")
        lines.append(f"  {emoji} *{name}* — {streak}d streak {streak_bar}")

    lines.insert(1, f"Active: *{active}/{len(habits)}* | Total streak days: *{total_streak}*\n")

    return {
        "dashboard": "\n".join(lines),
        "data": {
            "total": len(habits),
            "active": active,
            "total_streak": total_streak,
        },
    }


def _dashboard_goals(nb_id: str, today) -> Dict[str, Any]:
    """Goals dashboard: status breakdown, overdue items."""
    goals = siyuan_sql_query(
        f"SELECT b.id, b.content, "
        f"a_s.value as status, a_p.value as progress, a_dl.value as deadline "
        f"FROM blocks b "
        f"LEFT JOIN attributes a_s ON b.id = a_s.block_id AND a_s.name = 'custom-status' "
        f"LEFT JOIN attributes a_p ON b.id = a_p.block_id AND a_p.name = 'custom-progress' "
        f"LEFT JOIN attributes a_dl ON b.id = a_dl.block_id AND a_dl.name = 'custom-deadline' "
        f"WHERE b.box = '{nb_id}' AND b.type = 'd' AND b.hpath LIKE '%/goals/%'"
    )

    if not goals:
        return {
            "dashboard": "🎯 *Goals*\n\nNo goals tracked yet. Create pages in `/goals/` to start.",
            "data": {"total": 0},
        }

    status_map: Dict[str, list] = {
        "active": [], "completed": [], "paused": [], "abandoned": [], "": []
    }
    overdue = []

    for g in goals:
        name = g.get("content", "?")
        status = (g.get("status") or "").lower()
        progress = g.get("progress", "0") or "0"
        deadline = g.get("deadline", "")

        bucket = status if status in status_map else ""
        status_map[bucket].append({"name": name, "progress": progress, "deadline": deadline})

        if deadline and deadline < today.isoformat() and status not in ("completed", "abandoned"):
            overdue.append(name)

    lines = [f"🎯 *Goals — {today.isoformat()}*\n"]

    # Summary bar
    total = len(goals)
    completed = len(status_map["completed"])
    active = len(status_map["active"])
    lines.append(
        f"Total: *{total}* | ✅ {completed} completed | 🔵 {active} active"
        + (f" | ⚠️ {len(overdue)} overdue" if overdue else "")
        + "\n"
    )

    if overdue:
        lines.append("🚨 *Overdue:*")
        for name in overdue[:5]:
            lines.append(f"  • {name}")
        lines.append("")

    if status_map["active"]:
        lines.append("🔵 *Active:*")
        for g in status_map["active"][:8]:
            pct = g["progress"]
            try:
                pct_int = int(float(pct))
                bar = "▓" * (pct_int // 10) + "░" * (10 - pct_int // 10)
                lines.append(f"  • *{g['name']}* [{bar}] {pct_int}%")
            except (ValueError, TypeError):
                lines.append(f"  • *{g['name']}* — {pct}")

    if status_map["completed"]:
        lines.append(f"\n✅ *Completed ({completed}):*")
        for g in status_map["completed"][:5]:
            lines.append(f"  • {g['name']}")

    return {
        "dashboard": "\n".join(lines),
        "data": {
            "total": total,
            "completed": completed,
            "active": active,
            "overdue": overdue,
        },
    }


def _pct(part: int, whole: int) -> str:
    """Format a percentage string."""
    if whole == 0:
        return "0%"
    return f"{part * 100 // whole}%"


def _parse_date(date_str: str):
    """Parse an ISO date string, returning a date or None."""
    from datetime import date
    try:
        return date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return None


# ── Wiki bootstrap ───────────────────────────────────────────────────────────

_SCHEMA_TEMPLATE = """# Wiki Schema

## Domain
{domain}

## Conventions
- Document paths: lowercase, hyphens, no spaces (e.g., `/entities/transformer-architecture`)
- **Cross-reference using SiYuan refs:** link to other wiki pages with `((block-id "anchor text"))` syntax
- When updating a page, set `custom-updated` attribute to today's date
- Every new page must be added to {index_ref} under the correct section
- Every action must be appended to {log_ref}
- **Provenance:** On pages synthesizing 3+ sources, note which source each claim comes from
- **Paragraph citations:** End prose paragraphs with the source path, e.g., `[raw/articles/source-name]`

## Navigation
- {index_ref} — content catalog
- {log_ref} — chronological action log

## Custom Attributes (replaces YAML frontmatter)
All wiki pages should have these custom attributes set via `siyuan_set_attrs`:

### Required
- `custom-type`: entity | concept | synthesis | overview | summary | journal | person | habit | goal | query | raw
- `custom-tags`: comma-separated tags from the taxonomy below
- `custom-sources`: comma-separated source document paths
- `custom-created`: YYYY-MM-DD
- `custom-updated`: YYYY-MM-DD

### Provenance (set on Layer 2 pages)
- `custom-confidence`: numeric 0.0–1.0 (1.0 = directly stated in source, 0.0 = highly speculative)
- `custom-provenance`: extracted | merged | inferred | ambiguous
  - `extracted` — directly from a single source
  - `merged` — synthesized across multiple sources
  - `inferred` — model deduction, not directly cited
  - `ambiguous` — sources disagree
- `custom-contradicted-by`: comma-separated paths of pages with conflicting claims
- `custom-inferred-paragraphs`: integer count of paragraphs that are inference, not cited

### Personal CRM (set on person/habit/goal pages)
- `custom-birthday`: YYYY-MM-DD (person pages)
- `custom-last-contact`: YYYY-MM-DD (person pages — when you last interacted)
- `custom-contact-frequency`: daily | weekly | biweekly | monthly | quarterly (person pages)
- `custom-circle`: family | close-friends | professional | acquaintance (person pages)
- `custom-frequency`: daily | weekly | monthly (habit pages)
- `custom-streak`: integer — current consecutive completions (habit pages)
- `custom-target-date`: YYYY-MM-DD (goal pages)
- `custom-progress`: 0–100 integer (goal pages)
- `custom-status`: active | paused | completed | abandoned (habit/goal pages)

## Page Kind Policies
Each page kind has a minimum cross-link requirement enforced by lint.

| Kind | Min Links | Description |
|------|-----------|-------------|
| entity | 2 | Specific organization, product, or named artifact |
| person | 1 | A person — birthday, preferences, relationship notes |
| concept | 1 | Standalone idea, technique, or pattern |
| synthesis | 3 | Cross-cutting analysis across 2+ entities or concepts |
| overview | 5 | Map page that connects several concepts in a domain |
| summary | 1 | One-page distillation of a raw source |
| journal | 0 | Daily/session reflection — dated, append-only |
| habit | 1 | Recurring practice with streak tracking |
| goal | 2 | Active objective with progress and target date |
| query | 1 | Filed query result worth keeping |
| raw | 0 | Immutable source material — never modify |

## Tag Taxonomy
Define domain-specific tags here. Every tag used on a page must appear in this list.
Add new tags here BEFORE using them on pages.

{tags}

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Create a person page** when someone is mentioned by name in 2+ contexts
- **Create a habit page** when the user wants to track a recurring practice
- **Add to existing page** when a source mentions something already covered
- **Don't create a page** for passing mentions, minor details, or things outside the domain
- **Split a page** when it exceeds ~3000 chars — break into sub-topics with cross-links

## Update Policy
When new information conflicts with existing content:
1. Check the dates — newer sources generally supersede older ones
2. If genuinely contradictory, note both positions with dates and sources
3. Set `custom-contradicted-by` on both pages, pointing to each other
4. Set `custom-provenance: ambiguous` on the affected page
5. Flag for user review in the lint report
"""

_INDEX_TEMPLATE = """# Wiki Index

> Content catalog. Every wiki page listed under its type with a one-line summary.
> Read this first to find relevant pages for any query.
> See {schema_ref} for conventions. See {log_ref} for recent activity.
> Last updated: {date} | Total pages: 0

## People
- {people_ref} — person pages (relationships, contacts)

## Entities
- {entities_ref} — organizations, products, named artifacts

## Concepts
- {concepts_ref} — ideas, techniques, patterns

## Syntheses
- {syntheses_ref} — cross-cutting analyses and comparisons

## Summaries
- {summaries_ref} — one-page distillations of raw sources

## Journal
- {journal_ref} — research sessions and daily reflections

## Habits & Goals
- {habits_ref} — recurring practices with streak tracking
- {goals_ref} — active objectives with progress

## Circles
- {circles_ref} — relationship groups (family, friends, professional)

## Queries
- {queries_ref} — filed query results worth keeping

## Raw Sources
- {raw_ref} — immutable source material ({articles_ref}, {papers_ref}, {transcripts_ref})
"""

_LOG_TEMPLATE = """# Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, update, query, lint, create, reflect, archive, delete
> See {schema_ref} for conventions. See {index_ref} for page catalog.

## [{date}] create | Wiki initialized
- Domain: {domain}
- Created: {schema_ref}, {index_ref}, this log
- Directories: {entities_ref}, {concepts_ref}, {syntheses_ref}, {summaries_ref}, {journal_ref}, {people_ref}, {circles_ref}, {habits_ref}, {goals_ref}, {queries_ref}, {raw_ref}
"""


def _siyuan_ref(block_id: Optional[str], label: str) -> str:
    """Build a SiYuan block reference string, falling back to plain text."""
    if block_id:
        return f'(({block_id} "{label}"))'
    return label


def siyuan_init_wiki(
    name: str = "Wiki",
    domain: str = "General knowledge",
    tags: str = "",
) -> Dict[str, Any]:
    """
    Bootstrap a new LLM Wiki in SiYuan.

    Creates a notebook with Schema, Index, and Log documents,
    plus the standard directory structure for the wiki pattern.
    All documents are cross-linked with SiYuan block references.

    Args:
        name: Notebook name (default: "Wiki")
        domain: What this wiki covers (e.g., "AI/ML research")
        tags: Initial tag taxonomy (multi-line string)

    Returns:
        Dict with notebook_id and document IDs, or error
    """
    from datetime import date

    today = date.today().isoformat()

    # 1. Create notebook
    nb_id = siyuan_create_notebook(name)
    if not nb_id:
        return {"error": "Failed to create notebook"}

    tag_section = tags if tags else (
        "- General: topic, reference, tutorial, guide\n"
        "- Meta: comparison, timeline, question, answer"
    )

    # 2. Create all documents first (to get their IDs)
    #    Use temporary placeholder content — we'll update with links after.
    schema_id = siyuan_create_doc(name, "/Schema", "# Schema\nInitializing...")
    index_id = siyuan_create_doc(name, "/Index", "# Index\nInitializing...")
    log_id = siyuan_create_doc(name, "/Log", "# Log\nInitializing...")

    # 3. Create directory-placeholder documents
    dirs = {}
    dir_labels = {
        "/raw": "Raw Sources",
        "/raw/articles": "Articles",
        "/raw/papers": "Papers",
        "/raw/transcripts": "Transcripts",
        "/entities": "Entities",
        "/concepts": "Concepts",
        "/syntheses": "Syntheses",
        "/summaries": "Summaries",
        "/journal": "Journal",
        "/people": "People",
        "/circles": "Circles",
        "/habits": "Habits",
        "/goals": "Goals",
        "/queries": "Queries",
    }
    for dir_path, dir_label in dir_labels.items():
        doc_id = siyuan_create_doc(name, dir_path, f"# {dir_label}\n\nInitializing...")
        dirs[dir_path] = doc_id

    # 4. Build ref strings for cross-linking
    refs = {
        "schema_ref": _siyuan_ref(schema_id, "Schema"),
        "index_ref": _siyuan_ref(index_id, "Index"),
        "log_ref": _siyuan_ref(log_id, "Log"),
        "raw_ref": _siyuan_ref(dirs.get("/raw"), "Raw Sources"),
        "articles_ref": _siyuan_ref(dirs.get("/raw/articles"), "Articles"),
        "papers_ref": _siyuan_ref(dirs.get("/raw/papers"), "Papers"),
        "transcripts_ref": _siyuan_ref(dirs.get("/raw/transcripts"), "Transcripts"),
        "entities_ref": _siyuan_ref(dirs.get("/entities"), "Entities"),
        "concepts_ref": _siyuan_ref(dirs.get("/concepts"), "Concepts"),
        "syntheses_ref": _siyuan_ref(dirs.get("/syntheses"), "Syntheses"),
        "summaries_ref": _siyuan_ref(dirs.get("/summaries"), "Summaries"),
        "journal_ref": _siyuan_ref(dirs.get("/journal"), "Journal"),
        "people_ref": _siyuan_ref(dirs.get("/people"), "People"),
        "circles_ref": _siyuan_ref(dirs.get("/circles"), "Circles"),
        "habits_ref": _siyuan_ref(dirs.get("/habits"), "Habits"),
        "goals_ref": _siyuan_ref(dirs.get("/goals"), "Goals"),
        "queries_ref": _siyuan_ref(dirs.get("/queries"), "Queries"),
    }

    # 5. Render templates with actual cross-links
    schema_md = _SCHEMA_TEMPLATE.format(domain=domain, tags=tag_section, **refs)
    index_md = _INDEX_TEMPLATE.format(date=today, **refs)
    log_md = _LOG_TEMPLATE.format(date=today, domain=domain, **refs)

    # 6. Update documents with linked content
    if schema_id:
        siyuan_update_block(schema_id, schema_md)
    if index_id:
        siyuan_update_block(index_id, index_md)
    if log_id:
        siyuan_update_block(log_id, log_md)

    # 7. Update directory placeholders with back-links
    for dir_path, dir_label in dir_labels.items():
        doc_id = dirs.get(dir_path)
        if doc_id:
            dir_md = (
                f"# {dir_label}\n\n"
                f"Part of the {refs['schema_ref']} wiki structure. "
                f"See {refs['index_ref']} for the full content catalog."
            )
            siyuan_update_block(doc_id, dir_md)

    # 8. Set custom attributes on Schema, Index, Log
    for doc_id, doc_type in [(schema_id, "schema"), (index_id, "index"), (log_id, "log")]:
        if doc_id:
            siyuan_set_attrs(doc_id, {
                "type": doc_type,
                "created": today,
                "updated": today,
            })

    result = {
        "notebook_id": nb_id,
        "schema_id": schema_id,
        "index_id": index_id,
        "log_id": log_id,
        "directories": dirs,
        "message": f"Wiki '{name}' initialized with {3 + len(dirs)} cross-linked documents.",
    }
    logger.info(f"SiYuan Wiki bootstrapped: {name} → {nb_id}")
    return result


