"""Tools for troubleshooting (storage figures, cache resets, parser playground, test fetch, queue preview,
outlier clean-up, a data check with safe repairs, what is planned when, the last jobs, where set data comes from,
debug logging and a debug dump). Admin only; nothing here runs on its own."""
from __future__ import annotations

import json
import logging
import re
import time
from statistics import median
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .const import RETAILERS
from .models import copies, normalize_set_number, sync_copies
from .i18n import T
from .parsers import lego_number, parse_page, retailer_from_url

RESETS = ("sitemaps", "relay_searched", "shop_js", "cooldowns", "manual", "trace", "suspects", "compare_debug")
OUTLIER_FACTOR = 3.0          # a history point more than 3× above or below the median of that link


def stats(coord: Any) -> dict[str, Any]:
    """How much is stored, and how big the store is."""
    st = coord.store
    offers = [o for per in st["offers"].values() for o in per.values()]
    size = len(json.dumps(st, default=str))
    biggest = sorted(((k, len(json.dumps(v, default=str))) for k, v in st.items()), key=lambda x: -x[1])[:8]
    return {
        "sets": len(st["sets"]), "collection": len(st["collection"]), "links": sum(1 for o in offers if o.get("url")),
        "history_points": sum(len(o.get("history") or []) for o in offers), "activity": len(st.get("activity", [])),
        "events": len(st.get("events", [])), "reports": len(st.get("reports", [])),
        "sitemap_urls": sum(len((m or {}).get("urls") or []) for m in st.get("sitemaps", {}).values()),
        "relay_searched": len(st.get("relay_searched", {})), "shop_js": sorted(st.get("shop_js", {})),
        "suspects": sum(1 for o in offers if o.get("suspect")), "approved": sum(1 for o in offers if o.get("approved")),
        "errors": sum(1 for o in offers if o.get("error")), "paused": coord.fetcher.paused(),
        "manual_gates": len(coord._manual), "store_kb": round(size / 1024, 1),
        "biggest": [{"key": k, "kb": round(n / 1024, 1)} for k, n in biggest],
        "transport": coord.fetcher.transport, "debug": logging.getLogger(__package__).isEnabledFor(logging.DEBUG),
    }


def reset(coord: Any, what: str) -> int:
    """Forget one kind of cached state; returns how many entries were removed."""
    st, f = coord.store, coord.fetcher
    if what in ("sitemaps", "relay_searched", "shop_js"):
        n = len(st.get(what, {}))
        st[what] = {}
    elif what == "cooldowns":
        n = len(f.blocked_until)
        f.reset_cooldowns()
    elif what == "manual":
        n = len(coord._manual)
        coord._manual.clear()
        f.last_request.clear()
        f.last_search.clear()
    elif what == "trace":
        n = sum(len(v) for v in f.trace.values())
        f.trace.clear()
    elif what == "suspects":
        n = 0
        for per in st["offers"].values():
            for o in per.values():
                n += bool(o.pop("suspect", None)) + bool(o.pop("approved", None))
    elif what == "compare_debug":
        n = len(coord._compare_debug)
        coord._compare_debug.clear()
    else:
        raise ValueError(what)
    coord.log("info", "meta", T("developer tools: {what} reset ({n})", what=what, n=n), source="user")
    return n


def parse(retailer: str | None, html: str, url: str = "", set_number: str | None = None) -> dict[str, Any]:
    """What the parser reads from a page that was pasted (or fetched)."""
    rid = retailer or (retailer_from_url(url) if url else None) or "generic"
    p = parse_page(rid if rid in RETAILERS else "generic", html, set_number)
    return {"retailer": rid, "price": p.price, "list_price": p.list_price, "title": p.title, "image": p.image,
            "blocked": p.blocked, "unavailable": p.unavailable, "retiring": p.retiring, "size": len(html)}


async def fetch(coord: Any, url: str, set_number: str | None = None) -> dict[str, Any]:
    """Fetch one page like a check would (same pacing and same rules) and show what is read from it."""
    rid = retailer_from_url(url)
    if not rid:
        raise ValueError(T("Not a shop the integration knows (add it under Shops first)."))
    coord.manual_gate("site:" + coord._site(rid))
    t0 = time.time()
    status, page, error = await coord.fetcher.get_page(rid, url, force=True, note_block=False)
    num = set_number or (lego_number(url) if rid == "lego_com" else None)
    out = parse(rid, page or "", url, num) if page else {"retailer": rid}
    if page and rid == "lego_com":
        out["lego"] = lego_summary(page, num)
    return {**out, "status": status, "error": error, "ms": int((time.time() - t0) * 1000),
            "final_url": coord.fetcher.final_url.get(rid), "head": (page or "")[:1500]}


def lego_summary(page: str, num: str | None) -> dict[str, Any]:
    """Where a page of the official shop keeps its product data, to see quickly why a price is (not) read."""
    from .parsers import _jsonld_blocks, _lego_own_page, _meta, _walk

    nodes = [n for b in _jsonld_blocks(page) for n in _walk(b)
             if "Product" in (n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")])]
    escaped = '\\"centAmount\\"' in page or '\\"productCode\\"' in page
    state = page.replace('\\"', '"') if escaped else page
    return {"own_page": _lego_own_page(page, num), "escaped_state": escaped,
            "jsonld_products": [{k: str(n.get(k))[:80] for k in ("name", "sku", "mpn", "productID", "url") if n.get(k)}
                                | {"offers": str(n.get("offers"))[:200]} for n in nodes][:5],
            "meta_price": _meta(page, "product:price:amount", "og:price:amount"),
            "product_codes": re.findall(r'"productCode"\s*:\s*"(\d+)"', state)[:10],
            "cent_amounts": [state[max(0, m.start() - 120):m.end() + 20] for m in re.finditer(r'"centAmount"', state)][:4]}


def outliers(coord: Any, apply: bool = False) -> list[dict[str, Any]]:
    """History points far from the usual price of the same link (a wrong product or a lost comma).
    Preview first; with apply they are removed."""
    found = []
    for num, per in coord.store["offers"].items():
        for rid, o in per.items():
            hist = o.get("history") or []
            if len(hist) < 4:
                continue
            mid = median(p for _, p in hist)
            bad = [[ts, p] for ts, p in hist if mid and not mid / OUTLIER_FACTOR <= p <= mid * OUTLIER_FACTOR]
            for ts, p in bad:
                found.append({"set_number": num, "retailer": rid, "shop": RETAILERS.get(rid, (rid,))[0], "ts": ts,
                              "price": p, "usual": round(mid, 2)})
            if apply and bad:
                o["history"] = [h for h in hist if list(h) not in bad]
    if apply and found:
        coord.log("info", "price", T("developer tools: {n} outlier prices removed", n=len(found)), source="user")
    return found


def set_debug(on: bool) -> bool:
    """Set the integration log level to DEBUG or INFO and return the requested toggle."""
    logging.getLogger(__package__).setLevel(logging.DEBUG if on else logging.INFO)
    return on


SECRET_PARAM = re.compile(r"(key|token|secret|password|passwd|pwd|auth|sig|session|code)", re.I)


def redact(value: Any) -> Any:
    """URLs without credentials: user:password@ and secret-looking query values are replaced by ***
    (only in the dump; the stored links stay as they are)."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if not isinstance(value, str) or "://" not in value:
        return value

    def one(m: re.Match) -> str:
        p = urlsplit(m.group(0))
        if not p.scheme or not p.netloc:
            return m.group(0)
        host = p.hostname or ""
        netloc = ("***@" if (p.username or p.password) else "") + host + (f":{p.port}" if p.port else "")
        query = urlencode([(k, "***" if SECRET_PARAM.search(k) else v) for k, v in parse_qsl(p.query, keep_blank_values=True)], safe="*")
        return urlunsplit((p.scheme, netloc, p.path, query, p.fragment))
    return re.sub(r"https?://[^\s\"'<>]+", one, value)


def dump(coord: Any) -> dict[str, Any]:
    """Everything useful for a bug report, without credentials (download as JSON)."""
    opts = {k: ("***" if any(w in k for w in ("key", "secret", "token", "password")) and v else v)
            for k, v in dict(coord.entry.options).items()}
    return redact({"ts": time.time(), "version": coord.version if hasattr(coord, "version") else None, "options": opts,
                   "stats": stats(coord), "retailer_stats": coord.retailer_stats(),
                   "trace": {k: list(v) for k, v in coord.fetcher.trace.items()},
                   "queue": coord.continuous_items(50), "activity": coord.store.get("activity", [])[-300:]})


# ------------------------------------------------------------------ data check
def health(coord: Any, repair: bool = False) -> list[dict[str, Any]]:
    """Inconsistencies in the stored data, each with how many, a few examples and whether a safe repair exists.
    With repair, only the safe ones are fixed (nothing you entered yourself is changed)."""
    st = coord.store
    sets, offers, coll = st["sets"], st["offers"], st["collection"]
    found: list[dict[str, Any]] = []

    def add(kind: str, items: list[Any], fix: Any = None) -> None:
        if not items:
            return
        found.append({"kind": kind, "count": len(items), "examples": [str(x) for x in items[:8]], "repairable": fix is not None})
        if repair and fix is not None:
            fix(items)

    add("orphan_offers", [n for n in offers if n not in sets], lambda xs: [offers.pop(n, None) for n in xs])
    add("orphan_collection", [n for n in coll if n not in sets], None)       # the set may come back from a backup: kept
    add("bad_set_numbers", [n for n in sets if normalize_set_number(n) != n], None)
    gone = sorted({f"{n}/{r}" for n, per in offers.items() for r in per if r not in RETAILERS and r != "lego_com"})
    add("links_of_unknown_shops", gone, None)                                # a shop removed from the settings: history kept
    unsorted = [f"{n}/{r}" for n, per in offers.items() for r, o in per.items()
                if any(a[0] > b[0] for a, b in zip(o.get("history") or [], (o.get("history") or [])[1:]))]

    def sort_hist(xs: list[str]) -> None:
        for key in xs:
            n, r = key.split("/", 1)
            o = offers[n][r]
            o["history"] = sorted({(h[0], h[1]) for h in o["history"]})
            o["history"] = [list(h) for h in o["history"]]
    add("unsorted_history", unsorted, sort_hist)
    urls: dict[str, list[str]] = {}
    for n, per in offers.items():
        for r, o in per.items():
            if o.get("url"):
                urls.setdefault(o["url"].split("?")[0].rstrip("/").lower(), []).append(n)
    add("same_link_for_several_sets", [f"{u} ({', '.join(sorted(set(ns)))})" for u, ns in urls.items() if len(set(ns)) > 1], None)
    qty = [n for n, e in coll.items() if e.get("items") and e.get("qty") != len(e["items"])]
    add("quantity_out_of_step", qty, lambda xs: [sync_copies(coll[n]) for n in xs])
    add("suspicious_piece_counts", [f"{n}: {s.get('pieces')}" for n, s in sets.items()
                                    if s.get("pieces") and (s["pieces"] < 10 and (s.get("rrp") or 0) >= 15 or s["pieces"] > 12000)], None)
    add("suspicious_rrp", [f"{n}: {s.get('rrp')}" for n, s in sets.items() if s.get("rrp") and not 0.5 <= s["rrp"] <= 2000], None)
    add("future_purchase_dates", [n for n, e in coll.items() for c in copies(e)
                                  if c.get("added") and c["added"] > time.strftime("%Y-%m-%d")], None)
    if repair and any(f["repairable"] for f in found):
        coord.log("info", "meta", T("developer tools: data check repaired {n} kinds of problems",
                                    n=sum(1 for f in found if f["repairable"])), source="user")
    return found


# ------------------------------------------------------------------ what is planned when
def schedule(coord: Any) -> dict[str, Any]:
    """When the automatic work runs next: price checks, market value, set database, sitemaps, deal scan."""
    from . import setdb, sitemaps

    now = time.time()
    info = coord.schedule_info()
    maps = coord.store.get("sitemaps") or {}
    nxt_maps = {RETAILERS.get(r, (r,))[0]: (maps.get(r) or {}).get("ts", 0) + sitemaps.REFRESH_DAYS * 86400
                for r in coord.sitemap_shops()}
    return {
        "now": now, "price_checks": {k: info.get(k) for k in ("mode", "next", "next_set", "per_hour", "cycle_hours", "checked_24h", "total")},
        "market_value": {"on": coord.market_enabled, "next": getattr(coord, "_market_next", None)},
        "set_database": {"updated": coord.setdb_info.get("ts") or None, "sets": coord.setdb_info.get("count"),
                         "next": (coord.setdb_info.get("ts") or now) + setdb.REFRESH_HOURS * 3600},
        "sitemaps": nxt_maps,
        "deal_scan": {"per_day": coord.scan_per_day, "today": coord.scan_info.get("today"), "candidates": len(coord.scan_candidates())},
        "paused": {RETAILERS.get(r, (r,))[0]: until for r, until in coord.fetcher.blocked_until.items() if until > now},
    }


def jobs(coord: Any) -> list[dict[str, Any]]:
    """The last jobs, newest first, with their duration."""
    out = []
    for j in reversed(coord.store.get("job_history") or []):
        took = (j.get("finished") or 0) - (j.get("started") or 0)
        out.append({**j, "seconds": round(took) if took > 0 else None})
    return out


def sources(coord: Any) -> dict[str, dict[str, int]]:
    """Per set field, from which source the values come (official shop, set data, a parts database, you...)."""
    out: dict[str, dict[str, int]] = {}
    for s in coord.store["sets"].values():
        for field in ("name", "rrp", "image", "pieces", "theme"):
            if not s.get(field):
                key = "missing"
            else:
                key = s.get(f"{field}_source") or "unknown"
            row = out.setdefault(field, {})
            row[key] = row.get(key, 0) + 1
    return out
