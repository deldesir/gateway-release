"""Closed-set intent: what the router missed is matched to a known verb, or asked about.

Order on the line: the capture verbs, then RiveBot (exact, then spelling-tolerant), then
this, then the model. One small-model call reads the message and may only answer from a
closed list: a known verb, ``chat`` or ``unsure``. A verb is **replayed through RiveBot as
if the person had typed it exactly** (the rule executes, never the model; the model never
sees a tool). ``chat`` goes on to the normal turn. ``unsure`` asks with two buttons: the
verb, or *jis pale* (just chat), in which case the original line goes to the model.

Only operator identities are routed this way by default (``INTENT_CLASSIFIER`` =
``operator`` | ``all`` | ``off``); the verbs are theirs. Needs the Anthropic provider;
with any other provider the line goes to the model as before.
"""

import asyncio
import os
import re
import threading
import time

from loguru import logger

from app.api.middleware.rivebot_client import match_intent
from app.hermes.provider import active_provider, provider_key

INTENT_MODEL = os.getenv("INTENT_MODEL", "claude-haiku-5-5")
MARKER = "⌁"          # ⌁ text ⌁ button|button — the Default AI flow splits a reply on it
ASK_SECONDS = 120

# name: (persona to replay in, {lang: replay text}, what it does, argument kind)
# Replay texts are triggers the brains hold today (English always; Kreyòl where it exists).
INTENTS: dict[str, tuple] = {
    "retail_sales":      (None, {"en": "sales {arg}", "ht": "vant {arg}"}, "the shop's sales for a period", "the period (today, yesterday, this week...)"),
    "retail_stock":      (None, {"en": "stock {arg}", "ht": "estok {arg}"}, "stock on hand of an item at the shop", "the item"),
    "retail_price":      (None, {"en": "price {arg}", "ht": "pri {arg}"}, "the selling price of an item", "the item"),
    "retail_debts":      (None, {"en": "debts", "ht": "det"}, "who owes the shop money", None),
    "retail_shifts":     (None, {"en": "shifts", "ht": "kes"}, "the open till shifts", None),
    "retail_low_stock":  (None, {"en": "low stock", "ht": "estok ba"}, "the items running low", None),
    "talk_status":       ("talkprep", {"en": "status", "ht": "estati"}, "where the talk preparation stands", None),
    "show_talks":        ("talkprep", {"en": "show my talks", "ht": "montre diskou mwen"}, "list the person's talks", None),
    "publications":      ("talkprep", {"en": "show publications", "ht": "piblikasyon"}, "list the source publications", None),
    "topics":            ("talkprep", {"en": "list topics {arg}", "ht": "sijè nan {arg}"}, "list the talks in a publication", "the publication"),
    "import_talk":       ("talkprep", {"en": "import talk {arg}", "ht": "enpòte diskou {arg}"}, "start a new talk by its name", "the talk's name"),
    "use_talk":          ("talkprep", {"en": "use talk {arg}", "ht": "itilize diskou {arg}"}, "pick the talk to work on by its number", "the number"),
    "create_revision":   ("talkprep", {"en": "create revision {arg}", "ht": "kreye revizyon {arg}"}, "create a revision for an audience", "name and audience"),
    "golden_thread":     ("talkprep", {"en": "set golden thread {arg}", "ht": "mete fil wouj {arg}"}, "set the talk's central idea", "the sentence"),
    "challenge_section": ("talkprep", {"en": "challenge section {arg}", "ht": "defi seksyon {arg}"}, "coaching questions on the person's own draft of a section", "the section"),
    "develop_section":   ("talkprep", {"en": "develop section {arg}", "ht": "devlope seksyon {arg}"}, "a reference draft of a section", "the section"),
    "evaluate":          ("talkprep", {"en": "evaluate talk", "ht": "evalye diskou"}, "evaluate the draft against the rubric", None),
    "scores":            ("talkprep", {"en": "scores", "ht": "nòt"}, "the evaluation scores", None),
    "rehearsal":         ("talkprep", {"en": "rehearsal", "ht": "repete"}, "delivery cues for rehearsal", None),
    "export":            ("talkprep", {"en": "export", "ht": "ekspòte"}, "export the final manuscript", None),
    "anki":              ("talkprep", {"en": "generate anki deck", "ht": "kreye kat anki"}, "memory flashcards for the talk", None),
    "to_siyuan":         ("talkprep", {"en": "push to siyuan", "ht": "voye nan siyuan"}, "push the talk to the notes app", None),
    "cost":              ("talkprep", {"en": "cost", "ht": "depans"}, "token usage and cost", None),
    "talk_help":         ("talkprep", {"en": "help", "ht": "ede"}, "how the talk preparation works", None),
    "contacts":          (None, {"en": "contacts", "ht": "kontak"}, "the team contact directory: sync, updates", None),
    "contacts_check":    (None, {"en": "check contacts", "ht": "verifye kontak"}, "check the directory for problems", None),
    "contacts_push":     (None, {"en": "push contacts", "ht": "voye kontak"}, "push directory updates to the team", None),
    "close_ticket":      (None, {"en": "close ticket {arg}", "ht": "fèmen {arg}"}, "close an ICT request by its number", "the number"),
    "crm_ops":           (None, {"en": "ops", "ht": "ops"}, "the CRM operations menu", None),
    "org_info":          (None, {"en": "org info", "ht": "org info"}, "workspace information", None),
    "list_groups":       (None, {"en": "list groups", "ht": "list groups"}, "list the contact groups", None),
    "lookup":            (None, {"en": "lookup {arg}", "ht": "lookup {arg}"}, "look up a contact", "name or number"),
    "create_group":      (None, {"en": "create group {arg}", "ht": "create group {arg}"}, "create a contact group", "the group's name"),
    "health":            (None, {"en": "sysadmin health", "ht": "sysadmin health"}, "health of the services", None),
    "reload":            (None, {"en": "sysadmin reload", "ht": "sysadmin reload"}, "reload the router's brains", None),
    "noai":              (None, {"en": "noai", "ht": "noai"}, "switch the model off for this person", None),
    "enableai":          (None, {"en": "enableai", "ht": "enableai"}, "switch the model back on", None),
    "reset":             (None, {"en": "reset", "ht": "reset"}, "reset the conversation state", None),
}
_JUST_CHAT = {"jis pale", "just chat", "juste parler", "pale", "chat", "non"}
MAX_WORDS = int(os.getenv("INTENT_MAX_WORDS", "10"))     # verbs are short; conversation is not
STORE = os.getenv("INTENT_STORE", "/opt/iiab/ai-gateway/data/pending_intents.json")
_lock = threading.Lock()
_asked: dict[str, dict] = {}


def _load() -> None:
    try:
        import json
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f) or {}
        _asked.update({k: v for k, v in data.items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass


def _save() -> None:
    """Best-effort write-through, so a restart inside the two minutes keeps the question."""
    try:
        import json
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_asked, f, ensure_ascii=False)
        os.replace(tmp, STORE)
    except OSError as e:
        logger.debug(f"[intent] could not persist pending questions: {e}")


_load()


def looks_like_a_verb(text: str) -> bool:
    """Only a short line can be a misspelt or paraphrased verb; a long one is conversation."""
    words = (text or "").split()
    return 0 < len(words) <= MAX_WORDS and "\n" not in (text or "").strip() and "http" not in text.lower()


def _enabled_for(user_id: str) -> bool:
    mode = os.getenv("INTENT_CLASSIFIER", "operator").strip().lower()
    if mode == "off":
        return False
    if mode == "all":
        return True
    from app.graph.tools.capture import _operator_digits
    digits = re.sub(r"\D", "", (user_id or "").split(":")[-1])
    return bool(digits) and digits in _operator_digits()


def _lang(code: str | None) -> str:
    return "en" if (code or "").lower().startswith("en") else "ht"


def replay_text(name: str, arg: str, lang: str) -> str:
    """The exact line to put through the router for an intent (English when the brain has
    no Kreyòl form); "" when the intent needs an argument the message did not give."""
    persona, forms, _desc, arg_kind = INTENTS[name]
    template = forms.get(_lang(lang)) or forms["en"]
    if "{arg}" in template:
        arg = re.sub(r"\s+", " ", (arg or "")).strip()
        if not arg:
            return ""
        return template.replace("{arg}", arg)
    return template


def _tool() -> dict:
    names = list(INTENTS)
    return {
        "name": "route",
        "description": "Route one WhatsApp message to exactly one known verb, or to chat, or say unsure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": names + ["chat", "unsure"]},
                "argument": {"type": "string", "description": "The verb's argument taken from the message, or empty."},
                "guess": {"type": "string", "enum": names + [""], "description": "When unsure: the most likely verb, else empty."},
            },
            "required": ["intent", "argument", "guess"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def _system() -> str:
    lines = ["You route one short WhatsApp message (Haitian Creole, French or English) to a known verb of a "
             "personal assistant. Call the tool `route` exactly once and say nothing else.",
             "Rules: `chat` for conversation, questions, notes, requests the verbs do not cover, or anything "
             "ambiguous enough that a wrong verb would annoy the person. `unsure` only when the message most "
             "likely means one verb but you are not confident; then put that verb in `guess`. Copy the "
             "verb's argument from the message verbatim when the verb takes one.",
             "The verbs:"]
    for name, (_p, _forms, desc, arg_kind) in INTENTS.items():
        lines.append(f"- {name}: {desc}" + (f" (argument: {arg_kind})" if arg_kind else ""))
    return "\n".join(lines)


def _classify(message: str, lang: str) -> dict:
    """One small-model call; a closed answer. Any failure reads as `chat`."""
    import anthropic
    client = anthropic.Anthropic(api_key=provider_key("anthropic"), timeout=8.0, max_retries=1)
    # No temperature (rejected by this model) and no thinking (it ate the token budget before
    # the tool call was complete); verified live: three of three routed, ~1.6k tokens in, ~150 out.
    response = client.messages.create(
        model=INTENT_MODEL, max_tokens=200, thinking={"type": "disabled"}, system=_system(), tools=[_tool()],
        messages=[{"role": "user", "content": f"Language hint: {lang}\nMessage: {message}\n\nCall route once."}],
    )
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "route":
            data = dict(block.input or {})
            return {"intent": str(data.get("intent") or "chat"), "argument": str(data.get("argument") or ""),
                    "guess": str(data.get("guess") or "")}
    return {"intent": "chat", "argument": "", "guess": ""}


def _ask_text(name: str, line: str, lang: str) -> str:
    desc = INTENTS[name][2]
    if _lang(lang) == "en":
        return f"Do you mean *{line}* ({desc}), or just chat?\n{MARKER}{line[:20]}|Just chat"
    return f"Ou vle di *{line}* ({desc}), oswa jis pale?\n{MARKER}{line[:20]}|Jis pale"


def _pending(user_id: str) -> dict | None:
    with _lock:
        entry = _asked.get(user_id)
        if entry and entry["expires"] < time.time():
            _asked.pop(user_id, None)
            _save()
            return None
        return entry


async def route(user_id: str, message: str, persona: str, lang: str | None = None) -> dict | None:
    """None: go on to the model with the message as it is. {"reply", "persona"}: the router
    answered the replayed verb (or the question was asked). {"message"}: the person chose to
    just chat; the model gets this (original) line."""
    text = (message or "").strip()
    if not text or not _enabled_for(user_id):
        return None
    pending = _pending(user_id)
    if pending and text.lower().strip(" .!") in _JUST_CHAT:
        with _lock:
            _asked.pop(user_id, None)
            _save()
        return {"message": pending["original"]}
    if not looks_like_a_verb(text):
        return None                                   # no call spent on conversation
    if active_provider() != "anthropic" or not provider_key("anthropic"):
        return None
    try:
        verdict = await asyncio.to_thread(_classify, text, _lang(lang))
    except Exception as e:
        logger.warning(f"[intent] classifier unavailable: {e}")
        return None
    name = verdict.get("intent") or "chat"
    if name == "chat":
        return None
    if name == "unsure":
        guess = verdict.get("guess") or ""
        line = replay_text(guess, verdict.get("argument", ""), lang) if guess in INTENTS else ""
        if not line:
            return None
        with _lock:
            _asked[user_id] = {"original": text, "expires": time.time() + ASK_SECONDS}
            _save()
        logger.info(f"[intent] unsure → asking about {guess}")
        return {"reply": _ask_text(guess, line, lang), "persona": None}
    if name not in INTENTS:
        return None
    line = replay_text(name, verdict.get("argument", ""), lang)
    if not line:
        return None
    target = INTENTS[name][0] or persona
    response, _ctx = await match_intent(line, target, user_id)
    if response is None or response == "":
        logger.info(f"[intent] {name} → '{line}' did not match the router; to the model")
        return None
    logger.info(f"[intent] {name} → replayed '{line}' in {target}")
    return {"reply": response, "persona": target}
