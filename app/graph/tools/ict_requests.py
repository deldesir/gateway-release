"""ICT requests from the contact directory line, recorded as RapidPro tickets.

A colleague picks "Demande ICT" in the directory menu, chooses a category and
describes the problem. The request becomes a **RapidPro ticket** (topic
`CONTACT_SYNC_ICT_TOPIC`, default "ICT") on the colleague's own contact, so it
has a lifecycle, an owner and a place in that post's history, and RapidPro's
ticket screen shows the whole conversation.

Who does what:

- **The RapidPro flow opens the ticket.** Nothing outside a flow can (the API
  only lists and acts on tickets), so this module hands the flow the ticket
  text and the flow calls back with the ticket's UUID.
- **This module does the rest:** a second request while one is open becomes a
  note on the open ticket (RapidPro allows one open ticket per contact); the
  operator is told at once; `mes demandes` lists a person's requests; the
  operator's `ferme <n> <note>` closes a ticket and tells the requester.

Ticket numbers are RapidPro's own ids, read from the shared database (the API
exposes only UUIDs). Nothing here writes to that database.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from loguru import logger

ICT_TOPIC = os.getenv("CONTACT_SYNC_ICT_TOPIC", "ICT")
# Directory corrections reported from the field land on their own topic.
DIRECTORY_TOPIC = os.getenv("CONTACT_SYNC_DIRECTORY_TOPIC", "Directory update")
# Open tickets older than this many days are flagged in reports.
AGING_DAYS = int(os.getenv("CONTACT_SYNC_TICKET_AGING_DAYS", "7"))

# Two kinds of request. Each category is (text key, topic the ticket moves to
# once opened, so RapidPro's own ticket screen counts by category).
KINDS = {
    "ict": [("ict_cat_hardware", f"{ICT_TOPIC} Hardware"), ("ict_cat_network", f"{ICT_TOPIC} Network"),
            ("ict_cat_accounts", f"{ICT_TOPIC} Accounts"), ("ict_cat_radio", f"{ICT_TOPIC} Radio"),
            ("ict_cat_other", f"{ICT_TOPIC} Other")],
    "dir": [("dir_cat_number", DIRECTORY_TOPIC), ("dir_cat_holder", DIRECTORY_TOPIC),
            ("dir_cat_wrong", DIRECTORY_TOPIC), ("dir_cat_other", DIRECTORY_TOPIC)],
}
CATEGORIES = [key for key, _ in KINDS["ict"]]  # kept for callers that predate KINDS

# Self-help shown after a category is chosen, before the description is asked:
# {"network": {"fr": "...", "en": "..."}, "hardware": ..., "accounts": ..., "radio": ...}.
# Operator-written, never in git (it names the mission's own tools and steps).
HELP_FILE = os.getenv("CONTACT_SYNC_HELP_FILE", "/opt/iiab/ai-gateway/data/ict_help.json")
STALE_DAYS = int(os.getenv("CONTACT_SYNC_STALE_DAYS", "60"))


def help_for(category_key: str, lang: str) -> str:
    """The self-help text for a category in the reader's language, else French,
    else English, else whatever is written. "" when there is none (then the
    request is asked for directly, as before)."""
    try:
        with open(HELP_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    short = category_key.removeprefix("ict_cat_")
    entry = data.get(short) or data.get(category_key) or {}
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    for code in (lang, "fr", "en", *entry.keys()):
        if str(entry.get(code) or "").strip():
            return str(entry[code]).strip()
    return ""


def _ours(topic_name: str) -> bool:
    """Whether a ticket topic belongs to this feature."""
    return topic_name == ICT_TOPIC or topic_name.startswith(ICT_TOPIC + " ") or topic_name == DIRECTORY_TOPIC


# ── RapidPro reads and actions (API) ─────────────────────────────────────────


def _rp(method: str, endpoint: str, **kwargs) -> dict:
    from app.graph.tools.contact_sync import _RP_AUTH
    from app.graph.tools.rapidpro import _rp_api

    return _rp_api(method, endpoint, **{**_RP_AUTH, **kwargs})


def tickets_of(contact_uuid: str, status: str = "") -> list[dict]:
    """This contact's ICT tickets, newest first. [] on error (fail closed)."""
    from app.graph.tools.contact_sync import _rp_paged

    # The API's own `status` filter is not applied by this RapidPro (it answers
    # every status whatever is asked), so the status is checked here.
    return [t for t in _rp_paged("tickets.json", {"contact": contact_uuid})
            if _ours((t.get("topic") or {}).get("name") or "")
            and (not status or t.get("status") == status)]


def all_tickets() -> list[dict]:
    """Every ticket of this feature in the workspace, newest first. [] on error."""
    from app.graph.tools.contact_sync import _rp_paged

    return [t for t in _rp_paged("tickets.json", {}) if _ours((t.get("topic") or {}).get("name") or "")]


_topic_cache: dict = {"at": 0.0, "map": {}}


def topic_uuid(name: str) -> str:
    """UUID of a ticket topic by name, '' if the workspace has no such topic."""
    import time

    from app.graph.tools.contact_sync import _rp_paged

    if time.monotonic() - _topic_cache["at"] > 300 or name not in _topic_cache["map"]:
        _topic_cache["map"] = {t.get("name"): t.get("uuid") for t in _rp_paged("topics.json", {})}
        _topic_cache["at"] = time.monotonic()
    return _topic_cache["map"].get(name) or ""


def move_to_topic(ticket_uuid: str, topic_name: str) -> bool:
    """Put a ticket on its category topic. Missing topic: stays where it is."""
    uuid = topic_uuid(topic_name)
    if not uuid:
        logger.warning(f"[ict] no topic named {topic_name!r}; ticket keeps the default one")
        return False
    result = _rp("POST", "ticket_actions.json", json={"tickets": [ticket_uuid], "action": "change_topic", "topic": uuid})
    err = str(result.get("error") or "")
    return not err or "Expecting value" in err


def open_ticket_of(contact_uuid: str) -> dict | None:
    found = tickets_of(contact_uuid, "open")
    return found[0] if found else None


def ticket_action(ticket_uuid: str, action: str, note: str = "") -> bool:
    payload = {"tickets": [ticket_uuid], "action": action}
    if note:
        payload["note"] = note[:10_000]
    result = _rp("POST", "ticket_actions.json", json=payload)
    # RapidPro answers 204 with an empty body, which the helper reports as a JSON
    # error; a real failure carries an HTTP status in its message.
    err = str(result.get("error") or "")
    return not err or ("Expecting value" in err)


def contact_by_uuid(uuid: str) -> dict | None:
    data = _rp("GET", "contacts.json", params={"uuid": uuid})
    results = data.get("results") or [] if "error" not in data else []
    return results[0] if results else None


# ── Ticket numbers (shared database, read-only) ──────────────────────────────


def _query(sql: str, params: tuple) -> tuple | None:
    uri = os.getenv("POSTGRES_URI", "")
    if not uri.startswith("postgresql"):
        return None
    try:
        import psycopg2

        conn = psycopg2.connect("postgresql://" + uri.split("://", 1)[1])
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[ict] database read failed: {exc}")
        return None


def ticket_number(ticket_uuid: str) -> int | None:
    """RapidPro's numeric id for a ticket, the number people are given."""
    row = _query("SELECT id FROM tickets_ticket WHERE uuid = %s", (ticket_uuid,))
    return int(row[0]) if row else None


def ticket_by_number(number: int) -> dict | None:
    """{uuid, contact_uuid, status} for an ICT ticket number, or None."""
    row = _query(
        "SELECT t.uuid, c.uuid, t.status FROM tickets_ticket t "
        "JOIN contacts_contact c ON c.id = t.contact_id "
        "JOIN tickets_topic tp ON tp.id = t.topic_id "
        "WHERE t.id = %s AND (tp.name = %s OR tp.name LIKE %s OR tp.name = %s)",
        (int(number), ICT_TOPIC, ICT_TOPIC + " %", DIRECTORY_TOPIC))
    return {"uuid": str(row[0]), "contact_uuid": str(row[1]), "status": row[2]} if row else None


# ── Messages ─────────────────────────────────────────────────────────────────


def _post_label(contact: dict) -> str:
    """How a requester is named to the operator: the post, and who holds it."""
    name = (contact.get("name") or "").strip() or "(no name)"
    holder = str((contact.get("fields") or {}).get("holder") or "").strip()
    return f"{name} ({holder})" if holder else name


def notify_operator(subject: str, text: str) -> None:
    """Email always; WhatsApp too when a channel can carry it. Never raises."""
    from app.graph.tools.email import notify_admin

    for channel in ("email", "whatsapp"):
        try:
            notify_admin(subject, text, channel=channel)
        except Exception as exc:
            logger.warning(f"[ict] operator notification by {channel} failed: {exc}")


def notify_requester(contact: dict, subject: str, text: str) -> bool:
    from app.graph.tools.contact_sync import notify_member_text

    return notify_member_text(contact, subject, text)


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%d/%m")
    except (ValueError, AttributeError):
        return ""


# ── The dialog steps ─────────────────────────────────────────────────────────


def category_options(kind: str, lang: str) -> list[dict]:
    from app.graph.tools.contact_sync import _t, option

    section = _t("sec_categories", lang)
    options = [option(str(i), _t(key + "_t", lang), _t(key, lang), section) for i, (key, _topic) in enumerate(KINDS[kind], 1)]
    return options + [option("0", _t("back", lang), "", section)]


def category_menu(kind: str, lang: str, lead: str = "") -> str:
    from app.graph.tools.contact_sync import _t, menu_text

    title = _t("ict_new_title" if kind == "ict" else "dir_fix_title", lang)
    ask = _t("ask_category" if kind == "ict" else "ask_changed", lang)
    body = menu_text(title, ask, category_options(kind, lang), prompt=_t("prompt_number", lang))
    return f"{lead}\n\n{body}" if lead else body


def _help_options(lang: str) -> list[dict]:
    from app.graph.tools.contact_sync import _t, option

    return [option("1", _t("help_solved", lang)), option("2", _t("help_open", lang)), option("0", _t("back", lang))]


def _help_text(key: str, tip: str, lang: str, lead: str = "") -> str:
    from app.graph.tools.contact_sync import _t, menu_text

    body = menu_text(_t("help_body", lang, category=_t(key + "_t", lang), tip=tip), "", _help_options(lang),
                     prompt=_t("prompt_number", lang))
    return f"{lead}\n\n{body}" if lead else body


def _describe(key: str, lang: str) -> tuple[str, list[dict]]:
    """The prompt for the free text, with its one button: a request describes a
    problem, a correction names the post and the right information."""
    from app.graph.tools.contact_sync import _t, menu_text, option

    cancel = [option("0", _t("cancel", lang))]
    body_key = "dir_describe_body" if key.startswith("dir_") else "describe_body"
    return menu_text(_t(body_key, lang, category=_t(key + "_t", lang)), "", cancel), cancel


def step(name: str, text: str, contact: dict, lang: str, ticket_uuid: str = "") -> tuple[str, str, str, list]:
    """One turn of the request part of the menu (ICT requests and directory corrections).

    Returns (reply, next_step, ticket_text, options). A non-empty `ticket_text`
    asks the flow to open a ticket with it and to call back at the returned step
    with the ticket's UUID; `options` are the reply's tappable choices. Step
    names: `ict_start`/`dir_start`, `ict_cat:<kind>`, `ict_help:<kind>:<n>`,
    `ict_text:<kind>:<n>`, `ict_opened:<kind>:<n>`, `ict_mine`.
    """
    from app.graph.tools.contact_sync import _t, menu_button, resolve

    uid = contact.get("uuid", "")
    parts = name.split(":")
    back = menu_button(lang)   # every closing reply offers the way back

    if name in ("ict_start", "dir_start"):
        kind = name[:3]
        return category_menu(kind, lang), f"ict_cat:{kind}", "", category_options(kind, lang)

    if parts[0] == "ict_cat":
        kind = parts[1] if len(parts) > 1 else "ict"
        options = category_options(kind, lang)
        choice = resolve(text, options)
        if not choice.isdigit() or not 1 <= int(choice) <= len(KINDS[kind]):
            return category_menu(kind, lang, _t("menu_invalid", lang)), name, "", options
        key = KINDS[kind][int(choice) - 1][0]
        tip = help_for(key, lang) if kind == "ict" else ""
        if tip:
            return _help_text(key, tip, lang), f"ict_help:{kind}:{choice}", "", _help_options(lang)
        body, cancel = _describe(key, lang)
        return body, f"ict_text:{kind}:{choice}", "", cancel

    if parts[0] == "ict_text":
        kind, index = parts[1], int(parts[2]) - 1
        key, topic = KINDS[kind][index]
        body = text.strip()
        if len(body) < 3:
            return _t("ict_describe_more", lang), name, "", _describe(key, lang)[1]
        entry = f"[{_t(key, 'en')}] {body}"
        existing = open_ticket_of(uid)
        if existing:
            number = ticket_number(existing["uuid"])
            if ticket_action(existing["uuid"], "add_note", entry):
                notify_operator(f"Request #{number}: new message",
                                f"{_post_label(contact)} added to ticket #{number}:\n\n{entry}")
                logger.info(f"[ict] NOTE_ADDED ticket={number}")
                return _t("ict_added_to_open", lang, number=number or "?"), "", "", back
            return _t("failed", lang), "", "", back
        # No open ticket: the flow opens one with this text and calls back.
        return "", f"ict_opened:{kind}:{index + 1}", entry, []

    if parts[0] == "ict_help":
        # The tip was shown: 1 solved it, 2 opens the request, and a sentence
        # typed straight away is taken as the description itself.
        kind, index = parts[1], parts[2]
        key = KINDS[kind][int(index) - 1][0]
        choice = resolve(text, _help_options(lang))
        if choice == "1":
            logger.info(f"[ict] SELF_HELP_SOLVED category={key}")
            return _t("ict_help_solved", lang), "", "", back
        if choice == "2":
            body, cancel = _describe(key, lang)
            return body, f"ict_text:{kind}:{index}", "", cancel
        if not choice and len(text.strip()) >= 3:
            return step(f"ict_text:{kind}:{index}", text, contact, lang)
        return _help_text(key, help_for(key, lang), lang, _t("menu_invalid", lang)), name, "", _help_options(lang)

    if parts[0] == "ict_opened":
        kind = parts[1] if len(parts) > 1 else "ict"
        index = int(parts[2]) - 1 if len(parts) > 2 else 0
        key, topic = KINDS[kind][index]
        number = ticket_number(ticket_uuid) if ticket_uuid else None
        if ticket_uuid:
            move_to_topic(ticket_uuid, topic)
        details = (open_ticket_of(uid) or {}).get("body") or ""
        label = f"#{number}" if number is not None else "(number unavailable)"
        # The operator is told whatever happened to the number lookup: a
        # request must never be recorded without anyone hearing of it.
        notify_operator(f"{'Directory update' if kind == 'dir' else 'ICT request'} {label}: {_post_label(contact)}",
                        f"{_post_label(contact)} opened ticket {label} ({_t(key, 'en')}):\n\n{details}")
        if not ticket_uuid or number is None:
            logger.error(f"[ict] TICKET_NUMBER_UNKNOWN uuid={ticket_uuid[:8] if ticket_uuid else '-'}")
            return _t("ict_opened_no_number", lang), "", "", back
        logger.info(f"[ict] TICKET_OPENED ticket={number} kind={kind}")
        return _t("ict_opened", lang, number=number), "", "", back

    if name == "ict_mine":
        mine = tickets_of(uid)[:5]
        if not mine:
            return _t("ict_none", lang), "", "", back
        lines = []
        for t in mine:
            number = ticket_number(t.get("uuid", ""))
            state = _t("ict_status_open" if t.get("status") == "open" else "ict_status_closed", lang)
            lines.append(f"#{number or '?'} \u00b7 {_when(t.get('opened_on', ''))} \u00b7 {state}\n   {(t.get('body') or '')[:80]}")
        return _t("ict_mine", lang, tickets="\n".join(lines)), "", "", back

    return "", "", "", []


# ── Reports ──────────────────────────────────────────────────────────────────


def _parse(stamp: str):
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def ticket_figures(since: datetime, now: datetime | None = None) -> dict:
    """Opened / closed since `since`, still open, and open ones older than AGING_DAYS."""
    now = now or datetime.now(timezone.utc)
    tickets = all_tickets()
    opened = [t for t in tickets if (_parse(t.get("opened_on")) or now) >= since]
    closed = [t for t in tickets if t.get("status") == "closed" and (_parse(t.get("closed_on")) or since) >= since]
    open_now = [t for t in tickets if t.get("status") == "open"]
    aging = [t for t in open_now if (now - (_parse(t.get("opened_on")) or now)).days >= AGING_DAYS]
    by_topic: dict[str, int] = {}
    for t in opened:
        name = (t.get("topic") or {}).get("name") or ICT_TOPIC
        by_topic[name] = by_topic.get(name, 0) + 1
    return {"opened": len(opened), "closed": len(closed), "open": len(open_now), "aging": aging,
            "by_topic": by_topic, "open_list": open_now}


def figures_lines(fig: dict, lang: str = "en", detail: bool = True) -> list[str]:
    """The report block for the evening wrap (detail: open tickets listed with
    their text) and the sitrep (counts only: it leaves the box)."""
    fr = lang == "fr"
    lines = [(f"Ouvertes {fig['opened']} · closées {fig['closed']} · en cours {fig['open']}" if fr
              else f"Opened {fig['opened']} · closed {fig['closed']} · open {fig['open']}")]
    if fig["by_topic"]:
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in sorted(fig["by_topic"].items())))
    for t in (fig["open_list"][:8] if detail else []):
        number = ticket_number(t.get("uuid", ""))
        who = (t.get("contact") or {}).get("name") or "?"
        old = " ⚠️" if t in fig["aging"] else ""
        lines.append(f"  #{number or '?'} {who} · {_when(t.get('opened_on', ''))} · {(t.get('body') or '')[:60]}{old}")
    if detail and len(fig["open_list"]) > 8:
        lines.append(f"  … +{len(fig['open_list']) - 8}")
    if not detail and fig["aging"]:
        lines.append(("  dont en attente depuis plus de %d jours : %d" if fr else "  of which older than %d days: %d") % (AGING_DAYS, len(fig["aging"])))
    return lines


def directory_drift(now: datetime | None = None) -> dict:
    """Counts only (they leave the box in the sitrep): posts with no holder,
    listed numbers that have not written for STALE_DAYS, and never."""
    from app.graph.tools import contact_sync as cs

    now = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    posts = no_holder = numbers = stale = never = 0
    stale_names: list[str] = []
    for key in cs.list_teams():
        for raw in cs.node_listing(key):
            uid = raw.get("uuid")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            if cs._field(raw, "kind") == "post":
                posts += 1
                if not cs._field(raw, "holder"):
                    no_holder += 1
            if not cs._phone_of(raw):
                continue
            numbers += 1
            last = _parse(raw.get("last_seen_on") or "")
            if last is None:
                never += 1
            elif (now - last).days >= STALE_DAYS:
                stale += 1
                stale_names.append(raw.get("name") or uid[:8])
    return {"posts": posts, "no_holder": no_holder, "numbers": numbers, "stale": stale, "never": never,
            "stale_names": stale_names, "days": STALE_DAYS}


def drift_lines(drift: dict, lang: str = "en") -> list[str]:
    """One line for the sitrep: the directory's drift this week."""
    if lang == "fr":
        return [f"Annuaire\u00a0: {drift['posts']} postes, {drift['no_holder']} sans titulaire \u00b7 "
                f"{drift['stale']} num\u00e9ros silencieux depuis {drift['days']} j, {drift['never']} jamais vus"]
    return [f"Directory: {drift['posts']} posts, {drift['no_holder']} without holder \u00b7 "
            f"{drift['stale']} numbers silent for {drift['days']} d, {drift['never']} never seen"]


def check_report() -> str:
    """Operator report on the directory's data quality and the sync ledger."""
    from app.graph.tools import contact_sync as cs
    from app.jobs.contact_sync_notify import STATE_FILE, _load_json

    numbers: dict[str, list[str]] = {}
    no_number, no_holder, no_function = [], [], []
    seen: set[str] = set()
    for key in cs.list_teams():
        for raw in cs.node_listing(key):
            uid = raw.get("uuid")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            name = raw.get("name") or uid[:8]
            phones = cs._phone_of(raw)
            for p in phones:
                numbers.setdefault(p, []).append(name)
            kind = cs._field(raw, "kind")
            if not phones and not cs._email_of(raw):
                no_number.append(name)
            if kind == "post" and not cs._field(raw, "holder"):
                no_holder.append(name)
            if kind == "post" and not cs._function_code(cs._field(raw, "function")):
                no_function.append(name)
    shared = {p: names for p, names in numbers.items() if len(names) > 1}

    state = _load_json(STATE_FILE).get("teams", {})
    behind, unreachable = [], []
    for key, node in state.items():
        version = int(node.get("version", 0))
        for uid, v in (node.get("delivered") or {}).items():
            if int(v) < version:
                behind.append(f"{cs.team_label(key)}: {uid[:8]} v{v}/{version}")
        for uid, v in (node.get("unreachable") or {}).items():
            if int(v) >= version:
                unreachable.append(f"{cs.team_label(key)}: {uid[:8]}")

    fig = ticket_figures(datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7))
    drift = directory_drift()
    lines = [f"📇 Directory check — {len(seen)} contacts",
             f"Numbers used twice ({len(shared)}):"] + [f"  {p[:-4]}xxxx: {', '.join(n)}" for p, n in shared.items()]
    lines += [f"No number or email ({len(no_number)}): {', '.join(no_number) or '—'}",
              f"Posts with no holder ({len(no_holder)}): {', '.join(no_holder) or '—'}",
              f"Posts with no function ({len(no_function)}): {', '.join(no_function) or '—'}",
              f"Members behind ({len(behind)}): {', '.join(behind) or '—'}",
              f"Unreachable ({len(unreachable)}): {', '.join(unreachable) or '—'}",
              f"Silent for {STALE_DAYS}+ days ({len(drift['stale_names'])}): {', '.join(drift['stale_names']) or '—'}",
              f"Requests, last 7 days: " + " / ".join(figures_lines(fig))]
    return "\n".join(lines)


def macro_contact_check(args: dict, **kwargs) -> str:
    """Operator command: the directory check, by email (it is long)."""
    from app.graph.tools.contact_sync import _deliver_async

    def _send() -> None:
        from app.graph.tools.email import notify_admin
        try:
            notify_admin("Directory check", check_report(), channel="email")
        except Exception:
            logger.exception("[ict] CHECK_CRASHED")

    _deliver_async(_send)
    return "Checking the directory; the report will come by email."


# ── Operator command ─────────────────────────────────────────────────────────


def macro_ict_close(args: dict, **kwargs) -> str:
    """`ferme <number> [note]`: close an ICT ticket and tell the requester."""
    from app.graph.tools.contact_sync import _t

    raw = (args or {}).get("_args") or []
    raw = " ".join(raw) if isinstance(raw, list) else str(raw)
    raw = raw.strip() or f"{(args or {}).get('number') or ''} {(args or {}).get('note') or ''}".strip()
    match = re.match(r"^#?(\d+)\s*(.*)$", raw, re.S)
    if not match:
        return "Usage: ferme <number> [note]"
    number, note = int(match.group(1)), match.group(2).strip()

    ticket = ticket_by_number(number)
    if not ticket:
        return f"No ICT ticket #{number}."
    if ticket["status"] != "O":
        return f"Ticket #{number} is already closed."
    if note and not ticket_action(ticket["uuid"], "add_note", note):
        return f"Could not add the note to ticket #{number}."
    if not ticket_action(ticket["uuid"], "close"):
        return f"Could not close ticket #{number}."
    logger.info(f"[ict] TICKET_CLOSED ticket={number}")

    requester = contact_by_uuid(ticket["contact_uuid"]) or {}
    lang = requester.get("language") or ""
    told = notify_requester(requester, _t("ict_closed_subject", lang, number=number),
                            _t("ict_closed", lang, number=number, note=note or "—"))
    return f"Ticket #{number} closed." + ("" if told else " The requester could not be reached.")
