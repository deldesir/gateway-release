"""The retail line: a shop's box on WhatsApp.

The tenant comes from the caller's binding, never from the message. ``data/tenants.json`` is
written by the fleet tool (``fleet assistant push``) and holds, per box, the address of its site
and the assistant user's key pair, and, per number, the boxes that number may ask about. Every
tool here resolves the caller first and runs against that box under that user's permissions
(Frappe's own permission system does the filtering); the model never names a site.

Reads are free of any confirmation. The two writes are registered under the confirmation tiers
and refused altogether until the ``retail_writes`` setting is on.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

REGISTRY_PATH = Path(os.getenv("RETAIL_TENANTS_PATH", "data/tenants.json"))
TIMEOUT = float(os.getenv("RETAIL_TIMEOUT", "12"))
LIST_LIMIT = 10

# ── language ──────────────────────────────────────────────────────────────────────────────

T: dict[str, dict[str, str]] = {
    "ht": {
        "unbound": "Nimewo sa a pa mare ak okenn boutik. Mande operatè a pou l mare l.",
        "which_box": "Ki boutik? Ekri non an apre vèb la (egzanp: «vant jodi a {alias}»).",
        "down": "Bwat {shop} la pa reponn kounye a. Eseye ankò nan yon ti moman.",
        "refused": "Bwat {shop} la refize demann lan (aksè a).",
        "sales": "🛍️ Vant {label} · {shop}",
        "no_sales": "Pa gen vant {label} nan {shop}.",
        "invoices": "{n} fakti · total {total}",
        "unpaid": "Poko peye: {amount}",
        "by_cashier": "Pa kesye",
        "stock": "📦 Estòk · {shop}",
        "no_item": "Pa jwenn atik «{q}» nan {shop}.",
        "which_item": "Plizyè atik matche «{q}». Kilès?",
        "stock_line": "{name}: {qty} {uom}",
        "warehouse": "nan {warehouse}: {qty}",
        "low_stock": "📦 Estòk ki ba ({threshold} oswa mwens) · {shop}",
        "no_low": "Okenn atik anba {threshold} nan {shop}.",
        "price": "🏷️ Pri · {shop}",
        "no_price": "Pa gen pri vann pou «{name}» nan {shop}.",
        "price_line": "{name}: {rate} / {uom}",
        "debts": "💳 Dèt kliyan · {shop}",
        "no_debts": "Pèsonn pa dwe {shop} anyen.",
        "debt_line": "{customer}: {due} ({n} fakti, pi ansyen {days} jou)",
        "debts_total": "Total: {total} sou {n} kliyan",
        "customer": "👤 {name} · {shop}",
        "no_customer": "Pa jwenn kliyan «{q}» nan {shop}.",
        "customer_due": "Li dwe {due} sou {n} fakti; dènye acha {last}.",
        "customer_clear": "Li pa dwe anyen; dènye acha {last}.",
        "shifts": "🧾 Kès louvri · {shop}",
        "no_shifts": "Pa gen kès louvri nan {shop}.",
        "shift_line": "{user} depi {since} ({hours} èdtan)",
        "top": "🏆 Pi bon vant {label} · {shop}",
        "no_top": "Pa gen vant {label} nan {shop}.",
        "top_line": "{name}: {qty} vann, {amount}",
        "writes_off": "Chanjman nan boutik la fèmen. Louvri l nan «reglaj» anvan.",
        "price_set": "Pri {name} kounye a {rate} / {uom} nan {shop}.",
        "payment_recorded": "Peman {amount} {mode} pou {customer} anrejistre nan {shop} ({invoice}).",
        "no_unpaid": "{customer} pa gen fakti ki poko peye nan {shop}.",
        "bad_amount": "Di m kantite lajan an (egzanp: 500).",
        "need_item": "Di m ki atik (egzanp: «pri savon»).",
        "need_customer": "Di m ki kliyan.",
        "today": "jodi a", "yesterday": "yè", "week": "semèn sa a", "last_week": "semèn pase", "month": "mwa sa a", "last_month": "mwa pase",
        "and_more": "… ak {n} lòt",
    },
    "fr": {
        "unbound": "Ce numéro n'est lié à aucune boutique. Demande à l'opérateur de le lier.",
        "which_box": "Quelle boutique ? Écris son nom après le verbe (exemple : « ventes du jour {alias} »).",
        "down": "La machine de {shop} ne répond pas pour l'instant. Réessaie dans un moment.",
        "refused": "La machine de {shop} a refusé la demande (accès).",
        "sales": "🛍️ Ventes {label} · {shop}",
        "no_sales": "Pas de vente {label} à {shop}.",
        "invoices": "{n} factures · total {total}",
        "unpaid": "Impayé : {amount}",
        "by_cashier": "Par caissier",
        "stock": "📦 Stock · {shop}",
        "no_item": "Aucun article « {q} » à {shop}.",
        "which_item": "Plusieurs articles correspondent à « {q} ». Lequel ?",
        "stock_line": "{name} : {qty} {uom}",
        "warehouse": "à {warehouse} : {qty}",
        "low_stock": "📦 Stock bas ({threshold} ou moins) · {shop}",
        "no_low": "Aucun article sous {threshold} à {shop}.",
        "price": "🏷️ Prix · {shop}",
        "no_price": "Pas de prix de vente pour « {name} » à {shop}.",
        "price_line": "{name} : {rate} / {uom}",
        "debts": "💳 Dettes clients · {shop}",
        "no_debts": "Personne ne doit rien à {shop}.",
        "debt_line": "{customer} : {due} ({n} factures, la plus ancienne {days} j)",
        "debts_total": "Total : {total} sur {n} clients",
        "customer": "👤 {name} · {shop}",
        "no_customer": "Aucun client « {q} » à {shop}.",
        "customer_due": "Doit {due} sur {n} factures ; dernier achat {last}.",
        "customer_clear": "Ne doit rien ; dernier achat {last}.",
        "shifts": "🧾 Caisses ouvertes · {shop}",
        "no_shifts": "Aucune caisse ouverte à {shop}.",
        "shift_line": "{user} depuis {since} ({hours} h)",
        "top": "🏆 Meilleures ventes {label} · {shop}",
        "no_top": "Pas de vente {label} à {shop}.",
        "top_line": "{name} : {qty} vendus, {amount}",
        "writes_off": "Les changements dans la boutique sont fermés. Ouvre-les dans « réglages » d'abord.",
        "price_set": "Le prix de {name} est maintenant {rate} / {uom} à {shop}.",
        "payment_recorded": "Paiement de {amount} {mode} pour {customer} enregistré à {shop} ({invoice}).",
        "no_unpaid": "{customer} n'a pas de facture impayée à {shop}.",
        "bad_amount": "Dis-moi le montant (exemple : 500).",
        "need_item": "Dis-moi quel article (exemple : « prix savon »).",
        "need_customer": "Dis-moi quel client.",
        "today": "du jour", "yesterday": "d'hier", "week": "de la semaine", "last_week": "de la semaine passée", "month": "du mois", "last_month": "du mois passé",
        "and_more": "… et {n} autres",
    },
    "en": {
        "unbound": "This number is not bound to any shop. Ask the operator to bind it.",
        "which_box": "Which shop? Put its name after the verb (for example: “sales today {alias}”).",
        "down": "The {shop} box is not answering right now. Try again in a moment.",
        "refused": "The {shop} box refused the request (access).",
        "sales": "🛍️ Sales {label} · {shop}",
        "no_sales": "No sales {label} at {shop}.",
        "invoices": "{n} invoices · total {total}",
        "unpaid": "Unpaid: {amount}",
        "by_cashier": "By cashier",
        "stock": "📦 Stock · {shop}",
        "no_item": "No item “{q}” at {shop}.",
        "which_item": "Several items match “{q}”. Which one?",
        "stock_line": "{name}: {qty} {uom}",
        "warehouse": "at {warehouse}: {qty}",
        "low_stock": "📦 Low stock ({threshold} or fewer) · {shop}",
        "no_low": "No item under {threshold} at {shop}.",
        "price": "🏷️ Price · {shop}",
        "no_price": "No selling price for “{name}” at {shop}.",
        "price_line": "{name}: {rate} / {uom}",
        "debts": "💳 Customer debts · {shop}",
        "no_debts": "Nobody owes {shop} anything.",
        "debt_line": "{customer}: {due} ({n} invoices, oldest {days} d)",
        "debts_total": "Total: {total} across {n} customers",
        "customer": "👤 {name} · {shop}",
        "no_customer": "No customer “{q}” at {shop}.",
        "customer_due": "Owes {due} on {n} invoices; last purchase {last}.",
        "customer_clear": "Owes nothing; last purchase {last}.",
        "shifts": "🧾 Open shifts · {shop}",
        "no_shifts": "No open shift at {shop}.",
        "shift_line": "{user} since {since} ({hours} h)",
        "top": "🏆 Top sellers {label} · {shop}",
        "no_top": "No sales {label} at {shop}.",
        "top_line": "{name}: {qty} sold, {amount}",
        "writes_off": "Changes in the shop are off. Turn them on in “settings” first.",
        "price_set": "{name} now sells at {rate} / {uom} at {shop}.",
        "payment_recorded": "Payment of {amount} {mode} for {customer} recorded at {shop} ({invoice}).",
        "no_unpaid": "{customer} has no unpaid invoice at {shop}.",
        "bad_amount": "Tell me the amount (for example: 500).",
        "need_item": "Tell me which item (for example: “price soap”).",
        "need_customer": "Tell me which customer.",
        "today": "today", "yesterday": "yesterday", "week": "this week", "last_week": "last week", "month": "this month", "last_month": "last month",
        "and_more": "… and {n} more",
    },
}


def t(lang: str, key: str, **kw) -> str:
    table = T.get(lang) or T["ht"]
    return (table.get(key) or T["en"].get(key) or key).format(**kw)


# ── registry and bindings ─────────────────────────────────────────────────────────────────

_cache: dict = {"mtime": None, "data": None}


def load_registry() -> dict:
    """{"tenants": {box_id: {...}}, "bindings": {digits: {...}}}; empty when the file is absent."""
    try:
        mtime = REGISTRY_PATH.stat().st_mtime
    except OSError:
        return {"tenants": {}, "bindings": {}}
    if _cache["mtime"] != mtime:
        try:
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _cache.update(mtime=mtime, data={"tenants": data.get("tenants") or {}, "bindings": data.get("bindings") or {}})
    return _cache["data"]


def _digits(user_id: str) -> str:
    return re.sub(r"\D", "", (user_id or "").split(":")[-1])


def binding_for(user_id: str) -> dict | None:
    """The caller's binding, or None: {"tenants": [ids], "default": id, "role": ..., "lang": ...}."""
    reg = load_registry()
    b = reg["bindings"].get(_digits(user_id))
    if not b:
        return None
    tenants = [x for x in (b.get("tenants") or []) if x in reg["tenants"]]
    if not tenants:
        return None
    return {"tenants": tenants, "default": b.get("default") if b.get("default") in tenants else tenants[0],
            "role": b.get("role") or "owner", "lang": b.get("lang") or ""}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def resolve(user_id: str, text: str = "") -> tuple[dict | None, str, str, str | None]:
    """(tenant, remaining text, lang, error key). The box is the caller's default, or the alias
    the caller named in the text (any of their boxes), never anything the message invents."""
    b = binding_for(user_id)
    if not b:
        return None, text, "ht", "unbound"
    reg = load_registry()
    words = (text or "").split()
    chosen = b["default"]
    rest = words
    for tid in b["tenants"]:
        alias = _fold(reg["tenants"][tid].get("alias") or tid)
        for i, w in enumerate(words):
            if _fold(w) == alias:
                chosen = tid
                rest = [x for j, x in enumerate(words) if j != i and _fold(x) not in ("nan", "a", "at", "chez", "de", "du", "bay", "pou", "pour", "for")]
                break
        if chosen == tid and rest is not words:
            break
    tenant = dict(reg["tenants"][chosen], id=chosen)
    lang = b["lang"] or tenant.get("lang") or "ht"
    return tenant, " ".join(rest).strip(), lang, None


# ── the box's API ─────────────────────────────────────────────────────────────────────────

class BoxError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail)
        self.kind = kind


def _request(tenant: dict, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    """One HTTP call to the tenant's site as its assistant user. Raises BoxError(kind)."""
    url = tenant["url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {tenant['key']}:{tenant['secret']}",
        "Accept": "application/json", "Content-Type": "application/json",
        **({"Host": tenant["host"]} if tenant.get("host") else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=float(tenant.get("timeout") or TIMEOUT)) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BoxError("refused" if e.code in (401, 403) else "down", f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BoxError("down", str(getattr(e, "reason", e))) from e


def get_list(tenant: dict, doctype: str, fields: list, filters: list | None = None, *, group_by: str | None = None,
             order_by: str | None = None, limit: int = 50, parent: str | None = None, or_filters: list | None = None) -> list[dict]:
    # aggregates are dicts ({"SUM": "amount", "as": "amount"}): this frappe refuses SQL functions written as strings
    params: dict = {"doctype": doctype, "fields": fields, "limit_page_length": limit}
    if filters:
        params["filters"] = filters
    if or_filters:
        params["or_filters"] = or_filters
    if group_by:
        params["group_by"] = group_by
    if order_by:
        params["order_by"] = order_by
    if parent:
        params["parent"] = parent
    return _request(tenant, "GET", "/api/method/frappe.client.get_list", params).get("message") or []


def call(tenant: dict, method: str, **kw) -> dict:
    return _request(tenant, "POST", f"/api/method/{method}", body=kw).get("message") or {}


# ── helpers ───────────────────────────────────────────────────────────────────────────────

def money(x, cur: str) -> str:
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        v = 0.0
    s = f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"
    return f"{s} {cur}".strip()


def qty(x) -> str:
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        return str(x)
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


_PERIOD_WORDS = {
    "today": ("today", "jodi", "jodia", "jodi a", "jounen", "aujourd'hui", "aujourdhui", "jour"),
    "yesterday": ("yesterday", "ye", "yè", "hier"),
    "week": ("week", "semen", "semèn", "semaine"),
    "last_week": ("last week", "semen pase", "semèn pase", "semaine passee", "semaine passée", "semaine derniere"),
    "month": ("month", "mwa", "mois"),
    "last_month": ("last month", "mwa pase", "mois passe", "mois passé", "mois dernier"),
}


def period_of(text: str, today: date | None = None) -> tuple[date, date, str]:
    """(start, end, label key) from free words in three languages; a date as dd/mm or yyyy-mm-dd; default today."""
    today = today or date.today()
    f = _fold(text or "")
    for key, words in (("last_week", _PERIOD_WORDS["last_week"]), ("last_month", _PERIOD_WORDS["last_month"]),
                       ("yesterday", _PERIOD_WORDS["yesterday"]), ("week", _PERIOD_WORDS["week"]),
                       ("month", _PERIOD_WORDS["month"]), ("today", _PERIOD_WORDS["today"])):
        if any(_fold(w) in f for w in words):
            break
    else:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", f) or re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", f)
        if m:
            try:
                if len(m.group(1)) == 4:
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    d = date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1)))
                return d, d, d.strftime("%d/%m")
            except ValueError:
                pass
        key = "today"
    if key == "today":
        return today, today, key
    if key == "yesterday":
        y = today - timedelta(days=1)
        return y, y, key
    if key == "week":
        return today - timedelta(days=today.weekday()), today, key
    if key == "last_week":
        s = today - timedelta(days=today.weekday() + 7)
        return s, s + timedelta(days=6), key
    if key == "month":
        return today.replace(day=1), today, key
    first = today.replace(day=1)
    last_prev = first - timedelta(days=1)
    return last_prev.replace(day=1), last_prev, key


def _label(lang: str, key: str) -> str:
    return key if "/" in key else t(lang, key)


def _date_filters(doctype: str, start: date, end: date) -> list:
    return [[doctype, "docstatus", "=", 1], [doctype, "posting_date", ">=", start.isoformat()],
            [doctype, "posting_date", "<=", end.isoformat()], [doctype, "is_return", "=", 0]]


def find_items(tenant: dict, q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    rows = get_list(tenant, "Item", ["item_code", "item_name", "stock_uom"], [["disabled", "=", 0]],
                    or_filters=[["item_name", "like", f"%{q}%"], ["item_code", "like", f"%{q}%"]], limit=8)
    exact = [r for r in rows if _fold(r.get("item_name") or "") == _fold(q) or _fold(r.get("item_code") or "") == _fold(q)]
    return exact[:1] if exact else rows


def _choose_item(tenant: dict, lang: str, q: str) -> tuple[dict | None, str | None]:
    if not q:
        return None, t(lang, "need_item")
    items = find_items(tenant, q)
    if not items:
        return None, t(lang, "no_item", q=q, shop=tenant["alias"])
    if len(items) > 1:
        return None, t(lang, "which_item", q=q) + "\n" + "\n".join(f"• {i.get('item_name') or i['item_code']}" for i in items)
    return items[0], None


def _guard(fn):
    """Resolve the caller and the box; turn transport errors into one plain line."""
    def wrapper(args: dict, **kw) -> str:
        user_id = kw.get("user_id") or (args or {}).get("user_id") or ""
        text = (args or {}).get("text") or ""
        tenant, rest, lang, err = resolve(user_id, text)
        if err:
            return t("ht", err)
        try:
            return fn(tenant, rest, lang, args or {})
        except BoxError as e:
            return t(lang, e.kind, shop=tenant["alias"])
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ── read tools ────────────────────────────────────────────────────────────────────────────

@_guard
def retail_sales(tenant, text, lang, args) -> str:
    """Sales of a period: invoices, total, unpaid part, by payment mode and cashier."""
    start, end, key = period_of(text)
    label = _label(lang, key)
    cur = tenant.get("currency") or ""
    head = get_list(tenant, "Sales Invoice", [{"COUNT": "*", "as": "n"}, {"SUM": "grand_total", "as": "total"}, {"SUM": "outstanding_amount", "as": "unpaid"}],
                    _date_filters("Sales Invoice", start, end), limit=1)
    h = head[0] if head else {}
    n = int(h.get("n") or 0)
    if not n:
        return t(lang, "no_sales", label=label, shop=tenant["alias"])
    lines = [t(lang, "sales", label=label, shop=tenant["alias"]), t(lang, "invoices", n=n, total=money(h.get("total"), cur))]
    modes = get_list(tenant, "Sales Invoice Payment", ["mode_of_payment", {"SUM": "amount", "as": "amount"}],
                     _date_filters("Sales Invoice", start, end), group_by="mode_of_payment", order_by="amount desc", parent="Sales Invoice")
    for m in modes:
        if float(m.get("amount") or 0):
            lines.append(f"• {m.get('mode_of_payment') or '?'}: {money(m.get('amount'), cur)}")
    if float(h.get("unpaid") or 0) > 0:
        lines.append("• " + t(lang, "unpaid", amount=money(h.get("unpaid"), cur)))
    cashiers = get_list(tenant, "Sales Invoice", ["owner", {"COUNT": "*", "as": "n"}, {"SUM": "grand_total", "as": "total"}],
                        _date_filters("Sales Invoice", start, end), group_by="owner", order_by="total desc", limit=6)
    if len(cashiers) > 1:
        lines.append(t(lang, "by_cashier"))
        for c in cashiers:
            who = (c.get("owner") or "?").split("@")[0]
            lines.append(f"• {who}: {money(c.get('total'), cur)} ({int(c.get('n') or 0)})")
    return "\n".join(lines)


@_guard
def retail_stock(tenant, text, lang, args) -> str:
    """Stock of one item, by warehouse when there are several."""
    item, msg = _choose_item(tenant, lang, text)
    if msg:
        return msg
    bins = get_list(tenant, "Bin", ["warehouse", "actual_qty"], [["item_code", "=", item["item_code"]]], limit=20)
    total = sum(float(b.get("actual_qty") or 0) for b in bins)
    name = item.get("item_name") or item["item_code"]
    lines = [t(lang, "stock", shop=tenant["alias"]), t(lang, "stock_line", name=name, qty=qty(total), uom=item.get("stock_uom") or "")]
    if len([b for b in bins if float(b.get("actual_qty") or 0)]) > 1:
        for b in bins:
            if float(b.get("actual_qty") or 0):
                lines.append("• " + t(lang, "warehouse", warehouse=b.get("warehouse"), qty=qty(b.get("actual_qty"))))
    return "\n".join(lines)


@_guard
def retail_low_stock(tenant, text, lang, args) -> str:
    """Items at or under the shop's low-stock threshold."""
    m = re.search(r"\d+", text or "")
    threshold = int(m.group()) if m else int(tenant.get("low_stock") or 5)
    bins = get_list(tenant, "Bin", ["item_code", "warehouse", "actual_qty"], [["actual_qty", "<=", threshold]],
                    order_by="actual_qty asc", limit=LIST_LIMIT + 1)
    if not bins:
        return t(lang, "no_low", threshold=threshold, shop=tenant["alias"])
    codes = sorted({b["item_code"] for b in bins})
    names = {i["item_code"]: i.get("item_name") for i in get_list(tenant, "Item", ["item_code", "item_name"], [["item_code", "in", codes]], limit=len(codes))}
    lines = [t(lang, "low_stock", threshold=threshold, shop=tenant["alias"])]
    for b in bins[:LIST_LIMIT]:
        lines.append(f"• {names.get(b['item_code']) or b['item_code']}: {qty(b.get('actual_qty'))}")
    if len(bins) > LIST_LIMIT:
        lines.append(t(lang, "and_more", n=len(bins) - LIST_LIMIT))
    return "\n".join(lines)


@_guard
def retail_price(tenant, text, lang, args) -> str:
    """Selling price of one item, per unit of measure."""
    item, msg = _choose_item(tenant, lang, text)
    if msg:
        return msg
    name = item.get("item_name") or item["item_code"]
    cur = tenant.get("currency") or ""
    prices = get_list(tenant, "Item Price", ["price_list_rate", "uom", "currency", "price_list"],
                      [["item_code", "=", item["item_code"]], ["selling", "=", 1]], order_by="modified desc", limit=6)
    if not prices:
        return t(lang, "no_price", name=name, shop=tenant["alias"])
    lines = [t(lang, "price", shop=tenant["alias"])]
    seen = set()
    for p in prices:
        key = (p.get("uom") or item.get("stock_uom") or "", p.get("price_list") or "")
        if key in seen:
            continue
        seen.add(key)
        lines.append("• " + t(lang, "price_line", name=name, rate=money(p.get("price_list_rate"), p.get("currency") or cur), uom=key[0]))
    return "\n".join(lines)


@_guard
def retail_debts(tenant, text, lang, args) -> str:
    """Who owes the shop, oldest first."""
    cur = tenant.get("currency") or ""
    rows = get_list(tenant, "Sales Invoice", ["customer", {"SUM": "outstanding_amount", "as": "due"}, {"MIN": "due_date", "as": "oldest"}, {"COUNT": "*", "as": "n"}],
                    [["docstatus", "=", 1], ["outstanding_amount", ">", 0], ["is_return", "=", 0]],
                    group_by="customer", order_by="due desc", limit=LIST_LIMIT + 1)
    rows = [r for r in rows if float(r.get("due") or 0) > 0]
    if not rows:
        return t(lang, "no_debts", shop=tenant["alias"])
    today = date.today()
    lines = [t(lang, "debts", shop=tenant["alias"])]
    for r in rows[:LIST_LIMIT]:
        try:
            days = (today - date.fromisoformat(str(r.get("oldest"))[:10])).days
        except ValueError:
            days = 0
        lines.append("• " + t(lang, "debt_line", customer=r.get("customer"), due=money(r.get("due"), cur), n=int(r.get("n") or 0), days=max(days, 0)))
    if len(rows) > LIST_LIMIT:
        lines.append(t(lang, "and_more", n=len(rows) - LIST_LIMIT))
    lines.append(t(lang, "debts_total", total=money(sum(float(r.get("due") or 0) for r in rows), cur), n=len(rows)))
    return "\n".join(lines)


def _find_customer(tenant: dict, q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    rows = get_list(tenant, "Customer", ["name", "customer_name"], [["disabled", "=", 0]],
                    or_filters=[["customer_name", "like", f"%{q}%"], ["name", "like", f"%{q}%"]], limit=6)
    exact = [r for r in rows if _fold(r.get("customer_name") or "") == _fold(q)]
    return exact[:1] if exact else rows


@_guard
def retail_customer(tenant, text, lang, args) -> str:
    """One customer: what they owe and when they last bought."""
    if not text:
        return t(lang, "need_customer")
    found = _find_customer(tenant, text)
    if not found:
        return t(lang, "no_customer", q=text, shop=tenant["alias"])
    if len(found) > 1:
        return t(lang, "which_item", q=text) + "\n" + "\n".join(f"• {c.get('customer_name') or c['name']}" for c in found)
    c = found[0]
    cur = tenant.get("currency") or ""
    agg = get_list(tenant, "Sales Invoice", [{"SUM": "outstanding_amount", "as": "due"}, {"COUNT": "*", "as": "n"}, {"MAX": "posting_date", "as": "last"}],
                   [["docstatus", "=", 1], ["customer", "=", c["name"]], ["is_return", "=", 0]], limit=1)
    a = agg[0] if agg else {}
    unpaid = get_list(tenant, "Sales Invoice", [{"COUNT": "*", "as": "n"}],
                      [["docstatus", "=", 1], ["customer", "=", c["name"]], ["outstanding_amount", ">", 0]], limit=1)
    n_unpaid = int((unpaid[0] if unpaid else {}).get("n") or 0)
    last = str(a.get("last") or "-")[:10]
    head = t(lang, "customer", name=c.get("customer_name") or c["name"], shop=tenant["alias"])
    if float(a.get("due") or 0) > 0:
        return head + "\n" + t(lang, "customer_due", due=money(a.get("due"), cur), n=n_unpaid, last=last)
    return head + "\n" + t(lang, "customer_clear", last=last)


@_guard
def retail_shifts(tenant, text, lang, args) -> str:
    """Open till shifts: who, since when."""
    rows = get_list(tenant, "POS Opening Shift", ["user", "period_start_date", "pos_profile"],
                    [["status", "=", "Open"], ["docstatus", "=", 1]], order_by="period_start_date asc", limit=LIST_LIMIT)
    if not rows:
        return t(lang, "no_shifts", shop=tenant["alias"])
    lines = [t(lang, "shifts", shop=tenant["alias"])]
    now = datetime.now()
    for r in rows:
        since = str(r.get("period_start_date") or "")[:16]
        try:
            hours = round((now - datetime.fromisoformat(since)).total_seconds() / 3600, 1)
        except ValueError:
            hours = 0
        lines.append("• " + t(lang, "shift_line", user=(r.get("user") or "?").split("@")[0], since=since[11:16] or since, hours=hours))
    return "\n".join(lines)


@_guard
def retail_top_items(tenant, text, lang, args) -> str:
    """Best-selling items of a period."""
    start, end, key = period_of(text or "week")
    label = _label(lang, key)
    cur = tenant.get("currency") or ""
    rows = get_list(tenant, "Sales Invoice Item", ["item_name", {"SUM": "qty", "as": "qty"}, {"SUM": "amount", "as": "amount"}],
                    _date_filters("Sales Invoice", start, end), group_by="item_code", order_by="amount desc", limit=5, parent="Sales Invoice")
    rows = [r for r in rows if float(r.get("amount") or 0)]
    if not rows:
        return t(lang, "no_top", label=label, shop=tenant["alias"])
    lines = [t(lang, "top", label=label, shop=tenant["alias"])]
    for r in rows:
        lines.append("• " + t(lang, "top_line", name=r.get("item_name") or "?", qty=qty(r.get("qty")), amount=money(r.get("amount"), cur)))
    return "\n".join(lines)


# ── change tools (confirmation tiers; off until the setting opens them) ───────────────────

def writes_enabled() -> bool:
    try:
        from app.graph.tools.settings import get_setting
        return bool(get_setting("retail_writes"))
    except Exception:
        return False


def _comment(tenant: dict, doctype: str, name: str, text: str) -> None:
    try:
        call(tenant, "frappe.client.insert", doc={"doctype": "Comment", "comment_type": "Comment",
                                                   "reference_doctype": doctype, "reference_name": name, "content": text})
    except BoxError:
        pass


@_guard
def retail_set_price(tenant, text, lang, args) -> str:
    """Change an item's selling price (the item and the amount must be in the person's words)."""
    if not writes_enabled():
        return t(lang, "writes_off")
    item_q = (args.get("item") or "").strip()
    price = args.get("price")
    if not item_q and text:
        m = re.search(r"(.*?)\s+(\d[\d\s.,]*)\s*$", text)
        if m:
            item_q, price = m.group(1), m.group(2)
    try:
        price_v = float(str(price).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return t(lang, "bad_amount")
    item, msg = _choose_item(tenant, lang, item_q)
    if msg:
        return msg
    name = item.get("item_name") or item["item_code"]
    rows = get_list(tenant, "Item Price", ["name", "uom", "currency"], [["item_code", "=", item["item_code"]], ["selling", "=", 1]],
                    order_by="modified desc", limit=1)
    if not rows:
        return t(lang, "no_price", name=name, shop=tenant["alias"])
    row = rows[0]
    call(tenant, "frappe.client.set_value", doctype="Item Price", name=row["name"], fieldname="price_list_rate", value=price_v)
    _comment(tenant, "Item Price", row["name"], f"WhatsApp: price set to {price_v} by {args.get('user_id') or 'the assistant'}")
    return t(lang, "price_set", name=name, rate=money(price_v, row.get("currency") or tenant.get("currency") or ""),
             uom=row.get("uom") or item.get("stock_uom") or "", shop=tenant["alias"])


@_guard
def retail_record_payment(tenant, text, lang, args) -> str:
    """Record a payment from a customer against their oldest unpaid invoice."""
    if not writes_enabled():
        return t(lang, "writes_off")
    customer_q = (args.get("customer") or "").strip()
    amount = args.get("amount")
    mode = (args.get("mode") or "").strip()
    if not customer_q and text:
        m = re.search(r"(.*?)\s+(\d[\d\s.,]*)\s*(\S+)?\s*$", text)
        if m:
            customer_q, amount, mode = m.group(1), m.group(2), (m.group(3) or "")
    try:
        amount_v = float(str(amount).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return t(lang, "bad_amount")
    if amount_v <= 0:
        return t(lang, "bad_amount")
    found = _find_customer(tenant, customer_q)
    if not found:
        return t(lang, "no_customer", q=customer_q, shop=tenant["alias"]) if customer_q else t(lang, "need_customer")
    if len(found) > 1:
        return t(lang, "which_item", q=customer_q) + "\n" + "\n".join(f"• {c.get('customer_name') or c['name']}" for c in found)
    c = found[0]
    cname = c.get("customer_name") or c["name"]
    inv = get_list(tenant, "Sales Invoice", ["name", "outstanding_amount"],
                   [["docstatus", "=", 1], ["customer", "=", c["name"]], ["outstanding_amount", ">", 0]], order_by="due_date asc", limit=1)
    if not inv:
        return t(lang, "no_unpaid", customer=cname, shop=tenant["alias"])
    pe = call(tenant, "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry", dt="Sales Invoice", dn=inv[0]["name"])
    if not pe:
        raise BoxError("down", "no payment entry")
    pe["paid_amount"] = pe["received_amount"] = amount_v
    if mode:
        pe["mode_of_payment"] = mode
    for ref in pe.get("references") or []:
        ref["allocated_amount"] = min(amount_v, float(ref.get("outstanding_amount") or amount_v))
    doc = call(tenant, "frappe.client.insert", doc=pe)
    name = (doc or {}).get("name", "")
    if name:
        call(tenant, "frappe.client.submit", doc=doc)
        _comment(tenant, "Sales Invoice", inv[0]["name"], f"WhatsApp: payment {amount_v} {mode} recorded by {args.get('user_id') or 'the assistant'}")
    return t(lang, "payment_recorded", amount=money(amount_v, tenant.get("currency") or ""), mode=mode or pe.get("mode_of_payment") or "",
             customer=cname, shop=tenant["alias"], invoice=inv[0]["name"])


READ_TOOLS = ("retail_sales", "retail_stock", "retail_low_stock", "retail_price", "retail_debts", "retail_customer", "retail_shifts", "retail_top_items")
CHANGE_TOOLS = ("retail_set_price", "retail_record_payment")
