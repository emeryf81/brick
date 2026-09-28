"""Pure data logic (no Home Assistant imports) so it can be unit tested."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from statistics import median
from typing import Any

from .const import MAX_HISTORY

DAY = 86400


def normalize_set_number(value: str | int) -> str:
    """'10281-1' / ' 10281 ' -> '10281'. Variant suffix -1 is dropped."""
    text = str(value).strip()
    text = re.sub(r"-1$", "", text)
    m = re.search(r"\d{4,7}", text)
    return m.group(0) if m else text


def new_store() -> dict[str, Any]:
    return {"sets": {}, "offers": {}, "collection": {}, "snapshots": []}


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


def best_offer(offers: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """Cheapest currently available offer."""
    live = [(rid, o) for rid, o in offers.items() if o.get("available") and o.get("last_price")]
    return min(live, key=lambda x: x[1]["last_price"]) if live else None


def combined_history(offers: dict[str, dict[str, Any]]) -> list[list[float]]:
    """Cheapest-of-all-retailers series (forward filled) across all timestamps."""
    stamps = sorted({t for o in offers.values() for t, _ in o.get("history", [])})
    out: list[list[float]] = []
    for ts in stamps:
        prices = [p for o in offers.values() if (p := price_at(o.get("history", []), ts)) is not None]
        if prices:
            out.append([ts, min(prices)])
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
    }
    status["offers_live"] = sum(1 for o in offers.values() if o.get("available"))
    if series:
        status["history_days"] = int((series[-1][0] - series[0][0]) // DAY)
        status["all_time_low"] = min(p for _, p in series)
    if not best:
        return status
    rid, offer = best
    price = offer["last_price"]
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
    return status


# ----------------------------------------------------------------- collection
def collection_value(entry: dict[str, Any], status: dict[str, Any], lego_set: dict[str, Any]) -> tuple[float, str]:
    """Value of one unit and where it came from."""
    if status.get("best_price"):
        return status["best_price"], "tracked"
    if entry.get("current_value"):
        return float(entry["current_value"]), "imported"
    if lego_set.get("rrp"):
        return float(lego_set["rrp"]), "rrp"
    return 0.0, "none"


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
            added = _added_ts(entry)
            if added and added > t:
                continue
            qty = int(entry.get("qty", 1) or 1)
            hist = hists[num]
            unit = price_at(hist, t)
            if unit is None:
                unit = entry.get("current_value") or store["sets"].get(num, {}).get("rrp") or 0
            value += qty * float(unit)
            cost += qty * float(entry.get("paid") or 0)
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
        qty = int(entry.get("qty", 1) or 1)
        unit, _ = collection_value(entry, statuses.get(num, {}), s)
        value += qty * unit
        cost += qty * float(entry.get("paid") or 0)
        pieces += qty * int(s.get("pieces") or 0)
        count += qty
        theme = s.get("theme") or "Unknown"
        by_theme[theme] = by_theme.get(theme, 0) + qty * unit
    return {
        "sets": count, "pieces": pieces,
        "value": round(value, 2), "cost": round(cost, 2),
        "growth": round(value - cost, 2),
        "growth_pct": round((value - cost) / cost * 100, 1) if cost else None,
        "by_theme": {k: round(v, 2) for k, v in sorted(by_theme.items(), key=lambda x: -x[1])},
    }


def today_iso(now: float | None = None) -> str:
    return date.fromtimestamp(now or time.time()).isoformat()


def wishlist_summary(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Sets that are tracked but not owned: what they cost now vs. list price."""
    cost = rrp = 0.0
    priced = count = 0
    for num, s in store["sets"].items():
        if num in store["collection"]:
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
        unit, _ = collection_value(entry, statuses.get(num, {}), s)
        rows.append({
            "Number": num, "Name": s.get("name", ""), "Theme": s.get("theme", ""), "Subtheme": s.get("subtheme", ""),
            "Year": s.get("year", ""), "Pieces": s.get("pieces", ""), "Qty": entry.get("qty", 1),
            "Paid": entry.get("paid", ""), "Value": round(unit, 2) if unit else "",
            "Purchase Date": entry.get("added", ""), "Condition": entry.get("condition", ""),
            "Retail Price": s.get("rrp", ""),
        })
    return sorted(rows, key=lambda r: r["Number"])


def rows_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    import csv
    import io

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def validate_backup(data: Any) -> dict[str, Any]:
    """Sanity-check an imported backup and return a clean store."""
    if not isinstance(data, dict) or not isinstance(data.get("sets"), dict):
        raise ValueError("Not a LEGO Price Tracker backup (missing 'sets').")
    clean = new_store()
    for key in clean:
        if key in data:
            if type(data[key]) is not type(clean[key]):
                raise ValueError(f"Backup field {key!r} has the wrong type.")
            clean[key] = data[key]
    for num in clean["sets"]:
        if not str(num).isdigit():
            raise ValueError(f"Invalid set number {num!r} in backup.")
    return clean
