"""The shop dialog: one word opens it, one tool drives every step, rows are taps (D-014).

Contract (the settings dialog's): the caller posts {"text", "step"} and gets back {"text",
"next", "replies"}; "next" is the step to send with the person's next line, "" ends the
dialog. Rows are "Section|Title<extra>description" (contact_sync.menu_rows). The tool is
stateless: everything it needs for the next step is in the step string, including the shop
chosen by a number bound to several ("@<box>:" prefix), so the same tool serves a flow on
the operator's line and the bridge adapter on a shop's box.
"""
from __future__ import annotations

import json
import re
from datetime import date

from app.graph.tools import retail
from app.graph.tools.contact_sync import menu_rows, option

MENU_WORDS = {"boutik", "shop", "magasin", "magazen", "menu", "meni"}
EXIT_WORDS = {"0", "soti", "kite", "exit", "quit", "fini", "fin"}
BACK_WORDS = {"retounen", "back", "retour", "m"}
MAX_PICK = 8

S: dict[str, dict[str, str]] = {
    "ht": {"title": "🏪 {shop}", "ask": "Kisa ou vle wè?", "sales": "Vant", "sales_d": "{today}", "stock": "Estòk", "stock_d": "yon atik, oswa sa ki ba",
           "price": "Pri", "price_d": "pri yon atik", "debts": "Dèt", "debts_d": "ki moun ki dwe", "shifts": "Kès", "shifts_d": "kès ki louvri",
           "top": "Pi bon vant", "top_d": "semèn sa a", "settings": "Reglaj boutik", "settings_d": "lang, estòk ba, rezime", "leave": "Soti", "back": "Retounen", "menu": "Meni",
           "which_shop": "Ki boutik?", "period": "Ki peryòd?", "today": "Jodi a", "yesterday": "Yè", "week": "Semèn sa a", "last_week": "Semèn pase", "month": "Mwa sa a", "last_month": "Mwa pase",
           "ask_item": "Ki atik? Ekri non an.", "ask_customer": "Ki kliyan? Ekri non an.", "pick": "Kilès?", "low": "Estòk ki ba", "low_d": "lis la",
           "bye": "Dakò. Tape *boutik* lè ou bezwen.", "set_lang": "Lang", "set_low": "Limit estòk ba", "set_eod": "Rezime fen jounen", "ask_low": "Anba konbyen inite? (egzanp: 5)", "on": "Louvri", "off": "Fèmen", "saved": "Anrejistre.", "not_owner": "Sa a se pou mèt boutik la.", "debtors": "Tape yon kliyan pou plis detay."},
    "fr": {"title": "🏪 {shop}", "ask": "Que veux-tu voir ?", "sales": "Ventes", "sales_d": "{today}", "stock": "Stock", "stock_d": "un article, ou le stock bas",
           "price": "Prix", "price_d": "le prix d'un article", "debts": "Dettes", "debts_d": "qui doit", "shifts": "Caisses", "shifts_d": "caisses ouvertes",
           "top": "Meilleures ventes", "top_d": "cette semaine", "settings": "Réglages boutique", "settings_d": "langue, stock bas, résumé", "leave": "Sortir", "back": "Retour", "menu": "Menu",
           "which_shop": "Quelle boutique ?", "period": "Quelle période ?", "today": "Aujourd'hui", "yesterday": "Hier", "week": "Cette semaine", "last_week": "Semaine passée", "month": "Ce mois", "last_month": "Mois passé",
           "ask_item": "Quel article ? Écris son nom.", "ask_customer": "Quel client ? Écris son nom.", "pick": "Lequel ?", "low": "Stock bas", "low_d": "la liste",
           "bye": "D'accord. Tape *boutik* quand tu veux.", "set_lang": "Langue", "set_low": "Seuil de stock bas", "set_eod": "Résumé de fin de journée", "ask_low": "Sous combien d'unités ? (exemple : 5)", "on": "Activé", "off": "Désactivé", "saved": "Enregistré.", "not_owner": "Réservé au propriétaire.", "debtors": "Tape un client pour le détail."},
    "en": {"title": "🏪 {shop}", "ask": "What do you want to see?", "sales": "Sales", "sales_d": "{today}", "stock": "Stock", "stock_d": "an item, or what is low",
           "price": "Price", "price_d": "an item's price", "debts": "Debts", "debts_d": "who owes", "shifts": "Tills", "shifts_d": "open shifts",
           "top": "Best sellers", "top_d": "this week", "settings": "Shop settings", "settings_d": "language, low stock, summary", "leave": "Leave", "back": "Back", "menu": "Menu",
           "which_shop": "Which shop?", "period": "Which period?", "today": "Today", "yesterday": "Yesterday", "week": "This week", "last_week": "Last week", "month": "This month", "last_month": "Last month",
           "ask_item": "Which item? Type its name.", "ask_customer": "Which customer? Type the name.", "pick": "Which one?", "low": "Low stock", "low_d": "the list",
           "bye": "OK. Type *boutik* whenever you need.", "set_lang": "Language", "set_low": "Low-stock threshold", "set_eod": "End-of-day summary", "ask_low": "Under how many units? (for example: 5)", "on": "On", "off": "Off", "saved": "Saved.", "not_owner": "That is for the shop's owner.", "debtors": "Tap a customer for the detail."},
}


def s(lang: str, key: str, **kw) -> str:
    return ((S.get(lang) or S["ht"]).get(key) or S["en"].get(key) or key).format(**kw)


# ── shop settings kept per box, beside the registry the fleet tool overwrites ─────────────

SETTINGS_PATH = retail.REGISTRY_PATH.with_name("retail_settings.json")


def shop_settings(box_id: str) -> dict:
    try:
        return (json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) or {}).get(box_id) or {}
    except (OSError, ValueError):
        return {}


def set_shop_setting(box_id: str, key: str, value) -> None:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        data = {}
    data.setdefault(box_id, {})[key] = value
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")


# ── steps ─────────────────────────────────────────────────────────────────────────────────

def _split_step(step: str) -> tuple[str | None, str]:
    """("box-002", "menu") from "@box-002:menu"; (None, "menu") from "menu"."""
    m = re.match(r"^@([^:]+):(.*)$", step or "")
    return (m.group(1), m.group(2)) if m else (None, step or "")


def _with_box(box: str | None, step: str) -> str:
    return f"@{box}:{step}" if box and step else step


def _resolve(user_id: str, box: str | None):
    """(tenant, lang, binding) honouring a chosen box; the shop's own settings override the registry."""
    b = retail.binding_for(user_id)
    if not b:
        return None, "ht", None
    reg = retail.load_registry()
    tid = box if box in b["tenants"] else b["default"]
    tenant = dict(reg["tenants"][tid], id=tid)
    st = shop_settings(tid)
    if st.get("low_stock"):
        tenant["low_stock"] = st["low_stock"]
    lang = b["lang"] or st.get("lang") or tenant.get("lang") or "ht"
    return tenant, lang, b


def _payload(text: str, nxt: str, options: list[dict] | None = None) -> dict:
    return {"text": text, "next": nxt, "replies": menu_rows(options or [])}


def _end_rows(lang: str) -> list[dict]:
    return [option("m", s(lang, "menu"), "", s(lang, "menu")), option("0", s(lang, "leave"), "", s(lang, "menu"))]


def _pick(text: str, options: list[dict]) -> str | None:
    """The option key the person meant: its key, or its title, case-folded."""
    t = retail._fold((text or "").strip())
    for o in options:
        if t == o["key"] or t == retail._fold(o["title"]):
            return o["key"]
    return None


def _menu(tenant: dict, lang: str, role: str, box: str | None, user_id: str) -> dict:
    rows: list[dict] = []
    sect = s(lang, "title", shop=tenant["alias"])
    if role in ("owner", "manager"):
        rows.append(option("1", s(lang, "sales"), s(lang, "today"), sect))
    rows.append(option("2", s(lang, "stock"), s(lang, "stock_d"), sect))
    rows.append(option("3", s(lang, "price"), s(lang, "price_d"), sect))
    if role in ("owner", "manager"):
        rows.append(option("4", s(lang, "debts"), s(lang, "debts_d"), sect))
    rows.append(option("5", s(lang, "shifts"), s(lang, "shifts_d"), sect))
    if role in ("owner", "manager"):
        rows.append(option("6", s(lang, "top"), s(lang, "top_d"), sect))
    if role == "owner":
        rows.append(option("7", s(lang, "settings"), s(lang, "settings_d"), sect))
    rows.append(option("0", s(lang, "leave"), "", s(lang, "menu")))
    return _payload(s(lang, "title", shop=tenant["alias"]) + "\n" + s(lang, "ask"), _with_box(box, "menu"), rows)


_MENU_STEPS = {"1": "sales", "2": "stock", "3": "price", "4": "debts", "5": "shifts", "6": "top", "7": "settings"}
_PERIODS = [("1", "today"), ("2", "yesterday"), ("3", "week"), ("4", "last_week"), ("5", "month"), ("6", "last_month")]


def _run(tool, user_id: str, text: str, tenant: dict, lang: str) -> str:
    """A read tool's text for this box: the alias is appended so retail.resolve picks the chosen shop."""
    return tool({"text": f"{text} {tenant['alias']}".strip(), "user_id": user_id}, user_id=user_id)


def retail_dialog(args: dict, **kwargs) -> str:
    user_id = kwargs.get("user_id") or (args or {}).get("user_id") or ""
    text = ((args or {}).get("text") or "").strip()
    step_in = ((args or {}).get("step") or "").strip()
    box, step = _split_step(step_in)
    tenant, lang, b = _resolve(user_id, box)
    if not b:
        return json.dumps(_payload(retail.t("ht", "unbound"), ""), ensure_ascii=False)
    role = b.get("role") or "owner"
    low = retail._fold(text)

    # the way out and the way back, on every screen
    if low in EXIT_WORDS and step not in ("stock:item", "price:item", "settings:low"):
        return json.dumps(_payload(s(lang, "bye"), ""), ensure_ascii=False)
    if (low in BACK_WORDS or low in MENU_WORDS) and step != "shop" and not (len(b["tenants"]) > 1 and not box):
        return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)

    # a number bound to several shops chooses one first
    if not step and len(b["tenants"]) > 1 and not box:
        reg = retail.load_registry()
        rows = [option(str(i), reg["tenants"][t].get("alias") or t, reg["tenants"][t].get("name") or "", s(lang, "which_shop")) for i, t in enumerate(b["tenants"], 1)]
        rows.append(option("0", s(lang, "leave"), "", s(lang, "menu")))
        return json.dumps(_payload(s(lang, "which_shop"), "shop", rows), ensure_ascii=False)
    if step == "shop":
        reg = retail.load_registry()
        rows = [option(str(i), reg["tenants"][t].get("alias") or t, "", "") for i, t in enumerate(b["tenants"], 1)]
        key = _pick(text, rows)
        if not key:
            rows.append(option("0", s(lang, "leave"), "", s(lang, "menu")))
            return json.dumps(_payload(s(lang, "which_shop"), "shop", rows), ensure_ascii=False)
        box = b["tenants"][int(key) - 1]
        tenant, lang, b = _resolve(user_id, box)
        return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)

    if not step or step == "menu":
        if not step:
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        key = _pick(text, [option(k, s(lang, v), "", "") for k, v in _MENU_STEPS.items()])
        target = _MENU_STEPS.get(key or "")
        if target == "sales" and role not in ("owner", "manager") or target in ("debts", "top") and role not in ("owner", "manager") or target == "settings" and role != "owner":
            target = None
        if not target:
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        step = target
        text = ""

    if step == "sales":
        rows = [option(k, s(lang, p), "", s(lang, "period")) for k, p in _PERIODS] + [option("0", s(lang, "back"), "", s(lang, "menu"))]
        key = _pick(text, rows) if text else None
        if not key or key == "0":
            if key == "0":
                return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
            return json.dumps(_payload(s(lang, "period"), _with_box(box, "sales"), rows), ensure_ascii=False)
        period = dict(_PERIODS)[key]
        out = _run(retail.retail_sales, user_id, retail.T["en"][period], tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)

    if step in ("stock", "price"):
        rows = [option("1", s(lang, "low"), s(lang, "low_d"), "")] if step == "stock" else []
        return json.dumps(_payload(s(lang, "ask_item"), _with_box(box, f"{step}:item"), rows + [option("0", s(lang, "back"), "", s(lang, "menu"))]), ensure_ascii=False)
    if step in ("stock:item", "price:item"):
        kind = step.split(":")[0]
        if low in EXIT_WORDS or low == "0":
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        if kind == "stock" and (low == "1" or low == retail._fold(s(lang, "low"))):
            out = _run(retail.retail_low_stock, user_id, "", tenant, lang)
            return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
        items = retail.find_items(tenant, text) if text else []
        if len(items) > 1:
            rows = [option(str(i), (it.get("item_name") or it["item_code"])[:24], it["item_code"], s(lang, "pick")) for i, it in enumerate(items[:MAX_PICK], 1)]
            codes = ";".join(it["item_code"] for it in items[:MAX_PICK])
            return json.dumps(_payload(s(lang, "pick"), _with_box(box, f"{kind}:pick:{codes}"), rows + [option("0", s(lang, "back"), "", s(lang, "menu"))]), ensure_ascii=False)
        tool = retail.retail_stock if kind == "stock" else retail.retail_price
        out = _run(tool, user_id, items[0]["item_code"] if items else text, tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
    if step.startswith(("stock:pick:", "price:pick:")):
        kind, _p, codes = step.split(":", 2)
        codes = codes.split(";")
        if low in EXIT_WORDS or low == "0":
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        try:
            code = codes[int(low) - 1]
        except (ValueError, IndexError):
            code = text
        tool = retail.retail_stock if kind == "stock" else retail.retail_price
        out = _run(tool, user_id, code, tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)

    if step == "debts":
        out = _run(retail.retail_debts, user_id, "", tenant, lang)
        names = [ln.split(":")[0].lstrip("• ").strip() for ln in out.splitlines() if ln.startswith("• ")][:MAX_PICK]
        if not names:
            return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
        rows = [option(str(i), n[:24], "", s(lang, "debts")) for i, n in enumerate(names, 1)] + [option("0", s(lang, "back"), "", s(lang, "menu"))]
        return json.dumps(_payload(out + "\n" + s(lang, "debtors"), _with_box(box, "debts:pick:" + ";".join(names)), rows), ensure_ascii=False)
    if step.startswith("debts:pick:"):
        names = step.split(":", 2)[2].split(";")
        if low in EXIT_WORDS or low == "0":
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        try:
            name = names[int(low) - 1]
        except (ValueError, IndexError):
            name = text
        out = _run(retail.retail_customer, user_id, name, tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)

    if step == "shifts":
        out = _run(retail.retail_shifts, user_id, "", tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
    if step == "top":
        out = _run(retail.retail_top_items, user_id, "week", tenant, lang)
        return json.dumps(_payload(out, _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)

    if step == "settings":
        if role != "owner":
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        st = shop_settings(tenant["id"])
        rows = [option("1", s(lang, "set_lang"), (st.get("lang") or lang), s(lang, "settings")),
                option("2", s(lang, "set_low"), str(tenant.get("low_stock") or 5), s(lang, "settings")),
                option("3", s(lang, "set_eod"), s(lang, "on") if st.get("eod") else s(lang, "off"), s(lang, "settings")),
                option("0", s(lang, "back"), "", s(lang, "menu"))]
        key = _pick(text, rows) if text else None
        if key == "1":
            lrows = [option("1", "Kreyòl", "", ""), option("2", "Français", "", ""), option("3", "English", "", ""), option("0", s(lang, "back"), "", s(lang, "menu"))]
            return json.dumps(_payload(s(lang, "set_lang"), _with_box(box, "settings:lang"), lrows), ensure_ascii=False)
        if key == "2":
            return json.dumps(_payload(s(lang, "ask_low"), _with_box(box, "settings:low"), []), ensure_ascii=False)
        if key == "3":
            set_shop_setting(tenant["id"], "eod", not st.get("eod"))
            return json.dumps(_payload(s(lang, "saved"), _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
        if key == "0":
            return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
        return json.dumps(_payload(s(lang, "settings"), _with_box(box, "settings"), rows), ensure_ascii=False)
    if step == "settings:lang":
        choice = {"1": "ht", "2": "fr", "3": "en"}.get(low)
        if choice:
            set_shop_setting(tenant["id"], "lang", choice)
            return json.dumps(_payload(s(choice, "saved"), _with_box(box, "end"), _end_rows(choice)), ensure_ascii=False)
        return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
    if step == "settings:low":
        m = re.search(r"\d+", text)
        if m:
            set_shop_setting(tenant["id"], "low_stock", int(m.group()))
            return json.dumps(_payload(s(lang, "saved"), _with_box(box, "end"), _end_rows(lang)), ensure_ascii=False)
        return json.dumps(_payload(s(lang, "ask_low"), _with_box(box, "settings:low"), []), ensure_ascii=False)

    if step == "end":
        return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
    return json.dumps(_menu(tenant, lang, role, box, user_id), ensure_ascii=False)
