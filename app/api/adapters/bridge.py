"""The bridge-to-gateway adapter: a shop's box answers its bound numbers without a flow engine.

The bridge (wuzapi) posts every event here. Known numbers only, decided before any work: a
sender without a binding is dropped with nothing done and nothing logged (D-014). Then the
three doors, in order: the shop dialog (one word opens it, taps drive it), a verb (the router,
free), a session word for the model when the box has one, bounded in time. Anything else gets
one deterministic hint, at most once an hour. Replies go back through the bridge as text, a
list or buttons. Processing runs in the background; the bridge is answered at once.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.graph.tools import retail, retail_dialog
from app.api.middleware import wuzapi_client

logger = logging.getLogger("ai-gateway.bridge")
router = APIRouter()

HMAC_KEY = os.getenv("BRIDGE_HMAC_KEY", "")
ALLOW_REMOTE = os.getenv("BRIDGE_ALLOW_REMOTE", "").lower() in ("1", "true", "yes")
SESSION_MINUTES = int(os.getenv("BRIDGE_SESSION_MINUTES", "30"))
DIALOG_MINUTES = int(os.getenv("BRIDGE_DIALOG_MINUTES", "10"))
HINT_MINUTES = int(os.getenv("BRIDGE_HINT_MINUTES", "60"))
CHAT_URL = os.getenv("BRIDGE_CHAT_URL", "http://127.0.0.1:8086/v1/chat/completions")
CHAT_KEY = os.getenv("GATEWAY_API_KEY", "")
PERSONA = os.getenv("BRIDGE_PERSONA", "shop")
STATE_PATH = Path(os.getenv("BRIDGE_STATE_PATH", "data/bridge_state.json"))

SESSION_WORDS = {"ai", "alva", "pale", "chat"}
CLOSE_WORDS = {"fin", "fini", "soti", "stop", "bye"}
MARKER = "⌁"

HINT = {"ht": "Tape *boutik* pou boutik la.", "fr": "Tape *boutik* pour la boutique.", "en": "Type *boutik* for the shop."}
NO_MODEL = {"ht": "Pa gen modèl sou liy sa a. Tape *boutik*.", "fr": "Pas de modèle sur cette ligne. Tape *boutik*.", "en": "No model on this line. Type *boutik*."}
OPENED = {"ht": "M ap koute. Tape *fin* pou fèmen; li fèmen poukont li apre {m} minit.", "fr": "Je t'écoute. Tape *fin* pour fermer ; ça se ferme seul après {m} minutes.", "en": "Listening. Type *fin* to close; it closes by itself after {m} minutes."}
CLOSED = {"ht": "Fèmen.", "fr": "Fermé.", "en": "Closed."}
NOT_TEXT = {"ht": "Liy sa a li tèks sèlman.", "fr": "Cette ligne ne lit que le texte.", "en": "This line reads text only."}

_seen: "OrderedDict[str, float]" = OrderedDict()


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _pick(d: dict, *names, default=None):
    for n in names:
        if isinstance(d, dict) and n in d and d[n] not in (None, ""):
            return d[n]
    return default


def parse_event(payload: dict) -> dict | None:
    """{"digits", "text", "id", "kind"} for a text, list or button message from a person; None otherwise."""
    if (payload.get("type") or payload.get("Type")) not in ("Message", None):
        return None
    event = payload.get("event") or payload.get("Event") or payload
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except ValueError:
            return None
    info = _pick(event, "Info", "info", default={}) or {}
    if _pick(info, "IsFromMe", "isFromMe", default=False) or _pick(info, "IsGroup", "isGroup", default=False):
        return None
    sender = str(_pick(info, "Sender", "sender", default="") or "")
    chat = str(_pick(info, "Chat", "chat", default="") or "")
    if chat.endswith("@g.us"):
        return None
    digits = re.sub(r"\D", "", sender.split("@")[0].split(":")[0])
    if not digits:
        return None
    msg = _pick(event, "Message", "message", default={}) or {}
    text = _pick(msg, "conversation", "Conversation")
    kind = "text"
    if not text:
        ext = _pick(msg, "extendedTextMessage", "ExtendedTextMessage", default={}) or {}
        text = _pick(ext, "text", "Text")
    if not text:
        lst = _pick(msg, "listResponseMessage", "ListResponseMessage", default={}) or {}
        reply = _pick(lst, "singleSelectReply", "SingleSelectReply", default={}) or {}
        text = _pick(reply, "selectedRowId", "selectedRowID", "SelectedRowID") or _pick(lst, "title", "Title")
        kind = "list"
    if not text:
        btn = _pick(msg, "buttonsResponseMessage", "ButtonsResponseMessage", default={}) or {}
        text = _pick(btn, "selectedButtonId", "selectedButtonID", "SelectedButtonID") or _pick(btn, "selectedDisplayText", "SelectedDisplayText")
        kind = "button"
    if not text:
        tpl = _pick(msg, "templateButtonReplyMessage", "TemplateButtonReplyMessage", default={}) or {}
        text = _pick(tpl, "selectedId", "selectedID", "SelectedID") or _pick(tpl, "selectedDisplayText", "SelectedDisplayText")
        kind = "button"
    if not text:
        kind = "other"
    return {"digits": digits, "text": str(text or "").strip(), "id": str(_pick(info, "ID", "id", default="") or ""), "kind": kind}


def _signature_ok(raw: bytes, header: str) -> bool:
    if not HMAC_KEY:
        return True
    want = hmac.new(HMAC_KEY.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, (header or "").strip().lower())


def _duplicate(msg_id: str) -> bool:
    if not msg_id:
        return False
    if msg_id in _seen:
        return True
    _seen[msg_id] = time.time()
    while len(_seen) > 500:
        _seen.popitem(last=False)
    return False


@router.post("/v1/bridge/wuzapi")
async def bridge_wuzapi(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()
    if not ALLOW_REMOTE and request.client and request.client.host not in ("127.0.0.1", "::1"):
        return Response(status_code=403)
    if not _signature_ok(raw, request.headers.get("x-hmac-signature", "")):
        return Response(status_code=403)
    ctype = request.headers.get("content-type", "")
    try:
        if "json" in ctype:
            payload = json.loads(raw.decode("utf-8") or "{}")
        else:
            form = await request.form()
            payload = {"type": form.get("type"), "event": json.loads(form.get("jsonData") or "{}")}
            if isinstance(payload["event"], dict) and "event" in payload["event"]:
                payload = payload["event"]
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=200)
    ev = parse_event(payload)
    if not ev or _duplicate(ev["id"]):
        return Response(status_code=200)
    # the stranger rule: no binding, nothing happens, nothing is written
    if not retail.binding_for(ev["digits"]):
        return Response(status_code=200)
    background.add_task(handle_message, ev["digits"], ev["text"], ev["kind"])
    return Response(status_code=200)


# ── the doors ─────────────────────────────────────────────────────────────────────────────

def _lang(digits: str) -> str:
    tenant, _rest, lang, _err = retail.resolve(digits, "")
    return lang if tenant else "ht"


def model_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("GEMINI_API_KEY"))


async def handle_message(digits: str, text: str, kind: str = "text") -> None:
    """The bridge path: decide, then send through the bridge."""
    out = await answer(digits, text, kind)
    if out is not None:
        await send(digits, out)


async def answer(digits: str, text: str, kind: str = "text") -> dict | None:
    """The doors, in order, for one message from a bound number: the reply to send ({text, next,
    replies}), or None when the line stays silent. Shared by the bridge path and the retail_line
    tool a RapidPro flow calls; the per-number state (dialog step, session, hint) lives here."""
    user_id = f"whatsapp:{digits}"
    lang = _lang(digits)
    state = _state()
    me = state.get(digits) or {}
    now = time.time()
    low = retail._fold(text)
    if kind == "other":
        return {"text": NOT_TEXT.get(lang, NOT_TEXT["ht"]), "next": me.get("step", ""), "replies": []}
    # 1. the dialog: open, or in progress
    in_dialog = bool(me.get("step")) and now - float(me.get("at", 0)) < DIALOG_MINUTES * 60
    if in_dialog or low in retail_dialog.MENU_WORDS:
        out = json.loads(retail_dialog.retail_dialog({"text": text, "step": me.get("step", "") if in_dialog else ""}, user_id=user_id))
        me.update(step=out.get("next") or "", at=now)
        if not me["step"]:
            me.pop("step", None)
        state[digits] = me
        _save(state)
        return out
    # 2. a verb
    from app.api.middleware.rivebot_client import match_intent
    reply, _ctx = await match_intent(text, PERSONA, user_id)
    if reply and reply.strip():
        return {"text": reply, "next": "", "replies": []}
    # 3. the session word, bounded
    session_open = now - float(me.get("session", 0)) < SESSION_MINUTES * 60
    if low in CLOSE_WORDS and session_open:
        me.pop("session", None)
        state[digits] = me
        _save(state)
        return {"text": CLOSED.get(lang, CLOSED["ht"]), "next": "", "replies": []}
    if low in SESSION_WORDS:
        if not model_available():
            return {"text": NO_MODEL.get(lang, NO_MODEL["ht"]), "next": "", "replies": []}
        me["session"] = now
        state[digits] = me
        _save(state)
        return {"text": OPENED.get(lang, OPENED["ht"]).format(m=SESSION_MINUTES), "next": "", "replies": []}
    if session_open and model_available():
        me["session"] = now
        state[digits] = me
        _save(state)
        return await chat(user_id, text)          # None when the model has nothing to say: the line stays silent
    # 4. the hint, once an hour
    if now - float(me.get("hint", 0)) >= HINT_MINUTES * 60:
        me["hint"] = now
        state[digits] = me
        _save(state)
        return {"text": HINT.get(lang, HINT["ht"]), "next": "", "replies": []}
    return None


async def chat(user_id: str, text: str) -> dict | None:
    """One model turn through the gateway's own chat path (gate, confirmation tiers, intent, breaker)."""
    body = {"model": PERSONA, "messages": [{"role": "user", "content": f"{user_id}: {text}"}]}
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {CHAT_KEY}"} if CHAT_KEY else {})}
    try:
        async with httpx.AsyncClient(timeout=70.0) as client:
            r = await client.post(CHAT_URL, json=body, headers=headers)
        content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as e:  # the breaker and the budget already shaped the reply; this is transport
        logger.warning(f"[bridge] chat failed for {user_id[-4:]}: {e}")
        return None
    if not content.strip() or "{{noreply}}" in content:
        return None
    text_part, _, buttons = content.partition(MARKER)
    replies = [b.strip() for b in buttons.split("|") if b.strip()] if buttons else []
    return {"text": text_part.strip(), "next": "", "replies": replies}


# ── sending through the bridge ────────────────────────────────────────────────────────────

def _rows(replies: list[str]) -> list[dict]:
    """"Section|Title<extra>desc" rows into {section, title, desc, id}; the id is the row's number."""
    out = []
    for i, r in enumerate(replies, 1):
        section, _, rest = r.partition("|") if "|" in r else ("", "", r)
        title, _, desc = rest.partition("<extra>")
        key = "0" if retail._fold(title) in retail_dialog.EXIT_WORDS | {"soti", "sortir", "leave", "retounen", "retour", "back"} and i == len(replies) else str(i)
        out.append({"section": section.strip(), "title": title.strip()[:24], "desc": desc.strip()[:72], "id": key})
    return out


async def send(digits: str, out: dict) -> None:
    text = (out.get("text") or "").strip()
    replies = out.get("replies") or []
    if not replies:
        await wuzapi_client.send_text(digits, text)
        return
    if len(replies) <= 3 and all("<extra>" not in r for r in replies):
        await wuzapi_client.send_buttons(digits, text, [{"DisplayText": r.split("|")[-1][:20], "Id": str(i)} for i, r in enumerate(replies, 1)])
        return
    rows = _rows(replies)
    sections: dict[str, list] = {}
    for r in rows:
        sections.setdefault(r["section"] or "·", []).append({"title": r["title"], "desc": r["desc"], "RowId": r["id"]})
    await wuzapi_client.send_list(digits, text, [{"title": k, "rows": v} for k, v in sections.items()])
