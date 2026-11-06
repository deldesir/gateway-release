"""The retail line as one tool: a RapidPro flow on a shop's box posts each incoming message here and sends
back what comes out. Same doors, same per-number state as the bridge adapter (D-014): the box's flow engine
carries the message, the gateway decides. The caller is the bound number (X-User-Id); the catch-all flow fires
for whoever writes to the shop's number, so the stranger rule is applied here, as the bridge path applies it:
no binding, no answer, nothing written.

Answer: {"text", "next", "replies"}; an empty text means the line stays silent and the flow sends nothing.
"""
from __future__ import annotations

import asyncio
import json
import re


def _digits(user_id: str) -> str:
    return re.sub(r"\D", "", (user_id or "").rpartition(":")[-1])


def retail_line(args: dict, **kwargs) -> str:
    from app.api.adapters import bridge
    from app.graph.tools import retail
    args = args or {}
    # the tools endpoint passes the caller inside the one arguments dict; the engine passes it as a keyword
    user_id = str(args.get("user_id") or kwargs.get("user_id") or "")
    text = str(args.get("text") or "")
    kind = "text" if text.strip() or not args.get("attachments") else "other"
    digits = _digits(user_id)
    if not digits or not retail.binding_for(digits):
        return json.dumps({"text": "", "next": "", "replies": []})
    out = asyncio.run(bridge.answer(digits, text, kind)) or {}
    return json.dumps({"text": out.get("text") or "", "next": out.get("next") or "", "replies": list(out.get("replies") or [])})
