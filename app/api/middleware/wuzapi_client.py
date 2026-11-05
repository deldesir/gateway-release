"""
WuzAPI client — direct API calls for WhatsApp features not available via RapidPro.

Currently supports:
- Message reactions (emoji on existing messages)
- Mark as read
- Typing indicators (chat presence)
- Button messages (Quick Reply)
- Document sending (send_document)
- WhatsApp Status text (set_status)

WuzAPI docs: /opt/iiab/wuzapi/API.md
"""

import os
import logging
import httpx

from app.api.middleware.wuzapi_token import get_wuzapi_token, invalidate_wuzapi_token

logger = logging.getLogger("ai-gateway.wuzapi")

WUZAPI_URL = os.getenv("WUZAPI_URL", "http://localhost:8095")


async def _post(path: str, payload: dict, timeout: float, feature: str, channel: str = ""):
    """POST to WuzAPI as the active WhatsApp channel's user.

    The token comes from the channel row RapidPro maintains (see wuzapi_token), so a
    re-claimed channel needs no configuration change. A 401 means the cached token
    is stale: re-read it once and retry. Returns the response, or None when no token
    is known (the feature is then reported as unavailable, not as an error).
    """
    token = await get_wuzapi_token(channel=channel)
    if not token:
        logger.warning(f"No WuzAPI token for the active WhatsApp channel — cannot {feature}")
        return None
    url = f"{WUZAPI_URL}{path}"
    for attempt in (1, 2):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers={"Authorization": token})
        if resp.status_code != 401 or attempt == 2:
            return resp
        invalidate_wuzapi_token()
        token = await get_wuzapi_token(force_refresh=True, channel=channel)
        if not token:
            return resp
        logger.info(f"WuzAPI refused the cached token; retrying {feature} with the channel's current token")
    return resp


async def send_reaction(
    phone: str,
    message_id: str,
    emoji: str = "👍",
) -> bool:
    """React to a WhatsApp message with an emoji.

    Args:
        phone: Recipient phone number (digits only, e.g. "15550104949").
        message_id: WhatsApp message ID to react to.
        emoji: Emoji to react with (default: 👍).

    Returns:
        True if the reaction was sent successfully.
    """
    payload = {"Phone": phone, "Body": emoji, "Id": message_id}

    try:
        resp = await _post("/chat/react", payload, 5.0, "send a reaction")
        if resp is None:
            return False
        if resp.status_code == 200:
            logger.info(f"Reaction {emoji} sent to {phone} on msg {message_id}")
            return True
        else:
            logger.warning(f"WuzAPI react failed: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        logger.warning(f"WuzAPI react error: {e}")
        return False


async def mark_as_read(phone: str, message_id: str) -> bool:
    """Mark a message as read (blue ticks).

    Args:
        phone: Sender phone number.
        message_id: WhatsApp message ID to mark as read.

    Returns:
        True if successful.
    """
    payload = {"Id": message_id, "Chat": f"{phone}@s.whatsapp.net"}

    try:
        resp = await _post("/chat/markread", payload, 5.0, "mark as read")
        if resp is None:
            return False
        return resp.status_code == 200
    except Exception:
        return False


async def send_presence(
    phone: str,
    state: str = "composing",
) -> bool:
    """Send chat presence indicator (typing/paused) via WuzAPI.

    ADR-011 T2: Shows "typing..." in WhatsApp while L2 commands process.
    Call this BEFORE the API request, then let WhatsApp auto-clear it
    when the response message is sent.

    Args:
        phone: Recipient phone number (digits only).
        state: "composing" for typing, "paused" to clear.

    Returns:
        True if successful.
    """
    payload = {"Phone": phone, "State": state}

    try:
        resp = await _post("/chat/presence", payload, 3.0, "send presence")
        if resp is None:
            return False
        return resp.status_code == 200
    except Exception:
        return False


async def send_buttons(
    phone: str,
    content: str,
    buttons: list[dict],
    footer: str = "",
) -> bool:
    """Send a WhatsApp message with Quick Reply buttons via WuzAPI.

    Uses /chat/send/buttons (ADR-011 Finding 2 fix — was dead /chat/send/template).
    Source: wuzapi/handlers.go:1976-2225 (SendButtons handler).

    Args:
        phone: Recipient phone number (digits only, e.g. "15550105893").
        content: Message body text.
        buttons: List of {"DisplayText": "text"} dicts.
                 Max 3 buttons per WhatsApp constraints.
                 Legacy format {"Type": "quickreply"} is accepted but ignored.
        footer: Optional footer text (max 60 chars recommended).

    Returns:
        True if sent successfully.

    CRITICAL: The button's DisplayText is BOTH the label shown on the button AND
    the exact text WhatsApp sends as a message when the user taps it.
    It MUST match a RiveBot trigger in _common/admin.rive exactly (case-insensitive).
    """
    # Convert to SendButtons handler format (handlers.go:1978-1988).
    # Handler accepts: type, title (or text/buttonText fallback), id (or buttonId).
    formatted_buttons = []
    for b in buttons[:3]:
        title = b.get("DisplayText", b.get("title", ""))
        formatted_buttons.append({
            "type": "reply",
            "title": title,
            "id": b.get("Id", b.get("id", title)),
        })

    payload = {
        "Phone": phone,
        "Body": content,   # SendButtons expects "Body" not "Content"
        "Footer": footer,
        "Buttons": formatted_buttons,
    }

    try:
        resp = await _post("/chat/send/buttons", payload, 5.0, "send buttons")
        if resp is None:
            return False
        if resp.status_code == 200:
            labels = [b["title"] for b in formatted_buttons]
            logger.info(f"Button message sent to {phone}: {labels}")
            return True
        else:
            logger.warning(f"WuzAPI button send failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"WuzAPI button send error: {e}")
        return False


async def send_document(
    phone: str,
    document_b64: str,
    filename: str,
    channel: str = "",
    mime_type: str = "",
) -> bool:
    """Send a document via WuzAPI.

    Args:
        phone: Recipient phone number (digits only).
        document_b64: Base64 encoded document content, prefixed with MIME type.
            Example: "data:application/pdf;base64,JVBERi0xLjQK..."
        filename: Name of the file (e.g. "report.csv").

    Returns:
        True if sent successfully.
    """
    payload = {
        "Phone": phone,
        "FileName": filename,
        "Document": document_b64,
    }
    if mime_type:
        # WuzAPI no longer reads the type out of a data: URI; without it the
        # phone gets a document it cannot open.
        payload["MimeType"] = mime_type

    try:
        resp = await _post("/chat/send/document", payload, 30.0, "send a document", channel=channel)
        if resp is None:
            return False
        if resp.status_code == 200:
            logger.info(f"Document {filename} sent to {phone}")
            return True
        else:
            logger.warning(f"WuzAPI send document failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"WuzAPI send document error: {e}")
        return False


async def send_contact_card(
    phone: str,
    name: str,
    vcard: str,
    channel: str = "",
) -> bool:
    """Send a native WhatsApp contact card.

    Renders as a tappable card the recipient can add straight to their address
    book — better than a .vcf attachment for a single contact, since it needs no
    file manager. Use send_document for a whole directory.

    Args:
        phone: Recipient phone number (digits only).
        name: Display name shown on the card.
        vcard: A complete BEGIN:VCARD…END:VCARD block.

    Returns:
        True if sent successfully.
    """
    payload = {"Phone": phone, "Name": name, "Vcard": vcard}

    try:
        resp = await _post("/chat/send/contact", payload, 30.0, "send a contact card", channel=channel)
        if resp is None:
            return False
        if resp.status_code == 200:
            logger.info(f"Contact card sent to {phone}")
            return True
        logger.warning(f"WuzAPI send contact failed: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"WuzAPI send contact error: {e}")
        return False


async def set_status(
    text: str,
) -> bool:
    """Set the WhatsApp account's text status via WuzAPI.

    Args:
        text: Status text to display.

    Returns:
        True if set successfully.
    """
    payload = {
        "Body": text,
    }

    try:
        resp = await _post("/status/set/text", payload, 10.0, "set the status")
        if resp is None:
            return False
        if resp.status_code == 200:
            logger.info(f"WhatsApp status set to: {text}")
            return True
        else:
            logger.warning(f"WuzAPI set status failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"WuzAPI set status error: {e}")
        return False


async def send_text(phone: str, text: str) -> bool:
    """Send a plain text message via WuzAPI (/chat/send/text)."""
    try:
        resp = await _post("/chat/send/text", {"Phone": phone, "Body": text}, 10.0, "send text")
    except httpx.HTTPError as e:
        logger.warning(f"send text failed: {e}")
        return False
    return bool(resp) and resp.status_code == 200


async def send_list(phone: str, text: str, sections: list[dict], button_text: str = "Chwazi") -> bool:
    """Send a WhatsApp list message via WuzAPI (/chat/send/list): sections of rows, each row
    {"title", "desc", "RowId"}; the RowId comes back as the person's reply. Ten rows at most."""
    kept, n = [], 0
    for sec in sections:
        rows = []
        for r in sec.get("rows") or []:
            if n >= 10:
                break
            rows.append({"title": r.get("title", "")[:24], "desc": r.get("desc", "")[:72], "RowId": str(r.get("RowId") or r.get("title", ""))})
            n += 1
        if rows:
            kept.append({"title": sec.get("title", "")[:24], "rows": rows})
    payload = {"Phone": phone, "ButtonText": button_text[:20], "Desc": text, "Sections": kept}
    try:
        resp = await _post("/chat/send/list", payload, 10.0, "send list")
    except httpx.HTTPError as e:
        logger.warning(f"send list failed: {e}")
        return False
    return bool(resp) and resp.status_code == 200

