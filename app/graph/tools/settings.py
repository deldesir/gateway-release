"""
⚙️ Operator settings — the cockpit made yours to tune, from chat.

Every cockpit knob used to live in `.env.local` / systemd env, so changing
one meant an edit + a restart (a developer in the loop). This is a small,
box-local settings store the jobs read through `get_setting()`, plus a
`macro_settings` tool so the operator just tells Alva:

  "set my ministry goal to 55"      → set ministry_goal_hours 55
  "mute energy nudges until August" → snooze energy 2026-08-01
  "make the evening wrap compact"   → set evening_wrap compact
  "what are my settings"            → list

Resolution order for any key: settings.json → environment (legacy knob) →
schema default. So existing `.env.local` values keep working until the
operator overrides one from chat. The file is `data/settings.json`
(box-local, gitignored, in the nightly backup); jobs read it fresh each run.

Operator-only (toolset `config`): it changes how the operator's own cockpit
behaves. Never grant it to a tier.
"""

import json
import os
from datetime import datetime

from loguru import logger

SETTINGS_PATH = os.getenv(
    "SETTINGS_PATH", "/opt/iiab/ai-gateway/data/settings.json"
)

# key → how to resolve/validate it. `env` is the legacy knob it falls back to.
SCHEMA: dict[str, dict] = {
    "retail_writes": {
        "type": "bool", "env": "RETAIL_WRITES", "default": False,
        "help": "Changes in the shop from WhatsApp (price, payment) — off until the reads have been walked",
    },
    "ministry_goal_hours": {
        "type": "number", "env": "MINISTRY_GOAL_HOURS", "default": None,
        "help": "Monthly ministry-hours goal (blank = just a running tally)",
    },
    "secretary_reports": {
        "type": "bool", "env": "SECRETARY_REPORTS", "default": False,
        "help": "Congregation-secretary month-end / late-month nudges",
    },
    "ticket_stale_days": {
        "type": "int", "env": "TICKET_STALE_DAYS", "default": 7,
        "help": "Days before an open ticket is flagged as aging",
    },
    "energy_streak_days": {
        "type": "int", "env": "ENERGY_STREAK_DAYS", "default": 5,
        "help": "Consecutive all-work days before the energy nudge fires",
    },
    "shepherd_quiet_days": {
        "type": "int", "env": "SHEPHERD_QUIET_DAYS", "default": 21,
        "help": "Days without a person touch before they count as 'gone quiet'",
    },
    "reminder_channel": {
        "type": "choice", "env": "REMINDER_CHANNEL", "default": "whatsapp",
        "choices": ["whatsapp", "email"], "help": "Where reminders are delivered",
    },
    "notify_channel": {
        "type": "choice", "env": "ADMIN_NOTIFY_CHANNEL", "default": "email",
        "choices": ["email", "whatsapp"], "help": "Default channel for scheduled reports",
    },
    "evening_wrap": {
        "type": "choice", "env": None, "default": "full",
        "choices": ["full", "compact"], "help": "Evening-wrap detail level",
    },
    "evening_wrap_push": {
        "type": "choice", "env": None, "default": "notify",
        "choices": ["off", "notify", "email", "whatsapp", "both"],
        "help": "Push the evening wrap (your day's captures) to a channel; "
                "'notify' follows notify_channel, 'off' keeps it SiYuan-only",
    },
    "daily_brief_push": {
        "type": "choice", "env": None, "default": "notify",
        "choices": ["off", "notify", "email", "whatsapp", "both"],
        "help": "Push the morning brief (day ahead + your open loops) to a "
                "channel; 'notify' follows notify_channel, 'off' keeps it "
                "SiYuan-only. Only pushes when there's something to say.",
    },
    "daily_text_nudge": {
        "type": "bool", "env": "DAILY_TEXT_NUDGE", "default": True,
        "help": "Morning nudge to read the daily text (in JW Library)",
    },
    "midweek_meeting_day": {
        "type": "choice", "env": "MIDWEEK_MEETING_DAY", "default": "",
        "choices": ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "help": "Midweek meeting day → a morning prep nudge (blank = off)",
    },
    "weekend_meeting_day": {
        "type": "choice", "env": "WEEKEND_MEETING_DAY", "default": "",
        "choices": ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "help": "Weekend meeting day → a morning prep nudge (blank = off)",
    },
    "study_goal_days": {
        "type": "int", "env": "STUDY_GOAL_DAYS", "default": 0,
        "help": "Personal-study days/week to aim for (0 = just observe)",
    },
    "field_group": {
        "type": "str", "env": "FIELD_GROUP", "default": "",
        "help": "Your field-service group label in Organized (e.g. 'Gwoup 13') "
                "for the shepherd pump — blank = auto-detect from the roster",
    },
    # ── The assistant itself: read per turn by app/hermes/provider.py ──────────────
    "llm_model": {
        "type": "choice", "env": "LLM_MODEL", "default": "claude-sonnet-5-5",
        "choices": ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "gemini-3.5-flash"],
        "help": "The model for the line (every persona without its own)",
    },
    "llm_model_talkprep": {
        "type": "choice", "env": "LLM_MODEL_TALKPREP", "default": "",
        "choices": ["", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "gemini-3.5-flash"],
        "help": "The model for talk preparation (blank = the line's model)",
    },
    "llm_effort": {
        "type": "choice", "env": "HERMES_EFFORT", "default": "",
        "choices": ["", "low", "medium", "high", "xhigh", "max"],
        "help": "How hard the model thinks (blank = its default, medium)",
    },
    "llm_fallback": {
        "type": "choice", "env": None, "default": "auto",
        "choices": ["auto", "none"],
        "help": "Fall back to a smaller model, then the other provider, when the primary fails",
    },
    "reply_language": {
        "type": "choice", "env": "TALKPREP_LANG", "default": "ht",
        "choices": ["ht", "en"], "help": "Language of the talk-preparation replies",
    },
}
# What the settings dialog shows for a stored value.
LABELS = {
    "claude-fable-5-1": "Fable 5.1", "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5", "gemini-3.5-flash": "Gemini 3.5 Flash",
    "low": "Ba", "medium": "Mwayen", "high": "Wo", "xhigh": "Pi wo", "max": "Maksimòm",
    "auto": "Otomatik", "none": "Fèmen", "ht": "Kreyòl", "en": "English",
    "whatsapp": "WhatsApp", "email": "Imèl", "full": "Konplè", "compact": "Kout",
}

# nudges/sections the operator can mute for a while
SNOOZABLE = {"energy", "radar", "week_ahead", "sitrep", "talk_nudge", "aging",
             "daily_text", "meeting_prep", "spiritual", "shepherd", "capture_idle"}


def _load() -> dict:
    try:
        d = json.loads(open(SETTINGS_PATH).read())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
        os.replace(tmp, SETTINGS_PATH)
        return True
    except OSError as e:
        logger.warning(f"settings: cannot persist: {e}")
        return False


def _coerce(key: str, raw):
    """Type a stored/env value per the schema. Returns None on a bad value."""
    spec = SCHEMA.get(key, {})
    t = spec.get("type")
    if raw is None:
        return None
    try:
        if t == "number":
            return float(raw)
        if t == "int":
            return int(float(raw))
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
        if t == "choice":
            v = str(raw).strip().lower()
            return v if v in spec.get("choices", []) else None
        return str(raw)
    except (ValueError, TypeError):
        return None


def get_setting(key: str):
    """settings.json → env (legacy knob) → schema default, typed."""
    spec = SCHEMA.get(key)
    if spec is None:
        return None
    values = _load().get("values", {})
    if key in values:
        v = _coerce(key, values[key])
        if v is not None:
            return v
    env = spec.get("env")
    if env and os.getenv(env, "").strip() != "":
        v = _coerce(key, os.getenv(env))
        if v is not None:
            return v
    return spec.get("default")


def set_setting(key: str, value) -> tuple[bool, str]:
    if key not in SCHEMA:
        return False, f"unknown setting `{key}`"
    coerced = _coerce(key, value)
    if coerced is None and value not in (None, ""):
        spec = SCHEMA[key]
        hint = ""
        if spec.get("choices"):
            hint = f" (allowed: {', '.join(c or 'blank' for c in spec['choices'])})"
        return False, f"`{value}` isn't valid for `{key}`{hint}"
    d = _load()
    d.setdefault("values", {})[key] = coerced
    if not _save(d):
        return False, "could not save settings"
    return True, f"{key} → {coerced}"


def unset_setting(key: str) -> tuple[bool, str]:
    d = _load()
    if key in d.get("values", {}):
        del d["values"][key]
        _save(d)
        return True, f"{key} reset to default ({get_setting(key)})"
    return True, f"{key} was already at its default"


def is_snoozed(feature: str, now: datetime | None = None) -> bool:
    until = _load().get("snooze", {}).get(feature)
    if not until:
        return False
    try:
        end = datetime.strptime(until, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = (now or datetime.now()).date()
    return today < end


def snooze(feature: str, until: str) -> tuple[bool, str]:
    if feature not in SNOOZABLE:
        return False, f"can't snooze `{feature}` (snoozable: {', '.join(sorted(SNOOZABLE))})"
    try:
        datetime.strptime(until, "%Y-%m-%d")
    except ValueError:
        return False, f"give the date as YYYY-MM-DD (got `{until}`)"
    d = _load()
    d.setdefault("snooze", {})[feature] = until
    if not _save(d):
        return False, "could not save settings"
    return True, f"{feature} muted until {until}"


def unsnooze(feature: str) -> tuple[bool, str]:
    d = _load()
    if feature in d.get("snooze", {}):
        del d["snooze"][feature]
        _save(d)
        return True, f"{feature} un-muted"
    return True, f"{feature} wasn't muted"


def _list_text() -> str:
    lines = ["⚙️ **Your cockpit settings** (chat to change any):"]
    for key, spec in SCHEMA.items():
        val = get_setting(key)
        shown = "—" if val is None else val
        lines.append(f"- `{key}` = **{shown}** · {spec['help']}")
    snz = _load().get("snooze", {})
    if snz:
        lines.append("**Muted:** " + ", ".join(f"{k}→{v}" for k, v in snz.items()))
    return "\n".join(lines)


def macro_settings(args: dict, **kwargs) -> str:
    """Get/set/list operator cockpit settings, or snooze a nudge. The
    assistant maps natural language → structured args:
      set:      {"action":"set","key":"ministry_goal_hours","value":"55"}
      snooze:   {"action":"snooze","key":"energy","until":"2026-08-01"}
      list:     {"action":"list"}
    """
    action = (args.get("action") or "").strip().lower()
    key = (args.get("key") or "").strip()
    value = args.get("value")

    if action in ("list", "get", "show", "") and not key:
        return _list_text()
    if action == "get" and key:
        return f"`{key}` = {get_setting(key)}"
    if action == "set":
        ok, msg = set_setting(key, value)
        return ("✅ " if ok else "🤔 ") + msg
    if action in ("unset", "reset"):
        ok, msg = unset_setting(key)
        return ("✅ " if ok else "🤔 ") + msg
    if action == "snooze":
        ok, msg = snooze(key, str(args.get("until") or "").strip())
        return ("🔕 " if ok else "🤔 ") + msg
    if action in ("unsnooze", "unmute"):
        ok, msg = unsnooze(key)
        return ("🔔 " if ok else "🤔 ") + msg
    return _list_text()


# ── The settings dialog: a WhatsApp list the operator taps through ─────────────────
# Same contract as the directory line's dialog: the flow posts {"text", "step"} and
# gets {"text", "next", "replies"} back; "next" is the step it sends with the reply,
# "" ends the conversation. Rows are "Section|Title<extra>description".
# retail_writes has no row yet: a WhatsApp list holds ten rows and the menu is full (nine settings + Toujou + leave);
# it is set by the typed settings verb until the dialog pages.
DIALOG = [
    ("Modèl", "llm_model", "Modèl liy lan"),
    ("Modèl", "llm_model_talkprep", "Modèl preparasyon diskou"),
    ("Konpòtman", "llm_effort", "Efò"),
    ("Konpòtman", "llm_fallback", "Sekou"),
    ("Konpòtman", "reply_language", "Lang repons yo"),
    ("Kokpit", "reminder_channel", "Kanal rapèl yo"),
    ("Kokpit", "notify_channel", "Kanal rapò yo"),
    ("Kokpit", "evening_wrap", "Rezime aswè a"),
]
_EXIT_WORDS = {"0", "soti", "kite", "exit", "quit", "fini"}
_MENU_WORDS = {"menu", "meni", "reglaj", "settings", "paramet", "paramèt"}


def _label(value) -> str:
    if value in (None, ""):
        return "pa fikse"
    return LABELS.get(str(value), str(value))


def _operator(user_id: str) -> bool:
    from app.graph.tools.capture import _operator_digits
    import re as _re
    digits = _re.sub(r"\D", "", (user_id or "").split(":")[-1])
    return bool(digits) and digits in _operator_digits()


ALLOW_ROW = ("Sekirite", "confirm_allow", "Otorizasyon Toujou")     # the Toujou list, after the settings


def _allow_entries() -> list[dict]:
    from app.graph.tools import confirm
    return confirm.allow_list()


def _menu_payload(lead: str = "") -> dict:
    from app.graph.tools.contact_sync import menu_rows, option
    options = [option(str(i), title, _label(get_setting(key)), section)
               for i, (section, key, title) in enumerate(DIALOG, 1)]
    n = len(_allow_entries())
    options.append(option(str(len(DIALOG) + 1), ALLOW_ROW[2], f"{n} otorizasyon" if n else "okenn", ALLOW_ROW[0]))
    options.append(option("0", "Soti", "", "Fini"))
    text = (lead + "\n\n" if lead else "") + "⚙️ *Reglaj*\nKisa ou vle chanje?"
    return {"text": text, "next": "menu", "replies": menu_rows(options)}


def _allow_payload() -> dict:
    """The Toujou list: every standing authorisation, tap one to revoke it, or clear all."""
    from app.graph.tools.contact_sync import menu_rows, option
    entries = _allow_entries()
    options = [option(str(i), e["label"], e.get("detail", ""), ALLOW_ROW[0]) for i, e in enumerate(entries, 1)]
    if entries:
        options.append(option(str(len(entries) + 1), "Efase tout", "", ALLOW_ROW[0]))
    options.append(option("0", "Retounen", "", "Fini"))
    text = (f"*{ALLOW_ROW[2]}*\nSa yo pa mande konfimasyon ankò. Tape youn pou retire l:"
            if entries else f"*{ALLOW_ROW[2]}*\nOkenn otorizasyon pou kounye a.")
    return {"text": text, "next": "allow", "replies": menu_rows(options)}


def _choices_payload(key: str) -> dict:
    from app.graph.tools.contact_sync import menu_rows, option
    section, _k, title = next(d for d in DIALOG if d[1] == key)
    current = get_setting(key)
    options = []
    for i, choice in enumerate(SCHEMA[key]["choices"], 1):
        mark = " ✓" if choice == current else ""
        options.append(option(str(i), _label(choice) + mark, "", title))
    options.append(option("0", "Retounen", "", "Fini"))
    text = f"*{title}*\nKounye a: {_label(current)}. Chwazi:"
    return {"text": text, "next": f"choose:{key}", "replies": menu_rows(options)}


def _resolve(text: str, options: list[dict]) -> str:
    from app.graph.tools.contact_sync import resolve
    return resolve(text, options)


def settings_dialog(args: dict, **kwargs) -> str:
    """One step of the settings list: operator only; returns the directory dialog's JSON."""
    from app.graph.tools.contact_sync import option
    user_id = args.get("user_id") or ""
    if not _operator(user_id):
        return json.dumps({"text": "⚠️ Ou pa gen aksè nan reglaj yo.", "next": "", "replies": []}, ensure_ascii=False)
    text = (args.get("text") or "").strip()
    step = (args.get("step") or "").strip()
    word = text.lower().lstrip("#/").strip()

    if not step or word in _MENU_WORDS:
        return json.dumps(_menu_payload(), ensure_ascii=False)
    if word in _EXIT_WORDS and step == "menu":
        return json.dumps({"text": "Fini. Ekri *reglaj* lè w vle chanje yon bagay.", "next": "", "replies": []},
                          ensure_ascii=False)
    if step == "menu":
        options = [option(str(i), title, "", section) for i, (section, key, title) in enumerate(DIALOG, 1)]
        options.append(option(str(len(DIALOG) + 1), ALLOW_ROW[2], "", ALLOW_ROW[0]))
        picked = _resolve(text, options)
        if not picked or not 1 <= int(picked) <= len(DIALOG) + 1:
            return json.dumps(_menu_payload("Chwa sa a pa nan lis la."), ensure_ascii=False)
        if int(picked) == len(DIALOG) + 1:
            return json.dumps(_allow_payload(), ensure_ascii=False)
        key = DIALOG[int(picked) - 1][1]
        return json.dumps(_choices_payload(key), ensure_ascii=False)
    if step == "allow":
        from app.graph.tools import confirm
        if word in _EXIT_WORDS:
            return json.dumps(_menu_payload(), ensure_ascii=False)
        entries = _allow_entries()
        options = [option(str(i), e["label"], "", "x") for i, e in enumerate(entries, 1)]
        options.append(option(str(len(entries) + 1), "Efase tout", "", "x"))
        picked = _resolve(text, options)
        if not picked or not 1 <= int(picked) <= len(entries) + 1:
            return json.dumps(_allow_payload(), ensure_ascii=False)
        if int(picked) == len(entries) + 1:
            confirm.disallow_all()
            return json.dumps(_menu_payload("✅ Tout otorizasyon yo retire. M ap mande ankò."), ensure_ascii=False)
        confirm.disallow(entries[int(picked) - 1]["key"])
        return json.dumps(_menu_payload(f"✅ Retire: {entries[int(picked) - 1]['label']}."), ensure_ascii=False)
    if step.startswith("choose:"):
        key = step.split(":", 1)[1]
        if key not in SCHEMA:
            return json.dumps(_menu_payload(), ensure_ascii=False)
        if word in _EXIT_WORDS:
            return json.dumps(_menu_payload(), ensure_ascii=False)
        choices = SCHEMA[key]["choices"]
        options = [option(str(i), _label(c), "", "x") for i, c in enumerate(choices, 1)]
        picked = _resolve(text.replace("✓", "").strip(), options)   # a tapped row carries the mark
        if not picked or not 1 <= int(picked) <= len(choices):
            return json.dumps(_choices_payload(key), ensure_ascii=False)
        value = choices[int(picked) - 1]
        ok, _msg = set_setting(key, value)
        title = next(d[2] for d in DIALOG if d[1] == key)
        lead = f"✅ {title}: {_label(value)}. Li aplike kounye a." if ok else "⚠️ Sa pa t mache."
        return json.dumps(_menu_payload(lead), ensure_ascii=False)
    return json.dumps(_menu_payload(), ensure_ascii=False)
