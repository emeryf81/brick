"""Deals on every LEGO set, also the ones you don't follow.

The set database (setdb.py) knows every set. The sets that may still be in the shops (released in the
last few years and not retired) are looked up one at a time on a comparison site, spread over the day;
one page there lists the prices of many shops at once. Whether a set is retired is checked on its market
value page about once a month; retired sets are no longer looked up. A set that no shop sells is looked
up less often. Sets you follow (watchlist, collection) are left out: they get the full shop rounds.

Everything is kept compact in its own storage file, one entry per set:
    {"ts": last price lookup, "price": lowest price, "shop": retailer id, "url": offer link, "src": source,
     "rrp": retail price, "retired": text or None, "rts": last retirement check, "miss": lookups without offers,
     "deal": discount when it is a deal, "deal_ts": since when, "notified": price that was notified}
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from . import setdb

YEARS_BACK = 3                     # sets of this year and the 3 years before may still be in the shops
RETIRED_DAYS = 30                  # retirement checked again after this many days
MISS_LIMIT = 3                     # no offers this many times in a row ...
MISS_DAYS = 14                     # ... then only looked up every 14 days
PER_DAY_DEFAULT = 300
PER_DAY_CHOICES = (0, 100, 300, 600, 1000)
MIN_SHARE = 0.25                   # a price below a quarter of the RRP is not the set (a part, a sticker, ...)
MAX_DEAL = 80                      # nor is a discount above 80 %
PRICE_SOURCES = ("kieskeurig", "shoparize", "channable")    # comparison sites that list many shops for a set number


def candidates(db: dict[str, list[Any]], scan: dict[str, dict[str, Any]], tracked: set[str] | dict[str, Any],
               themes_off: set[str], min_pieces: float | None = None, max_pieces: float | None = None,
               year: int | None = None) -> list[str]:
    """Sets worth looking up: recent, not followed already, not retired, not in a switched-off theme,
    within the piece limits. `themes_off` holds normalised theme keys."""
    from .themes import key as theme_key

    year = year or date.today().year
    out = []
    for num, row in db.items():
        if num in tracked or not (year - YEARS_BACK <= (row[setdb.YEAR] or 0) <= year + 1):
            continue
        if (scan.get(num) or {}).get("retired"):
            continue
        if themes_off and row[setdb.THEME] and theme_key(row[setdb.THEME]) in themes_off:
            continue
        pieces = row[setdb.PIECES] or 0
        if (min_pieces is not None and pieces < min_pieces) or (max_pieces is not None and pieces > max_pieces):
            continue
        out.append(num)
    return out


def due(entry: dict[str, Any] | None) -> float:
    """When a set should be looked up (lower = sooner). Never looked up: right away; no offers several
    times in a row: only after MISS_DAYS."""
    if not entry or not entry.get("ts"):
        return 0.0
    return entry["ts"] + (MISS_DAYS * 86400 if entry.get("miss", 0) >= MISS_LIMIT else 0)


def pick(cands: list[str], scan: dict[str, dict[str, Any]], db: dict[str, list[Any]], now: float | None = None) -> str | None:
    """The set that waited longest (new sets first among those never looked up); None when nothing is due."""
    now = now or time.time()
    best, best_key = None, None
    for num in cands:
        d = due(scan.get(num))
        if d > now:
            continue
        k = (d, -(db.get(num, [None, 0])[setdb.YEAR] or 0), num)
        if best_key is None or k < best_key:
            best, best_key = num, k
    return best


def needs_retired_check(entry: dict[str, Any] | None, now: float | None = None) -> bool:
    """Whether the set's retirement should be (re)checked."""
    now = now or time.time()
    return not entry or now - (entry.get("rts") or 0) > RETIRED_DAYS * 86400


def best_offer(shops: list[dict[str, Any]], retailers: dict[str, Any], rrp: float | None) -> dict[str, Any] | None:
    """The cheapest offer of a known shop, leaving out prices that can't be the set itself."""
    best = None
    for s in shops or []:
        price, rid = s.get("price"), s.get("retailer")
        if not price or rid not in retailers or (rrp and price < rrp * MIN_SHARE):
            continue
        if best is None or price < best["price"]:
            best = {"price": round(float(price), 2), "shop": rid, "url": s.get("url")}
    return best


def discount(entry: dict[str, Any]) -> float | None:
    """Discount of the lowest price against the RRP, in percent."""
    price, rrp = entry.get("price"), entry.get("rrp")
    if not price or not rrp:
        return None
    return round((rrp - price) / rrp * 100, 1)


def deal(entry: dict[str, Any], threshold: float, flt: dict[str, Any]) -> float | None:
    """The discount when the set is a deal (at least `threshold` %, within the price filters), else None."""
    d = discount(entry)
    if d is None or d < threshold or d > MAX_DEAL or entry.get("retired"):
        return None
    price = entry["price"]
    if (flt.get("min_price") is not None and price < flt["min_price"]) or \
            (flt.get("max_price") is not None and price > flt["max_price"]):
        return None
    if flt.get("min_discount") is not None and d < flt["min_discount"]:
        return None
    return d


def status(entry: dict[str, Any] | None) -> str:
    """'retired', 'deal', 'sale' (a shop sells it), 'none' (no shop found) or 'unknown' (not looked up yet)."""
    if not entry:
        return "unknown"
    if entry.get("retired"):
        return "retired"
    if entry.get("deal") is not None:
        return "deal"
    if entry.get("price"):
        return "sale"
    return "none" if entry.get("ts") else "unknown"
