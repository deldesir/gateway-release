"""ERPNext (Frappe) — read-only business view for the operator.

The companion had no business surface at all: "how were sales last week?"
ran for two minutes and produced nothing. This is the smallest useful window
onto the store: submitted Sales Invoices (POS invoices land here too) over a
named period, aggregated locally. Read-only by construction — the API user
behind ERPNEXT_API_KEY holds a read-only role; this module never POSTs.

Env:
  ERPNEXT_URL         https://store.example (no trailing slash)
  ERPNEXT_API_KEY / ERPNEXT_API_SECRET   token auth ("token key:secret")
  ERPNEXT_HOST        optional Host header (multi-site benches, e.g. site.local)
  ERPNEXT_TIMEOUT     seconds (default 10)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

PERIODS = ("today", "yesterday", "week", "last_week", "month", "last_month")


def _cfg() -> dict:
    return {
        "url": os.getenv("ERPNEXT_URL", "").rstrip("/"),
        "key": os.getenv("ERPNEXT_API_KEY", ""),
        "secret": os.getenv("ERPNEXT_API_SECRET", ""),
        "host": os.getenv("ERPNEXT_HOST", ""),
        "timeout": float(os.getenv("ERPNEXT_TIMEOUT", "10")),
    }


def period_bounds(period: str, today: date | None = None) -> tuple[date, date, str]:
    """(start, end, label) inclusive. Weeks run Monday → Sunday."""
    today = today or date.today()
    p = (period or "week").strip().lower().replace(" ", "_")
    if p == "today":
        return today, today, "today"
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    if p in ("week", "this_week"):
        start = today - timedelta(days=today.weekday())
        return start, today, "this week"
    if p == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6), "last week"
    if p in ("month", "this_month"):
        return today.replace(day=1), today, "this month"
    if p == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev, "last month"
    raise ValueError(f"unknown period {period!r} (use one of {', '.join(PERIODS)})")


def fetch_invoices(start: date, end: date, cfg: dict | None = None) -> list[dict]:
    """Submitted Sales Invoices in [start, end]. Raises on transport errors."""
    cfg = cfg or _cfg()
    if not (cfg["url"] and cfg["key"] and cfg["secret"]):
        raise RuntimeError("ERPNext is not configured (ERPNEXT_URL / API key / secret)")
    params = {
        "fields": json.dumps(["name", "posting_date", "grand_total", "customer", "is_pos", "currency"]),
        "filters": json.dumps([
            ["docstatus", "=", 1],
            ["posting_date", ">=", start.isoformat()],
            ["posting_date", "<=", end.isoformat()],
        ]),
        "limit_page_length": 2000,
        "order_by": "posting_date asc",
    }
    url = f"{cfg['url']}/api/resource/Sales%20Invoice?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {cfg['key']}:{cfg['secret']}",
        "Accept": "application/json",
        **({"Host": cfg["host"]} if cfg["host"] else {}),
    })
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", [])


def summarize(invoices: list[dict], label: str, start: date, end: date) -> str:
    if not invoices:
        return f"🛍️ No submitted sales {label} ({start:%d %b} – {end:%d %b})."
    cur = invoices[0].get("currency") or ""
    total = sum(float(i.get("grand_total") or 0) for i in invoices)
    n = len(invoices)
    by_day: dict[str, float] = defaultdict(float)
    by_cust: dict[str, float] = defaultdict(float)
    for i in invoices:
        by_day[str(i.get("posting_date"))] += float(i.get("grand_total") or 0)
        by_cust[i.get("customer") or "?"] += float(i.get("grand_total") or 0)
    days_active = len(by_day)
    lines = [
        f"🛍️ Sales {label} ({start:%d %b} – {end:%d %b}): {total:,.0f} {cur} "
        f"across {n} invoice(s), avg {total / n:,.0f} {cur}/invoice, {days_active} active day(s).",
    ]
    best_day, best_amt = max(by_day.items(), key=lambda kv: kv[1])
    lines.append(f"Best day: {best_day} ({best_amt:,.0f} {cur}).")
    top = sorted(by_cust.items(), key=lambda kv: -kv[1])[:3]
    if len(by_cust) > 1:
        lines.append("Top customers: " + ", ".join(f"{c} {a:,.0f}" for c, a in top))
    return "\n".join(lines)


def erpnext_sales_summary(args: dict, **kwargs) -> str:
    """Tool entry point."""
    period = (args or {}).get("period") or "week"
    try:
        start, end, label = period_bounds(period)
    except ValueError as e:
        return f"⚠️ {e}"
    try:
        invoices = fetch_invoices(start, end)
    except urllib.error.HTTPError as e:
        return f"⚠️ ERPNext refused the request (HTTP {e.code})."
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"⚠️ ERPNext unreachable: {getattr(e, 'reason', e)}"
    except RuntimeError as e:
        return f"⚠️ {e}"
    return summarize(invoices, label, start, end)
