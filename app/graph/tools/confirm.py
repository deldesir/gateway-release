"""Confirm before change: the model may propose, only a rule may execute — in three tiers.

A **model turn** that calls a tool which changes something is not trusted blindly, and not
distrusted blindly either:

* ``always`` — the change reaches someone else or cannot be undone (a form filed, the
  directory pushed to the team, a flow started for a contact, a command on the box, a file,
  a note deleted, an email to somebody other than the operator). Asked every time, in one
  sentence that says what will happen, with *Wi* / *Non* and, where it makes sense,
  *Toujou*: a standing authorisation for that tool and target, listed and revocable from
  the Settings list.
* ``echo`` — the change is ordinary but a detail could be wrong (a ticket number, a name,
  a key). Asked only when the model supplied a detail the person did not say: every
  argument that matters must appear in the person's own words, accents and case aside.
  "fèmen demann 12" closes ticket 12 without a question.
* ``never`` — the person's own, reversible things (settings, their notes, their facts, the
  model switched off for themselves, an email to themselves). Runs.

The tap is handled before the router and before any model: the stored call runs exactly
as stored. Calls through flows and matched verbs (the tools API) are never held. The
buttons travel as a trailing marker line (``⌁Wi|Non|Toujou``) the Default AI flow splits.
"""

import inspect
import json
import os
import re
import threading
import time
import unicodedata

from loguru import logger

PENDING_SECONDS = 120
MARKER = "⌁"
ALWAYS, ECHO, NEVER = "always", "echo", "never"
_YES = {"wi", "oui", "yes", "ok", "oke", "okay", "dako", "dakò", "konfime", "confirm", "confirme", "✅"}
_NO = {"non", "no", "anile", "cancel", "annule", "annuler", "pa", "✖", "❌"}
_ALWAYS_WORDS = {"toujou", "toujours", "always", "toujou wi", "always yes"}

DATA_DIR = os.getenv("GATEWAY_DATA_DIR", "/opt/iiab/ai-gateway/data")
STORE = os.getenv("CONFIRM_STORE", os.path.join(DATA_DIR, "pending_confirmations.json"))
ALLOW_STORE = os.getenv("CONFIRM_ALLOW_STORE", os.path.join(DATA_DIR, "confirm_allow.json"))
STATS_STORE = os.getenv("CONFIRM_STATS_STORE", os.path.join(DATA_DIR, "confirm_stats.json"))


# ── Tiers ─────────────────────────────────────────────────────────────────────────────
_own_cache: dict[str, tuple[float, set[str]]] = {}


def _own_addresses(urn: str = "") -> set[str]:
    """The caller's own email addresses, found without any setting: an admin identity that
    is an email, the email on the caller's RapidPro contact (looked up once per ten
    minutes), plus the optional ``OPERATOR_EMAILS`` override for what the box cannot know."""
    found = {x.strip().lower() for x in os.getenv("OPERATOR_EMAILS", "").split(",") if x.strip()}
    try:
        from app.graph.tools import email as _email
        admin = _email._admin_email()
        if admin:
            found.add(admin.strip().lower())
        if urn:
            hit = _own_cache.get(urn)
            if hit and hit[0] > time.time() - 600:
                found |= hit[1]
            else:
                own = _email._contact_email(urn)
                addresses = {own.strip().lower()} if own else set()
                _own_cache[urn] = (time.time(), addresses)
                found |= addresses
    except Exception as e:
        logger.debug(f"[confirm] own address lookup: {e}")
    return found


def _send_email_tier(a: dict) -> str:
    to = str(a.get("to") or "").strip().lower()
    if not to:
        return NEVER                                  # the tool sends to the caller
    try:
        from app.hermes.engine import _current_urn
        urn = _current_urn.get() or str(a.get("user_id") or "")
    except Exception:
        urn = str(a.get("user_id") or "")
    return NEVER if to in _own_addresses(urn) else ALWAYS


def _action(a: dict) -> str:
    return str(a.get("action") or "").strip().lower()


_READ_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "df", "du", "free", "uptime", "date", "pwd", "whoami", "id",
    "ps", "top", "ss", "ip", "hostname", "uname", "stat", "file", "which", "echo", "grep", "rg", "find",
    "journalctl", "dmesg", "systemctl", "git", "curl", "dig", "nslookup", "ping", "sqlite3", "psql",
}
_WRITE_HINTS = {
    "systemctl": ("restart", "stop", "start", "enable", "disable", "mask", "reload", "kill", "edit", "set-"),
    "git": ("push", "commit", "reset", "checkout", "merge", "rebase", "clean", "rm", "stash", "pull", "am", "apply"),
    "curl": ("-X", "--request", "-d", "--data", "-F", "--form", "-T", "--upload", "-o", "--output"),
    "find": ("-delete", "-exec", "-ok"),
    "psql": ("-c", "-f"),
    "sqlite3": ("insert", "update", "delete", "drop", "alter", "create"),
}


def _terminal_is_read_only(command: str) -> bool:
    """A conservative allowlist: one plain read command, no redirection, no chaining."""
    cmd = command.strip()
    if not cmd or re.search(r"[>|;&`$]|\bsudo\b|\brm\b|\bmv\b|\bcp\b|\btee\b|\bchmod\b|\bchown\b", cmd):
        return False
    words = cmd.split()
    head = words[0]
    if head not in _READ_COMMANDS:
        return False
    low = " ".join(words[1:]).lower()
    return not any(h.lower() in low for h in _WRITE_HINTS.get(head, ()))


# tool → tier, or a function of the arguments returning one.
TIERS: dict = {
    # the retail line: a payment reaches the shop's books; a price change is checked against the person's words
    "retail_record_payment": ALWAYS, "retail_set_price": ECHO,
    # reaches someone else, or cannot be undone
    "submit_form": ALWAYS, "contact_sync_push": ALWAYS, "start_flow": ALWAYS, "start_crm_ops": ALWAYS,
    "send_crm_help": ALWAYS, "macro_noai_global": ALWAYS, "macro_enableai_global": ALWAYS,
    "organized_field_report": ALWAYS, "siyuan_delete_block": ALWAYS, "siyuan_remove_doc": ALWAYS,
    "siyuan_init_wiki": ALWAYS, "kg_invalidate": ALWAYS, "execute_code": ALWAYS, "process_manage": ALWAYS,
    "write_file": ALWAYS, "patch": ALWAYS,
    "terminal": lambda a: NEVER if _terminal_is_read_only(str(a.get("command") or "")) else ALWAYS,
    "send_email": _send_email_tier,
    # ordinary, but a detail could be wrong
    "ict_close": ECHO, "crm_create_group": ECHO, "macro_label": ECHO, "macro_persona": ECHO,
    "siyuan_rename_doc": ECHO,
    "macro_access": lambda a: NEVER if _action(a) in ("list", "get", "show", "status", "") else ECHO,
    "macro_global": lambda a: ECHO if _action(a) in ("set", "add") else NEVER,
    "macro_channel": lambda a: ECHO if _action(a) in ("assign", "set") else NEVER,
    # the person's own, reversible
    "macro_settings": NEVER, "kg_add": NEVER, "siyuan_append_block": NEVER, "siyuan_create_doc": NEVER,
    "siyuan_create_notebook": NEVER, "siyuan_set_attrs": NEVER, "siyuan_update_block": NEVER,
    "siyuan_upsert_page": NEVER, "macro_noai": NEVER, "macro_enableai": NEVER, "macro_reset": NEVER,
    "macro_reload": NEVER, "macro_flow": NEVER, "macro_elevate": NEVER,
}
CHANGE_TOOLS = TIERS            # the names the registry guard wraps

# The argument that identifies a Toujou authorisation's target (None: the tool as a whole);
# a tool absent here never offers Toujou.
TARGET_KEY: dict[str, str | None] = {
    "retail_record_payment": "customer", "retail_set_price": "item",
    "submit_form": "form_type", "contact_sync_push": None, "start_flow": "flow_identifier",
    "start_crm_ops": None, "send_crm_help": None, "organized_field_report": None,
    "kg_invalidate": None, "write_file": "path", "patch": "path", "terminal": "command", "send_email": "to",
    "ict_close": None, "crm_create_group": None, "macro_label": None, "macro_persona": None,
    "siyuan_rename_doc": None, "siyuan_delete_block": None, "siyuan_remove_doc": None,
    "macro_access": None, "macro_global": "key", "macro_channel": None,
}
# Arguments the echo check ignores: free text the model is expected to write.
_FREE_TEXT = {"user_id", "body", "content", "text", "note", "reason", "data", "message", "summary",
              "description", "markdown", "code", "instructions"}


def tier_for(tool: str, args: dict | None) -> str:
    rule = TIERS.get(tool)
    if rule is None:
        return NEVER
    if isinstance(rule, str):
        return rule
    try:
        return rule(args or {})
    except Exception:
        return ALWAYS                                 # unsure → ask


def is_change(tool: str, args: dict | None) -> bool:
    """Kept for callers that only ask "is this a change at all": any tier above never."""
    return tier_for(tool, args) != NEVER


def _fold(text) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[^\w@.+-]+", " ", plain).split())


def echo_ok(args: dict | None, message: str) -> bool:
    """True when every argument that matters was said by the person: no detail to confirm."""
    said = _fold(message)
    if not said:
        return False
    for key, value in (args or {}).items():
        if key in _FREE_TEXT or key.startswith("active_") or value in (None, "", [], {}):
            continue
        if isinstance(value, (dict, list)):
            continue
        needle = _fold(value)
        if not needle:
            continue
        if isinstance(value, (int, float)) or needle.isdigit():
            if not re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", said):
                return False
        elif needle not in said:
            return False
    return True


def needs_confirmation(tool: str, args: dict | None, message: str) -> tuple[bool, str]:
    """(ask?, tier) for a model-proposed call, the person's words of this turn given."""
    tier = tier_for(tool, args)
    if tier == NEVER:
        return False, tier
    if allowed(tool, args):
        return False, tier
    if tier == ECHO and echo_ok(args, message):
        return False, tier
    return True, tier


# ── Small JSON stores (one process; best effort) ──────────────────────────────────────
_lock = threading.Lock()
_pending: dict[str, dict] = {}
_allow: dict[str, dict] = {}
_stats: dict[str, dict] = {}
_originals: dict[str, object] = {}            # tool → the unguarded handler


def _now() -> float:
    return time.time()


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, TypeError) as e:
        logger.debug(f"[confirm] could not persist {os.path.basename(path)}: {e}")


def _load() -> None:
    _pending.update({k: v for k, v in _read(STORE).items() if isinstance(v, dict) and v.get("expires", 0) > _now()})
    _allow.update({k: v for k, v in _read(ALLOW_STORE).items() if isinstance(v, dict)})
    _stats.update({k: v for k, v in _read(STATS_STORE).items() if isinstance(v, dict)})


def _save() -> None:
    _write(STORE, _pending)


def _bump(tool: str, field: str) -> None:
    with _lock:
        row = _stats.setdefault(tool, {"asked": 0, "wi": 0, "non": 0, "toujou": 0})
        row[field] = int(row.get(field, 0)) + 1
        _stats.setdefault("_since", {"ts": _now()})
        _write(STATS_STORE, _stats)


def stats() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _stats.items() if not k.startswith("_")}


_load()


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip(" .!")


def _key(urn: str) -> str:
    return (urn or "").strip().lower()


# ── Toujou: standing authorisations ──────────────────────────────────────────────────
def _target(tool: str, args: dict | None) -> str:
    key = TARGET_KEY.get(tool)
    return _fold((args or {}).get(key)) if key else ""


def allow_key(tool: str, args: dict | None) -> str | None:
    if tool not in TARGET_KEY:
        return None
    target = _target(tool, args)
    return f"{tool}:{target}" if target else tool


def allowed(tool: str, args: dict | None) -> bool:
    key = allow_key(tool, args)
    with _lock:
        return bool(key) and key in _allow


def allow(tool: str, args: dict | None, lang: str = "ht") -> str | None:
    key = allow_key(tool, args)
    if not key:
        return None
    with _lock:
        _allow[key] = {"tool": tool, "label": tool.replace("macro_", "").replace("_", " "),
                       "detail": _short((args or {}).get(TARGET_KEY.get(tool) or "", ""), 60), "since": _now()}
        _write(ALLOW_STORE, _allow)
    logger.info(f"[confirm] standing authorisation: {key}")
    return key


def disallow(key: str) -> None:
    with _lock:
        _allow.pop(key, None)
        _write(ALLOW_STORE, _allow)


def disallow_all() -> None:
    with _lock:
        _allow.clear()
        _write(ALLOW_STORE, _allow)


def allow_list() -> list[dict]:
    """Every standing authorisation: a short title (fits a WhatsApp row) and its target."""
    with _lock:
        return [{"key": k, "label": v.get("label", k), "detail": v.get("detail", "")} for k, v in sorted(_allow.items())]


# ── Pending changes ──────────────────────────────────────────────────────────────────
def pending_for(urn: str) -> dict | None:
    with _lock:
        entry = _pending.get(_key(urn))
        if entry and entry["expires"] < _now():
            _pending.pop(_key(urn), None)
            _save()
            return None
        return dict(entry) if entry else None


def clear(urn: str) -> None:
    with _lock:
        _pending.pop(_key(urn), None)
        _save()


def _short(v, n: int = 70) -> str:
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v if v is not None else "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


class _Args(dict):
    def __missing__(self, key):
        return "…"


# The question, per tool, saying what will happen — to whom, and whether it can be undone.
_QUESTIONS: dict[str, dict[str, str]] = {
    "retail_record_payment": {"ht": "M anrejistre peman {amount} pou {customer} nan liv boutik la?", "en": "Record the payment of {amount} for {customer} in the shop's books?", "fr": "J'enregistre le paiement de {amount} pour {customer} dans les livres de la boutique ?"},
    "retail_set_price":      {"ht": "M chanje pri {item} pou {price}?", "en": "Change the price of {item} to {price}?", "fr": "Je change le prix de {item} à {price} ?"},
    "send_email":         {"ht": "M voye imèl la bay {to} («{subject}») kounye a?", "en": "Send the email to {to} (“{subject}”) now?", "fr": "J’envoie le courriel à {to} (« {subject} ») maintenant ?"},
    "submit_form":        {"ht": "M voye fòm «{form_type}» lan? Y ap trete l.", "en": "Submit the “{form_type}” form? It will be processed.", "fr": "J’envoie le formulaire « {form_type} » ? Il sera traité."},
    "contact_sync_push":  {"ht": "M voye mizajou anyè a bay tout ekip la?", "en": "Push the directory update to the whole team?", "fr": "J’envoie la mise à jour de l’annuaire à toute l’équipe ?"},
    "start_flow":         {"ht": "M lanse flow «{flow_identifier}» pou {urn}? Moun nan ap resevwa mesaj.", "en": "Start the flow “{flow_identifier}” for {urn}? They will receive messages.", "fr": "Je lance le flow « {flow_identifier} » pour {urn} ? La personne recevra des messages."},
    "start_crm_ops":      {"ht": "M louvri meni operasyon CRM lan?", "en": "Open the CRM operations menu?", "fr": "J’ouvre le menu des opérations CRM ?"},
    "send_crm_help":      {"ht": "M voye èd CRM lan?", "en": "Send the CRM help?", "fr": "J’envoie l’aide CRM ?"},
    "macro_noai_global":  {"ht": "M fèmen AI a pou tout moun?", "en": "Switch the AI off for everyone?", "fr": "Je coupe l’IA pour tout le monde ?"},
    "macro_enableai_global": {"ht": "M relouvri AI a pou tout moun?", "en": "Switch the AI back on for everyone?", "fr": "Je réactive l’IA pour tout le monde ?"},
    "organized_field_report": {"ht": "M voye rapò sèvis la bay kongregasyon an?", "en": "Submit the field service report to the congregation?", "fr": "J’envoie le rapport de service à la congrégation ?"},
    "siyuan_delete_block": {"ht": "M efase blòk nòt sa a ({id})? Sa pa ka defèt.", "en": "Delete this note block ({id})? This cannot be undone.", "fr": "Je supprime ce bloc de note ({id}) ? Irréversible."},
    "siyuan_remove_doc":  {"ht": "M efase dokiman sa a ({path})? Sa pa ka defèt.", "en": "Delete this document ({path})? This cannot be undone.", "fr": "Je supprime ce document ({path}) ? Irréversible."},
    "siyuan_init_wiki":   {"ht": "M reyinisyalize wiki a? Sa pa ka defèt.", "en": "Initialise the wiki? This cannot be undone.", "fr": "J’initialise le wiki ? Irréversible."},
    "kg_invalidate":      {"ht": "M retire fè sa a: {fact}?", "en": "Retire this fact: {fact}?", "fr": "Je retire ce fait : {fact} ?"},
    "execute_code":       {"ht": "M egzekite kòd la sou sèvè a?", "en": "Run the code on the server?", "fr": "J’exécute le code sur le serveur ?"},
    "process_manage":     {"ht": "M {action} pwosesis la sou sèvè a?", "en": "{action} the process on the server?", "fr": "Je {action} le processus sur le serveur ?"},
    "write_file":         {"ht": "M ekri fichye {path} la sou sèvè a?", "en": "Write the file {path} on the server?", "fr": "J’écris le fichier {path} sur le serveur ?"},
    "patch":              {"ht": "M modifye fichye {path} la sou sèvè a?", "en": "Edit the file {path} on the server?", "fr": "Je modifie le fichier {path} sur le serveur ?"},
    "terminal":           {"ht": "M egzekite «{command}» sou sèvè a?", "en": "Run “{command}” on the server?", "fr": "J’exécute « {command} » sur le serveur ?"},
    "ict_close":          {"ht": "M fèmen demann ICT #{number} an? Moun nan ap resevwa yon mesaj.", "en": "Close ICT request #{number}? The requester gets a message.", "fr": "Je ferme la demande ICT n°{number} ? La personne recevra un message."},
    "crm_create_group":   {"ht": "M kreye gwoup «{name}» lan?", "en": "Create the group “{name}”?", "fr": "Je crée le groupe « {name} » ?"},
    "macro_label":        {"ht": "M ajoute etikèt «{name}» lan?", "en": "Add the label “{name}”?", "fr": "J’ajoute l’étiquette « {name} » ?"},
    "macro_persona":      {"ht": "M chanje pèsona a ({action})?", "en": "Change the persona ({action})?", "fr": "Je change le persona ({action}) ?"},
    "siyuan_rename_doc":  {"ht": "M chanje non dokiman an ({title})?", "en": "Rename the document ({title})?", "fr": "Je renomme le document ({title}) ?"},
    "macro_access":       {"ht": "M {action} aksè pou {target} ({tier})? Sa chanje sa moun nan ka fè.", "en": "{action} access for {target} ({tier})? This changes what they can do.", "fr": "Je {action} l’accès de {target} ({tier}) ? Cela change ce que la personne peut faire."},
    "macro_global":       {"ht": "M mete «{key}» = {value} nan RapidPro?", "en": "Set “{key}” = {value} in RapidPro?", "fr": "Je mets « {key} » = {value} dans RapidPro ?"},
    "macro_channel":      {"ht": "M chanje kanal la ({action})? Sa chanje ki pèsona ki reponn.", "en": "Change the channel mapping ({action})? This changes who answers.", "fr": "Je change le canal ({action}) ? Cela change qui répond."},
}
_GENERIC_Q = {"ht": "M fè sa a?", "en": "Do this?", "fr": "Je fais ceci ?"}
_DONE = {"ht": "✅ Fèt.", "en": "✅ Done.", "fr": "✅ Fait."}
_DROPPED = {"ht": "Dakò, m pa fè l.", "en": "All right, not done.", "fr": "D'accord, je ne le fais pas."}
_FAILED = {"ht": "⚠️ Sa pa t mache: ", "en": "⚠️ That did not work: ", "fr": "⚠️ Cela n'a pas marché : "}
_ALWAYS_NOTE = {"ht": "✅ M p ap mande ankò pou sa (retire l nan *reglaj* → Otorizasyon Toujou).",
                "en": "✅ I will not ask again for this (revoke it in *reglaj* → Otorizasyon Toujou).",
                "fr": "✅ Je ne demanderai plus pour ceci (révocable dans *reglaj* → Otorizasyon Toujou)."}


def _lang(code: str | None) -> str:
    code = (code or "ht").lower()
    return "en" if code.startswith("en") else "fr" if code.startswith("fr") else "ht"


def label_for(tool: str, args: dict, lang: str) -> str:
    """The question the person reads: one sentence saying what happens, in their language."""
    lang = _lang(lang)
    shown = _Args({k: _short(v, 60) for k, v in (args or {}).items() if v not in (None, "", [], {})})
    if tool in _QUESTIONS:
        return _QUESTIONS[tool][lang].format_map(shown)
    return _GENERIC_Q[lang] + " " + _summary(tool, args)


def _summary(tool: str, args: dict) -> str:
    shown = []
    for k, v in (args or {}).items():
        if k in _FREE_TEXT or k.startswith("active_") or v in (None, "", [], {}):
            continue
        shown.append(f"{k}: {_short(v)}")
        if len(shown) == 4:
            break
    verb = tool.replace("macro_", "").replace("_", " ")
    return verb + (" — " + "; ".join(shown) if shown else "")


def ask_text(entry: dict) -> str:
    """The question with its buttons; Toujou only where a standing authorisation makes sense."""
    lang = _lang(entry.get("lang"))
    buttons = {"ht": ["Wi", "Non", "Toujou"], "en": ["Yes", "No", "Always"], "fr": ["Oui", "Non", "Toujours"]}[lang]
    if not entry.get("toujou"):
        buttons = buttons[:2]
    return f"{entry['label']}\n{MARKER}{'|'.join(buttons)}"


def request(urn: str, tool: str, args: dict, lang: str | None = None, message: str = "", tier: str = ALWAYS) -> dict:
    """Hold a model-proposed change for the person's tap. A newer request replaces an older one."""
    entry = {
        "tool": tool, "args": dict(args or {}), "label": label_for(tool, args or {}, lang),
        "lang": _lang(lang), "created": _now(), "expires": _now() + PENDING_SECONDS, "urn": urn,
        "message": (message or "")[:2000], "tier": tier, "toujou": tool in TARGET_KEY,
    }
    with _lock:
        _pending[_key(urn)] = entry
        _save()
    _bump(tool, "asked")
    logger.info(f"[confirm] held {tool} ({tier}) for {_mask(urn)}")
    return dict(entry)


def requested_since(urn: str, since: float) -> dict | None:
    entry = pending_for(urn)
    return entry if entry and entry["created"] >= since - 1 else None


def _mask(urn: str) -> str:
    return (urn or "")[:9] + "…" + (urn or "")[-3:] if len(urn or "") > 12 else "***"


def _run_original(tool: str, args: dict) -> str:
    fn = _originals.get(tool)
    if fn is None:
        raise RuntimeError(f"no handler kept for {tool}")
    result = fn(args)
    if inspect.isawaitable(result):
        import asyncio
        result = asyncio.run(result)
    return str(result)


def take(urn: str, text: str, lang: str | None = None) -> dict | None:
    """Handle a *Wi* / *Non* / *Toujou* when something is pending for this caller. None when
    nothing is pending or the message is none of them. Otherwise {"text", "executed",
    "tool", "result", "lang", "label", "message"}. Call it off the event loop."""
    entry = pending_for(urn)
    if not entry:
        return None
    word = _norm(text)
    lang_code = _lang(lang or entry.get("lang"))
    out = {"executed": False, "tool": entry["tool"], "result": "", "lang": lang_code,
           "label": entry["label"], "message": entry.get("message", "")}
    always = word in _ALWAYS_WORDS and entry.get("toujou")
    if word in _YES or always:
        clear(urn)
        _bump(entry["tool"], "toujou" if always else "wi")
        note = ""
        if always:
            allow(entry["tool"], entry["args"], lang_code)
            note = "\n" + _ALWAYS_NOTE[lang_code]
        args = dict(entry["args"])
        args.setdefault("user_id", urn)
        try:
            result = _run_original(entry["tool"], args)
        except Exception as e:
            logger.warning(f"[confirm] {entry['tool']} failed after confirmation: {e}")
            return {**out, "text": _FAILED[lang_code] + str(e)[:200] + note}
        logger.info(f"[confirm] ran {entry['tool']} for {_mask(urn)}")
        body = result.strip()
        text_out = (_DONE[lang_code] if (not body or body[:1] in "{[") else body[:1500]) + note
        return {**out, "executed": True, "result": body[:4000], "text": text_out}
    if word in _NO:
        clear(urn)
        _bump(entry["tool"], "non")
        return {**out, "text": _DROPPED[lang_code]}
    return None


# ── The registry guard ───────────────────────────────────────────────────────────────
import contextvars

_origin: contextvars.ContextVar[str] = contextvars.ContextVar("gateway_call_origin", default="direct")
_turn_lang: contextvars.ContextVar[str] = contextvars.ContextVar("gateway_turn_lang", default="ht")
_turn_message: contextvars.ContextVar[str] = contextvars.ContextVar("gateway_turn_message", default="")
_HOLD_NOTE = ("This change needs the person's confirmation. They have been asked with Wi/Non "
              "buttons; it has NOT been done. Do not claim it was done and do not call the tool "
              "again. Reply with at most one short sentence, or nothing.")


def _from_model(kw: dict) -> bool:
    """A call from a model turn: the engine marks its thread, and Hermes always passes a
    task/session id, which the tools API never does."""
    return _origin.get() == "model" or "task_id" in kw or "session_id" in kw


def _guard(name: str, fn):
    def _hold_if_needed(args):
        ask, tier = needs_confirmation(name, args, _turn_message.get())
        if not ask:
            return None
        from app.hermes.engine import _current_urn
        urn = _current_urn.get() or str((args or {}).get("user_id") or "")
        request(urn, name, args or {}, _turn_lang.get(), _turn_message.get(), tier)
        return json.dumps({"status": "awaiting_confirmation", "note": _HOLD_NOTE})

    if inspect.iscoroutinefunction(fn):
        async def guarded_async(args: dict, **kw):
            if _from_model(kw):
                held = _hold_if_needed(args)
                if held is not None:
                    return held
            return await fn(args, **kw)
        guarded_async.__name__ = getattr(fn, "__name__", name)
        return guarded_async

    def guarded(args: dict, **kw):
        if _from_model(kw):
            held = _hold_if_needed(args)
            if held is not None:
                return held
        return fn(args, **kw)
    guarded.__name__ = getattr(fn, "__name__", name)
    return guarded


def guard_registry(registry) -> int:
    """Wrap every tool that can need a confirmation, once. Returns how many were wrapped."""
    count = 0
    for name, rule in TIERS.items():
        if rule == NEVER:
            continue                                  # never asked: left untouched
        entry = registry._tools.get(name)
        if entry is None or name in _originals:
            continue
        _originals[name] = entry.handler
        entry.handler = _guard(name, entry.handler)
        count += 1
    return count
