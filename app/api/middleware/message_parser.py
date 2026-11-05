"""
Message parsing utilities for the OpenAI-compatible endpoint.

RapidPro sends messages in the format::

    "Name (whatsapp:+1234 > 5678) says: Hello"

This module extracts the URN, optional channel ID, and clean message
content from that prefix, making the rest of the handler clean.
"""

import re
from dataclasses import dataclass
from typing import Optional

# RapidPro URN prefix: "Name (urn > channel) says: content"
# Paths are phone digits (tel/whatsapp/telegram) OR an email address (mailto),
# so the email channel's URN prefix is stripped just like a phone one.
_PATH = r"(?:\+?\d+|[^\s:()>]+@[^\s:()>]+)"
_URN_SIMPLE = re.compile(r"(tel|whatsapp|telegram|mailto):(\+?\d+|[^\s()>]+@[^\s()>]+)")
_URN_PREFIX = re.compile(
    r"^(?P<contact_name>[^(]+?)\s*\("
    r"(?P<urn>(?:(?:tel|whatsapp|telegram|mailto):)?(?:\+?\d+|[^\s()>]+@[^\s()>]+))"
    r"(?:\s*>\s*(?P<channel>[a-zA-Z0-9\-]+))?"
    r"\) says:\s+"
)
# Colon-separated format: "scheme:urn:channel: message"
# e.g. "whatsapp:15550104949:15550105893: Hello" or "mailto:a@x.com:bot@x.com: Hi"
_COLON_PREFIX = re.compile(
    r"^(?P<scheme>tel|whatsapp|telegram|mailto):"
    r"(?P<urn>" + _PATH + r"):"
    r"(?P<channel>" + _PATH + r"):\s*"
)
# Suffix injected by the RapidPro flow: [msg_id:WHATSAPP_MSG_ID]
_MSG_ID_SUFFIX = re.compile(r"\s*\[msg_id:([^\]]+)\]\s*$")
# Attachments injected by call_llm expression: [Attachments: type:url, type:url]
_ATTACHMENTS_BLOCK = re.compile(r"\s*\[Attachments:\s*([^\]]+)\]\s*$", re.IGNORECASE)



@dataclass
class ParsedMessage:
    content: str              # cleaned message, prefix stripped
    user_id: Optional[str]   # e.g. "whatsapp:+12345"
    channel_id: Optional[str] # e.g. "5678" (maps to a persona)
    attachments: list = None   # e.g. ["application/octet-stream:https://…/file.zip"]
    external_msg_id: Optional[str] = None  # WhatsApp message ID for reactions
    contact_name: Optional[str] = None     # WhatsApp contact name from RapidPro

    def __post_init__(self):
        if self.attachments is None:
            self.attachments = []


def parse_rapidpro_message(raw: str, user_hint: Optional[str] = None,
                           attachments: list = None) -> ParsedMessage:
    """Extract URN, channel, and clean content from a RapidPro message.

    Args:
        raw: Raw message content from RapidPro.
        user_hint: Explicit user ID from the ``user`` JSON field (may be None).
        attachments: List of attachment strings in RapidPro format
                     (``content_type:url``).

    Returns:
        ParsedMessage with content, user_id, channel_id, and attachments.
    """
    user_id = user_hint
    channel_id = None
    content = raw
    external_msg_id = None

    # 0. Strip [msg_id:...] suffix (injected by flow for WuzAPI reactions)
    mid_match = _MSG_ID_SUFFIX.search(content)
    if mid_match:
        external_msg_id = mid_match.group(1).strip()
        content = _MSG_ID_SUFFIX.sub("", content).rstrip()

    # 0.5 Extract [Attachments: url1, url2] block embedded by call_llm expression
    inline_attachments = []
    att_match = _ATTACHMENTS_BLOCK.search(content)
    if att_match:
        inline_attachments = [a.strip() for a in att_match.group(1).split(",") if a.strip()]
        content = _ATTACHMENTS_BLOCK.sub("", content).rstrip()
    merged_attachments = inline_attachments + (attachments or [])

    # 1. Try colon-separated format: "whatsapp:NUM:CHANNEL: message"
    m = _COLON_PREFIX.match(content)
    if m:
        content = _COLON_PREFIX.sub("", content, count=1)
        if not user_id:
            user_id = f"{m.group('scheme')}:{m.group('urn')}"
        channel_id = m.group("channel")
        return ParsedMessage(
            content=content,
            user_id=user_id,
            channel_id=channel_id,
            attachments=merged_attachments,
            external_msg_id=external_msg_id,
        )

    # 2. Try the simple URN pattern first (e.g. in bare messages)
    if not user_id:
        m = _URN_SIMPLE.search(content)
        if m:
            user_id = f"{m.group(1)}:{m.group(2)}"

    contact_name = None

    # 3. Try the full RapidPro prefix pattern
    m = _URN_PREFIX.search(content)
    if m:
        content = _URN_PREFIX.sub("", content, count=1)
        if not user_id and m.group("urn"):
            user_id = m.group("urn")
        if m.group("channel"):
            channel_id = m.group("channel").lstrip("+")
        name = m.group("contact_name").strip()
        if name and not name.startswith(("whatsapp", "tel", "telegram")):
            contact_name = name

    return ParsedMessage(
        content=content,
        user_id=user_id,
        channel_id=channel_id,
        attachments=merged_attachments,
        external_msg_id=external_msg_id,
        contact_name=contact_name,
    )
