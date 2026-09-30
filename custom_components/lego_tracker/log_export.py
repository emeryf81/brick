"""Logbook export (Logbook → Export): shop checks, price history per set, and price history in total.

All three read the stored data; nothing is fetched. The result is one CSV file: with one part chosen a
plain table, with several parts one table per part (a title line, then the header and the rows).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .const import RETAILERS
from .models import price_at, rows_to_csv, trusted

PARTS = ("checks", "history", "total")
DAY = 86400


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def select_sets(store: dict[str, Any], f: dict[str, Any], is_watched: Callable[[str], bool],
                retiring: Callable[[dict[str, Any]], bool]) -> list[str]:
    """Set numbers matching the set filters (all optional): sets, scope (watchlist / collection),
    condition, priority, retiring soon, location, theme, subtheme, year."""
    out = []
    wanted = {str(x) for x in (f.get("sets") or [])}
    for num, s in store["sets"].items():
        coll = store["collection"].get(num)
        if wanted and num not in wanted:
            continue
        scope = f.get("scope")
        if scope == "watchlist" and not is_watched(num):
            continue
        if scope == "collection" and coll is None:
            continue
        if f.get("condition") and (coll is None or (coll.get("condition") or "Unknown") != f["condition"]):
            continue
        if f.get("priority") and int(s.get("priority") or 0) != int(f["priority"]):   # validated by the websocket
            continue
        if f.get("retiring") and not retiring(s):
            continue
        if f.get("location") and (coll is None or f["location"].lower() not in (coll.get("location") or "").lower()):
            continue
        if f.get("theme") and s.get("theme") != f["theme"]:
            continue
        if f.get("subtheme") and s.get("subtheme") != f["subtheme"]:
            continue
        if f.get("year") and str(s.get("year") or "") != str(f["year"]):
            continue
        out.append(num)
    return out


def checks_rows(store: dict[str, Any], start: float, end: float, f: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per shop per check: when, set, shop, result, price, error, source."""
    wanted_sets = {str(x) for x in (f.get("sets") or [])}
    shops = set(f.get("shops") or [])
    status = f.get("status")                       # ok | failed | skipped
    rows = []
    for e in store.get("activity", []):
        if e.get("kind") != "check" or not start <= e.get("ts", 0) <= end:
            continue
        num = e.get("set_number")
        if wanted_sets and num not in wanted_sets:
            continue
        for rid, r in (e.get("results") or {}).items():
            if shops and rid not in shops:
                continue
            st = "ok" if r.get("ok") else "failed" if r.get("ok") is False else "skipped"
            if status and st != status:
                continue
            rows.append({"time": _stamp(e["ts"]), "set_number": num, "name": store["sets"].get(num, {}).get("name", ""),
                         "shop": RETAILERS.get(rid, (rid,))[0], "result": st,
                         "price": r.get("price") if r.get("price") is not None else "", "error": r.get("error") or "",
                         "via": r.get("via") or "", "source": e.get("source") or ""})
    rows.sort(key=lambda r: r["time"])
    return rows


def history_rows(store: dict[str, Any], nums: list[str], start: float, end: float, shops: set[str] | None = None) -> list[dict[str, Any]]:
    """Every recorded price per set and shop in the period."""
    rows = []
    for num in nums:
        s = store["sets"].get(num, {})
        for rid, o in (store["offers"].get(num) or {}).items():
            if shops and rid not in shops:
                continue
            for ts, price in o.get("history", []):
                if start <= ts <= end:
                    rows.append({"date": _stamp(ts), "set_number": num, "name": s.get("name", ""), "theme": s.get("theme", ""),
                                 "shop": RETAILERS.get(rid, (rid,))[0], "price": price, "rrp": s.get("rrp") or ""})
    rows.sort(key=lambda r: (r["date"], r["set_number"]))
    return rows


def total_rows(store: dict[str, Any], nums: list[str], start: float, end: float) -> list[dict[str, Any]]:
    """Per day: how many of the chosen sets have a price, and the sum of their lowest price that day
    (the lowest of all shops, last known price carried forward)."""
    series = {}
    for num in nums:
        offers = trusted(store["offers"].get(num) or {})
        series[num] = [(ts, p) for o in offers.values() for ts, p in o.get("history", [])]
    first = min((ts for pts in series.values() for ts, _ in pts), default=None)
    if first is None:
        return []
    day = max(start, first)
    day = datetime.fromtimestamp(day).replace(hour=23, minute=59, second=59).timestamp()
    end = min(end, time.time())
    rows = []
    while day <= end + DAY - 1:
        total, n = 0.0, 0
        for num in nums:
            offers = trusted(store["offers"].get(num) or {})
            prices = [p for o in offers.values() if (p := price_at(o.get("history", []), day)) is not None]
            if prices:
                total += min(prices)
                n += 1
        rows.append({"date": _day(day), "sets_with_price": n, "total_lowest_price": round(total, 2)})
        day += DAY
    return rows


COLUMNS = {
    "checks": ["time", "set_number", "name", "shop", "result", "price", "error", "via", "source"],
    "history": ["date", "set_number", "name", "theme", "shop", "price", "rrp"],
    "total": ["date", "sets_with_price", "total_lowest_price"],
}
TITLES = {"checks": "Shop checks", "history": "Price history per set", "total": "Price history in total"}


def build(store: dict[str, Any], opts: dict[str, Any], is_watched: Callable[[str], bool],
          retiring: Callable[[dict[str, Any]], bool]) -> tuple[str, dict[str, int]]:
    """(csv text, rows per part)."""
    parts = [p for p in PARTS if p in (opts.get("parts") or [])]
    start = float(opts.get("start") or 0)
    end = float(opts.get("end") or time.time())
    f = opts.get("filters") or {}
    nums = select_sets(store, f, is_watched, retiring)
    shops = set(f.get("shops") or []) or None
    tables = {}
    if "checks" in parts:
        tables["checks"] = checks_rows(store, start, end, f)      # only the set / shop / result filters apply here
    if "history" in parts:
        tables["history"] = history_rows(store, nums, start, end, shops)
    if "total" in parts:
        tables["total"] = total_rows(store, nums, start, end)
    if len(tables) == 1:
        (key, rows), = tables.items()
        return rows_to_csv(rows, COLUMNS[key]), {key: len(rows)}
    chunks = [f"# {TITLES[k]}\n" + rows_to_csv(rows, COLUMNS[k]) for k, rows in tables.items()]
    return "\n".join(chunks), {k: len(v) for k, v in tables.items()}
