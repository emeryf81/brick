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
    """Offers whose link is not flagged as pointing to the wrong product."""
    return {rid: o for rid, o in offers.items() if o.get("link_status") != "suspect"}


def best_offer(offers: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """Cheapest currently available offer (suspect links excluded)."""
    live = [(rid, o) for rid, o in trusted(offers).items() if o.get("available") and o.get("last_price")]
    return min(live, key=lambda x: x[1]["last_price"]) if live else None


def combined_history(offers: dict[str, dict[str, Any]]) -> list[list[float]]:
    """Cheapest-of-all-retailers series (forward filled) across all timestamps; suspect links excluded."""
    offers = trusted(offers)
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
    status["deal_score"] = deal_score(status)
    status["deal_label"] = "Topdeal" if status["deal_score"] >= 70 else "Goede deal" if status["deal_score"] >= 45 else None
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
    keep = [h for h in hist if rrp * 0.2 <= h[1] <= rrp * 4]
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
        return "confirmed", "handmatig goedgekeurd"
    if re.search(rf"lego\.com/[a-z]{{2}}-[a-z]{{2}}/product/[^?#]*?(?<!\d){re.escape(set_number)}/?(?:[?#]|$)", offer.get("url") or ""):
        return "ok", "officiële LEGO.com-pagina van deze set"
    title = offer.get("title")
    status, reason = title_check(title, set_number) if title else (None, "")
    if status is None and offer.get("url"):
        slug = slug_title(offer["url"])
        if slug:
            status, reason = title_check(f"lego {slug}", set_number)
            reason = f"URL: {reason}"
    if status == "suspect":
        return status, reason
    rrp = lego_set.get("rrp")
    low = min((p for _, p in offer.get("history", [])), default=None)
    if rrp and low is not None and low < rrp * 0.3:
        return "suspect", f"prijs €{low:.2f} is veel te laag voor deze set (adviesprijs €{rrp:.2f})"
    if status is None:
        return None, "nog geen producttitel bekend (wordt ingevuld bij de volgende prijsronde)"
    return status, reason


def is_suspicious_price(price: float, lego_set: dict[str, Any], offer: dict[str, Any]) -> str | None:
    """Catch parse errors (accessory/marketplace/multi-pack prices) before they pollute history."""
    rrp = lego_set.get("rrp")
    if rrp and price < rrp * 0.2:
        return f"verdachte prijs €{price:.2f} (<20% van adviesprijs) genegeerd"
    if rrp and price > rrp * 4:
        return f"verdachte prijs €{price:.2f} (>4× adviesprijs) genegeerd"
    prices = [p for _, p in offer.get("history", [])]
    if not rrp and len(prices) >= 3:
        mid = median(prices)
        if price < mid * 0.25 or price > mid * 4:
            return f"verdachte prijs €{price:.2f} (wijkt sterk af van €{mid:.2f}) genegeerd"
    return None


# ----------------------------------------------------------------- collection
def collection_value(entry: dict[str, Any], status: dict[str, Any], lego_set: dict[str, Any],
                     prefer_import: bool = False) -> tuple[float, str]:
    """Value of one unit and where it came from.

    shop_first (default): cheapest current shop price, else the imported value (e.g. BrickEconomy), else RRP.
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
            added = _added_ts(entry)
            if added and added > t:
                continue
            qty = int(entry.get("qty", 1) or 1)
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
        unit, _ = collection_value(entry, statuses.get(num, {}), s, _prefer_import(store))
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


def collection_analytics(store: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Breakdowns for the collection overview."""
    by_theme: dict[str, dict[str, float]] = {}
    by_year: dict[str, dict[str, float]] = {}
    by_condition: dict[str, int] = {}
    movers = []
    paid_total = paid_pieces = 0.0
    for num, entry in store["collection"].items():
        s = store["sets"].get(num, {})
        qty = int(entry.get("qty", 1) or 1)
        unit, _ = collection_value(entry, statuses.get(num, {}), s, _prefer_import(store))
        t = by_theme.setdefault(s.get("theme") or "Onbekend", {"count": 0, "value": 0.0, "cost": 0.0})
        t["count"] += qty
        t["value"] += unit * qty
        t["cost"] += float(entry.get("paid") or 0) * qty
        y = by_year.setdefault(str(s.get("year") or "?"), {"count": 0, "value": 0.0})
        y["count"] += qty
        y["value"] += unit * qty
        cond = entry.get("condition") or "Onbekend"
        by_condition[cond] = by_condition.get(cond, 0) + qty
        if entry.get("paid") and unit:
            movers.append({"set_number": num, "name": s.get("name"), "paid": entry["paid"], "value": round(unit, 2),
                           "pct": round((unit - entry["paid"]) / entry["paid"] * 100, 1)})
            if s.get("pieces"):
                paid_total += entry["paid"] * qty
                paid_pieces += s["pieces"] * qty
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
        unit, _ = collection_value(entry, statuses.get(num, {}), s, _prefer_import(store))
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
    def safe(v: Any) -> Any:  # spreadsheet formula injection guard
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v

    w.writerows({k: safe(v) for k, v in r.items()} for r in rows)
    return out.getvalue()


def validate_backup(data: Any) -> dict[str, Any]:
    """Sanity-check an imported backup and return a clean store (never trusts the file blindly)."""
    if not isinstance(data, dict) or not isinstance(data.get("sets"), dict):
        raise ValueError("Geen LEGO Price Tracker-back-up (veld 'sets' ontbreekt).")
    clean = new_store()
    for key in clean:
        if key in data:
            if type(data[key]) is not type(clean[key]):
                raise ValueError(f"Veld {key!r} in de back-up heeft een verkeerd type.")
            clean[key] = data[key]
    for num, s in clean["sets"].items():
        if not re.fullmatch(r"\d{3,7}", str(num)) or not isinstance(s, dict):
            raise ValueError(f"Ongeldige set {num!r} in back-up.")
        for k in ("rrp", "target_price", "pieces", "year"):
            if k in s and s[k] is not None and not isinstance(s[k], (int, float)):
                raise ValueError(f"Set {num}: {k} is geen getal.")
    for num, offers in clean["offers"].items():
        if num not in clean["sets"] or not isinstance(offers, dict):
            raise ValueError(f"Aanbiedingen voor onbekende set {num!r}.")
        for rid, o in offers.items():
            url = o.get("url", "") if isinstance(o, dict) else None
            if url is None or (url and not str(url).startswith(("https://", "http://"))):
                raise ValueError(f"Set {num}/{rid}: ongeldige URL.")
            hist = o.get("history", [])
            if not isinstance(hist, list) or any(
                not isinstance(h, list) or len(h) != 2 or not all(isinstance(x, (int, float)) for x in h) for h in hist
            ):
                raise ValueError(f"Set {num}/{rid}: ongeldige prijshistoriek.")
    for num, e in clean["collection"].items():
        if num not in clean["sets"] or not isinstance(e, dict):
            raise ValueError(f"Collectie-item {num!r} zonder set.")
    return clean
