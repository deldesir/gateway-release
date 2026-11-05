"""
Palace Writer — persists conversation turns to MemPalace after each
Hermes invocation.

Design rationale:
  - Runs AFTER the response is sent (fire-and-forget via asyncio.to_thread)
  - Uses tenacity retry for SQLite maintenance lockouts (VACUUM cron
    runs monthly and briefly locks the DB)
  - Deduplicates by content hash to prevent re-storing on WhatsApp retries
  - Scopes writes to per-user wings for tenant isolation
"""

import hashlib
import logging
import os
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_PALACE_ENABLED = os.getenv("MEMPALACE_PALACE_PATH", "")  # Empty = disabled


# ── Content dedup ────────────────────────────────────────────────────────────

def _content_hash(content: str) -> str:
    """SHA-256 prefix for dedup checks."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ── Retry wrapper for SQLite lockouts ────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type((OSError, IOError)),
    reraise=True,
)
def _store_with_retry(
    content: str,
    wing: str,
    room: str,
    metadata: Optional[dict] = None,
) -> None:
    """Store content in MemPalace with retry on SQLite lock."""
    from mempalace.mcp_server import tool_add_drawer

    tool_add_drawer(
        content=content,
        wing=wing,
        room=room,
    )


# ── Main entry point ────────────────────────────────────────────────────────

def persist_turn_to_palace(
    urn: str,
    persona: str,
    user_message: str,
    assistant_response: str,
    skip_persistence: bool = False,
) -> None:
    """
    Persist a conversation turn to MemPalace.

    Called from a thread (via asyncio.to_thread) after the response
    has been sent to the user. This means persistence failures
    don't block the user experience.

    Args:
        urn: WhatsApp URN (e.g. "whatsapp:+50937...")
        persona: Persona slug used for this turn
        user_message: The user's input text
        assistant_response: Hermes's response text
        skip_persistence: If True, skip storage (e.g., queue overflow)
    """
    if not _PALACE_ENABLED or skip_persistence:
        return

    # Per-user wing for tenant isolation (server-side, not user-controllable)
    phone = urn.split(":")[-1].lstrip("+")
    wing = f"wing_{phone}"
    room = "general"  # Flat room — persona stored as content tag, not partition key

    # Build the turn content (verbatim, per MemPalace design — no summarization)
    # Persona tag is embedded in content for grep-ability and search filtering
    content = (
        f"[persona:{persona}]\n"
        f"[User] {user_message}\n"
        f"[{persona}] {assistant_response}"
    )

    # Dedup check — prevent re-storing on WhatsApp message retries
    content_id = _content_hash(content)
    try:
        from mempalace.mcp_server import tool_check_duplicate
        is_dup = tool_check_duplicate(content=content, threshold=0.95)
        if is_dup and isinstance(is_dup, dict) and is_dup.get("is_duplicate"):
            logger.debug(f"Dedup hit ({content_id}) — skipping palace write for {urn}")
            return
    except Exception as e:
        # Dedup check is best-effort — proceed with store on failure
        logger.debug(f"Dedup check failed (proceeding): {e}")

    try:
        _store_with_retry(
            content=content,
            wing=wing,
            room=room,
        )
        logger.debug(f"Palace write OK: {wing}/{room} ({content_id})")
    except Exception as e:
        # Never crash on persistence failure — the response is already sent
        logger.error(f"Palace write FAILED for {urn}: {e}")
