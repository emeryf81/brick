"""Pure data logic (no Home Assistant imports) so it can be unit tested."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from heapq import heappop, heappush
from itertools import groupby
from math import isfinite
from statistics import median
from typing import Any

from .const import MAX_HISTORY
from .i18n import LocalizedError, T


DAY = 86400


def normalize_set_number(value: str | int) -> str:
    """'10281-1' / ' 10281 ' -> '10281'. Variant suffix -1 is dropped."""
    text = str(value).strip()
    text = re.sub(r"-1$", "", text)
    m = re.search(r"\d{4,7}", text)
    return m.group(0) if m else text


def new_store() -> dict[str, Any]:
    return {"sets": {}, "offers": {}, "collection": {}, "snapshots": [], "events": [], "rejected": {}, "cooldowns": {}}


def parse_price(text: str | float | int | None) -> float | None:
    """Parse '1.234,56', '1,234.56', '39,99', '€ 39.99' ... into a float."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.rpartition(".")[2]) == 3):
        s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return value if value > 0 else None


def record_price(offer: dict[str, Any], price: float | None, now: float | None = None,
                 error: str | None = None) -> bool:
    """Store an observation on an offer. Returns True if history changed."""
    now = now or time.time()
    offer["last_checked"] = now
    offer["error"] = error
    if price is None:
        offer["available"] = False
        return False
    offer["available"] = True
    offer["last_price"] = price
    hist: list[list[float]] = offer.setdefault("history", [])
    if hist:
        last_ts, last_price = hist[-1]
        same_day = int(last_ts // DAY) == int(now // DAY)
        if abs(last_price - price) < 0.005:
            return False
        if same_day:  # keep one point per day: overwrite with latest
            hist[-1] = [now, price]
            return True
    hist.append([now, price])
    if len(hist) > MAX_HISTORY:
        del hist[: len(hist) - MAX_HISTORY]
    return True


def price_at(history: list[list[float]], ts: float) -> float | None:
    """Last known price at or before ts (forward fill)."""
    result = None
    for t, p in history:
        if t <= ts:
            result = p
        else:
            break
    return result


def trusted(offers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Offers whose link is not flagged as pointing to the wrong product (manual links always count)."""
    return {rid: o for rid, o in offers.items() if o.get("link_status") != "suspect" or o.get("manual_price")}


def offer_price(o: dict[str, Any]) -> float | None:
    """Price that counts for an offer: a manual price always wins over the automatic one."""
    if (m := o.get("manual_price")) and m.get("price"):
        return float(m["price"])
    return o.get("last_price") if o.get("available") else None


def best_offer(offers: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """Cheapest currently available offer (suspect links excluded)."""
    live = [(rid, o) for rid, o in trusted(offers).items() if offer_price(o)]
    return min(live, key=lambda x: offer_price(x[1])) if live else None


def combined_history(offers: dict[str, dict[str, Any]]) -> list[list[float]]:
    """Cheapest-of-all-retailers series (forward filled) across all timestamps; suspect links excluded."""
    # Consume each observation once, including legacy histories beyond MAX_HISTORY.
    # Stable sorting preserves the last observation at duplicate timestamps.
    events = sorted(
        ((t, i, p) for i, o in enumerate(trusted(offers).values()) for t, p in o.get("history", [])),
        key=lambda event: event[0],
    )
    latest: dict[int, float] = {}
    prices: list[tuple[float, int]] = []
    out: list[list[float]] = []
    for ts, observations in groupby(events, key=lambda event: event[0]):
        for _, i, price in observations:
            latest[i] = price
            heappush(prices, (price, i))
        # Discard superseded prices lazily: each heap entry is removed at most once.
        while prices[0][0] != latest[prices[0][1]]:
            heappop(prices)
        out.append([ts, prices[0][0]])
    return out


def compute_set_status(
    lego_set: dict[str, Any],
    offers: dict[str, dict[str, Any]],
    *,
    threshold: float,
    min_history_days: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Derive price, lows and discount flags for one set."""
    now = now or time.time()
    best = best_offer(offers)
    series = combined_history(offers)
    status: dict[str, Any] = {
        "best_price": None, "best_retailer": None, "best_url": None,
        "all_time_low": None, "is_all_time_low": False,
        "discount_rrp": None, "discount_avg": None,
        "high_discount": False, "history_days": 0, "offers_live": 0,
        "price_per_piece": None, "change_7d": None, "change_30d": None,
        "target_price": lego_set.get("target_price"), "target_hit": False,
        "offers_error": sum(1 for o in offers.values() if o.get("error")),
        "offers_suspect": sum(1 for o in offers.values() if o.get("link_status") == "suspect"),
        "deal_score": 0, "deal_label": None, **retirement_status(lego_set, now),
    }
    status["offers_live"] = sum(1 for o in offers.values() if o.get("available"))
    if series:
        status["history_days"] = int((series[-1][0] - series[0][0]) // DAY)
        status["all_time_low"] = min(p for _, p in series)
    if not best:
        return status
    rid, offer = best
    price = offer_price(offer)
    status.update(best_price=price, best_retailer=rid, best_url=offer.get("url"))
    if status["all_time_low"] is not None:
        # "record low" needs enough history to be meaningful
        status["is_all_time_low"] = (
            price <= status["all_time_low"] + 0.005
            and status["history_days"] >= min_history_days
            and len({p for _, p in series}) > 1
        )
    rrp = lego_set.get("rrp")
    if rrp:
        status["discount_rrp"] = round((rrp - price) / rrp * 100, 1)
    recent = [p for t, p in series if now - t <= 90 * DAY]
    if len(recent) >= 3:
        avg = median(recent)
        status["discount_avg"] = round((avg - price) / avg * 100, 1)
    pieces = lego_set.get("pieces")
    if pieces:
        status["price_per_piece"] = round(price / pieces, 4)
    for key, days in (("change_7d", 7), ("change_30d", 30)):
        old = price_at(series, now - days * DAY)
        if old:
            status[key] = round((price - old) / old * 100, 1)
    target = lego_set.get("target_price")
    status["target_hit"] = bool(target) and price <= float(target)
    ref = status["discount_rrp"] if status["discount_rrp"] is not None else status["discount_avg"]
    status["high_discount"] = ref is not None and ref >= threshold
    status["deal_score"] = deal_score(status)
    status["deal_label"] = "top" if status["deal_score"] >= 70 else "good" if status["deal_score"] >= 45 else None
    return status


def deal_score(st: dict[str, Any]) -> int:
    """0-100: how good is the current price? Weighs list-price discount, distance to the
    all-time low, discount vs. the 90-day median and a reached target price."""
    price = st.get("best_price")
    if not price:
        return 0
    score = 0.0
    if st.get("discount_rrp") is not None:
        score += min(max(st["discount_rrp"], 0), 50) / 50 * 45
    atl = st.get("all_time_low")
    if atl and st.get("history_days", 0) >= 1:
        gap = (price - atl) / atl
        score += 25 if gap <= 0.005 else max(0.0, 1 - gap / 0.2) * 20
    if st.get("discount_avg") is not None:
        score += min(max(st["discount_avg"], 0), 25) / 25 * 20
    if st.get("target_hit"):
        score += 10
    return int(round(min(score, 100)))


def retirement_status(lego_set: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """'Retiring soon' = exit date within 180 days, or flagged manually."""
    exit_date = lego_set.get("exit_date")
    days = None
    if exit_date:
        try:
            days = (date.fromisoformat(str(exit_date)[:10]) - date.fromtimestamp(now or time.time())).days
        except ValueError:
            days = None
    return {
        "retiring_soon": bool(lego_set.get("retiring")) or (days is not None and 0 <= days <= 180),
        "retired": days is not None and days < 0,
        "retires_in_days": days,
    }


def clean_history(offer: dict[str, Any], rrp: float | None) -> int:
    """Drop impossible price points (< 20 % or > 4× RRP), e.g. an accessory price read by mistake.
    Returns the number of removed points; fixes last_price when the latest point was removed."""
    if not rrp:
        return 0
    hist = offer.get("history", [])
    manual = (offer.get("manual_price") or {}).get("price")
    keep = [h for h in hist if rrp * 0.2 <= h[1] <= rrp * 4 or (manual and abs(h[1] - manual) < 0.005)]
    removed = len(hist) - len(keep)
    if removed:
        offer["history"] = keep
        if offer.get("last_price") is not None and not rrp * 0.2 <= offer["last_price"] <= rrp * 4:
            offer["last_price"] = keep[-1][1] if keep else None
            offer["available"] = bool(keep) and offer.get("available", False)
    return removed


def link_check(offer: dict[str, Any], lego_set: dict[str, Any], set_number: str) -> tuple[str | None, str]:
    """Is this shop link the right product? Uses the page title (or URL slug) and the price.

    Returns (status, reason) with status 'ok', 'suspect' or None (cannot judge yet).
    A manual 'confirmed' is never overridden."""
    from .parsers import slug_title, title_check  # local import: parsers imports this module

    if offer.get("link_status") == "confirmed":
        return "confirmed", T("confirmed by hand")
    if re.search(rf"lego\.com/[a-z]{{2}}-[a-z]{{2}}/product/[^?#]*?(?<!\d){re.escape(set_number)}/?(?:[?#]|$)", offer.get("url") or ""):
        return "ok", T("official LEGO.com page of this set")
    title = offer.get("title")
    status, reason = title_check(title, set_number) if title else (None, "")
    if status is None and offer.get("url"):
        slug = slug_title(offer["url"])
        if slug:
            status, reason = title_check(f"lego {slug}", set_number)
            reason = T("URL: {reason}", reason=reason)
    if status == "suspect":
        return status, reason
    rrp = lego_set.get("rrp")
    low = min((p for _, p in offer.get("history", [])), default=None)
    if rrp and low is not None and low < rrp * 0.3:
        return "suspect", T("price €{price} is far too low for this set (RRP €{rrp})", price=f"{low:.2f}", rrp=f"{rrp:.2f}")
    if status is None:
        return None, T("no product title yet (filled in at the next price check)")
    return status, reason


def agreeing_group(prices: list[float]) -> list[float]:
    """The lowest group of at least two prices that agree (each within ±50 % of the group's lowest price)."""
    ps = sorted(p for p in prices if p and p > 0)
    for i, low in enumerate(ps):
        group = [p for p in ps[i:] if p <= low * 1.5]
        if len(group) >= 2:
            return group
    return []


def is_suspicious_price(price: float, lego_set: dict[str, Any], offer: dict[str, Any],
                        others: list[float] | None = None) -> str | None:
    """Catch parse errors (accessory/marketplace/multi-pack prices, cents read without the comma) before
    they pollute history. Without an RRP the other shops' prices for the same set are the yardstick, so a
    new set (no RRP, no history yet) is checked too."""
    ap = offer.get("approved")
    if ap and abs(price - ap) <= ap * 0.25:
        return None              # you approved a price like this for this link: the shop really asks it
    rrp = lego_set.get("rrp")
    group = agreeing_group(others or [])
    if rrp and group and not 0.25 <= rrp / median(group) <= 4:
        rrp = None               # shops that agree contradict the RRP (e.g. 16499 for 164,99): don't trust the RRP
    if not rrp and others:
        mid = median(others)
        # far above the others: e.g. cents read without the comma
        if price > mid * 4:
            return T("suspicious price €{price} (far from €{usual}) ignored", price=f"{price:.2f}", usual=f"{mid:.2f}")
        # far below needs two other shops that agree with each other (an accessory, not one odd quote); the lowest
        # such group counts, also when the others form two groups ([100, 100, 400, 400])
        group = agreeing_group(others)
        if group and price < median(group) * 0.25:
            return T("suspicious price €{price} (far from €{usual}) ignored", price=f"{price:.2f}", usual=f"{median(group):.2f}")
    if rrp and price < rrp * 0.2:
        return T("suspicious price €{price} (under 20% of RRP) ignored", price=f"{price:.2f}")
    if rrp and price > rrp * 4:
        return T("suspicious price €{price} (over 4× RRP) ignored", price=f"{price:.2f}")
    prices = [p for _, p in offer.get("history", [])]
    if not rrp and len(prices) >= 3:
        mid = median(prices)
        if price < mid * 0.25 or price > mid * 4:
            return T("suspicious price €{price} (far from €{usual}) ignored", price=f"{price:.2f}", usual=f"{mid:.2f}")
    return None


# ----------------------------------------------------------------- collection
def collection_value(entry: dict[str, Any], status: dict[str, Any], lego_set: dict[str, Any],
                     prefer_import: bool = False) -> tuple[float, str]:
    """Value of one unit and where it came from.

    shop_first (default): cheapest current shop price, else the imported value, else RRP.
    import_first: imported value first (better for retired sets that shops no longer sell new)."""
    shop = status.get("best_price")
    imported = entry.get("current_value")
    order = (("imported", imported), ("tracked", shop)) if prefer_import else (("tracked", shop), ("imported", imported))
    for source, value in order:
        if value:
            return float(value), source
    if lego_set.get("rrp"):
        return float(lego_set["rrp"]), "rrp"
    return 0.0, "none"


OPENED = ("Opened", "Built", "Incomplete")       # conditions whose value is the "used" market value
COPY_FIELDS = ("paid", "added", "condition", "location", "notes")
MARKET_LABEL = "Market value"


def copies(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every copy of a set in your collection, each with its own purchase price, date, condition, location
    and notes. Entries from before 0.9.21 (one set of fields and a quantity) are as many equal copies."""
    items = entry.get("items")
    if isinstance(items, list) and items:
        return items
    one = {k: entry[k] for k in COPY_FIELDS if entry.get(k) not in (None, "")}
    return [dict(one) for _ in range(max(1, int(entry.get("qty", 1) or 1)))]


def sync_copies(entry: dict[str, Any]) -> None:
    """Keep the set-level fields in line with the copies (quantity, average price paid, first purchase date,
    the first copy's condition and location), for exports and older views."""
    items = entry.get("items")
    if not isinstance(items, list) or not items:
        entry.pop("items", None)
        return
    entry["qty"] = len(items)
    paid = [float(c["paid"]) for c in items if c.get("paid") is not None]
    if paid:
        entry["paid"] = round(sum(paid) / len(paid), 2)
    else:
        entry.pop("paid", None)
    dates = sorted(c["added"] for c in items if c.get("added"))
    if dates:
        entry["added"] = dates[0]
    else:
        entry.pop("added", None)
    for k in ("condition", "location"):
        if items[0].get(k):
            entry[k] = items[0][k]
        else:
            entry.pop(k, None)


def copy_value(copy: dict[str, Any], entry: dict[str, Any], status: dict[str, Any], lego_set: dict[str, Any],
               prefer_import: bool = False) -> tuple[float, str]:
    """Value of one copy: an opened, built or incomplete copy is worth the used market value (when known),
    a sealed one the new value (shop price / imported value / RRP, see collection_value). A value you
    imported yourself wins when 'imported value first' is chosen."""
    unit, source = collection_value(entry, status, lego_set, prefer_import)
    used = (lego_set.get("market") or {}).get("market_used")
    own_import = prefer_import and entry.get("current_value") and entry.get("value_source") not in (None, MARKET_LABEL)
    if copy.get("condition") in OPENED and used and not own_import:
        return float(used), "market_used"
    return unit, source


def _prefer_import(store: dict[str, Any]) -> bool:
    return store.get("value_source") == "import_first"


def _added_ts(entry: dict[str, Any]) -> float | None:
    raw = entry.get("added")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def collection_series(store: dict[str, Any], now: float | None = None, points: int = 120) -> list[dict[str, float]]:
    """Value/cost over time, rebuilt from price history (no external service needed).

    Value of a set before its first tracked price uses the imported value if any.
    Sets only count from their purchase date (or from the first sample when unknown).
    """
    now = now or time.time()
    coll = store["collection"]
    if not coll:
        return []
    hists = {n: combined_history(store["offers"].get(n, {})) for n in coll}
    pref = _prefer_import(store)
    starts = [h[0][0] for h in hists.values() if h]
    starts += [t for e in coll.values() if (t := _added_ts(e))]
    if not starts:
        return []
    start = min(starts)
    step = max(DAY, (now - start) / points)
    out = []
    ts = start
    while ts <= now + step:
        t = min(ts, now)
        value = cost = 0.0
        for num, entry in coll.items():
            hist = hists[num]
            shop = price_at(hist, t)
            imported = price_at(entry.get("value_history", []), t) or (
                entry.get("current_value") if not entry.get("value_history") else None)
            if pref:
                unit = imported or shop
            else:
                unit = shop or imported
            if unit is None:
                unit = entry.get("current_value") or store["sets"].get(num, {}).get("rrp") or 0
            used = (store["sets"].get(num, {}).get("market") or {}).get("market_used")
            for c in copies(entry):               # every copy from its own purchase date
                added = _added_ts(c)
                if added and added > t:
                    continue
                value += float(used if used and c.get("condition") in OPENED else unit)
                cost += float(c.get("paid") or 0)
        out.append({"ts": t, "value": round(value, 2), "cost": round(cost, 2)})
        if t >= now:
            break
        ts += step
    return out


def collection_summary(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    value = cost = 0.0
    pieces = count = 0
    by_theme: dict[str, float] = {}
    for num, entry in store["collection"].items():
        s = store["sets"].get(num, {})
        theme = s.get("theme") or "Unknown"
        for c in copies(entry):
            unit, _ = copy_value(c, entry, statuses.get(num, {}), s, _prefer_import(store))
            value += unit
            cost += float(c.get("paid") or 0)
            pieces += int(s.get("pieces") or 0)
            count += 1
            by_theme[theme] = by_theme.get(theme, 0) + unit
    return {
        "sets": count, "pieces": pieces,
        "value": round(value, 2), "cost": round(cost, 2),
        "growth": round(value - cost, 2),
        "growth_pct": round((value - cost) / cost * 100, 1) if cost else None,
        "by_theme": {k: round(v, 2) for k, v in sorted(by_theme.items(), key=lambda x: -x[1])},
    }


def collection_analytics(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Breakdowns for the collection overview."""
    by_theme: dict[str, dict[str, float]] = {}
    by_year: dict[str, dict[str, float]] = {}
    by_condition: dict[str, int] = {}
    movers = []
    paid_total = paid_pieces = 0.0
    for num, entry in store["collection"].items():
        s = store["sets"].get(num, {})
        paid_set = value_set = 0.0
        for c in copies(entry):
            unit, _ = copy_value(c, entry, statuses.get(num, {}), s, _prefer_import(store))
            t = by_theme.setdefault(s.get("theme") or "Unknown", {"count": 0, "value": 0.0, "cost": 0.0})
            t["count"] += 1
            t["value"] += unit
            t["cost"] += float(c.get("paid") or 0)
            y = by_year.setdefault(str(s.get("year") or "?"), {"count": 0, "value": 0.0})
            y["count"] += 1
            y["value"] += unit
            cond = c.get("condition") or "Unknown"
            by_condition[cond] = by_condition.get(cond, 0) + 1
            if c.get("paid") and unit:
                paid_set += float(c["paid"])
                value_set += unit
                if s.get("pieces"):
                    paid_total += float(c["paid"])
                    paid_pieces += s["pieces"]
        if paid_set and value_set:                 # one line per set: all copies with a known price together
            movers.append({"set_number": num, "name": s.get("name"), "paid": round(paid_set, 2), "value": round(value_set, 2),
                           "pct": round((value_set - paid_set) / paid_set * 100, 1)})
    movers.sort(key=lambda m: -m["pct"])
    rnd = lambda d: {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in d.items()}  # noqa: E731
    return {
        "by_theme": rnd(dict(sorted(by_theme.items(), key=lambda x: -x[1]["value"]))),
        "by_year": rnd(dict(sorted(by_year.items()))),
        "by_condition": by_condition,
        "top_gainers": [m for m in movers if m["pct"] >= 0][:5],
        "top_losers": [m for m in reversed(movers) if m["pct"] < 0][:5],
        "avg_paid_per_piece": round(paid_total / paid_pieces, 4) if paid_pieces else None,
    }


def add_event(store: dict[str, Any], kind: str, payload: dict[str, Any], now: float | None = None,
              keep: int = 200) -> None:
    events = store.setdefault("events", [])
    events.append({"ts": now or time.time(), "kind": kind, **payload})
    del events[: max(0, len(events) - keep)]


def parse_times(raw: str) -> list[str]:
    """'7:30, 19.30 en 23u05' -> ['07:30', '19:30', '23:05'] (sorted, unique, valid only)."""
    out = {f"{int(h):02d}:{mm}" for h, mm in re.findall(r"(\d{1,2})[:.hu](\d{2})", raw or "") if int(h) < 24 and int(mm) < 60}
    return sorted(out)


# ------------------------------------------------------------------ activity log
ACTIVITY_MAX = 6000
ACTIVITY_KINDS = {
    "check": "Shop check", "price": "Price change", "fetch": "Shop connection", "discover": "Link search", "link": "Link check",
    "userscript": "Userscript (Tampermonkey)", "import": "Import", "job": "Job", "notify": "Notification",
    "meta": "Set data", "user": "Your action", "settings": "Settings", "shop": "Shop status", "report": "Problem report",
}


def add_activity(store: dict[str, Any], level: str, kind: str, message: str, *, now: float | None = None,
                 **fields: Any) -> dict[str, Any]:
    """Append to the activity log. An identical message for the same set/shop right after the previous
    one is collapsed (count + time updated) so repeated failures don't flood the log."""
    log = store.setdefault("activity", [])
    now = now or time.time()
    fields = {k: v for k, v in fields.items() if v is not None}
    for prev in reversed(log[-20:]):
        if prev.get("set_number") == fields.get("set_number") and prev.get("retailer") == fields.get("retailer") \
                and prev.get("kind") == kind:
            if prev["message"] == message and prev["level"] == level:
                prev["count"] = prev.get("count", 1) + 1
                prev["ts"] = now
                prev.update(fields)             # keep the latest details (e.g. per-shop results)
                return prev
            break
    entry = {"id": f"{int(now * 1000):x}{len(log) % 1000:03d}", "ts": now, "level": level, "kind": kind,
             "message": message[:500], **fields}
    log.append(entry)
    if len(log) > ACTIVITY_MAX:
        del log[: len(log) - ACTIVITY_MAX]
    return entry


def query_activity(store: dict[str, Any], *, level: str = "", kind: str = "", retailer: str = "", source: str = "",
                   set_number: str = "", q: str = "", status: str = "", before: float | None = None,
                   limit: int = 100) -> dict[str, Any]:
    """Filter the log (newest first). level: '' | 'problems' (error+warning) | 'events' (info+ok) | exact."""
    log = store.get("activity", [])
    q = q.strip().lower()
    num = normalize_set_number(set_number) if set_number.strip() else ""

    def ok(e: dict[str, Any]) -> bool:
        """Return whether an activity entry matches the requested filters and time boundary."""
        if level == "problems" and e["level"] not in ("error", "warning"):
            return False
        if level == "events" and e["level"] not in ("info", "ok"):
            return False
        if level not in ("", "problems", "events") and e["level"] != level:
            return False
        if kind and e["kind"] != kind or source and e.get("source") != source:
            return False
        res = e.get("results") or {}
        if retailer and e.get("retailer") != retailer and retailer not in res:
            return False
        if status == "suspect":         # a price that was held back as suspicious (can be approved)
            txt = " ".join(str((r or {}).get("error") or "") for r in res.values()) + " " + e["message"]
            if "suspicious price" not in txt:
                return False
        if status in ("ok", "fail"):
            if retailer and retailer in res:
                good = res[retailer].get("ok")
            else:
                good = e["level"] in ("ok", "info") if not res else all(r.get("ok") is not False for r in res.values())
            if (status == "ok") != bool(good):
                return False
        if num and e.get("set_number") != num:
            return False
        if q and q not in (e["message"] + " " + (e.get("url") or "") + " " + (e.get("set_number") or "")).lower():
            return False
        return before is None or e["ts"] < before

    matched = [e for e in reversed(log) if ok(e)]
    facets: dict[str, dict[str, int]] = {"kind": {}, "retailer": {}, "source": {}, "level": {}}
    for e in log:
        for f in facets:
            if e.get(f):
                facets[f][e[f]] = facets[f].get(e[f], 0) + 1
        for rid in (e.get("results") or {}):
            if rid != e.get("retailer"):
                facets["retailer"][rid] = facets["retailer"].get(rid, 0) + 1
    return {"entries": matched[:limit], "total": len(matched), "more": len(matched) > limit, "facets": facets,
            "kinds": ACTIVITY_KINDS}


def today_iso(now: float | None = None) -> str:
    return date.fromtimestamp(now or time.time()).isoformat()


def is_watched(store: dict[str, Any], num: str) -> bool:
    """On the watchlist: every set you don't own, plus owned sets you also watch (e.g. for a second copy);
    watch = False takes any set off it."""
    w = store["sets"].get(num, {}).get("watch")
    return bool(w) if w is not None else num not in store["collection"]


def wishlist_summary(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The watchlist (sets not owned, and owned sets you put on it): what they cost now vs. list price."""
    cost = rrp = 0.0
    priced = count = 0
    for num, s in store["sets"].items():
        if not is_watched(store, num):
            continue
        count += 1
        price = statuses.get(num, {}).get("best_price")
        if price:
            priced += 1
            cost += price
            rrp += s.get("rrp") or price
    return {"sets": count, "priced": priced, "cost": round(cost, 2), "rrp": round(rrp, 2),
            "saving": round(rrp - cost, 2)}


COLLECTION_COLUMNS = ["Number", "Name", "Theme", "Subtheme", "Year", "Pieces", "Qty", "Paid", "Value", "Purchase Date",
                      "Condition", "Retail Price"]


def collection_rows(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows in the same layout `csv_import` understands, so export -> import round-trips."""
    rows = []
    for num, entry in store["collection"].items():
        s = store["sets"].get(num, {})
        # one line per copy when the copies differ (import reads them back as separate copies), else one line
        lines = copies(entry) if entry.get("items") else [dict(entry, qty=entry.get("qty", 1))]
        for c in lines:
            unit, _ = copy_value(c, entry, statuses.get(num, {}), s, _prefer_import(store))
            rows.append({
                "Number": num, "Name": s.get("name", ""), "Theme": s.get("theme", ""), "Subtheme": s.get("subtheme", ""),
                "Year": s.get("year", ""), "Pieces": s.get("pieces", ""), "Qty": c.get("qty", 1),
                "Paid": c.get("paid", ""), "Value": round(unit, 2) if unit else "",
                "Purchase Date": c.get("added", ""), "Condition": c.get("condition", ""),
                "Retail Price": s.get("rrp", ""),
            })
    return sorted(rows, key=lambda r: r["Number"])


def rows_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    import csv
    import io

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    def safe(v: Any) -> Any:  # spreadsheet formula injection guard
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v

    w.writerows({k: safe(v) for k, v in r.items()} for r in rows)
    return out.getvalue()


def validate_backup(data: Any) -> dict[str, Any]:
    """Sanity-check an imported backup and return a clean store (never trusts the file blindly)."""
    if not isinstance(data, dict) or not isinstance(data.get("sets"), dict):
        raise LocalizedError("Not a LEGO Price Tracker backup (field 'sets' is missing).")
    clean = new_store()
    for key in clean:
        if key in data:
            if type(data[key]) is not type(clean[key]):
                raise LocalizedError("Field {field} in the backup has the wrong type.", field=key)
            clean[key] = data[key]
    for num, s in clean["sets"].items():
        if not re.fullmatch(r"\d{3,7}", str(num)) or not isinstance(s, dict):
            raise LocalizedError("Invalid set {number} in the backup.", number=num)
        for k in ("rrp", "target_price", "pieces", "year"):
            if k in s and s[k] is not None and not isinstance(s[k], (int, float)):
                raise LocalizedError("Set {number}: {field} is not a number.", number=num, field=k)
    for num, offers in clean["offers"].items():
        if num not in clean["sets"] or not isinstance(offers, dict):
            raise LocalizedError("Offers for unknown set {number}.", number=num)
        for rid, o in offers.items():
            url = o.get("url", "") if isinstance(o, dict) else None
            if url is None or (url and not str(url).startswith(("https://", "http://"))):
                raise LocalizedError("Set {number}/{shop}: invalid URL.", number=num, shop=rid)
            hist = o.get("history", [])
            if not isinstance(hist, list) or len(hist) > MAX_HISTORY or any(
                not isinstance(h, list) or len(h) != 2
                or not all(type(x) in (int, float) and (not isinstance(x, float) or isfinite(x)) for x in h)
                for h in hist
            ) or any(a[0] > b[0] for a, b in zip(hist, hist[1:])):
                raise LocalizedError("Set {number}/{shop}: invalid price history.", number=num, shop=rid)
    for num, e in clean["collection"].items():
        if num not in clean["sets"] or not isinstance(e, dict):
            raise LocalizedError("Collection item {number} without a set.", number=num)
    return clean
