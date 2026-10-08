"""Data coordinator: owns the store, polls retailers, computes statuses."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .client import DOMAIN_GAP, PARTS_API, SEARCH_GAP, SET_DATA_API, SET_DATA_PAGE, Fetcher, lookup_metadata
from .const import (
    OFFICIAL,
    CONF_COMPARE, CONF_BLOCK_WORDS, CYCLE_CHOICES, CONF_WATCH_CYCLE, WATCH_CYCLE_CHOICES, WATCH_LIMIT,
    FULL_REFRESH_GAP, MANUAL_GAP, CONF_DEAL_MIN_SCORE, CONF_DEAL_ATL, CONF_DEAL_TARGET, DEFAULT_DEAL_MIN_SCORE, CONF_DEV_FIXED_TIMES, CONF_DEV_FULL_REFRESH, CONF_DEV_FREE_CYCLE, CONF_DEV_WATCH_UNLIMITED, CONF_ALLOW_WORDS, CONF_COMPARE_SOURCES, COMPARE_FRESH_HOURS, COMPARE_MISSING_HOURS, COMPARE_NET_ERRORS, COMPARE_PAUSE_HOURS,
    CONF_PARTNER_CLIENT_ID, CONF_PARTNER_CLIENT_SECRET, CONF_PARTNER_COUNTRY, CONF_RELAY, CONF_RELAY_HOURS, DEFAULT_RELAY_HOURS, CONF_MARKET, CONF_TICKER, TICKER_DEFAULT, TICKER_GOOD_SCORE, TICKER_RELOAD, LEGO_RETIRED_REST, CONF_DEAL_FILTER, DEAL_FILTER_DEFAULT, CONF_SCAN,
    CONF_AUTO_REFRESH, CONF_SET_DATA_KEY, CONF_LANGUAGE, CONF_REFRESH_MODE, CONF_SPREAD_HOURS, DEFAULT_REFRESH_MODE, DEFAULT_SPREAD_HOURS, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE, DEFAULT_SEARCH, CONF_CUSTOM_SHOPS, CONF_DIGEST_TIME, CONF_NO_AUTOPAUSE, CONF_SHOP_SEARCH, CONF_VALUE_SOURCE, DEFAULT_DIGEST_TIME, GENERIC_SHOPS, CONF_DISCOUNT_THRESHOLD, CONF_PARTS_KEY, CONF_REFRESH_TIMES, CONF_IMPERSONATE, CONF_NOTIFY, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    DEFAULT_REFRESH_TIMES, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS,
    DOMAIN, EVENT_JOB_DONE, EVENT_HIGH_DISCOUNT, EVENT_NEW_LOW, EVENT_TARGET_HIT, RETAILERS, STORAGE_KEY,
    STORAGE_VERSION,
)
from .models import (
    add_activity, add_event, clean_history, collection_analytics, link_check, collection_rows, collection_series, is_suspicious_price, collection_summary, COLLECTION_COLUMNS, rows_to_csv, validate_backup, wishlist_summary, is_watched, compute_set_status, new_store, normalize_set_number,
    offer_price, query_activity, record_price, today_iso, COPY_FIELDS, OPENED, copies, copy_value, sync_copies,
    investment_report, sold_record, sold_summary, SOLD_MAX, set_pieces,
)
from .i18n import DEFAULT_LANGUAGE, LANGUAGES, LocalizedError, T, resolve, set_language
from .notifications import Notifier, default_rules
from .partner_api import PartnerApi, PartnerApiError
from . import catalog, sitemaps, compare, setdb, scan
from .shops import (CONF_LEGAL, CONF_SHOP_PROFILE, LEGAL_VERSION, MARKET, legacy, all_domains, partner_site, partner_site_url, domain_of, profile_ids, shop_with_reader,
                    reader_of, ready, site_root, source_url, CONF_KEY_HOSTS, KEY_SOURCES, key_allowed, source_hosts)
from .parsers import Parsed, title_check, wrong_product
from .shops import SEARCH, valid_search
from .parsers import BUILTIN_WORDS, KNOCKOFF_RE, find_search_result, is_search_url, search_url, accessory_word, set_custom_words, clean_title, normalize_url, retailer_from_url, url_key

_LOGGER = logging.getLogger(__name__)


class LegoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """data = {"statuses": {set: status}, "summary": {...}}"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize storage, fetchers, notification handling, and background-job state."""
        # No polling interval: shop rounds run as background jobs (buttons or the schedule).
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.entry = entry
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        # the LEGO set database (every set there is), in its own file: it is large and changes once a day
        self._setdb_store = Store[dict[str, Any]](hass, 1, f"{STORAGE_KEY}.setdb")
        self.setdb: dict[str, list[Any]] = {}
        self.setdb_info: dict[str, Any] = {"ts": 0, "count": 0, "error": None, "busy": False}
        # deals on every LEGO set (scan.py): one compact entry per looked-up set, also in its own file
        self._scan_store = Store[dict[str, Any]](hass, 1, f"{STORAGE_KEY}.scan")
        self.scan: dict[str, dict[str, Any]] = {}
        self.scan_info: dict[str, Any] = {"busy": False, "next": 0.0, "error": None, "day": "", "today": 0, "src": 0}
        self.store: dict[str, Any] = new_store()
        self.fetcher = Fetcher(hass, bool(self.opt(entry, CONF_IMPERSONATE, True)))
        self.fetcher.no_autopause = set(self.opt(entry, CONF_NO_AUTOPAUSE, []) or [])
        self._partner_api: PartnerApi | None = None
        self._partner_found: dict[str, dict[str, Any]] = {}
        self._alerted: set[tuple[str, str]] = set()
        self.job: dict[str, Any] | None = None
        self.last_job: dict[str, Any] | None = None
        self._job_task: asyncio.Task | None = None
        self._cancel = False
        self._job_source = "panel"
        self.notifier = Notifier(self)
        self._net_errors: dict[str, int] = {}
        self._compare_debug: dict[str, dict[str, Any]] = {}
        self._compare_fallback: dict[tuple[str, str], Any] = {}   # prices on a search page, used when its product page fails
        self._no_follow: dict[str, float] = {}                     # site -> until when its product pages are skipped (403)
        self._compare_retry_unsub: Callable[[], None] | None = None
        self._first_checks: list[str] = []                         # sets just added, waiting for their first prices
        self._first_busy = False
        self._rediscover: list[tuple[str, str]] = []               # (set, shop) to search again after a blocked link
        self._first_current: str | None = None
        def _paused(rid: str, hours: float) -> None:
            if rid in compare.SOURCES:
                self.log("error", "shop", T("{shop} blocked us: paused for {hours} h", shop=compare.SOURCES[rid][0], hours=hours),
                         source=compare.SOURCES[rid][0])
                return
            self.log("error", "shop", T("{shop} blocked us: paused for {hours} h", shop=RETAILERS.get(rid, (rid,))[0], hours=hours), retailer=rid)
            self.hass.async_create_task(self.notifier.on_shop_paused(rid, hours))
        self.fetcher.on_pause = _paused
        self.fetcher.abort = lambda: self._cancel and self.job_running     # 'Stop' also ends a wait for a site
        self._manual: dict[str, float] = {}

    # ---------------------------------------------------------------- options
    @staticmethod
    def opt(entry: ConfigEntry, key: str, default: Any) -> Any:
        return entry.options.get(key, entry.data.get(key, default))

    @property
    def threshold(self) -> float:
        return float(self.opt(self.entry, CONF_DISCOUNT_THRESHOLD, DEFAULT_DISCOUNT_THRESHOLD))

    @property
    def retailers(self) -> list[str]:
        return [r for r in self.opt(self.entry, CONF_RETAILERS, profile_ids()) if r in RETAILERS]

    # ---------------------------------------------------------------- storage
    @property
    def language(self) -> str:
        return resolve(self.opt(self.entry, CONF_LANGUAGE, DEFAULT_LANGUAGE), self.hass.config.language)

    async def async_load(self) -> None:
        """Load and migrate stored data, initialize the fetcher, and restore cooldowns."""
        await self.hass.async_add_executor_job(catalog.load)
        set_language(self.language)
        set_custom_words(self.opt(self.entry, CONF_BLOCK_WORDS, []), self.opt(self.entry, CONF_ALLOW_WORDS, []))
        await self.fetcher.async_setup()
        if (data := await self._store.async_load()):
            self.store = {**new_store(), **data}
        if (db := await self._setdb_store.async_load()):
            self.setdb = db.get("sets") or {}
            self.setdb_info.update(ts=db.get("ts", 0), count=len(self.setdb))
        if (sc := await self._scan_store.async_load()):
            self.scan = sc.get("sets") or {}
        self._drop_old_source_links()
        self._partner_links_to_site()
        self._marketplace_no_price_is_out_of_stock()
        self._fix_lego_rrp()
        self._fix_lost_commas()
        self._fix_piece_counts()
        self._rename_market_source()
        self._watch_dates(first=True)
        for num, st in self.store["sets"].items():            # fill gaps from the built-in catalogue (no network)
            catalog.apply(num, st, self.store["offers"].setdefault(num, {}))
        from .csv_import import LEGACY_CONDITIONS
        for e in self.store["collection"].values():
            if e.get("condition") in LEGACY_CONDITIONS:
                e["condition"] = LEGACY_CONDITIONS[e["condition"]]
        for offers in self.store["offers"].values():          # link reasons stored by versions < 0.9
            for o in offers.values():
                if o.get("link_status") == "confirmed" and o.get("link_reason") in ("handmatig goedgekeurd", "handmatig ingesteld"):
                    o["link_reason"] = T("confirmed by hand")
        if "notify_rules" not in self.store:     # first run / upgrade: sensible defaults
            self.store["notify_rules"] = default_rules(self.threshold, self.opt(self.entry, CONF_NOTIFY, "") or "")
        cd = self.store.get("cooldowns") or {}
        now = time.time()
        self.fetcher.blocked_until.update({r: t for r, t in (cd.get("until") or {}).items() if t > now})
        self.fetcher.blocks.update(cd.get("blocks") or {})
        self.store["value_source"] = self.opt(self.entry, CONF_VALUE_SOURCE, "shop_first")
        self.verify_links()   # flag wrong links from older versions right away

    def move_shop(self, old: str, new: str) -> int:
        """Links and prices of a shop you added yourself go to the built-in shop for the same site (where that
        one has no link yet). Your own shop is switched off, not deleted. Returns the number of links moved."""
        n = 0
        for num, offers in self.store["offers"].items():
            mine = offers.get(old)
            if not mine or not mine.get("url"):
                continue
            ours = offers.get(new)
            if not ours or not ours.get("url"):
                offers[new] = offers.pop(old)
            else:
                # both linked: keep the built-in link, and also the price history and anything you entered by hand
                hist = {int(ts // 86400): [ts, p] for ts, p in sorted((ours.get("history") or []) + (mine.get("history") or []))}
                ours["history"] = sorted(hist.values())
                for k in ("manual_price", "manual_url", "approved"):
                    if mine.get(k) and not ours.get(k):
                        ours[k] = mine[k]
                if (mine.get("last_checked") or 0) > (ours.get("last_checked") or 0):
                    for k in ("last_price", "available", "last_checked", "last_ok"):
                        if k in mine:
                            ours[k] = mine[k]
                del offers[old]
            n += 1
        if n:
            self.log("info", "shop", T("{n} links of your own shop {old} moved to the built-in shop {new}", n=n,
                                       old=RETAILERS.get(old, (old,))[0], new=RETAILERS.get(new, (new,))[0]))
            self._save()
        return n

    def open_error_keys(self) -> dict[str, str]:
        """{"set|shop": kind} for every open error, the same list as Logbook → Open errors: a set without any
        shop link, a shop page that failed, a suspicious link or price. Ignored errors don't count."""
        out: dict[str, str] = {}
        for num in self.store["sets"]:
            offers = self.store["offers"].get(num) or {}
            if not offers:
                out[f"{num}|"] = "nolinks"
            for rid, o in offers.items():
                if o.get("ignored") or not (o.get("error") or o.get("link_status") == "suspect"):
                    continue
                out[f"{num}|{rid}"] = "suspect" if o.get("link_status") == "suspect" else "error"
        return out

    def _track_errors(self, now: float | None = None) -> None:
        """Keep count of the errors that are solved and the ones that come in, per day (Logbook → Open errors).
        The first time, today's open errors are only the starting point (not counted as new)."""
        now = now or time.time()
        tr = self.store.setdefault("err_track", {})
        cur = self.open_error_keys()
        day = dt_util.as_local(dt_util.utc_from_timestamp(now)).date().isoformat()
        if "open" not in tr:
            tr.update(open={k: now for k in cur}, days={}, solved=[], new=[], since=now)
            return
        was: dict[str, float] = tr["open"]
        for k in [k for k in was if k not in cur and k.partition("|")[0] not in self.store["sets"]]:
            was.pop(k)                                     # the set was removed: not solved, just gone
        gone = [k for k in was if k not in cur]
        came = [k for k in cur if k not in was]
        if not gone and not came:
            return
        d = tr["days"].setdefault(day, {"new": 0, "solved": 0})
        d["solved"] += len(gone)
        d["new"] += len(came)
        for k in gone:
            tr["solved"].append([now, k, was.pop(k)])
        for k in came:
            was[k] = now
            tr["new"].append([now, k, cur[k]])
        del tr["solved"][:-500], tr["new"][:-500]
        for old in sorted(tr["days"])[:-400]:              # a little more than a year of days
            del tr["days"][old]

    def error_stats(self) -> dict[str, Any]:
        """Solved and new errors per day (for charts per day, week and month) and the latest of each, with
        the set, the shop and how long the error was open."""
        self._track_errors()
        tr = self.store.get("err_track") or {}

        def row(e: list[Any], solved: bool) -> dict[str, Any]:
            num, _, rid = e[1].partition("|")
            s = self.store["sets"].get(num) or {}
            return {"ts": e[0], "set_number": num, "name": s.get("name") or "", "retailer": rid or None,
                    "shop": RETAILERS.get(rid, (rid,))[0] if rid else None,
                    **({"open_for": max(0, e[0] - e[2])} if solved else {"kind": e[2]})}
        return {"open": len(tr.get("open") or {}), "since": tr.get("since"), "days": tr.get("days") or {},
                "solved": [row(e, True) for e in reversed(tr.get("solved") or [])][:200],
                "new": [row(e, False) for e in reversed(tr.get("new") or [])][:200]}

    def _save(self) -> None:
        # pauses survive restarts/reloads, otherwise a reload would hammer a shop that just blocked us
        try:
            self._track_errors()
        except Exception:  # noqa: BLE001 - statistics must never stop a save
            _LOGGER.exception("error statistics failed")
        self.store["cooldowns"] = {"until": dict(self.fetcher.blocked_until), "blocks": dict(self.fetcher.blocks)}
        self._store.async_delay_save(lambda: self.store, 5)

    async def async_shutdown(self) -> None:
        self.stop_spread()
        if self._compare_retry_unsub:
            self._compare_retry_unsub()
        self._cancel = True
        if self._job_task and not self._job_task.done():
            self._job_task.cancel()
        await self._store.async_save(self.store)
        await self.fetcher.async_close()
        await super().async_shutdown()

    # -------------------------------------------------------------- computing
    def compute(self) -> dict[str, Any]:
        min_days = int(self.opt(self.entry, CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS))
        statuses = {
            num: compute_set_status(s, self.store["offers"].get(num, {}),
                                    threshold=self.threshold, min_history_days=min_days)
            for num, s in self.store["sets"].items()
        }
        return {"statuses": statuses, "summary": collection_summary(self.store, statuses),
                "wishlist": wishlist_summary(self.store, statuses),
                "analytics": collection_analytics(self.store, statuses)}

    async def _async_update_data(self) -> dict[str, Any]:
        return self.compute()

    def _watch_dates(self, first: bool = False) -> None:
        """When each set came on the watchlist (sort by date). first: older data gets its earliest known date."""
        early: dict[str, float] = {}
        if first:
            for e in self.store.get("activity", []):
                if (n := e.get("set_number")) and n not in early:
                    early[n] = e["ts"]
            for n, per in self.store["offers"].items():
                pts = [h[0] for o in per.values() for h in (o.get("history") or [])[:1]]
                if pts:
                    early[n] = min(early.get(n, pts[0]), *pts)
        for num, st in self.store["sets"].items():
            if is_watched(self.store, num):
                st.setdefault("watch_since", early.get(num) or time.time())
            else:
                st.pop("watch_since", None)

    def push_update(self) -> None:
        """Recompute without hitting the network (after edits/imports)."""
        self._watch_dates()
        self._save()
        self.async_set_updated_data(self.compute())

    # ------------------------------------------------------------------ schedule
    @property
    def refresh_times(self) -> list[tuple[int, int]]:
        raw = str(self.opt(self.entry, CONF_REFRESH_TIMES, DEFAULT_REFRESH_TIMES))
        out = []
        for h, m in re.findall(r"(\d{1,2})[:.hu](\d{2})", raw):
            if int(h) < 24 and int(m) < 60:
                out.append((int(h), int(m)))
        return sorted(set(out))

    def dev(self, key: str) -> bool:
        return bool(self.opt(self.entry, key, False))

    @property
    def refresh_mode(self) -> str:
        mode = self.opt(self.entry, CONF_REFRESH_MODE, None)
        if mode is None:   # older installs: auto_refresh off = off, else the new spread mode
            mode = DEFAULT_REFRESH_MODE if self.opt(self.entry, CONF_AUTO_REFRESH, True) else "off"
        if mode == "times" and not self.dev(CONF_DEV_FIXED_TIMES):
            mode = "spread"            # full rounds at fixed times put too much load on the shops
        return mode if mode in ("spread", "times", "off") else DEFAULT_REFRESH_MODE

    @property
    def watch_cycle_minutes(self) -> int:
        try:
            v = int(self.opt(self.entry, CONF_WATCH_CYCLE, 0) or 0)
        except (TypeError, ValueError):
            return 0
        return v if v in WATCH_CYCLE_CHOICES else 0

    @property
    def watch_limit(self) -> int | None:
        return None if self.dev(CONF_DEV_WATCH_UNLIMITED) else WATCH_LIMIT

    def deal_of_the_day(self, include: Any = None, shops: list[str] | None = None) -> tuple[str, dict[str, Any]] | None:
        """The best deal right now (as the panel shows it under Deals → Today): a set that counts as a deal,
        highest deal score first. include(num): which sets may count (default: your watchlist)."""
        data, rules = self.data or self.compute(), self.deal_rules
        best = None
        for num, st in data["statuses"].items():
            if num not in self.store["sets"] or not (include(num) if include else self.is_watched(num)):
                continue
            if st.get("best_price") is None or self.deal_blocked(num, st) or (shops and st.get("best_retailer") not in shops):
                continue
            deal = (rules["atl"] and st.get("is_all_time_low")) or (rules["target"] and st.get("target_hit")) \
                or st.get("deal_score", 0) >= rules["min_score"] or (st.get("discount_rrp") or 0) >= rules["threshold"]
            key = (st.get("deal_score", 0), st.get("discount_rrp") or 0)
            if deal and (best is None or key > best[0]):
                best = (key, num, st)
        return (best[1], best[2]) if best else None

    @property
    def deal_rules(self) -> dict[str, Any]:
        """What counts as a deal (the panel's Deals view and badges)."""
        try:
            score = max(0, min(100, int(self.opt(self.entry, CONF_DEAL_MIN_SCORE, DEFAULT_DEAL_MIN_SCORE))))
        except (TypeError, ValueError):
            score = DEFAULT_DEAL_MIN_SCORE
        return {"threshold": self.threshold, "min_score": score, "atl": bool(self.opt(self.entry, CONF_DEAL_ATL, True)),
                "target": bool(self.opt(self.entry, CONF_DEAL_TARGET, True))}

    @property
    def deal_filter(self) -> dict[str, Any]:
        """Return deal filter defaults merged with the configured overrides."""
        return {**DEAL_FILTER_DEFAULT, **(self.opt(self.entry, CONF_DEAL_FILTER, None) or {})}

    def deal_blocked(self, num: str, status: dict[str, Any] | None = None) -> str | None:
        """Why a set is left out of Deals and price notifications (Deals → Settings), or None."""
        from .themes import key as theme_key

        if num in (self.store.get("deal_always") or []):
            return None                                    # put back by hand: the filters don't apply to it
        f, s = self.deal_filter, self.store["sets"].get(num, {})
        if status is None:
            status = ((self.data or {}).get("statuses") or {}).get(num) or self.compute()["statuses"].get(num, {})
        st = status
        off = {theme_key(x) for x in f["themes_off"]}
        if s.get("theme") and theme_key(s["theme"]) in off:
            return "theme"
        price, pieces = st.get("best_price"), s.get("pieces")
        if price is not None and f["min_price"] is not None and price < f["min_price"]:
            return "min_price"
        if price is not None and f["max_price"] is not None and price > f["max_price"]:
            return "max_price"
        if f["min_discount"] is not None and (st.get("discount_rrp") is None or st["discount_rrp"] < f["min_discount"]):
            return "min_discount"
        if pieces and f["min_pieces"] is not None and pieces < f["min_pieces"]:
            return "min_pieces"
        if pieces and f["max_pieces"] is not None and pieces > f["max_pieces"]:
            return "max_pieces"
        if f["skip_owned"] and num in self.store["collection"]:
            return "owned"
        if f["skip_retired"] and st.get("retired"):
            return "retired"
        return None

    def set_deal_exception(self, set_number: str, show: bool) -> None:
        """Deals → Settings → left-out sets: 'show anyway' makes one set an exception to the filters (it shows in
        Deals, the ticker and notifications again); undoing it lets the filters decide again."""
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise LocalizedError("Set {number} is not tracked.", number=num)
        always = self.store.setdefault("deal_always", [])
        if show and num not in always:
            always.append(num)
        elif not show and num in always:
            always.remove(num)
        self.log("info", "user", T("set shown in Deals again despite the deal settings") if show else T("deal settings apply to this set again"),
                 set_number=num, source="panel")
        self.async_set_updated_data(self.compute())
        self._save()

    def is_watched(self, num: str) -> bool:
        """On the watchlist: every set you don't own, plus owned sets you also watch (e.g. for a second copy)."""
        return is_watched(self.store, num)

    def watched_sets(self) -> list[str]:
        return [n for n in self.store["sets"] if self.is_watched(n)]

    @property
    def auto_refresh(self) -> bool:
        return self.refresh_mode == "times" and bool(self.refresh_times)

    @property
    def spread_hours(self) -> float:
        try:
            h = max(1.0, min(168.0, float(self.opt(self.entry, CONF_SPREAD_HOURS, DEFAULT_SPREAD_HOURS))))
        except (TypeError, ValueError):
            return DEFAULT_SPREAD_HOURS
        if self.dev(CONF_DEV_FREE_CYCLE):
            return h
        return float(min(CYCLE_CHOICES, key=lambda c: (abs(c - h), -c)))   # nearest allowed choice

    # ------------------------------------------------------------ spread checks
    def _spread_candidates(self) -> list[str]:
        if self.compare_enabled:          # comparison sites can also find prices for sets without links
            return list(self.store["sets"])
        live = set(self._live_retailers(False))
        return [n for n, offers in self.store["offers"].items() if n in self.store["sets"]
                and any(r in live and o.get("url") for r, o in offers.items())]

    def _watch_hours(self) -> float | None:
        """Watchlist cycle in hours (never slower than the normal cycle), or None when off."""
        wc = self.watch_cycle_minutes
        return min(wc / 60, self.spread_hours) if wc else None

    def check_rates(self) -> dict[str, float]:
        """Checks per hour: the normal cycle, the watchlist cycle, and together."""
        cands = set(self._spread_candidates())
        wh = self._watch_hours()
        watch = {n for n in cands if self.is_watched(n)} if wh else set()
        main = len(cands - watch) / self.spread_hours
        fast = len(watch) / wh if wh else 0.0
        return {"main": main, "watch": fast, "total": main + fast, "sets": len(cands - watch), "watched": len(watch)}

    def spread_interval(self) -> float:
        """Seconds between two set checks, so every set is checked once per cycle (watchlist sets
        once per watchlist cycle)."""
        rate = self.check_rates()["total"]
        return max(20.0, 3600 / rate) if rate else 3600.0

    def start_spread(self, delay: float = 60) -> None:
        from homeassistant.helpers.event import async_call_later

        self.stop_spread()
        self._spread_active = True
        self._spread_next = time.time() + delay
        self._spread_unsub = async_call_later(self.hass, delay, self._spread_tick)

    def stop_spread(self) -> None:
        self._spread_active = False
        if getattr(self, "_spread_unsub", None):
            self._spread_unsub()
        self._spread_unsub = None

    def next_spread_set(self) -> str | None:
        cands = self._spread_candidates()
        if not cands:
            return None
        checked = lambda n: self.store["sets"][n].get("checked", 0)   # noqa: E731
        now = time.time()
        if (wh := self._watch_hours()):            # watchlist sets that are due go first
            due = [n for n in cands if self.is_watched(n) and now - checked(n) >= wh * 3600]
            if due:
                return min(due, key=checked)
            cands = [n for n in cands if not self.is_watched(n)] or cands
        num = min(cands, key=checked)
        if now - checked(num) < self.spread_hours * 3600 * 0.5:
            return None        # everything was checked recently (e.g. after a full manual round)
        return num

    async def _spread_tick(self, _now: Any = None) -> None:
        from homeassistant.helpers.event import async_call_later

        self._spread_unsub = None
        try:
            if ready() and not self.job_running and (num := self.next_spread_set()):
                await self.refresh_set(num, self._live_retailers(False), source="schedule")
                self._spread_last = {"set_number": num, "ts": time.time()}
                self.push_update()
        except Exception:  # noqa: BLE001 - the loop must keep running
            _LOGGER.exception("Spread check failed")
        finally:
            if getattr(self, "_spread_active", False):
                interval = self.spread_interval()
                self._spread_next = time.time() + interval
                self._spread_unsub = async_call_later(self.hass, interval, self._spread_tick)

    def next_refresh(self) -> float | None:
        if not self.auto_refresh:
            return None
        now = dt_util.now()
        cands = []
        for h, m in self.refresh_times:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            cands.append(t if t > now else t + timedelta(days=1))
        return min(cands).timestamp()

    def schedule_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.refresh_mode, "auto": self.refresh_mode != "off",
                                "times": [f"{h:02d}:{m:02d}" for h, m in self.refresh_times], "next": self.next_refresh()}
        if self.refresh_mode == "spread":
            interval = self.spread_interval()
            day = time.time() - 86400
            rates = self.check_rates()
            info.update(watch_cycle_min=self.watch_cycle_minutes,
                        rates={k: round(v, 1) if isinstance(v, float) else v for k, v in rates.items()})
            info.update(cycle_hours=self.spread_hours, interval=round(interval), per_hour=round(3600 / interval, 1),
                        next=getattr(self, "_spread_next", None), next_set=self.next_spread_set(),
                        last=getattr(self, "_spread_last", None),
                        checked_24h=sum(1 for s in self.store["sets"].values() if s.get("checked", 0) > day),
                        total=len(self._spread_candidates()))
        return info

    # ------------------------------------------------------------------ logbook
    # ------------------------------------------------------------ manual actions (load protection)
    def _site(self, rid: str) -> str:
        return (domain_of(rid) or rid).removeprefix("www.")

    def manual_left(self, key: str) -> float:
        return max(0.0, self._manual.get(key, 0) + MANUAL_GAP - time.time())

    def manual_gate(self, key: str) -> None:
        """Manual fetches and searches: at most once every 2 minutes (per set action, per shop site)."""
        if not ready():
            raise LocalizedError("First import the shop settings and accept their terms (Manage → Shops).")
        if (left := self.manual_left(key)) > 0:
            s = int(left) + 1
            if key == "prices":
                raise LocalizedError("To protect the traffic to and the load on the shops, fetching prices is possible once every 2 minutes. Try again in {s} s.", s=s)
            if key == "find":
                raise LocalizedError("To protect the traffic to and the load on the shops, searching the shops is possible once every 2 minutes. Try again in {s} s.", s=s)
            raise LocalizedError("To protect the traffic to and the load on this shop, it can be fetched once every 2 minutes. Try again in {s} s.", s=s)
        self._manual[key] = time.time()

    def manual_status(self) -> dict[str, Any]:
        """When each manual button is available again (epoch seconds), for the panel to grey them out."""
        until = lambda k: (self._manual[k] + MANUAL_GAP) if self.manual_left(k) > 0 else None  # noqa: E731
        return {"now": time.time(), "gap": MANUAL_GAP, "prices": until("prices"), "find": until("find"),
                "shops": {rid: u for rid in RETAILERS if (u := until("site:" + self._site(rid)))}}

    def mark_sites(self, rids: list[str]) -> None:
        now = time.time()
        for rid in rids:
            self._manual["site:" + self._site(rid)] = now

    def _price_choice(self, num: str, rid: str, offer: dict[str, Any], price: float | None, source: str) -> None:
        """Your own price counts, but the shop now says something else: ask once (logbook, set) whether the
        new price should count from now on or your price stays. A price you declined is not asked again."""
        manual = float((offer.get("manual_price") or {}).get("price") or 0)
        if price is None or not manual or abs(price - manual) < 0.01:
            offer.pop("price_choice", None)
            return
        if offer.get("declined_price") is not None and abs(offer["declined_price"] - price) < 0.01:
            return
        if (offer.get("price_choice") or {}).get("price") == price:
            return
        offer["price_choice"] = {"price": price, "manual": manual, "ts": time.time()}
        self.log("warning", "price", T("the shop now says €{new}; your own price €{manual} still counts: choose which one", new=f"{price:.2f}", manual=f"{manual:.2f}"),
                 set_number=num, retailer=rid, url=offer.get("url"), price=price, old_price=manual, source=source, choice=True)

    def resolve_price_choice(self, set_number: str, retailer: str, accept: bool) -> float:
        """Logbook choice for a shop with your own price: accept the fetched price (your price goes away, the
        shop's price counts from now on) or keep your price (this fetched price is not asked again)."""
        num = normalize_set_number(set_number)
        offer = (self.store["offers"].get(num) or {}).get(retailer)
        if not offer or not offer.get("price_choice"):
            raise LocalizedError("There is no new price to choose for this shop.")
        choice = offer.pop("price_choice")
        before = self.compute()["statuses"].get(num, {})
        if accept:
            offer.pop("manual_price", None)
            offer.pop("declined_price", None)
            record_price(offer, float(choice["price"]))
            offer["last_ok"] = time.time()
            self.log("ok", "price", T("new price €{price} approved; your own price is gone", price=f"{choice['price']:.2f}"),
                     set_number=num, retailer=retailer, url=offer.get("url"), price=choice["price"], source="panel")
        else:
            offer["declined_price"] = float(choice["price"])
            self.log("info", "price", T("your own price €{price} kept", price=f"{choice['manual']:.2f}"),
                     set_number=num, retailer=retailer, url=offer.get("url"), price=choice["manual"], source="panel")
        self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self._save()
        self.push_update()
        return float(choice["price"] if accept else choice["manual"])

    def approve_price(self, num: str, rid: str) -> float:
        """A price held back as suspicious is right after all: store it, and accept prices like it (±25 %)
        for this link from now on."""
        num = normalize_set_number(num)
        offer = (self.store["offers"].get(num) or {}).get(rid)
        if not offer or not offer.get("suspect"):
            raise LocalizedError("There is no suspicious price to approve for this shop.")
        price = float(offer.pop("suspect")["price"])
        before = self.compute()["statuses"].get(num, {})
        offer["approved"] = price
        record_price(offer, price)
        offer["last_ok"], offer["error"] = offer.get("last_checked") or time.time(), None
        offer.pop("ignored_error", None)
        self.log("ok", "price", T("suspicious price €{price} approved by hand", price=f"{price:.2f}"),
                 set_number=num, retailer=rid, url=offer.get("url"), price=price, source="panel")
        self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self._save()
        self.push_update()
        return price

    def report_problem(self, num: str, problems: list[str], shops: list[str], comment: str,
                       save: bool = True) -> tuple[dict[str, Any], str]:
        """'Problem with this set': a snapshot of links, prices, errors and the recent log, with the user's remark."""
        from . import report

        rep = report.build(self.store, num, self.compute()["statuses"].get(num, {}), problems, shops, comment)
        if save:
            report.save(self.store, rep)
            what = ", ".join(rep["problems"]) or "?"
            # the remark itself stays in the report (export is for administrators); the shared logbook only says that there is one
            self.log("warning", "report", T("problem reported ({what})", what=what) + (" · " + T("with a remark") if rep["comment"] else ""),
                     set_number=num, report=rep["id"], retailer=rep["shops"][0] if len(rep["shops"]) == 1 else None,
                     source="panel")
            self._save()
            self.push_update()
        return rep, report.to_csv([rep])

    def log(self, level: str, kind: str, message: str, **fields: Any) -> None:
        """Everything the integration does ends up here (Beheer → Logboek)."""
        fields.setdefault("source", "server")
        add_activity(self.store, level, kind, message, **fields)

    # ---------------------------------------------------------------------- jobs
    def job_info(self) -> dict[str, Any]:
        return {"job": dict(self.job) if self.job else None, "last": self.last_job,
                "paused": {RETAILERS[r][0]: h for r, h in self.fetcher.paused().items() if r in RETAILERS},
                "schedule": self.schedule_info(), "first_checks": self.first_checks}

    @property
    def first_checks(self) -> list[str]:
        """Sets just added that are still waiting for (or getting) their first prices."""
        return ([self._first_current] if self._first_current else []) + list(self._first_checks)

    @property
    def job_running(self) -> bool:
        return self._job_task is not None and not self._job_task.done()

    def start_job(self, kind: str, label: str, items: list[str],
                  worker: Callable[[str], Awaitable[dict[str, int] | None]], note: str | None = None) -> dict[str, Any]:
        if self.job_running:
            raise LocalizedError("A job is already running: {label} ({done}/{total}).", label=self.job["label"],
                                 done=self.job["done"], total=self.job["total"])
        self._cancel = False
        self.job = {"kind": kind, "label": label, "total": len(items), "done": 0, "current": None,
                    "found": 0, "updated": 0, "errors": 0, "skipped": 0, "started": time.time(),
                    "running": True, "cancelled": False, "note": note}
        self.job["shops"] = {}
        self.log("info", "job", T("{label} started for {n} sets", label=label, n=len(items)) + (f" ({note})" if note else ""),
                 source=self._job_source)
        self._job_source = "panel"
        self._job_task = self.entry.async_create_background_task(
            self.hass, self._run_job(items, worker), f"{DOMAIN}_{kind}")
        return dict(self.job)

    def cancel_job(self) -> bool:
        if not self.job_running:
            return False
        self._cancel = True
        return True

    async def _run_job(self, items: list[str], worker: Callable[[str], Awaitable[dict[str, int] | None]]) -> None:
        job = self.job
        assert job is not None
        try:
            for num in items:
                if self._cancel:
                    job["cancelled"] = True
                    break
                job["current"] = f"{num} {self.store['sets'].get(num, {}).get('name') or ''}".strip()
                try:
                    res = await worker(num)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad set must not stop the round
                    _LOGGER.exception("%s failed for set %s", job["kind"], num)
                    job["errors"] += 1
                else:
                    for k, v in (res or {}).items():
                        job[k] = job.get(k, 0) + v
                job["done"] += 1
                if job["done"] % 10 == 0:
                    self.push_update()
        finally:
            job["running"] = False
            job["current"] = None
            job["finished"] = time.time()
            self.last_job = dict(job)
            parts = [T("{n} updated", n=job["updated"]) if job.get("updated") else "",
                     T("{n} links found", n=job["found"]) if job.get("found") else "",
                     T("{n} skipped (paused)", n=job["skipped"]) if job.get("skipped") else "",
                     T("{n} errors", n=job["errors"]) if job.get("errors") else ""]
            done_txt = (T("{label} stopped: {done}/{total} sets", label=job["label"], done=job["done"], total=job["total"])
                        if job["cancelled"] else
                        T("{label} finished: {done}/{total} sets", label=job["label"], done=job["done"], total=job["total"]))
            done_txt += "".join(", " + p for p in parts if p)
            self.log("warning" if job.get("errors") else "ok", "job", done_txt)
            for rid, st in (job.get("shops") or {}).items():
                why = sorted((st.get("why") or {}).items(), key=lambda x: -x[1])[:3]
                self.log("ok" if not st["err"] else "warning" if st["ok"] else "error", "fetch",
                         T("{shop}: {ok} succeeded, {failed} failed", shop=RETAILERS.get(rid, (rid,))[0], ok=st["ok"], failed=st["err"])
                         + "".join(f" · {n}× {r}" for r, n in why), retailer=rid)
            if job["kind"] == "refresh":
                self._snapshot()
            if job["kind"] in ("enrich", "update"):
                self.verify_links()      # new RRPs from the official shop: re-judge links, drop impossible prices
            self.push_update()
            self.hass.async_create_task(self.notifier.on_job_done(dict(job)))
            self.hass.bus.async_fire(EVENT_JOB_DONE, {k: job[k] for k in ("kind", "total", "done", "found", "updated",
                                                                         "errors", "skipped", "cancelled")})

    # ------------------------------------------------------------ partner API
    @property
    def partner_country(self) -> str:
        """The site of the partner shop asked for prices and links (scraping and API alike): "NL" or "BE"."""
        return partner_site({CONF_PARTNER_COUNTRY: self.opt(self.entry, CONF_PARTNER_COUNTRY, "auto"),
                         CONF_LEGO_LOCALE: self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE)}).upper()

    @property
    def partner_api(self) -> PartnerApi | None:
        """The partner shop's own API when the user entered partner credentials (else: scraping)."""
        cid, secret = self.api_key(CONF_PARTNER_CLIENT_ID), self.api_key(CONF_PARTNER_CLIENT_SECRET)
        token_url, api_url = source_url("partner_api_token"), source_url("partner_api")
        if not (cid and secret and token_url and api_url):     # source_url: only with shop settings + accepted terms
            return None
        if self._partner_api is None:
            self._partner_api = PartnerApi(async_get_clientsession(self.hass), cid, secret, self.partner_country,
                                   token_url=token_url, api_url=api_url)
        return self._partner_api

    async def _partner_match(self, num: str, url: str | None = None) -> dict[str, Any] | None:
        """Find the set in the partner shop's catalog: title must pass the link check; same product id wins."""
        found = [p for p in await self.partner_api.search(f"LEGO {num}") if title_check(p["title"], num)[0] == "ok"]
        pid = re.search(r"/(\d{13,17})/?", url or "")
        for p in found:
            if pid and p.get("url") and pid.group(1) in p["url"]:
                return p
        return found[0] if found else None

    async def _discover(self, rid: str, num: str, force: bool = False, url: str | None = None) -> str | None:
        if reader_of(rid) == "partner" and self.partner_api:
            try:
                match = await self._partner_match(num)
            except PartnerApiError as err:
                self.log("error", "discover", str(err), set_number=num, retailer=rid, source="partner_api")
                return None
            if not match:
                return None
            self._partner_found[num] = match
            return match.get("url") or partner_site_url(f"{site_root(rid)}/nl/nl/s/?searchtext={match['ean']}")
        skip = set(self.store.get("rejected", {}).get(num, []))
        return await self.fetcher.discover(rid, num, force=force, url=url, skip=skip)

    async def _fetch(self, rid: str, offer: dict[str, Any], num: str, force: bool = False) -> tuple[Any, str | None]:
        if compare.is_compare_url(offer.get("url")):
            return None, T("no comparison-site price for this shop")
        if reader_of(rid) == "partner" and self.partner_api:
            try:
                ean, title, image = offer.get("ean"), offer.get("title"), None
                if not ean:
                    match = self._partner_found.pop(num, None) or await self._partner_match(num, offer.get("url"))
                    if not match:
                        return None, T("Partner API: set not found in the catalog")
                    ean, title, image = match["ean"], match["title"], match.get("image")
                    offer["ean"] = ean
                    if num in self.store["sets"] and str(ean).isdigit():
                        self.store["sets"][num].setdefault("ean", str(ean).zfill(13))   # also used by comparison sites
                price = await self.partner_api.best_price(ean)
                return Parsed(price=price, title=title, image=image, unavailable=price is None), None
            except PartnerApiError as err:
                return None, str(err)
        return await self.fetcher.fetch_offer(rid, offer["url"], force=force)

    # ------------------------------------------------------------ price-comparison sites (hidden option)
    @property
    def compare_enabled(self) -> bool:
        """Price-comparison sites as extra price sources: on for everyone unless switched off in Settings
        (the option has a new name since 0.9.19, so earlier choices start from 'on')."""
        return bool(self.opt(self.entry, CONF_COMPARE, True))

    @property
    def market_enabled(self) -> bool:
        """Market value (and expected retirement), on by default."""
        return bool(self.opt(self.entry, CONF_MARKET, True))

    @property
    def ticker(self) -> dict[str, Any]:
        """Return ticker defaults merged with the configured overrides."""
        return {**TICKER_DEFAULT, **(self.opt(self.entry, CONF_TICKER, None) or {})}

    async def ticker_data(self, lang: str, since: float | None = None) -> dict[str, Any]:
        """The ticker at the bottom of the panel, each part as configured under Settings (on/off and how many):
        the current deals (deal score at least the minimum of Deals → Settings, best first), watched sets at
        a good price at least (deal score 45) that are not a deal yet, and news. Sets left out under
        Deals → Settings never show; a discount is only shown when the price is below the RRP."""
        from .news import NewsFeed, for_language

        cfg, now, out = self.ticker, time.time(), []
        statuses = (self.data or self.compute())["statuses"]

        def item(kind: str, num: str, st: dict[str, Any]) -> dict[str, Any]:
            """One set at its current lowest price; the discount only when there is one."""
            disc = st.get("discount_rrp")
            return {"kind": kind, "ts": self.store["sets"][num].get("last_deal") or now, "set_number": num,
                    "name": self.store["sets"][num].get("name") or "", "price": st["best_price"],
                    "discount": disc if disc is not None and disc > 0 else None, "score": st.get("deal_score"),
                    "shop": RETAILERS.get(st.get("best_retailer"), ("",))[0], "url": st.get("best_url")}
        by_score = lambda n: (-(statuses[n].get("deal_score") or 0), n)  # noqa: E731
        priced = [n for n in self.store["sets"] if statuses.get(n, {}).get("best_price") is not None]
        # deals: sets whose deal score reaches the minimum of Deals → Settings (not "lowest ever" at -1 % alone)
        min_score = self.deal_rules["min_score"]
        deals = sorted((n for n in priced if (statuses[n].get("deal_score") or 0) >= min_score
                        and not self.deal_blocked(n, statuses[n])), key=by_score) if cfg["deals"] else []
        shown = set(deals) if cfg["max_deals"] else set()     # every deal, also beyond the shown ones, stays out of W
        # watchlist: watched sets at a good price at least (deal score 45, one flame) that are not a deal above
        if cfg["watch"] and cfg["max_watch"]:
            good = sorted((n for n in priced if n not in shown and self.is_watched(n) and not self.deal_blocked(n, statuses[n])
                           and (statuses[n].get("deal_score") or 0) >= TICKER_GOOD_SCORE), key=by_score)
            out += [item("price", n, statuses[n]) for n in good[: cfg["max_watch"]]]
        if cfg["deals"] and cfg["max_deals"]:
            out += [item("deal", n, statuses[n]) for n in deals[: cfg["max_deals"]]]
        news: list[dict[str, Any]] = []
        news_hash = ""
        if cfg["news"] and cfg["max_news"]:
            if not hasattr(self, "news"):
                self.news = NewsFeed(lambda: async_get_clientsession(self.hass))
            every = await self.news.get()
            # changes when a new news file is put online: news you closed shows again then
            news_hash = hashlib.sha1(json.dumps(every, sort_keys=True).encode()).hexdigest()[:16] if every else ""
            news = for_language(every, lang)[: cfg["max_news"]]
        # errors solved since the previous ticker (shown once; gone at the next ticker load)
        self._track_errors(now)
        since = now - TICKER_RELOAD if since is None else max(float(since), now - 86400)
        # oldest first, at most 10 per load; when more are waiting, the cursor stops at the last one sent,
        # so the rest come with the next load instead of being skipped
        waiting = [(ts, key) for ts, key, _opened in (self.store.get("err_track") or {}).get("solved") or []
                   if ts > since and key.partition("|")[0] in self.store["sets"]]
        fixed = []
        for ts, key in waiting[:10]:
            num, _, rid = key.partition("|")
            fixed.append({"kind": "fixed", "ts": ts, "set_number": num, "name": self.store["sets"][num].get("name") or "",
                          "shop": RETAILERS.get(rid, (rid,))[0] if rid else None})
        cursor = fixed[-1]["ts"] if len(waiting) > 10 else now
        return {"items": out, "news": news, "news_hash": news_hash, "fixed": fixed, "config": cfg, "now": now,
                "cursor": cursor, "reload": TICKER_RELOAD}

    @callback
    def market_tick(self, _now: Any = None) -> None:
        """Every minute: when it is time, the market value of the set that waited longest. All sets are
        spread over the whole day (one set every 24 h / number of sets, at least 2 minutes apart)."""
        src, now = MARKET, time.time()
        if not self.market_enabled or getattr(self, "_market_busy", False) or now < getattr(self, "_market_next", 0) \
                or self.fetcher.cooldown_left(src) > 0 or not self.store["sets"]:
            return
        st = self._cstore(src)
        def age(n: str) -> float:
            """Return seconds since the last check, or -1 while a failed lookup is deferred."""
            e = st.get(n) or {}
            if e.get("status") in ("missing", "unreadable") and now - e.get("ts", 0) < COMPARE_MISSING_HOURS * 3600:
                return -1
            return now - e.get("ts", 0)
        num = max(self.store["sets"], key=age)
        if age(num) < compare.FRESH_HOURS.get(src, 24) * 3600:
            self._market_next = now + 600                    # everything is fresh: look again in 10 minutes
            return
        self._market_next = now + max(120.0, 86400 / len(self.store["sets"]))
        self._market_busy = True

        async def run() -> None:
            """Refresh the selected market value and always release the busy flag and publish state."""
            try:
                await self._compare_one(src, num, False, False, False)
            except Exception:  # noqa: BLE001 - an extra source must never break anything
                _LOGGER.exception("market value failed for %s", num)
            finally:
                self._market_busy = False
                self.push_update()
        self.entry.async_create_background_task(self.hass, run(), f"{DOMAIN}_market_{num}")

    MARKET_LABEL = "Market value"

    def _rename_market_source(self) -> None:
        """Data stored by earlier versions uses the identifiers of then (data/previous_shop_settings.json, "legacy"):
        the sources of set data, the market value source and its stored data get their current names."""
        old = legacy()
        names = {**(old.get("sources") or {}), **(old.get("labels") or {})}
        for oid, nid in (old.get("compare_ids") or {}).items():          # stored comparison data and pauses
            comp = self.store.setdefault("compare", {})
            if oid in comp:
                comp.setdefault(nid, {}).update(comp.pop(oid))
            for part in (self.store.get("cooldowns") or {}).values():
                if isinstance(part, dict) and oid in part:
                    part[nid] = part.pop(oid)

        def new_name(v: Any) -> Any:
            if not isinstance(v, str) or not names:
                return v
            return "+".join(names.get(p, p) for p in v.split("+"))
        for rec in [*self.store["sets"].values(), *self.store["collection"].values()]:
            for k, v in list(rec.items()):
                if k.endswith("source") and (nv := new_name(v)) != v:
                    rec[k] = nv
            if isinstance(rec.get("market"), dict) and (nv := new_name(rec["market"].get("source"))) != rec["market"].get("source"):
                rec["market"]["source"] = nv
        for e in self.store.get("activity", []):
            if (nv := new_name(e.get("source"))) != e.get("source"):
                e["source"] = nv
            for ol, nl in (old.get("labels") or {}).items():           # a source's old name in the words of the log
                if ol in (e.get("message") or ""):
                    e["message"] = e["message"].replace(ol, nl)
        for e in (self.store.get("compare", {}).get(MARKET) or {}).values():
            if (nv := new_name(e.get("name"))) != e.get("name"):
                e["name"] = nv

    def _fix_lego_rrp(self) -> None:
        """Once (1.0.0): earlier versions took every price of the official shop as the RRP, so a promotion could become it.
        A promotion is always below the regular price, so the regular price is the highest price the official shop showed
        at least twice (or the only price it showed). The RRP is only ever raised to it, never lowered: a
        promotion that was checked more often than the regular price can't replace a correct RRP. Your own RRPs
        are left alone."""
        if self.store.get("rrp_fixed") == 1:
            return
        fixed = 0
        for num, s in self.store["sets"].items():
            o = (self.store["offers"].get(num) or {}).get("lego_com") or {}
            prices = [round(float(p), 2) for _, p in o.get("history") or [] if p]
            if s.get("rrp_source") != OFFICIAL or not prices:
                continue
            count: dict[float, int] = {}
            for p in prices:
                count[p] = count.get(p, 0) + 1
            seen_twice = [p for p, n in count.items() if n >= 2]
            regular = max(seen_twice) if seen_twice else max(prices) if len(count) == 1 else None
            if regular is not None and (s.get("rrp") is None or regular - float(s["rrp"]) >= 0.01):
                self.log("info", "meta", T("RRP corrected: €{old} → €{new} (the regular price of the official shop, not a promotion)",
                                           old=f"{float(s.get('rrp') or 0):.2f}", new=f"{regular:.2f}"), set_number=num, source="server")
                s["rrp"] = regular
                fixed += 1
        self.store["rrp_fixed"] = 1

    MARKETPLACE_NO_PRICE = ("price not found on the page",)

    def _marketplace_no_price_is_out_of_stock(self) -> None:
        """A marketplace page without a price means not in stock, never an error (since 0.10.0). Errors stored by
        earlier versions become that warning at every start; they leave Open errors without counting as solved."""
        from .parsers import is_marketplace

        track = (self.store.get("err_track") or {}).get("open") or {}
        moved = 0
        for num, offers in self.store["offers"].items():
            for rid, o in offers.items():
                if is_marketplace(rid) and o.get("error") in self.MARKETPLACE_NO_PRICE and not o.get("manual_price"):
                    o.update(error=None, available=False, unavailable="sold_out")
                    o.pop("ignored_error", None)
                    track.pop(f"{num}|{rid}", None)
                    moved += 1
        if moved:
            self.log("info", "shop", T("{n} marketplace pages without a price are now 'not in stock' instead of an error", n=moved), source="server")

    def _partner_links_to_site(self) -> None:
        """Your links of the partner shop point at its site chosen in Settings (/nl/nl/ or /be/nl/),
        so prices, links you open and the browser relay all use the same site."""
        if not (rid := shop_with_reader("partner")):
            return
        for offers in self.store["offers"].values():
            if (o := offers.get(rid)) and o.get("url") and (new := partner_site_url(o["url"])) != o["url"]:
                o["url"] = new

    def _fix_piece_counts(self) -> None:
        """Older imports read a piece count with a thousands separator as its first digits (5.923 became 5).
        Such a count (below 10 pieces for a set of €15 or more) is removed, so it is looked up again."""
        for num, s in self.store["sets"].items():
            p = s.get("pieces")
            if isinstance(p, (int, float)) and p < 10 and (s.get("rrp") or 0) >= 15 and s.get("pieces_source") != "user":
                s.pop("pieces")
                s.pop("pieces_source", None)
                self.log("info", "meta", T("piece count {old} removed (thousands separator read wrongly); it is looked up again", old=p),
                         set_number=num, source="server")

    def _fix_lost_commas(self) -> None:
        """Older panels used number fields in which some phone keyboards dropped the decimal comma
        (an RRP of 164,99 became 16499). Only amounts with evidence are repaired: the amount is far above
        what the shops ask (or, for a purchase price, the RRP) and the amount / 100 matches it. Anything
        else, e.g. a real €2500, stays as it is."""
        def fits(v: float, ref: float | None) -> bool:
            """Check whether dividing an outlying amount by 100 brings it near the reference."""
            return bool(ref) and not 0.3 <= v / ref <= 3 and 0.4 <= (v / 100) / ref <= 2.5

        def shop_median(num: str) -> float | None:
            """Return the upper median of available automatic shop prices, or None."""
            prices = sorted(o["last_price"] for o in (self.store["offers"].get(num) or {}).values()
                            if o.get("available") and o.get("last_price") and not o.get("manual_price"))
            return prices[len(prices) // 2] if prices else None

        def fix(num: str, rec: dict[str, Any], key: str, ref: float | None) -> None:
            """Correct and log an amount above 1000 when the reference supports a lost comma."""
            v = rec.get(key)
            if isinstance(v, (int, float)) and v > 1000 and fits(v, ref):
                rec[key] = round(v / 100, 2)
                self.log("info", "meta", T("{field} {old} corrected to {new} (decimal comma lost)", field=key, old=f"{v:g}", new=f"{v / 100:.2f}"),
                         set_number=num, source="server")

        for num, st in self.store["sets"].items():
            ref = shop_median(num)
            for key in ("rrp", "target_price"):
                fix(num, st, key, ref)
        for num, e in self.store["collection"].items():
            st = self.store["sets"].get(num, {})
            ref = st.get("rrp") if st.get("rrp") and st.get("rrp") < 1000 else shop_median(num)
            for key in ("paid", "current_value"):
                fix(num, e, key, ref * max(1, int(e.get("qty") or 1)) if ref and key == "paid" else ref)

    def _drop_old_source_links(self) -> None:
        """A comparison source removed in 0.9.10: its prices and history stay, but no link to it remains
        (shop links that point to comparison pages of it are removed; real shop links stay)."""
        removed = legacy().get("removed_source") or {}
        key, hosts = removed.get("key"), tuple(removed.get("hosts") or ())
        if not key:
            return
        old = self.store.pop(key, None)                  # 0.9.4 layout
        bw = self.store.setdefault("compare", {}).setdefault(key, old or {}) if old else \
            (self.store.get("compare") or {}).get(key)
        gone = lambda url: bool(url) and any(h in url for h in hosts)   # noqa: E731
        for entry in (bw or {}).values():
            if gone(entry.get("url")):
                entry.pop("url", None)
            for shop in entry.get("shops", []):
                if gone(shop.get("url")):
                    shop.pop("url", None)
        for offers in self.store["offers"].values():
            for o in offers.values():
                if gone(o.get("url")):                            # price history stays, the link goes
                    o.pop("url", None)
                    o.pop("link_status", None)

    @property
    def compare_sources(self) -> list[str]:
        sel = self.opt(self.entry, CONF_COMPARE_SOURCES, None)
        return [src for src in compare.SOURCES if sel is None or src in sel]

    def _cstore(self, src: str) -> dict[str, Any]:
        return self.store.setdefault("compare", {}).setdefault(src, {})

    def compare_entries(self, num: str) -> dict[str, dict[str, Any]]:
        return {src: e for src in compare.SOURCES if (e := self._cstore(src).get(num))}

    def _net_error(self, src: str, error: str) -> None:
        """Network errors (e.g. 'SSL_connect: connection closed abruptly'): after 5 in a row the site
        is paused for an hour, so a site that drops us is not hammered."""
        n = self._net_errors.get(src, 0) + 1
        self._net_errors[src] = n
        if n >= COMPARE_NET_ERRORS:
            self._net_errors[src] = 0
            self.fetcher.blocked_until[src] = time.time() + COMPARE_PAUSE_HOURS * 3600
            self.log("error", "shop", T("{source}: {n} network errors in a row, paused for 1 hour ({error})",
                                        source=compare.SOURCES[src][0], n=n, error=error[:100]), source=compare.SOURCES[src][0])

    async def compare_refresh(self, num: str, refresh: bool = False, force: bool = False, retry_missing: bool = False,
                              sources: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """All enabled comparison sites for one set. Pages are re-used for a few hours, a site that doesn't
        have the set is not asked again within a day, a paused site is skipped (unless forced from the panel)."""
        out: dict[str, dict[str, Any]] = {}
        for src in (sources or self.compare_sources) if ready() else []:
            if src == MARKET and not self.market_enabled:      # the market value is switched off
                continue
            try:
                if (entry := await self._compare_one(src, num, refresh or force, force, retry_missing)):
                    out[src] = entry
            except Exception:  # noqa: BLE001 - an extra source must never break anything
                _LOGGER.exception("%s failed for %s", src, num)
        self._compare_links(num)
        return out

    async def _compare_one(self, src: str, num: str, refresh: bool, force: bool, retry_missing: bool,
                           steps: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        st, now = self._cstore(src), time.time()
        entry = st.get(num)
        if entry and entry.get("status") in ("missing", "unreadable") and now - entry["ts"] < COMPARE_MISSING_HOURS * 3600 and not retry_missing:
            return None
        if entry and entry.get("status") == "ok" and now - entry["ts"] < compare.FRESH_HOURS.get(src, COMPARE_FRESH_HOURS) * 3600 \
                and not refresh:
            return entry
        if not force and self.fetcher.cooldown_left(src) > 0:
            return entry if entry and entry.get("status") == "ok" else None
        s = self.store["sets"].get(num) or {}
        url = compare.first_url(src, num, self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE), s.get("ean"))
        if not url:
            return None                                      # site doesn't cover this country / needs an EAN
        step, kind = 0, "error"
        while url:
            status, page, error = await self.fetcher.get_page(src, url, force=True, note_block=step == 0)
            if steps is not None:
                steps.append({"url": url, "status": status, "error": error, "size": len(page or "")})
            kind, url = self.compare_page(src, num, url, status, page, error, step)
            step += 1
            if kind != "follow":
                break
        return self._cstore(src).get(num) if kind == "ok" else None

    def compare_page(self, src: str, num: str, url: str, status: int, page: str, error: str | None, step: int = 0,
                     via: str = "server") -> tuple[str, str | None]:
        """Handle one fetched page of a comparison site (by the server or by the user's browser).
        Returns ('ok' | 'missing' | 'error' | 'follow', next url)."""
        name, now = compare.SOURCES[src][0], time.time()
        st = self._cstore(src)
        self._compare_debug[src] = {"url": url, "status": status, "error": error, "set": num, "ts": now, "via": via,
                                    "html": (page or "")[:1_500_000]}

        fb = self._compare_fallback.pop((src, num), None) if step else None

        def fail(msg: str) -> tuple[str, None]:
            if fb is not None:                               # the product page failed: the search result's prices
                if status == 403:
                    self._no_follow[src] = now + 24 * 3600
                    self.log("info", "fetch", T("{source} blocks its product pages: prices from the search results for a day",
                                                source=name), set_number=num, url=url, source=name)
                return self._compare_store(src, num, *fb, via, now)
            old = st.get(num)
            if old and old.get("status") == "ok":            # keep the last good prices
                old.update(last_error=msg, err_ts=now)
            else:
                st[num] = {"status": "error", "ts": now, "url": url, "error": msg}
            self.log("warning", "fetch", msg, set_number=num, url=url, source=name)
            return "error", None

        if status == 0:                                      # network error: count towards the 1 h pause
            self._net_error(src, error or "?")
            return fail(error or T("network error: {error}", error="?"))
        self._net_errors[src] = 0
        if status in (403, 429, 503):
            return fail(error or T("blocked (HTTP {status})", status=status))
        if status in (404, 410):
            res = compare.Result("missing")
        elif status >= 400:
            return fail(T("HTTP error {status}", status=status))
        else:
            no_follow = self._no_follow.get(src, 0) > now
            res = compare.parse(src, page, num, url, all_domains(), compare.MAX_STEPS - 1 if no_follow else step)
        if res.kind == "follow" and res.url and compare.is_compare_url(res.url) and step < compare.MAX_STEPS - 1:
            if res.shops:
                self._compare_fallback[(src, num)] = (compare.Result("offers", shops=res.shops), url)
            return "follow", res.url
        if res.kind not in ("offers", "data") and fb is not None:
            return self._compare_store(src, num, *fb, via, now)
        if res.kind not in ("offers", "data") and res.note == "js":
            msg = T("{source} loads its results with JavaScript: this page has no results to read", source=name)
            st[num] = {"status": "unreadable", "ts": now, "url": url, "error": msg}
            self.log("warning", "fetch", msg, set_number=num, url=url, source=name)
            return "missing", None
        if res.kind not in ("offers", "data"):
            st[num] = {"status": "missing", "ts": now, "url": url, "note": res.note}
            self.log("info", "fetch", T("not on {source}: next try tomorrow", source=name), set_number=num, url=url, source=name)
            return "missing", None
        return self._compare_store(src, num, res, url, via, now)

    def _compare_store(self, src: str, num: str, res: Any, url: str, via: str, now: float) -> tuple[str, None]:
        name = compare.SOURCES[src][0]
        self._cstore(src)[num] = {"status": "ok", "ts": now, "url": url, "name": res.name, "image": res.image, "rrp": res.rrp,
                                  "ean": res.ean, "shops": res.shops, "via": via, **({"data": res.data} if res.data else {})}
        s = self.store["sets"].get(num)
        if s is not None and res.data:
            self._apply_market(num, s, res.data, name, now)
        if s is not None:
            if res.name and not s.get("name") and not compare.is_accessory(res.name):
                s["name"], s["name_source"] = res.name[:120], name
            if res.image and not s.get("image"):
                s["image"], s["image_source"] = res.image, name
            if res.rrp and not s.get("rrp"):
                s["rrp"], s["rrp_source"] = res.rrp, name
            if res.ean and not s.get("ean") and len(res.ean) == 13:
                s["ean"] = res.ean
        return "ok", None

    def _apply_market(self, num: str, s: dict[str, Any], d: dict[str, Any], name: str, now: float) -> None:
        """Market value: set data where empty, the retirement (forecast) date unless you set one yourself,
        and the market value of owned sets (new, or used for opened / built sets) as their value."""
        for key in ("theme", "subtheme", "year"):
            if d.get(key) and not s.get(key):
                s[key] = d[key]
        if d.get("pieces"):
            set_pieces(s, d["pieces"], "market", setdb_pieces=self._setdb_pieces(num))
        s["market"] = {k: d.get(k) for k in ("market_new", "market_used", "availability", "retired", "retirement",
                                               "forecast_1y", "forecast_5y")} | {"ts": now, "source": name}
        when = compare.forecast_date(d.get("retired") or d.get("retirement"))
        if when and (not s.get("exit_date") or s.get("exit_date_source") == name):
            s["exit_date"], s["exit_date_source"] = when, name
        entry = self.store["collection"].get(num)
        if entry is not None:
            # the new value; opened, built or incomplete copies take the used value themselves (models.copy_value).
            # The used value only stands in when no copy is sealed.
            opened = all(c.get("condition") in OPENED for c in copies(entry))
            value = d.get("market_new") or (d.get("market_used") if opened else None)
            if value:
                hist = list(entry.get("value_history") or [])
                if not hist or abs(hist[-1][1] - value) > 0.005:
                    hist.append([now, value])
                entry["value_history"], entry["current_value"], entry["value_source"] = hist[-500:], value, name

    def _compare_links(self, num: str) -> None:
        """Tracked shops without a link get one from a comparison site (the shop's own page when the site
        links to it directly, otherwise the comparison page itself)."""
        if num not in self.store["sets"]:
            return
        offers = self.store["offers"].setdefault(num, {})
        rejected = set(self.store.setdefault("rejected", {}).get(num, []))
        name = self.store["sets"][num].get("name") or ""
        for src, entry in self.compare_entries(num).items():
            if entry.get("status") != "ok":
                continue
            for shop in entry.get("shops", []):
                rid = shop.get("retailer")
                if rid not in self.retailers or (offers.get(rid) or {}).get("url"):
                    continue
                direct = shop.get("url") if shop.get("url") and retailer_from_url(shop["url"]) == rid else None
                link = direct or entry["url"]
                if url_key(rid, link) in rejected:
                    continue
                offers[rid] = {"url": link, "history": [], "found": time.time(), "via": src,
                               "title": f"LEGO {num} {name}".strip()}
                self.log("ok", "discover", T("link found via {source}", source=compare.SOURCES[src][0]), set_number=num,
                         retailer=rid, url=link, source=compare.SOURCES[src][0])

    def compare_prices(self, num: str) -> dict[str, dict[str, Any]]:
        """Per tracked retailer the best comparison-site price: the most recent one (per 6 h), then the lowest."""
        best: dict[str, tuple[tuple[float, float], dict[str, Any]]] = {}
        now = time.time()
        for src, entry in self.compare_entries(num).items():
            if src not in self.compare_sources or entry.get("status") != "ok" or now - entry["ts"] > 26 * 3600:
                continue
            for sh in entry.get("shops", []):
                if not (rid := sh.get("retailer")):
                    continue
                key = (-(entry["ts"] // (COMPARE_FRESH_HOURS * 3600)), sh["price"])
                if rid not in best or key < best[rid][0]:
                    best[rid] = (key, {**sh, "source": src, "ts": entry["ts"]})
        return {rid: v for rid, (_, v) in best.items()}

    def _compare_apply_prices(self, num: str) -> int:
        """Comparison prices for shops whose own page failed, is stale, or that only have a comparison link."""
        n = 0
        for rid, shop in self.compare_prices(num).items():
            o = self.store["offers"].get(num, {}).get(rid)
            if o and not o.get("manual_price") and (o.get("error") or not o.get("last_ok") or time.time() - o["last_ok"] > 20 * 3600
                                                     or compare.is_compare_url(o.get("url"))):
                others = [x["last_price"] for r, x in self.store["offers"].get(num, {}).items()
                          if r != rid and x.get("available") and x.get("last_price")]
                if is_suspicious_price(shop["price"], self.store["sets"][num], o, others):
                    continue
                record_price(o, shop["price"])
                o["last_ok"], o["last_via"] = o["last_checked"], shop["source"]   # shown as ⓒ next to the price
                n += 1
        return n

    def start_compare(self, nums: list[str] | None = None) -> dict[str, Any]:
        """Job: every comparison site for every set. When all sites are paused (e.g. 5 network errors in a row)
        the job stops and continues with the remaining sets an hour later."""
        items = [n for n in (nums or list(self.store["sets"])) if n in self.store["sets"]]

        async def work(num: str) -> dict[str, int]:
            live = [src for src in self.compare_sources if self.fetcher.cooldown_left(src) <= 0]
            if not live:
                if not self._cancel:
                    self._cancel = True
                    self._compare_retry(items[items.index(num):])
                return {"skipped": 1}
            got = await self.compare_refresh(num, refresh=True, sources=live)
            return {"updated": self._compare_apply_prices(num)} if got else {"skipped": 1}
        return self.start_job("compare", T("Fetching prices from comparison sites"), items, work)

    def _compare_retry(self, rest: list[str]) -> None:
        from homeassistant.helpers.event import async_call_later

        wait = max([self.fetcher.cooldown_left(src) for src in self.compare_sources] + [60.0])
        self.log("warning", "job", T("All comparison sites are paused: stopped, the other {n} sets follow in {minutes} min",
                                     n=len(rest), minutes=round(wait / 60)))
        if self._compare_retry_unsub:
            self._compare_retry_unsub()

        def _go(_now: Any) -> None:
            self._compare_retry_unsub = None
            if not self.compare_enabled:
                return
            if self.job_running:
                self._compare_retry_unsub = async_call_later(self.hass, 600, _go)
                return
            self._job_source = "auto"
            self.start_compare(rest)
        self._compare_retry_unsub = async_call_later(self.hass, wait + 30, _go)

    def _live_retailers(self, force: bool) -> list[str]:
        if force:
            self.fetcher.reset_cooldowns()
        if not ready():          # no shop settings imported or terms not accepted: no shop is contacted
            return []
        return [r for r in self.retailers if self.fetcher.cooldown_left(r) <= 0]

    # ------------------------------------------------------------------- refresh
    def start_full_refresh(self, force: bool = False) -> dict[str, Any]:
        """'Refresh all prices' by hand or by service: off unless enabled in developer mode (a full round
        puts a lot of load on the shops), and then at most once a minute."""
        if not self.dev(CONF_DEV_FULL_REFRESH):
            raise LocalizedError("Switched off: fetching every set at once puts too much load on the shops. Prices are checked set by set in the background; use ↻ in a set to fetch one set now.")
        wait = FULL_REFRESH_GAP - (time.time() - getattr(self, "_last_full_refresh", 0))
        if wait > 0:
            raise LocalizedError("A full price round can start at most once a minute: try again in {s} s.", s=int(wait) + 1)
        job = self.start_refresh(force)
        self._last_full_refresh = time.time()
        return job

    def start_refresh(self, force: bool = False) -> dict[str, Any]:
        """Start a background job that fetches the shop prices of every set with a live link (or all sets when
        comparison sites are on); `force` also includes paused shops and resting pages of the official shop."""
        live = self._live_retailers(force)
        nums = list(self.store["sets"]) if self.compare_enabled else [
            n for n, offers in self.store["offers"].items()
            if n in self.store["sets"] and any(r in live and o.get("url") for r, o in offers.items())]
        paused = [RETAILERS[r][0] for r in self.retailers if r not in live]
        note = T("paused and skipped: {shops}", shops=", ".join(paused)) if paused else None
        return self.start_job("refresh", T("Refreshing shop prices"), nums,
                              lambda n: self.refresh_set(n, live, wake=force), note)

    async def refresh_set(self, num: str, retailers: list[str] | None = None, source: str = "server",
                          force: bool = False, wake: bool = False) -> dict[str, int]:
        """Fetch all shop pages of one set, shops in parallel (each shop stays sequential and polite).

        Writes one 'check' entry to the logbook with a green/red result per shop, plus separate
        'price' entries for changes. A manual price always wins over the automatic one.
        `wake` (a forced round) also fetches a page of the official shop that rests because the set is out of the range."""
        live = retailers if retailers is not None else self._live_retailers(False)
        s = self.store["sets"][num]
        bw_prices: dict[str, dict[str, Any]] = {}
        if self.compare_enabled:
            try:
                await self.compare_refresh(num, force=force)
                bw_prices = self.compare_prices(num)
            except Exception:  # noqa: BLE001 - an extra source must never break the check
                _LOGGER.exception("Comparison sites failed for %s", num)
        before = self.compute()["statuses"].get(num, {})
        offers = [(rid, o) for rid, o in self.store["offers"].get(num, {}).items()
                  if (rid in live or (rid in bw_prices and rid in self.retailers)) and o.get("url") and o.get("link_status") != "rejected"
                  and (not self._lego_retired_rest(rid, o) or force or wake)]
        results = await asyncio.gather(*(self._fetch(rid, o, num, force) for rid, o in offers))
        counts = {"updated": 0, "errors": 0, "skipped": 0}
        shop_results: dict[str, dict[str, Any]] = {}
        # every shop's price in this round (and the last known ones): the yardstick for a set without RRP
        round_prices = {rid: p for (rid, _o), (pr, _e) in zip(offers, results) if pr and (p := pr.price)}
        known = {rid: o.get("last_price") for rid, o in self.store["offers"].get(num, {}).items()
                 if o.get("available") and o.get("last_price")}
        for (rid, offer), (parsed, error) in zip(offers, results):
            via = None
            # the official shop says "temporarily unavailable": no new LEGO price, the one we had stays
            held = self._lego_hold(num, offer, parsed) if rid == "lego_com" and not error else None
            retired = rid == "lego_com" and parsed is not None and parsed.unavailable and parsed.reason == "discontinued"
            if held is None and not retired and (bwp := bw_prices.get(rid)) and (error or not parsed or parsed.price is None):
                parsed, error, via = Parsed(price=bwp["price"], title=offer.get("title")), None, bwp["source"]
            if error and error.startswith("paused"):
                counts["skipped"] += 1        # keep the last known price, just skip
                shop_results[rid] = {"ok": None, "error": error}
                continue
            if parsed and parsed.title:
                offer["title"] = parsed.title[:300]
            status, reason = link_check(offer, s, num)
            offer["link_status"], offer["link_reason"] = status, reason
            if status == "suspect" and (why_wrong := self._auto_reject(num, rid, offer, source)):
                shop_results[rid] = {"ok": None, "error": why_wrong}
                continue
            price = held[0] if held else parsed.price if parsed else None
            others = [p for r, p in {**known, **round_prices}.items() if r != rid]
            if price is not None and (warn := is_suspicious_price(price, s, offer, others)):
                offer["suspect"] = {"price": price, "reason": warn, "ts": time.time()}   # kept: you can approve it
                price, error = None, warn
            elif price is not None:
                offer.pop("suspect", None)
            manual = offer.get("manual_price")
            if manual:                        # manual price has priority: only remember what the shop said
                offer["auto_price"], offer["last_checked"], offer["error"] = price, time.time(), error
                self._price_choice(num, rid, offer, price, source)
            else:
                old_price = offer.get("last_price") if offer.get("available") else None
                record_price(offer, price, error=error)
                if price is not None:
                    if via:
                        offer["last_via"] = via
                    else:
                        offer.pop("last_via", None)
                if price is not None and (old_price is None or abs(old_price - price) >= 0.01):
                    self.log("ok", "price", T("€{old} → €{new}", old=f"{old_price:.2f}", new=f"{price:.2f}") if old_price
                             else T("first price €{price}", price=f"{price:.2f}"),
                             set_number=num, retailer=rid, url=offer.get("url"), price=price, old_price=old_price, source=source)
            shop_results[rid] = {"ok": not error, "price": price, "error": error, "manual": bool(manual)}
            if via:
                shop_results[rid]["via"] = via
            # the page says it is sold out / no longer sold: a warning with the reason, not an error to solve
            why = parsed.reason if parsed and parsed.unavailable and (price is None or held) and not error else None
            if why:
                shop_results[rid]["unavailable"] = why
                if offer.get("unavailable") != why:
                    if held:
                        msg = T("temporarily unavailable: last price €{price} kept", price=f"{held[0]:.2f}") if held[1] == "kept" \
                            else T("temporarily unavailable: regular price €{price} used", price=f"{held[0]:.2f}")
                    else:
                        msg = T("no price: sold out") if why == "sold_out" else T("no price: no longer sold (out of the range)")
                    self.log("warning", "price", msg, set_number=num, retailer=rid, url=offer.get("url"), source=source, unavailable=why)
                self._set_unavailable(offer, why)
            elif price is not None:
                self._set_unavailable(offer, None)
            elif error and offer.get("unavailable") != "discontinued":
                offer.pop("unavailable", None)             # out of the range stays: a failed fetch confirms nothing
            if self.job and self.job.get("running"):
                st = self.job["shops"].setdefault(rid, {"ok": 0, "err": 0})
                st["err" if error else "ok"] += 1
            if status == "suspect" and offer.get("_logged_suspect") != reason:
                offer["_logged_suspect"] = reason
                self.log("warning", "link", T("suspicious link: {reason}", reason=reason), set_number=num, retailer=rid,
                         url=offer.get("url"), source=source)
            if price is not None:
                offer["last_ok"] = time.time()
                counts["updated"] += 1
            if error:
                counts["errors"] += 1
                _LOGGER.debug("%s/%s: %s", num, rid, error)
            if parsed and rid == "lego_com":
                self._apply_lego(num, parsed)
            elif parsed and status in ("ok", "confirmed"):
                if parsed.image and not s.get("image"):
                    s["image"], s["image_source"] = parsed.image, "shop"
                if parsed.title and self._name_replaceable(s):
                    s["name"], s["name_source"] = clean_title(parsed.title, num), "shop"
        s["checked"] = time.time()
        if shop_results:
            ok = sum(1 for r in shop_results.values() if r["ok"])
            failed = sum(1 for r in shop_results.values() if r["ok"] is False)
            level = "ok" if not failed else "error" if not ok else "warning"
            self.log(level, "check", T("{ok} of {n} shops OK", ok=ok, n=len(shop_results)), set_number=num,
                     results=shop_results, source=source)
        after = self.compute()["statuses"].get(num, {})
        self._fire_events(num, before, after)
        return counts

    async def fetch_shop(self, set_number: str, retailer: str, url: str | None = None) -> dict[str, Any]:
        """Panel button per shop: fetch this shop for this set now, also when the shop is paused.
        Without a link the shop is searched first. The official shop also fills in image, RRP and name.
        url: what you pasted — a product page (becomes the link) or a search page of the shop (searched)."""
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise LocalizedError("Set {number} is not tracked.", number=num)
        if retailer not in RETAILERS:
            raise LocalizedError("Unknown shop {shop}.", shop=retailer)
        self.manual_gate("site:" + self._site(retailer))
        offers = self.store["offers"].setdefault(num, {})
        found = False
        search_page = None
        if url and (url := url.strip()):
            if is_search_url(url):
                search_page = url if url.startswith("http") else None
            else:
                # a product page or ASIN is the link; the same page keeps its history, a manual price stays
                self.update_offer(num, retailer, url=url)
        if search_page or not (offers.get(retailer) or {}).get("url"):
            url = await self._discover(retailer, num, force=True, url=search_page) \
                or (None if search_page else await self._fallback_link(retailer, num))
            rejected = set(self.store.setdefault("rejected", {}).get(num, []))
            if not url or url_key(retailer, url) in rejected:
                reason = T("found a link you rejected earlier; not linked again") if url else \
                    (self.fetcher.discover_error.get(retailer) or T("no matching product found"))
                self.log("warning", "discover", reason, set_number=num, retailer=retailer, url=url, source="panel")
                self.push_update()
                return {"ok": False, "found": False, "error": reason}
            self._set_discovered(offers, retailer, url)
            if reader_of(retailer) == "partner" and num in self._partner_found:
                offers[retailer]["ean"] = self._partner_found[num]["ean"]
            found = True
            self.log("ok", "discover", T("link found"), set_number=num, retailer=retailer, url=url, source="panel")
        await self.refresh_set(num, [retailer], source="panel", force=True)
        o = offers[retailer]
        self._snapshot()
        self._save()
        self.push_update()
        return {"ok": not o.get("error"), "found": found, "price": offer_price(o), "error": o.get("error")}

    async def refresh_all(self) -> None:
        """Synchronous full round (used by tests); the UI uses start_refresh()."""
        live = self._live_retailers(False)
        for num in list(self.store["offers"]):
            if num in self.store["sets"]:
                await self.refresh_set(num, live)
        self._snapshot()
        self._save()

    # ------------------------------------------------------------------ discover
    def _missing(self, num: str, live: list[str]) -> list[str]:
        offers = self.store["offers"].get(num, {})
        return [r for r in live if r not in offers or not offers[r].get("url")]

    def start_discover(self, force: bool = False) -> dict[str, Any]:
        live = self._live_retailers(force)
        nums = [n for n in self.store["sets"] if self._missing(n, live)]
        paused = [RETAILERS[r][0] for r in self.retailers if r not in live]
        note = T("spread out: each shop is searched at most once every 2 minutes")
        if paused:
            note += "; " + T("paused and skipped: {shops}", shops=", ".join(paused))
        return self.start_job("discover", T("Finding missing shop links"), nums, lambda n: self.discover_set(n, live), note)

    async def discover_set(self, num: str, retailers: list[str] | None = None) -> dict[str, int]:
        live = retailers if retailers is not None else self._live_retailers(False)
        todo = self._missing(num, live)
        urls = list(await asyncio.gather(*(self._discover(r, num) for r in todo)))
        for i, rid in enumerate(todo):          # nothing via the search: the shop's sitemap, then (in a job) the EAN
            if not urls[i]:
                urls[i] = await self._fallback_link(rid, num, slow=bool(self.job and self.job.get("running")))
        rejected = set(self.store.setdefault("rejected", {}).get(num, []))
        found = 0
        for rid, url in zip(todo, urls):
            if url and url_key(rid, url) not in rejected:
                self.store["offers"].setdefault(num, {})[rid] = {"url": url, "history": [], "found": time.time()}
                if reader_of(rid) == "partner" and num in self._partner_found:
                    self.store["offers"][num][rid]["ean"] = self._partner_found[num]["ean"]
                found += 1
                self.log("ok", "discover", T("link found"), set_number=num, retailer=rid, url=url)
            elif url:
                self.log("info", "discover", T("found a link you rejected earlier; not linked again"), set_number=num, retailer=rid, url=url)
            elif self.job and self.job.get("running"):
                st = self.job["shops"].setdefault(rid, {"ok": 0, "err": 0})
                st["err"] += 1                  # summarised per shop at the end of the job, with the reasons
                why = st.setdefault("why", {})
                reason = self.fetcher.discover_error.get(rid) or T("no matching product found")
                why[reason] = why.get(reason, 0) + 1
            else:
                self.log("info", "discover", T("no matching product found"), set_number=num, retailer=rid)
            if url and self.job and self.job.get("running"):
                self.job["shops"].setdefault(rid, {"ok": 0, "err": 0})["ok"] += 1
        return {"found": found}

    # ------------------------------------------------------------ browser relay
    @property
    def relay_enabled(self) -> bool:
        return bool(self.opt(self.entry, CONF_RELAY, True))

    def relay_items(self, limit: int = 40) -> dict[str, Any]:
        """Shop pages the user's own browser should fetch (userscript relay): links that failed on the
        server or weren't fetched in the last 20 h, oldest first. The partner shop via its API is left out."""
        items: list[tuple[float, dict[str, Any]]] = []
        if self.relay_enabled and ready():          # no shop settings or terms: the browser fetches nothing either
            day = time.time() - 20 * 3600
            for num, offers in self.store["offers"].items():
                if num not in self.store["sets"]:
                    continue
                for rid, o in offers.items():
                    if (rid not in self.retailers or rid == "lego_com" or not o.get("url") or o.get("manual_price")
                            or o.get("link_status") == "rejected" or (reader_of(rid) == "partner" and self.partner_api)):
                        continue
                    last = o.get("last_ok") or 0
                    if o.get("error") or last < day:
                        items.append((last, {"set_number": num, "retailer": rid, "shop": RETAILERS[rid][0], "url": o["url"]}))
            if self.compare_enabled:
                # comparison sites the server can't reach (paused / errors): the browser fetches the page,
                # the server reads it with the same parser
                locale = self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE)
                for src in self.compare_sources:
                    paused = self.fetcher.cooldown_left(src) > 0
                    for num, s in self.store["sets"].items():
                        e = self._cstore(src).get(num) or {}
                        fresh = e.get("status") == "ok" and time.time() - e["ts"] < 20 * 3600 and not e.get("last_error")
                        missing = e.get("status") in ("missing", "unreadable") and time.time() - e["ts"] < COMPARE_MISSING_HOURS * 3600
                        if fresh or missing or not (paused or e.get("status") == "error" or e.get("last_error")):
                            continue
                        if (url := compare.first_url(src, num, locale, s.get("ean"))):
                            items.append((e.get("ts", 0), {"kind": "page", "source": src, "set_number": num,
                                                           "shop": compare.SOURCES[src][0], "url": url, "step": 0}))
        items.sort(key=lambda x: x[0])
        return {"enabled": self.relay_enabled, "interval_hours": int(self.opt(self.entry, CONF_RELAY_HOURS, DEFAULT_RELAY_HOURS)),
                "items": [i for _, i in items[:limit]], "total": len(items)}

    # ------------------------------------------------------------ product links from shop sitemaps
    SITEMAP_EXCLUDE = {"lego", "marketplace", "partner"}   # readers of shops with huge sitemaps / a search that works

    def _note_js(self, rid: str) -> None:
        """Remember shops whose search page is built with JavaScript (the userscript can render it in a tab)."""
        if "JavaScript" in (self.fetcher.discover_error.get(rid) or ""):
            first = rid not in self.store.setdefault("shop_js", {})
            self.store["shop_js"][rid] = time.time()
            if first:                            # earlier browser searches read an empty page: allow them again
                searched = self.store.setdefault("relay_searched", {})
                for k in [k for k in searched if k.endswith("|" + rid)]:
                    del searched[k]

    async def _fallback_link(self, rid: str, num: str, slow: bool = False) -> str | None:
        """Other ways to the product page when the shop's search found nothing: its sitemap (no request), and
        a search for the EAN (barcode), which shops often send straight to the product page."""
        self._note_js(rid)
        if (url := self.sitemap_link(rid, num)):
            self.log("ok", "discover", T("link found in the shop's sitemap"), set_number=num, retailer=rid, url=url)
            return url
        ean = str(self.store["sets"].get(num, {}).get("ean") or "")
        tpl = SEARCH.get(rid)
        if slow and ean.isdigit() and tpl and reader_of(rid) not in ("lego", "partner"):
            url = await self._discover(rid, num, url=tpl.replace("{query}", ean).replace("{number}", ean))
            if url:
                self.log("ok", "discover", T("link found by searching the EAN {ean}", ean=ean), set_number=num, retailer=rid, url=url)
            return url
        return None

    def sitemap_shops(self) -> list[str]:
        return [r for r in self.retailers if reader_of(r) not in self.SITEMAP_EXCLUDE and domain_of(r)]

    def start_sitemap(self, rid: str) -> None:
        """Shops → a shop → 'Read the sitemap now' (once every 2 minutes per shop, like any manual action)."""
        if rid not in self.sitemap_shops():
            raise LocalizedError("This shop has no sitemap to read (or is switched off).")
        if getattr(self, "_sitemap_busy", False):
            raise LocalizedError("A sitemap is being read already; try again in a few minutes.")
        self.manual_gate("site:" + self._site(rid))
        self._sitemap_busy = True
        self.entry.async_create_background_task(self.hass, self._sitemap_round([rid]), f"{DOMAIN}_sitemap_{rid}")

    @callback
    def sitemap_tick(self, _now: Any = None) -> None:
        """Every few hours: shops whose sitemap is older than a week are read again (in the background)."""
        if getattr(self, "_sitemap_busy", False):
            return
        maps = self.store.setdefault("sitemaps", {})
        due = [r for r in self.sitemap_shops() if time.time() - (maps.get(r) or {}).get("ts", 0) > sitemaps.REFRESH_DAYS * 86400]
        if due:
            self._sitemap_busy = True
            self.entry.async_create_background_task(self.hass, self._sitemap_round(due), f"{DOMAIN}_sitemaps")

    async def _sitemap_round(self, rids: list[str]) -> None:
        try:
            for rid in rids if ready() else []:
                await self.refresh_sitemap(rid)
        finally:
            self._sitemap_busy = False
            self._save()
            self.push_update()

    async def refresh_sitemap(self, rid: str) -> dict[str, Any]:
        """Read the shop's sitemap (robots.txt → index → product files), keep its LEGO product URLs and link
        every set that has no link at this shop yet."""
        domain = (domain_of(rid) or "").removeprefix("www.")
        base = f"https://www.{domain}"
        status, body, err = await self.fetcher.get_raw(rid, base + "/robots.txt")
        queue = sitemaps.robots_sitemaps(body[:200_000].decode("utf-8", errors="replace")) if status and status < 400 else []
        queue = [u for u in queue if sitemaps.on_site(u, domain)] or [base + "/sitemap.xml"]   # only the shop's own site
        seen, urls, files, errors = set(), [], 0, []
        while queue and files < sitemaps.MAX_FILES and len(urls) < sitemaps.MAX_URLS:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            files += 1
            status, body, err = await self.fetcher.get_raw(rid, url)
            if err or not body:
                errors.append(url + ": " + (err or T("empty response")))
                if err and err.startswith("paused"):
                    break
                continue
            # unpacking and reading a large file happens outside the event loop
            children, found_urls = await self.hass.async_add_executor_job(
                sitemaps.read_file, body, domain, sitemaps.MAX_URLS - len(urls))
            queue = sitemaps.order_children(children) + queue
            urls += found_urls
        urls = list(dict.fromkeys(urls))[:sitemaps.MAX_URLS]
        self.store.setdefault("sitemaps", {})[rid] = {"ts": time.time(), "files": files, "urls": urls,
                                                      "error": errors[-1] if errors and not urls else None}
        found = self.sitemap_link_all(rid)
        self.log("ok" if urls else "warning", "discover",
                 T("sitemap read: {n} LEGO product pages, {found} new links", n=len(urls), found=found) if urls
                 else T("sitemap: no LEGO product pages found ({error})", error=(errors[-1] if errors else T("no sitemap"))[:160]),
                 retailer=rid, source="server")
        return {"urls": len(urls), "files": files, "found": found}

    def sitemap_link(self, rid: str, num: str) -> str | None:
        urls = (self.store.get("sitemaps", {}).get(rid) or {}).get("urls") or []
        url = sitemaps.match(urls, num) if urls else None
        if url and url_key(rid, url) in set(self.store.setdefault("rejected", {}).get(num, [])):
            return None
        return url

    def sitemap_link_all(self, rid: str) -> int:
        """Link every set without a link at this shop to its product page in the sitemap (no requests)."""
        found = 0
        for num in self.store["sets"]:
            offers = self.store["offers"].setdefault(num, {})
            if (offers.get(rid) or {}).get("url"):
                continue
            if url := self.sitemap_link(rid, num):
                offers[rid] = {"url": url, "history": [], "found": time.time(), "found_via": "sitemap"}
                self.log("ok", "discover", T("link found in the shop's sitemap"), set_number=num, retailer=rid, url=url, source="server")
                found += 1
        return found

    # ------------------------------------------------------------ continuous check (userscript)
    RELAY_SEARCH_DAYS = 7          # a set/shop searched by the browser without result: not again for a week
    RELAY_FAIL_HOURS = 6           # a link the server can't fetch: the browser checks it at most every 6 h

    def continuous_items(self, limit: int = 20) -> dict[str, Any]:
        """Work for the userscript's continuous check, most useful first:
        1. links that never had a price; 2. open errors (shown under Shops & jobs): the browser fetches the page,
        a price solves the error; 3. sets without any price: search the shops that have no link yet;
        4. links the server can't fetch (blocked, paused) and not checked by the browser recently."""
        now = time.time()
        searched = self.store.setdefault("relay_searched", {})
        items: list[tuple[int, float, dict[str, Any]]] = []
        if not self.relay_enabled or not ready():   # no shop settings or terms: the browser fetches nothing either
            return {"enabled": False, "items": [], "total": 0, "counts": {}}
        live_shops = [r for r in self.retailers if r in RETAILERS]
        for num, s in self.store["sets"].items():
            offers = self.store["offers"].get(num, {})
            priced = any(o.get("available") and o.get("last_price") or o.get("manual_price") for o in offers.values())
            for rid in live_shops:
                o = offers.get(rid)
                if reader_of(rid) == "partner" and self.partner_api:
                    continue
                base = {"set_number": num, "retailer": rid, "shop": RETAILERS[rid][0]}
                if o and o.get("url") and not compare.is_compare_url(o["url"]):
                    if o.get("manual_price") or o.get("link_status") == "rejected":
                        continue
                    last_try = o.get("relay_ts", 0)
                    if not o.get("history") and not o.get("last_ok"):
                        if now - last_try > 3600:                       # never a price: first, once an hour at most
                            items.append((0, last_try, {**base, "url": o["url"], "reason": "no_price"}))
                    elif o.get("error") and o.get("ignored_error") != o["error"] and not o.get("suspect"):
                        if now - last_try > 3600:                       # an open error: try it in the browser, once an hour
                            items.append((1, last_try, {**base, "url": o["url"], "reason": "open_error"}))
                    elif (o.get("error") or self.fetcher.cooldown_left(rid) > 0) and now - max(last_try, o.get("last_ok") or 0) > self.RELAY_FAIL_HOURS * 3600:
                        items.append((3, o.get("last_ok") or 0, {**base, "url": o["url"], "reason": "server_fails"}))
                elif not priced and rid != "lego_com" and (url := search_url(rid, num)) \
                        and now - searched.get(f"{num}|{rid}", 0) > self.RELAY_SEARCH_DAYS * 86400:
                    items.append((2, searched.get(f"{num}|{rid}", 0), {**base, "kind": "search", "url": url, "reason": "search",
                                                                       "render": rid in self.store.get("shop_js", {})}))
        if self.market_enabled:
            # market values the server can't fetch (paused / errors): last, at most once a day per set
            src, locale = MARKET, self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE)
            paused, tried = self.fetcher.cooldown_left(src) > 0, self.store.setdefault("relay_market", {})
            for num, s in self.store["sets"].items():
                e = self._cstore(src).get(num) or {}
                fresh = e.get("status") == "ok" and now - e["ts"] < 24 * 3600 and not e.get("last_error")
                missing = e.get("status") in ("missing", "unreadable") and now - e["ts"] < COMPARE_MISSING_HOURS * 3600
                failing = paused or e.get("status") == "error" or bool(e.get("last_error"))
                if fresh or missing or not failing or now - tried.get(num, 0) < 24 * 3600:
                    continue
                if (url := compare.first_url(src, num, locale, s.get("ean"))):
                    items.append((4, tried.get(num, 0), {"kind": "page", "source": src, "set_number": num, "retailer": src,
                                                         "shop": compare.SOURCES[src][0], "url": url, "step": 0, "reason": "market"}))
        items.sort(key=lambda x: (x[0], x[1]))
        counts: dict[str, int] = {}
        for _, _, it in items:
            counts[it["reason"]] = counts.get(it["reason"], 0) + 1
        return {"enabled": True, "items": [i for _, _, i in items[:limit]], "total": len(items), "counts": counts,
                "site_gap": DOMAIN_GAP, "search_gap": SEARCH_GAP}

    def relay_heartbeat(self, beat: dict[str, Any]) -> None:
        """The userscript's continuous check says it is alive (shown under Manage → Userscript)."""
        keep = {k: beat.get(k) for k in ("done", "ok", "fail", "found", "waiting", "version") if isinstance(beat.get(k), (int, float, str))}
        self.store["relay_heartbeat"] = {"ts": time.time(), "on": bool(beat.get("on", True)), **keep}

    def _relay_search(self, item: dict[str, Any]) -> str | dict[str, Any]:
        """A shop's search page fetched by the browser: find the product here (same rules as the server)."""
        rid, num = item.get("retailer"), normalize_set_number(str(item.get("set_number") or ""))
        if rid not in RETAILERS or num not in self.store["sets"]:
            raise ValueError(f"{rid}: {num}: not a tracked set / shop")
        html = item.get("html") if isinstance(item.get("html"), str) else ""
        try:
            status = int(item.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        readable = bool(html) and 0 < status < 400
        if readable:              # blocked / failed searches are tried again, only real answers wait a week
            self.store.setdefault("relay_searched", {})[f"{num}|{rid}"] = time.time()
        found = find_search_result(rid, html, num, set(self.store.get("rejected", {}).get(num, []))) if readable else None
        offers = self.store["offers"].setdefault(num, {})
        if found and url_key(rid, found) not in set(self.store.setdefault("rejected", {}).get(num, [])) \
                and not (offers.get(rid) or {}).get("url"):
            offers[rid] = {"url": found, "history": [], "found": time.time()}
            self.log("ok", "discover", T("link found by your browser"), set_number=num, retailer=rid, url=found, source="relay")
            return {"set_number": num, "retailer": rid, "shop": RETAILERS[rid][0], "url": found, "reason": "no_price"}
        why = (T("the shop blocked the search (HTTP {status})", status=status) if status in (403, 429, 503)
               else T("could not reach the shop: {error}", error=str(item.get("error") or "?")[:100]) if not status
               else T("search page: HTTP error {status}", status=status) if status >= 400
               else T("no matching product found") if num in html
               else T("the search page does not contain {number}: this shop probably loads its results with JavaScript. Paste the product page URL instead.", number=num))
        if "JavaScript" in why and not item.get("rendered"):
            self.fetcher.discover_error[rid] = why
            self._note_js(rid)
            if readable:
                self.store.setdefault("relay_searched", {})[f"{num}|{rid}"] = time.time()
        self.log("info", "discover", T("your browser searched: {reason}", reason=why), set_number=num, retailer=rid, url=item.get("url"), source="relay")
        return "fail"

    def relay_result(self, item: dict[str, Any]) -> str:
        """One page fetched by the user's browser: a price (stored like the userscript) or a failure.
        A comparison-site page comes back as HTML and is read here ('follow' adds the next page to fetch)."""
        if item.get("kind") == "page":
            return self._relay_page(item)
        if item.get("kind") == "search":
            return self._relay_search(item)
        url, price, error = item.get("url"), item.get("price"), item.get("error")
        rid = item.get("retailer") or (retailer_from_url(url) if url else None)
        num = normalize_set_number(item["set_number"]) if item.get("set_number") else None
        if num and rid and (o := self.store["offers"].get(num, {}).get(rid)):
            o["relay_ts"] = time.time()                      # the continuous check doesn't ask for it again at once
        if price:
            self.report_price(float(price), url=url, set_number=num, retailer=rid, title=item.get("title"), via="relay")
            status = "ok"
        else:
            self.log("warning", "userscript", T("your browser could not fetch the price either: {error}", error=str(error or "")[:120] or "?"),
                     set_number=num, retailer=rid, url=url, source="relay")
            status = "fail"
        rl = self.store.setdefault("relay_last", {"ts": 0, "ok": 0, "fail": 0})
        if time.time() - rl.get("ts", 0) > 900:           # a new run: start counting again
            rl.update(ok=0, fail=0)
        rl["ts"] = time.time()
        rl[status] = rl.get(status, 0) + 1
        return status

    def _relay_page(self, item: dict[str, Any]) -> str | dict[str, Any]:
        """Process a browser comparison result and return a follow-up page or outcome key."""
        src, url = item.get("source"), str(item.get("url") or "")
        num = normalize_set_number(str(item.get("set_number") or ""))
        if src not in compare.SOURCES or not compare.is_compare_url(url) or num not in self.store["sets"]:
            raise ValueError(f"{src}: {url}: not a comparison page of a tracked set")
        html = item.get("html") if isinstance(item.get("html"), str) else ""
        try:
            status, step = int(item.get("status") or 0), max(0, min(compare.MAX_STEPS - 1, int(item.get("step") or 0)))
        except (TypeError, ValueError):
            status, step = 0, 0
        if src == MARKET:
            self.store.setdefault("relay_market", {})[num] = time.time()     # not asked again within a day
        if status == 0:        # the browser couldn't reach it either: log, but don't count it towards a server pause
            self.log("warning", "userscript", T("your browser could not fetch the price either: {error}", error=str(item.get("error") or "")[:120] or "?"),
                     set_number=num, url=url, source="relay")
            kind, nxt = "error", None
        else:
            kind, nxt = self.compare_page(src, num, url, status, html, None, step, via="relay")
        if kind == "ok" and src != MARKET:           # the market value has no shop prices
            self._compare_links(num)
            self._compare_apply_prices(num)
        elif kind == "ok":
            self.push_update()
        rl = self.store.setdefault("relay_last", {"ts": 0, "ok": 0, "fail": 0})
        if time.time() - rl.get("ts", 0) > 900:
            rl.update(ok=0, fail=0)
        rl["ts"] = time.time()
        if kind == "follow":
            return {"kind": "page", "source": src, "set_number": num, "shop": compare.SOURCES[src][0], "url": nxt, "step": step + 1}
        status_key = "ok" if kind in ("ok", "missing") else "fail"
        rl[status_key] = rl.get(status_key, 0) + 1
        return status_key

    async def discover_offers(self, set_number: str | None = None) -> int:
        """Inline discovery for one set (or all sets, used by tests)."""
        nums = [normalize_set_number(set_number)] if set_number else list(self.store["sets"])
        found = 0
        for num in nums:
            found += (await self.discover_set(num))["found"]
        self.push_update()
        return found

    # -------------------------------------------------------------------- enrich
    @staticmethod
    def _name_replaceable(s: dict[str, Any]) -> bool:
        name, src = s.get("name"), s.get("name_source")
        if not name or src == "shop":
            return True
        if src in ("user", "import", "catalog", setdb.SOURCE, SET_DATA_API, SET_DATA_PAGE, PARTS_API) \
                or (src and src[0].isupper() or "+" in (src or "")):
            return False
        # names from older versions: replace the ones that look like a shop title
        return bool(re.search(r"\blego\b", name, re.I) or accessory_word(name) or KNOCKOFF_RE.search(name)
                    or len(name) > 70)

    def needs_enrich(self, num: str) -> bool:
        s = self.store["sets"][num]
        lego_missing = s.get("rrp_source") not in (OFFICIAL, "user") or s.get("image_source") not in (OFFICIAL, "user")
        lego_due = lego_missing and time.time() - s.get("lego_checked", 0) > 7 * 86400
        return lego_due or self._name_replaceable(s) or not all(s.get(k) for k in ("theme", "year", "pieces", "image"))

    @staticmethod
    def _lego_retired_rest(rid: str, offer: dict[str, Any]) -> bool:
        """The official shop said the set is out of the range ("Product uit handel"): its page is not fetched again for
        a month (no prices to get there); ↻ in the set still fetches it. The month counts from the last time
        The official shop confirmed it (`discontinued_at`), so a failed fetch in between doesn't stretch it."""
        if rid != "lego_com" or offer.get("unavailable") != "discontinued":
            return False
        since = offer.setdefault("discontinued_at", offer.get("last_checked") or 0)   # offers from before 0.9.23
        return time.time() - since < LEGO_RETIRED_REST

    @staticmethod
    def _set_unavailable(offer: dict[str, Any], why: str | None) -> None:
        """Remember why a shop page has no price (or forget it); "out of the range" also notes when it was seen."""
        if why:
            offer["unavailable"] = why
        else:
            offer.pop("unavailable", None)
        if why == "discontinued":
            offer["discontinued_at"] = time.time()
        else:
            offer.pop("discontinued_at", None)

    def _lego_hold(self, num: str, offer: dict[str, Any], parsed: Any) -> tuple[float, str] | None:
        """The official shop shows the set as temporarily unavailable (sold out, not retired): no new price is taken.
        The LEGO price is the set's RRP (never a promotion price that happened to be the last one); without an
        RRP, the regular price the page shows ('rrp'). None: nothing to hold."""
        if not (parsed and parsed.unavailable and parsed.reason == "sold_out" and parsed.price is None):
            return None
        if offer.get("manual_price"):
            return None
        rrp = self.store["sets"][num].get("rrp") or parsed.list_price
        return (round(float(rrp), 2), "rrp") if rrp else None

    def _apply_lego(self, num: str, parsed: Any) -> bool:
        """The official shop is the first source for RRP, image and name. Values the user typed win."""
        s = self.store["sets"][num]
        before = (s.get("rrp"), s.get("image"), s.get("name"), s.get("retiring"), s.get("pieces"))
        # the RRP is the regular price the official shop shows the first time and stays that: a later promotion (or a
        # price that changes) never moves it. A value you typed yourself always wins.
        if parsed.list_price and s.get("rrp_source") not in (OFFICIAL, "user"):
            s["rrp"], s["rrp_source"] = round(max(parsed.list_price, parsed.price or 0), 2), OFFICIAL
        if parsed.list_price and (o := self.store["offers"].get(num, {}).get("lego_com")) and o.get("history"):
            # the official shop never sells far below its own regular price: such points were another product's price
            # (older versions read a recommended product on a sold-out page)
            keep = [p for p in o["history"] if p[1] >= parsed.list_price * 0.4]
            if len(keep) != len(o["history"]):
                self.log("info", "price", T("removed {n} wrong prices of the official shop (another product on the page)",
                                            n=len(o["history"]) - len(keep)), set_number=num, retailer="lego_com", source="server")
                o["history"] = keep
                if o.get("last_price") is not None and o["last_price"] < parsed.list_price * 0.4:
                    o["last_price"], o["available"] = None, False
        if parsed.image and parsed.image.startswith("https://") and s.get("image_source") != "user":
            s["image"], s["image_source"] = parsed.image, OFFICIAL
        if parsed.title and (self._name_replaceable(s) or s.get("name_source") == OFFICIAL):
            s["name"], s["name_source"] = clean_title(parsed.title, num), OFFICIAL
        if parsed.retiring:
            s["retiring"], s["retiring_source"] = True, OFFICIAL
        elif s.get("retiring_source") == OFFICIAL:
            s.pop("retiring", None)
            s.pop("retiring_source", None)
        if getattr(parsed, "pieces", None):              # the official piece count
            set_pieces(s, parsed.pieces, OFFICIAL, setdb_pieces=self._setdb_pieces(num))
        s["lego_checked"] = time.time()
        return before != (s.get("rrp"), s.get("image"), s.get("name"), s.get("retiring"), s.get("pieces"))

    async def lego_lookup(self, num: str, force: bool = False, wake: bool = False) -> bool:
        """Find + read the set's page at the official shop (also kept as the official shop's link)."""
        if not force and self.fetcher.cooldown_left("lego_com") > 0:
            return False
        s = self.store["sets"][num]
        offers = self.store["offers"].setdefault(num, {})
        offer = offers.get("lego_com")
        url = offer.get("url") if offer else None
        if not url:
            url = await self.fetcher.discover("lego_com", num, force=force)
            rejected = set(self.store.setdefault("rejected", {}).get(num, []))
            if not url or url_key("lego_com", url) in rejected:
                s["lego_checked"] = time.time()
                return False
            offer = offers["lego_com"] = {"url": url, "history": [], "found": time.time()}
        if self._lego_retired_rest("lego_com", offer) and not (force or wake):
            return False                                   # out of the range: nothing to read there this month
        parsed, error = await self.fetcher.fetch_offer("lego_com", url, force=force)
        if error and error.startswith("paused"):
            return False
        offer["link_status"], offer["link_reason"] = link_check(offer, s, num)
        if parsed and parsed.title:
            offer["title"] = parsed.title[:300]
        held = self._lego_hold(num, offer, parsed) if not error else None
        fetched = held[0] if held else parsed.price if parsed else None
        if offer.get("manual_price"):                     # your own price wins: only remember what the official shop said
            offer["auto_price"], offer["last_checked"], offer["error"] = fetched, time.time(), error
            self._price_choice(num, "lego_com", offer, fetched, "server")
        else:
            record_price(offer, fetched, error=error)
        if held:
            self._set_unavailable(offer, "sold_out")
        elif parsed and parsed.unavailable and parsed.reason and not error:
            self._set_unavailable(offer, parsed.reason)    # e.g. "Product uit handel": a warning, no price
        elif parsed and parsed.price:
            self._set_unavailable(offer, None)
        if parsed and (parsed.price or held):
            offer["last_ok"] = offer["last_checked"]
        if error:
            self.log("error", "fetch", error, set_number=num, retailer="lego_com", url=url)
        return self._apply_lego(num, parsed) if parsed else False

    def start_enrich(self, all_sets: bool = False) -> dict[str, Any]:
        nums = [n for n in self.store["sets"] if all_sets or self.needs_enrich(n)]
        has_key = bool(self.opt(self.entry, CONF_SET_DATA_KEY, "") or self.opt(self.entry, CONF_PARTS_KEY, ""))
        return self.start_job("enrich", T("Filling in set data"), nums, self.enrich_set,
                              None if has_key else T("no API key set: the public set data pages are used"))

    async def enrich_set(self, num: str, force: bool = False, wake: bool = False) -> dict[str, int]:
        """Fill in a set's RRP, image, name and other details from the official shop first, then the metadata sources."""
        s = self.store["sets"][num]
        lego_changed = await self.lego_lookup(num, force=force, wake=wake)       # 1st source: RRP, image, name
        meta, source = await lookup_metadata(async_get_clientsession(self.hass), self.api_key(CONF_SET_DATA_KEY),
                                             self.api_key(CONF_PARTS_KEY), num)
        await asyncio.sleep(1.0)   # be gentle with the metadata sources
        if not meta:
            return {"updated": 1} if lego_changed else {"errors": 1}
        changed = lego_changed
        if meta.get("name") and self._name_replaceable(s) and s.get("name") != meta["name"]:
            s["name"], s["name_source"] = meta["name"], source
            changed = True
        for key in ("theme", "subtheme", "year", "image", "rrp", "exit_date"):
            if meta.get(key) and not s.get(key):
                s[key] = meta[key]
                if key in self.SOURCE_KEYS:
                    s[self.SOURCE_KEYS[key]] = source or "meta"
                changed = True
        if meta.get("pieces"):
            changed |= set_pieces(s, meta["pieces"], (meta.get("_from") or {}).get("pieces") or source or "meta",
                                  setdb_pieces=self._setdb_pieces(num))
        if changed:
            self.log("ok", "meta", T("set data filled in from {source}", source=source or OFFICIAL), set_number=num,
                     source=(source or OFFICIAL))
        return {"updated": 1} if changed else {}

    def start_pieces(self) -> dict[str, Any]:
        """Manage → piece counts again: look up the piece count of every set in the best source there is
        (the official count first) and replace counts from a weaker source. A count you typed stays."""
        if not (self.api_key(CONF_SET_DATA_KEY) or self.api_key(CONF_PARTS_KEY) or source_url("set_data_page") or self.setdb):
            raise LocalizedError("There is no source for piece counts: add a key under Settings, or a set data source to your shop settings.")
        session = async_get_clientsession(self.hass)

        async def work(num: str) -> dict[str, int]:
            s = self.store["sets"].get(num)
            if s is None or s.get("pieces_source") == "user":
                return {"skipped": 1}
            meta, source = await lookup_metadata(session, self.api_key(CONF_SET_DATA_KEY), self.api_key(CONF_PARTS_KEY), num)
            await asyncio.sleep(1.0)        # be gentle with the metadata sources
            found = meta.get("pieces")
            src = (meta.get("_from") or {}).get("pieces") or source
            if not found and (row := self.setdb.get(num)) and row[setdb.PIECES]:
                found, src = row[setdb.PIECES], "setdb"
            if not found:
                return {"errors": 1}
            old = s.get("pieces")
            if set_pieces(s, found, src, setdb_pieces=self._setdb_pieces(num), replace_unknown=True):
                self.log("ok", "meta", T("piece count {old} → {new} ({source})", old=old or "–", new=s["pieces"], source=src),
                         set_number=num, source=src)
                return {"updated": 1}
            return {}

        nums = sorted(self.store["sets"], key=lambda n: (n not in self.store["collection"], n))
        return self.start_job("pieces", T("Piece counts"), nums, work)

    # ------------------------------------------------------------ update (CSV)
    def start_update(self, nums: list[str], force: bool = False) -> dict[str, Any]:
        """After a CSV re-import: per set fill in metadata, find missing links and fetch prices."""
        live = self._live_retailers(force)

        async def work(num: str) -> dict[str, int]:
            out = {"updated": 0, "found": 0, "errors": 0, "skipped": 0}
            if self.needs_enrich(num):
                out["updated"] += (await self.enrich_set(num, wake=force)).get("updated", 0)
            out["found"] += (await self.discover_set(num, live))["found"]
            res = await self.refresh_set(num, live, wake=force)
            out["errors"] += res["errors"]
            out["skipped"] += res["skipped"]
            return out

        nums = [n for n in dict.fromkeys(nums) if n in self.store["sets"]]
        return self.start_job("update", T("Updating collection"), nums, work)

    # ---------------------------------------------------------------- settings
    SECRET_KEYS = (CONF_SET_DATA_KEY, CONF_PARTS_KEY, CONF_PARTNER_CLIENT_ID, CONF_PARTNER_CLIENT_SECRET)

    def api_key(self, key: str) -> str:
        """A stored API key, but only for the address it was entered or used with (never one an imported file chose)."""
        return self.opt(self.entry, key, "") if key_allowed(dict(self.entry.options), key) else ""

    def shop_settings_info(self) -> dict[str, Any]:
        """Shop settings imported? Terms accepted (when, which version)? Without both, no shop is contacted."""
        legal = self.entry.options.get(CONF_LEGAL) or {}
        return {"imported": bool(self.entry.options.get(CONF_SHOP_PROFILE)), "ready": ready(), "shops": len(profile_ids()),
                "comparison_sites": len(compare.SOURCES), "accepted": legal.get("accepted"),
                "legal_version": LEGAL_VERSION, "accepted_version": legal.get("version")}

    def settings_get(self) -> dict[str, Any]:
        """Return panel settings and shop state with configured secret values masked."""
        o = {**self.entry.data, **self.entry.options}
        mask = lambda v: f"••••{v[-4:]}" if v and len(v) > 4 else ("••••" if v else "")  # noqa: E731
        shops = []
        for rid, (label, _) in RETAILERS.items():
            shops.append({
                "id": rid, "label": label, "builtin": not rid.startswith("c_"),
                "generic": rid in GENERIC_SHOPS, "domain": domain_of(rid), "reader": reader_of(rid),
                "search": SEARCH.get(rid, ""), "default_search": DEFAULT_SEARCH.get(rid, GENERIC_SHOPS.get(rid, {}).get("search", "")),
                "enabled": rid in self.retailers,
                "paused_hours": round(self.fetcher.cooldown_left(rid) / 3600, 2),
                "blocks": self.fetcher.blocks.get(rid, 0), "autopause": rid not in self.fetcher.no_autopause,
            })
        return {
            "shop_settings": self.shop_settings_info(),
            "discount_threshold": self.threshold, "min_history_days": int(self.opt(self.entry, CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS)),
            "auto_refresh": bool(o.get(CONF_AUTO_REFRESH, True)), "refresh_times": ", ".join(f"{h:02d}:{m:02d}" for h, m in self.refresh_times),
            "digest_time": str(o.get(CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME))[:5], "use_impersonation": bool(o.get(CONF_IMPERSONATE, True)),
            "notify_service": o.get(CONF_NOTIFY, "") or "", "value_source": o.get(CONF_VALUE_SOURCE, "shop_first"),
            "keys": {k: {"set": bool(o.get(k)), "masked": mask(o.get(k) or "")} for k in self.SECRET_KEYS},
            "shops": shops, "transport": self.fetcher.transport,
            "lego_locale": o.get(CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE),
            "refresh_mode": self.refresh_mode, "spread_hours": self.spread_hours, "watch_cycle_min": self.watch_cycle_minutes,
            "deal_min_score": self.deal_rules["min_score"], "deal_atl": self.deal_rules["atl"], "deal_target": self.deal_rules["target"],
            "cycle_choices": list(CYCLE_CHOICES), "watch_cycle_choices": list(WATCH_CYCLE_CHOICES), "watch_limit": self.watch_limit,
            "dev": {k: self.dev(k) for k in (CONF_DEV_FIXED_TIMES, CONF_DEV_FULL_REFRESH, CONF_DEV_FREE_CYCLE, CONF_DEV_WATCH_UNLIMITED)},
            "language": o.get(CONF_LANGUAGE, DEFAULT_LANGUAGE), "languages": LANGUAGES,
            "partner_country": self.partner_country, "partner_api": bool(self.partner_api),
            "browser_relay": bool(o.get(CONF_RELAY, True)), "compare": self.compare_enabled,
            "market_value": self.market_enabled, "ticker": self.ticker, "deal_filter": self.deal_filter,
            "catalog_scan": self.scan_per_day, "scan_choices": list(scan.PER_DAY_CHOICES),
            "compare_sources": self.compare_sources,
            "block_words": list(o.get(CONF_BLOCK_WORDS, [])), "allow_words": list(o.get(CONF_ALLOW_WORDS, [])),
            "builtin_words": list(BUILTIN_WORDS), "relay_hours": int(o.get(CONF_RELAY_HOURS, DEFAULT_RELAY_HOURS)),
        }

    def settings_validate(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Merge + validate panel settings into a new options dict. Raises LocalizedError."""
        from .models import parse_times
        from .shops import validate_custom_shop

        opts = dict(self.entry.options)
        def num(key: str, lo: int, hi: int) -> None:
            try:
                v = int(float(fields[key]))
            except (TypeError, ValueError) as err:
                raise LocalizedError("{field}: not a number", field=key) from err
            if not lo <= v <= hi:
                raise LocalizedError("{field}: must be between {lo} and {hi}", field=key, lo=lo, hi=hi)
            opts[key] = v
        if "discount_threshold" in fields:
            num("discount_threshold", 1, 90)
        if "min_history_days" in fields:
            num("min_history_days", 0, 90)
        if CONF_DEAL_MIN_SCORE in fields:
            num(CONF_DEAL_MIN_SCORE, 1, 100)
        for key in (CONF_DEAL_ATL, CONF_DEAL_TARGET):
            if key in fields:
                opts[key] = bool(fields[key])
        if CONF_DEAL_FILTER in fields:
            raw = fields[CONF_DEAL_FILTER] if isinstance(fields[CONF_DEAL_FILTER], dict) else {}
            df: dict[str, Any] = {"themes_off": sorted({str(x)[:80] for x in (raw.get("themes_off") or []) if str(x).strip()})[:300],
                                  "skip_owned": bool(raw.get("skip_owned")), "skip_retired": bool(raw.get("skip_retired"))}
            for k, hi in (("min_price", 10000), ("max_price", 10000), ("min_discount", 95), ("min_pieces", 20000), ("max_pieces", 20000)):
                v = raw.get(k)
                if v in (None, ""):
                    df[k] = None
                    continue
                try:
                    v = float(str(v).replace(",", "."))
                except ValueError as err:
                    raise LocalizedError("{field}: not a number", field=k) from err
                if not 0 <= v <= hi:
                    raise LocalizedError("{field}: must be between {lo} and {hi}", field=k, lo=0, hi=hi)
                df[k] = v
            for lo, hi in (("min_price", "max_price"), ("min_pieces", "max_pieces")):
                if df[lo] is not None and df[hi] is not None and df[lo] > df[hi]:
                    raise LocalizedError("{field}: must be between {lo} and {hi}", field=lo, lo=0, hi=df[hi])
            opts[CONF_DEAL_FILTER] = df
        if CONF_SCAN in fields:
            try:
                f = float(fields[CONF_SCAN])
                v = int(f)
            except (TypeError, ValueError, OverflowError) as err:       # also nan / inf
                raise LocalizedError("{field}: not a number", field=CONF_SCAN) from err
            if f != v or v not in scan.PER_DAY_CHOICES:                  # only the listed choices, no rounding
                raise LocalizedError("{field}: not a valid choice", field=CONF_SCAN)
            opts[CONF_SCAN] = v
        if CONF_TICKER in fields:
            raw, tk = fields[CONF_TICKER] if isinstance(fields[CONF_TICKER], dict) else {}, {}
            for k, v in TICKER_DEFAULT.items():
                val = raw.get(k, v)
                if isinstance(v, bool):
                    tk[k] = bool(val)
                else:
                    try:
                        tk[k] = max(0, min(50, int(float(val))))
                    except (TypeError, ValueError) as err:
                        raise LocalizedError("{field}: not a number", field=k) from err
            opts[CONF_TICKER] = tk
        for key in ("auto_refresh", "use_impersonation", CONF_RELAY, CONF_COMPARE, CONF_MARKET, CONF_DEV_FIXED_TIMES, CONF_DEV_FULL_REFRESH,
                    CONF_DEV_FREE_CYCLE, CONF_DEV_WATCH_UNLIMITED):
            if key in fields:
                opts[key] = bool(fields[key])
        if CONF_COMPARE in fields:
            opts.pop("compare", None)                       # the option's name before 0.9.19
        for key in (CONF_BLOCK_WORDS, CONF_ALLOW_WORDS):
            if key in fields:
                raw = fields[key]
                items = re.split(r"[\n,;]+", raw) if isinstance(raw, str) else raw if isinstance(raw, list) else None
                if items is None:
                    raise LocalizedError("{field}: not a valid choice", field=key)
                words: list[str] = []
                for w in items:
                    w = re.sub(r"\s+", " ", str(w)).strip()
                    if not w:
                        continue
                    if not 2 <= len(w.rstrip("*")) <= 60 or "*" in w.rstrip("*"):
                        raise LocalizedError("Word list: “{word}” must be 2 to 60 characters (a * only at the end)", word=w[:60])
                    if w.lower() not in (x.lower() for x in words):
                        words.append(w)
                if len(words) > 300:
                    raise LocalizedError("Word list: at most 300 words")
                opts[key] = words
        if CONF_COMPARE_SOURCES in fields:
            sel = fields[CONF_COMPARE_SOURCES]
            if not isinstance(sel, list) or any(src not in compare.SOURCES for src in sel):
                raise LocalizedError("{field}: not a valid choice", field=CONF_COMPARE_SOURCES)
            opts[CONF_COMPARE_SOURCES] = [src for src in compare.SOURCES if src in sel]
        if "refresh_times" in fields:
            times = parse_times(str(fields["refresh_times"]))
            if not 1 <= len(times) <= 6:
                raise LocalizedError("Enter 1 to 6 times as HH:MM, e.g. 07:30, 19:30")
            opts[CONF_REFRESH_TIMES] = ", ".join(times)
        if "digest_time" in fields:
            t = parse_times(str(fields["digest_time"]))
            if len(t) != 1:
                raise LocalizedError("Digest time: one time as HH:MM")
            opts[CONF_DIGEST_TIME] = t[0] + ":00"
        if "notify_service" in fields:
            ns = str(fields["notify_service"] or "").strip()
            if ns and not re.fullmatch(r"(notify\.)?[a-z0-9_]+", ns):
                raise LocalizedError("Notify service like notify.mobile_app_phone")
            opts[CONF_NOTIFY] = ns
        if "value_source" in fields:
            if fields["value_source"] not in ("shop_first", "import_first"):
                raise LocalizedError("Unknown value source")
            opts[CONF_VALUE_SOURCE] = fields["value_source"]
        for key in self.SECRET_KEYS:          # None/absent = keep, "" = clear
            if fields.get(key) is not None:
                val = str(fields[key]).strip()
                if val and not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}" if key in (CONF_SET_DATA_KEY, CONF_PARTS_KEY) else r"[\x21-\x7e]{8,256}", val):
                    raise LocalizedError("{field}: invalid key", field=key)
                opts[key] = val
                hosts = dict(opts.get(CONF_KEY_HOSTS) or {})   # a key you enter belongs to the addresses in force now
                if val:
                    hosts[key] = source_hosts(KEY_SOURCES[key])
                else:
                    hosts.pop(key, None)
                opts[CONF_KEY_HOSTS] = hosts
        if "custom_shops" in fields:
            shops, seen = [], set()
            for shop in fields["custom_shops"] or []:
                v = validate_custom_shop(shop)
                if v["id"] in seen or v["id"] in RETAILERS and not v["id"].startswith("c_"):
                    raise LocalizedError("Shop {name} already exists", name=v["name"])
                seen.add(v["id"])
                shops.append(v)
            opts[CONF_CUSTOM_SHOPS] = shops
        if "shop_search" in fields:
            searches = {}
            for rid, tpl in (fields["shop_search"] or {}).items():
                tpl = str(tpl or "").strip()
                if tpl and not valid_search(tpl):
                    raise LocalizedError("Search URL for {shop}: must start with https:// and contain {query} or {number}",
                                         shop=RETAILERS.get(rid, (rid,))[0], query="{query}", number="{number}")
                if tpl == DEFAULT_SEARCH.get(rid):
                    continue                     # default: don't store, so future default fixes still apply
                searches[rid] = tpl
            opts[CONF_SHOP_SEARCH] = searches
        if CONF_WATCH_CYCLE in fields:
            try:
                wc = int(fields[CONF_WATCH_CYCLE] or 0)
            except (TypeError, ValueError) as err:
                raise LocalizedError("{field}: not a valid choice", field=CONF_WATCH_CYCLE) from err
            if wc not in WATCH_CYCLE_CHOICES:
                raise LocalizedError("{field}: not a valid choice", field=CONF_WATCH_CYCLE)
            opts[CONF_WATCH_CYCLE] = wc
        if fields.get("refresh_mode") == "times" and not (fields.get(CONF_DEV_FIXED_TIMES) or self.dev(CONF_DEV_FIXED_TIMES)):
            raise LocalizedError("Checking at fixed times is switched off: a full round at once puts too much load on the shops.")
        if "spread_hours" in fields and not (fields.get(CONF_DEV_FREE_CYCLE) or self.dev(CONF_DEV_FREE_CYCLE)):
            try:
                ok = float(fields["spread_hours"]) in CYCLE_CHOICES
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise LocalizedError("Cycle: choose {choices} hours", choices="/".join(map(str, CYCLE_CHOICES)))
        if "refresh_mode" in fields:
            if fields["refresh_mode"] not in ("spread", "times", "off"):
                raise LocalizedError("Unknown refresh mode")
            opts[CONF_REFRESH_MODE] = fields["refresh_mode"]
            opts[CONF_AUTO_REFRESH] = fields["refresh_mode"] != "off"
        if "spread_hours" in fields:
            num("spread_hours", 1, 168)
        if "language" in fields:
            if fields["language"] != "auto" and fields["language"] not in LANGUAGES:
                raise LocalizedError("Unknown language")
            opts[CONF_LANGUAGE] = fields["language"]
        if "partner_country" in fields:
            if str(fields["partner_country"]) not in ("auto", "NL", "BE"):
                raise LocalizedError("{field}: invalid value", field="partner_country")
            opts[CONF_PARTNER_COUNTRY] = str(fields["partner_country"])
        if "relay_hours" in fields:
            num(CONF_RELAY_HOURS, 1, 168)
        if "lego_locale" in fields:
            loc = str(fields["lego_locale"] or "").strip().lower()
            if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", loc):
                raise LocalizedError("country of the official shop like en-gb, nl-be, de-de")
            opts[CONF_LEGO_LOCALE] = loc
        valid_ids = set(RETAILERS) | {s["id"] for s in opts.get(CONF_CUSTOM_SHOPS, [])}
        if "retailers" in fields:
            opts[CONF_RETAILERS] = [r for r in fields["retailers"] if r in valid_ids]
        if "custom_shops" in fields:
            # a shop you just added is searched and fetched right away (switch it off under Shops if you like)
            before = {s["id"] for s in self.opt(self.entry, CONF_CUSTOM_SHOPS, []) or []}
            active = list(opts.get(CONF_RETAILERS, self.opt(self.entry, CONF_RETAILERS, profile_ids())))
            opts[CONF_RETAILERS] = [r for r in active if r in valid_ids] + \
                [s["id"] for s in opts[CONF_CUSTOM_SHOPS] if s["id"] not in before and s["id"] not in active]
        if "no_autopause" in fields:
            opts[CONF_NO_AUTOPAUSE] = [r for r in fields["no_autopause"] if r in valid_ids]
        return opts

    async def test_partner(self, client_id: str | None = None, secret: str | None = None) -> tuple[bool, str]:
        """Settings test button: log in and look up set 10281 in the partner shop's catalog."""
        cid = client_id or self.api_key(CONF_PARTNER_CLIENT_ID)        # typed in now, or stored for these addresses
        sec = secret or self.api_key(CONF_PARTNER_CLIENT_SECRET)
        if not (cid and sec):
            return False, T("no key entered")
        token_url, api_url = source_url("partner_api_token"), source_url("partner_api")
        if not (token_url and api_url):
            return False, T("this source is not in your shop settings (or their terms are not accepted)")
        api = PartnerApi(async_get_clientsession(self.hass), cid, sec, self.partner_country, token_url=token_url, api_url=api_url)
        try:
            found = [p for p in await api.search("LEGO 10281") if title_check(p["title"], "10281")[0] == "ok"]
        except PartnerApiError as err:
            return False, str(err)
        if not found:
            return True, T("logged in, but set 10281 was not found")
        p = found[0]
        return True, T("works: 10281 = {name}", name=p["title"][:60] + (f" (€{p['price']:.2f})" if p.get("price") else ""))

    def resume_shop(self, retailer: str | None) -> None:
        self.fetcher.reset_cooldowns(retailer)
        self.log("info", "shop", (T("pause lifted for {shop}", shop=RETAILERS.get(retailer, (retailer,))[0]) if retailer else T("pause lifted for all shops")),
                 retailer=retailer, source="panel")
        self._save()
        self.push_update()

    # --------------------------------------------------------------- link check
    def verify_links(self) -> dict[str, int]:
        """Re-judge every link offline (title/URL/price). Suspect links stop counting for prices."""
        counts = {"ok": 0, "suspect": 0, "unknown": 0, "confirmed": 0, "cleaned": 0}
        for num, offers in self.store["offers"].items():
            s = self.store["sets"].get(num, {})
            for offer in offers.values():
                counts["cleaned"] += clean_history(offer, s.get("rrp"))
                status, reason = link_check(offer, s, num)
                if status is None and not offer.get("title") and s.get("name") and s.get("name_source") in (None, "shop") \
                        and re.search(r"\blego\b", s["name"], re.I):
                    from .parsers import title_check
                    st2, why = title_check(s["name"], num)
                    if st2 == "suspect":
                        status, reason = "suspect", T("set name came from a wrong product: {reason}", reason=why)
                offer["link_status"], offer["link_reason"] = status, reason
                counts["unknown" if status is None else status] += 1
        return counts

    def confirm_offer(self, set_number: str, retailer: str) -> None:
        offer = self._offer(set_number, retailer)
        self.log("ok", "link", T("link approved"), set_number=normalize_set_number(set_number), retailer=retailer,
                 url=offer.get("url"), source="panel")
        offer["link_status"], offer["link_reason"] = "confirmed", T("confirmed by hand")
        self.push_update()

    def _auto_reject(self, num: str, rid: str, offer: dict[str, Any], source: str) -> str | None:
        """A link that is clearly another product (other brand, accessory, another set) goes away by itself:
        only this link is blocked, and the same shop is searched again for the right page. A link or price
        you set yourself is never touched; a mere doubt stays (you choose in the logbook)."""
        if offer.get("manual_url") or offer.get("manual_price") or offer.get("link_status") == "confirmed":
            return None
        why = wrong_product(offer.get("title"), num)
        if not why:
            return None
        rej = self.store.setdefault("rejected", {}).setdefault(num, [])
        if (key := url_key(rid, offer["url"])) not in rej:
            rej.append(key)
        self.store["offers"][num].pop(rid, None)
        self.log("warning", "link", T("wrong product ({reason}): this link is blocked, the shop is searched again", reason=why),
                 set_number=num, retailer=rid, url=offer.get("url"), source=source)
        self.queue_rediscover(num, rid)
        return why

    def queue_rediscover(self, num: str, rid: str) -> None:
        """Search one shop again for a set, in the background (after a link was blocked)."""
        if (num, rid) in self._rediscover:
            return
        self._rediscover.append((num, rid))
        if len(self._rediscover) == 1 and self.hass and self.entry:
            self.entry.async_create_background_task(self.hass, self._run_rediscover(), f"{DOMAIN}_rediscover")

    async def _run_rediscover(self) -> None:
        try:
            while self._rediscover and ready():
                num, rid = self._rediscover[0]
                if num in self.store["sets"] and not (self.store["offers"].get(num) or {}).get(rid):
                    try:
                        if (await self.discover_set(num, [rid]))["found"]:
                            await self.refresh_set(num, [rid], source="rediscover")
                        self._save()
                        self.push_update()
                    except Exception:  # noqa: BLE001 - the regular rounds try again
                        _LOGGER.exception("searching %s again for %s failed", rid, num)
                self._rediscover.pop(0)
        finally:
            self._rediscover = []

    def reject_link(self, set_number: str, retailer: str) -> None:
        """Logbook choice 'never visit this link again': block only this link and search the shop again."""
        num = normalize_set_number(set_number)
        self.remove_offer(num, retailer, block=True)
        self.queue_rediscover(num, retailer)

    def remove_offer(self, set_number: str, retailer: str, block: bool = True) -> None:
        num = normalize_set_number(set_number)
        offer = self._offer(num, retailer)
        if block and offer.get("url"):
            rej = self.store.setdefault("rejected", {}).setdefault(num, [])
            key = url_key(retailer, offer["url"])
            if key not in rej:
                rej.append(key)
        self.log("info", "link", T("link removed and blocked") if block else T("link removed"), set_number=num,
                 retailer=retailer, url=offer.get("url"), source="panel")
        del self.store["offers"][num][retailer]
        s = self.store["sets"].get(num, {})
        if s.get("name_source") in (None, "shop") and s.get("name") and re.search(r"\blego\b", s["name"], re.I):
            s.pop("name", None)   # the name most likely came from this wrong page
            s.pop("name_source", None)
        self.push_update()

    _UNSET: Any = object()

    @staticmethod
    def _set_discovered(offers: dict[str, Any], retailer: str, url: str) -> None:
        """A link found by searching (not chosen by hand): the same page keeps its history, a manual
        price always stays; a hand-set confirmation or an earlier link verdict no longer applies."""
        old = offers.get(retailer) or {}
        if old.get("url") and url_key(retailer, old["url"]) == url_key(retailer, url):
            o = old
            o["url"] = url
        else:
            o = offers[retailer] = {"url": url, "history": [], "found": time.time(),
                                    **({"manual_price": old["manual_price"]} if old.get("manual_price") else {})}
        for key in ("manual_url", "link_status", "link_reason", "error"):
            o.pop(key, None)

    def update_offer(self, set_number: str, retailer: str, url: Any = _UNSET, manual_price: Any = _UNSET) -> None:
        """Manual link / manual price for one shop. Manual always wins over automatic and is never
        removed by the integration. An empty value clears it and hands the field back to automation:
        - url: "https://…" or ASIN = set by hand (same page keeps its history), "" = remove the link
          (not blocked: 'find links' may search again)
        - manual_price: number = fixed price that wins, "" / None = back to the automatic price"""
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise LocalizedError("Set {number} is not tracked.", number=num)
        if retailer not in RETAILERS:
            raise LocalizedError("Unknown shop {shop}.", shop=retailer)
        offers = self.store["offers"].setdefault(num, {})
        if url is not self._UNSET:
            if url in ("", None):
                if retailer in offers:
                    self.log("info", "link", T("link cleared: automatic search allowed again"), set_number=num,
                             retailer=retailer, url=offers[retailer].get("url"), source="panel")
                    del offers[retailer]
                manual_price = self._UNSET
            else:
                if is_search_url(str(url)):
                    raise LocalizedError("This is a search page, not a product page. Use 🔎 Find to search it, or paste the page of the product itself.")
                new = normalize_url(retailer, str(url))
                rej = self.store.setdefault("rejected", {}).get(num, [])
                if url_key(retailer, new) in rej:          # chosen by hand: no longer blocked
                    rej.remove(url_key(retailer, new))
                old = offers.get(retailer)
                if old and old.get("url") and url_key(retailer, old["url"]) == url_key(retailer, new):
                    old["url"] = new
                else:
                    keep_manual = old.get("manual_price") if old else None
                    offers[retailer] = {"url": new, "history": [], **({"manual_price": keep_manual} if keep_manual else {})}
                o = offers[retailer]
                o.update(manual_url=True, link_status="confirmed", link_reason=T("set by hand"), error=None)
                self.log("ok", "link", T("link set by hand"), set_number=num, retailer=retailer, url=new, source="panel")
        if manual_price is not self._UNSET:
            offer = offers.get(retailer)
            if offer is None:
                raise LocalizedError("This shop has no link yet: enter the link as well.")
            offer.pop("price_choice", None)          # a new own price (or none): earlier choices no longer apply
            offer.pop("declined_price", None)
            if manual_price in ("", None):
                if offer.pop("manual_price", None):
                    auto = offer.pop("auto_price", None)
                    if auto:
                        record_price(offer, auto)
                    self.log("info", "user", T("manual price cleared: the automatic price is used again"),
                             set_number=num, retailer=retailer, source="panel")
            else:
                try:
                    price = round(float(manual_price), 2)
                except (TypeError, ValueError) as err:
                    raise LocalizedError("Invalid price.") from err
                if not 0 < price <= 10000:
                    raise LocalizedError("Invalid price.")
                before = self.compute()["statuses"].get(num, {})
                offer["manual_price"] = {"price": price, "ts": time.time()}
                record_price(offer, price)
                offer["error"] = None
                offer["last_ok"] = time.time()
                self.log("ok", "user", T("manual price €{price} set (wins over automatic)", price=f"{price:.2f}"),
                         set_number=num, retailer=retailer, url=offer.get("url"), price=price, source="panel")
                self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self.push_update()

    def fix_offer(self, set_number: str, retailer: str, url: str | None = None, price: float | None = None) -> None:
        """Errors tab / service: correct link and/or price in one go (both count as manual)."""
        if not url and price is None:
            raise LocalizedError("Enter a link and/or a price.")
        self.update_offer(set_number, retailer, url=url if url else self._UNSET,
                          manual_price=price if price is not None else self._UNSET)

    def _offer(self, set_number: str, retailer: str) -> dict[str, Any]:
        num = normalize_set_number(set_number)
        try:
            return self.store["offers"][num][retailer]
        except KeyError as err:
            raise LocalizedError("No link for set {number} at {shop}.", number=num, shop=retailer) from err

    def _snapshot(self) -> None:
        summary = collection_summary(self.store, self.compute()["statuses"])
        day = today_iso()
        snaps = self.store["snapshots"]
        row = [day, summary["value"], summary["cost"], summary["sets"]]
        if snaps and snaps[-1][0] == day:
            snaps[-1] = row
        else:
            snaps.append(row)

    def _fire_events(self, num: str, before: dict, after: dict) -> None:
        """Emit eligible deal events once per day and schedule notifications for changed status."""
        s = self.store["sets"][num]
        # the link opens the product page itself; price, shop and discount describe that same offer
        price, retailer, url = self.notifier.shop_offer(num, after)
        discount = after.get("discount_rrp")
        if retailer != after.get("best_retailer") and price and s.get("rrp"):
            discount = round((s["rrp"] - price) / s["rrp"] * 100, 1)
        payload = {"set_number": num, "name": s.get("name"), "theme": s.get("theme"),
                   "price": price, "retailer": retailer, "url": url, "discount": discount,
                   "target_price": s.get("target_price")}
        day = today_iso()
        if self.deal_blocked(num, after):            # left out under Deals → Settings: no deal events or notifications
            if before.get("best_price") != after.get("best_price"):
                self.hass.async_create_task(self.notifier.on_set_change(num, dict(before), dict(after)))
            return
        for flag, event in (("is_all_time_low", EVENT_NEW_LOW), ("high_discount", EVENT_HIGH_DISCOUNT),
                            ("target_hit", EVENT_TARGET_HIT)):
            key = (num, flag + day)
            if after.get(flag) and not before.get(flag) and key not in self._alerted:
                self._alerted.add(key)
                self.hass.bus.async_fire(event, payload)
                self.store["sets"][num]["last_deal"] = time.time()      # sort deals by when they came in
                add_event(self.store, flag, {k: payload[k] for k in ("set_number", "name", "price", "retailer", "url")}
                          | {"discount": payload["discount"], "score": after.get("deal_score")})
        if before.get("best_price") != after.get("best_price") or any(
                before.get(k) != after.get(k) for k in ("is_all_time_low", "target_hit", "retiring_soon", "deal_score")):
            self.hass.async_create_task(self.notifier.on_set_change(num, dict(before), dict(after)))

    # ------------------------------------------------------------------ edits
    async def add_set(self, set_number: str, *, name: str | None = None, theme: str | None = None,
                      subtheme: str | None = None, rrp: float | None = None, pieces: int | None = None, target_price: float | None = None,
                      owned: dict | None = None, discover: bool = True, watch: bool | None = None) -> str:
        """Add or update a set, enrich its metadata, and return its normalized number.
        watch=False: tracked without being on the watchlist (Edit all, a set you neither own nor watch)."""
        num = normalize_set_number(set_number)
        if owned is None and watch is not False and not (num in self.store["sets"] and self.is_watched(num)) \
                and (limit := self.watch_limit) is not None and len(self.watched_sets()) >= limit:
            raise LocalizedError("The watchlist is full ({n} sets): remove a set or move one to your collection first.", n=limit)
        s = self.store["sets"].setdefault(num, {"set_number": num})
        if watch is False:
            s["watch"] = False
        elif owned is None and s.get("watch") is False:
            s.pop("watch")                                    # added to the watchlist again
        known = catalog.apply(num, s, self.store["offers"].setdefault(num, {}))   # built-in catalogue first
        self._fill_from_setdb(num, s)                                              # then the LEGO set database
        if known and catalog.complete(s):
            meta, source = {}, OFFICIAL                     # nothing to look up online
            self.log("info", "enrich", T("set data from the built-in catalogue"), set_number=num, source="catalog")
        else:
            meta, source = await lookup_metadata(async_get_clientsession(self.hass), self.api_key(CONF_SET_DATA_KEY),
                                                 self.api_key(CONF_PARTS_KEY), num)
        if name:
            s["name_source"] = "user"
        elif meta.get("name") and self._name_replaceable(s):
            s["name"], s["name_source"] = meta["name"], source
        for key, val in {"name": name, "theme": theme, "subtheme": subtheme, "rrp": rrp, "pieces": pieces,
                         "target_price": target_price}.items():
            if val:
                s[key] = val
        if pieces:
            s["pieces_source"] = "user"
        for key in ("name", "theme", "subtheme", "year", "image", "rrp", "exit_date"):
            if meta.get(key) and not s.get(key):
                s[key] = meta[key]
        if meta.get("pieces"):
            set_pieces(s, meta["pieces"], (meta.get("_from") or {}).get("pieces") or source or "meta",
                       setdb_pieces=self._setdb_pieces(num))
        self.store["offers"].setdefault(num, {})
        if owned is not None:
            self.store["collection"][num] = owned
        if discover:
            await self.discover_set(num)
            self.queue_first_check(num)
        self.push_update()
        return num

    def queue_first_check(self, num: str) -> None:
        """A set that was never checked gets its prices and market value right after it is added, instead of
        waiting for the next round. Sets are checked one after the other, so a bulk add stays polite."""
        offers = self.store["offers"].get(num, {})
        if num in self._first_checks or any(o.get("last_checked") for o in offers.values()):
            return
        self._first_checks.append(num)
        if not self._first_busy:
            self._first_busy = True
            self.entry.async_create_background_task(self.hass, self._run_first_checks(), f"{DOMAIN}_first_check")

    async def _run_first_checks(self) -> None:
        """Work through the sets waiting for their first check; always release the busy flag."""
        try:
            while self._first_checks and ready():
                num = self._first_checks.pop(0)
                if num not in self.store["sets"]:
                    continue                                       # removed again in the meantime
                self._first_current = num
                try:
                    await self.refresh_set(num, source="added")
                    if not self.compare_enabled and self.market_enabled:   # refresh_set already did it otherwise
                        await self.compare_refresh(num, sources=[MARKET])
                except Exception:  # noqa: BLE001 - the regular rounds try again
                    _LOGGER.exception("first check failed for %s", num)
                self._first_current = None
                self._save()
                self.push_update()
        finally:
            self._first_busy, self._first_current = False, None

    def _fill_from_setdb(self, num: str, s: dict[str, Any]) -> bool:
        """Empty fields of a set from the LEGO set database (no network). Values already there win."""
        row = self.setdb.get(num)
        if not row:
            return False
        known = setdb.as_set(num, row)
        changed = False
        if known["name"] and not s.get("name"):
            s["name"], s["name_source"] = known["name"], setdb.SOURCE
            changed = True
        for key in ("theme", "subtheme", "year", "image"):
            if known[key] and not s.get(key):
                s[key] = known[key]
                if key in self.SOURCE_KEYS:
                    s[self.SOURCE_KEYS[key]] = setdb.SOURCE
                changed = True
        if known["pieces"] and not s.get("pieces"):
            s["pieces"], s["pieces_source"] = known["pieces"], "setdb"
            changed = True
        return changed

    def _setdb_pieces(self, num: str) -> int | None:
        row = self.setdb.get(num)
        return row[setdb.PIECES] if row else None

    # ------------------------------------------------------------ the LEGO set database + new sets
    @callback
    def setdb_tick(self, _now: Any = None) -> None:
        """Every few hours: when the set database is a day old, download it again (in the background)."""
        if self.setdb_info["busy"] or time.time() - self.setdb_info["ts"] < setdb.REFRESH_HOURS * 3600 \
                or not (source_url("set_database_sets") and source_url("set_database_themes")):
            return
        self.setdb_info["busy"] = True
        self.entry.async_create_background_task(self.hass, self.refresh_setdb(), f"{DOMAIN}_setdb")

    async def _download(self, url: str) -> bytes:
        """Download bytes with a 120-second timeout; reject non-200 or oversized responses."""
        session = async_get_clientsession(self.hass)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > setdb.MAX_BYTES:
                    raise ValueError("set list too large")
            return bytes(data)

    async def refresh_setdb(self) -> list[str]:
        """Download every LEGO set, store it, and remember the sets that are new since last time."""
        self.setdb_info["busy"] = True
        try:
            try:
                sets_url, themes_url = source_url("set_database_sets"), source_url("set_database_themes")
                if not (sets_url and themes_url):
                    raise ValueError(T("the set database is not in your shop settings (or their terms are not accepted)"))
                sets_gz = await self._download(sets_url)
                if not source_url("set_database_themes"):        # withdrawn in the meantime
                    raise ValueError(T("the set database is not in your shop settings (or their terms are not accepted)"))
                themes_gz = await self._download(themes_url)
                new = await self.hass.async_add_executor_job(setdb.parse, sets_gz, themes_gz)
                if len(new) < 1000:
                    raise ValueError(f"only {len(new)} sets in the download")
            except Exception as err:  # noqa: BLE001 - the set database is optional
                self.setdb_info.update(error=str(err)[:150], ts=time.time() - setdb.REFRESH_HOURS * 3600 + 3 * 3600)  # retry in 3 h
                self.log("warning", "meta", T("set database could not be updated: {error}", error=str(err)[:150]), source=setdb.SOURCE)
                return []
            first = not self.setdb
            now = time.time()
            try:                                    # saved first: nothing changes in memory when that fails
                await self._setdb_store.async_save({"ts": now, "sets": new})
                # Store logs and swallows write errors itself: read the file back to be sure it was written
                saved = await self._setdb_store.async_load()
                if not saved or saved.get("ts") != now:
                    raise OSError("the set database file could not be written")
            except Exception as err:  # noqa: BLE001
                self.setdb_info.update(error=str(err)[:150], ts=now - setdb.REFRESH_HOURS * 3600 + 3 * 3600)
                self.log("warning", "meta", T("set database could not be updated: {error}", error=str(err)[:150]), source=setdb.SOURCE)
                return []
            found = setdb.find_new(self.setdb, new, first=first)
            seen = self.store.setdefault("new_sets", {})
            fresh = [n for n in found if n not in seen]
            for n in fresh:
                seen[n] = now
            self.store["new_sets"] = setdb.prune_new(seen, now)
            self.setdb = new
            self.setdb_info.update(ts=now, count=len(new), error=None)
            if (gone := [n for n in self.scan if n not in new]):     # sets that left the database
                for n in gone:
                    self.scan.pop(n)
                self._scan_save()
            self.log("ok", "meta", T("set database updated: {n} sets, {new} new", n=len(new), new=len(fresh)), source=setdb.SOURCE)
            if fresh and not first:                  # the first download only fills the list, it doesn't notify
                await self.notifier.on_new_sets([n for n in fresh if not self.deal_blocked_theme(n)])
            self.push_update()
            return fresh
        finally:
            self.setdb_info["busy"] = False

    def deal_blocked_theme(self, num: str) -> bool:
        """A set in a theme switched off under Deals → Settings (also for sets that are not tracked)."""
        from .themes import key as theme_key

        off = {theme_key(x) for x in self.deal_filter["themes_off"]}
        row = self.setdb.get(num)
        theme = (self.store["sets"].get(num) or {}).get("theme") or (row[setdb.THEME] if row else "")
        return bool(theme) and theme_key(theme) in off

    def new_sets(self, limit: int = 300) -> dict[str, Any]:
        """Deals → New sets: sets that appeared in the set database, newest first, without the themes and
        piece limits switched off under Deals → Settings."""
        f = self.deal_filter
        items = []
        for num, ts in sorted(self.store.get("new_sets", {}).items(), key=lambda x: -x[1]):
            row = self.setdb.get(num)
            if not row or self.deal_blocked_theme(num):
                continue
            if (f["min_pieces"] is not None and row[setdb.PIECES] < f["min_pieces"]) or \
                    (f["max_pieces"] is not None and row[setdb.PIECES] > f["max_pieces"]):
                continue
            items.append({**setdb.as_set(num, row), "first_seen": ts, "tracked": num in self.store["sets"],
                          "owned": num in self.store["collection"], "watched": num in self.store["sets"] and self.is_watched(num)})
            if len(items) >= limit:
                break
        return {"items": items, "updated": self.setdb_info["ts"], "count": self.setdb_info["count"],
                "error": self.setdb_info["error"], "busy": self.setdb_info["busy"]}

    # ------------------------------------------------------------ deals on every LEGO set (scan.py)
    @property
    def scan_per_day(self) -> int:
        """How many sets of the database are looked up per day (0 = off)."""
        try:
            return max(0, int(self.opt(self.entry, CONF_SCAN, scan.PER_DAY_DEFAULT)))
        except (TypeError, ValueError):
            return scan.PER_DAY_DEFAULT

    def _scan_save(self) -> None:
        self._scan_store.async_delay_save(lambda: {"sets": self.scan}, 60)

    def scan_candidates(self) -> list[str]:
        """Sets of the database worth looking up (recent, not followed, not retired, not filtered out)."""
        from .themes import key as theme_key

        f = self.deal_filter
        return scan.candidates(self.setdb, self.scan, self.store["sets"], {theme_key(x) for x in f["themes_off"]},
                               f["min_pieces"], f["max_pieces"])

    @callback
    def scan_tick(self, _now: Any = None) -> None:
        """Every minute: when it is time, look up the set of the database that waited longest. The sets
        per day are spread evenly over the day (at least a minute apart)."""
        now = time.time()
        if not self.scan_per_day or self.scan_info["busy"] or now < self.scan_info["next"] or not self.setdb or not ready():
            return
        num = scan.pick(self.scan_candidates(), self.scan, self.setdb, now)
        if num is None:
            self.scan_info["next"] = now + 600                  # nothing due: look again in 10 minutes
            return
        self.scan_info.update(next=now + max(60.0, 86400 / self.scan_per_day), busy=True)
        self.entry.async_create_background_task(self.hass, self._scan_run(num), f"{DOMAIN}_scan")

    async def _scan_run(self, num: str) -> None:
        """Look up one set and always release the busy flag."""
        try:
            await self.scan_set(num)
        except Exception:  # noqa: BLE001 - the next tick simply goes on
            _LOGGER.exception("deal scan failed for %s", num)
        finally:
            self.scan_info["busy"] = False

    def _scan_source(self, num: str) -> str | None:
        """The next comparison site to ask (in turn), skipping paused sites and sites that don't cover the country."""
        if not self.compare_enabled:
            return None
        locale = self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE)
        srcs = [x for x in self.compare_sources if (compare.COMPARE_SITES.get(x) or {}).get("reader") in scan.PRICE_READERS and self.fetcher.cooldown_left(x) <= 0
                and compare.first_url(x, num, locale, None)]
        if not srcs:
            return None
        self.scan_info["src"] += 1
        return srcs[self.scan_info["src"] % len(srcs)]

    async def _scan_page(self, src: str, num: str) -> Any:
        """Read a set on one source, following the site's own links (search → product page). Returns the
        compare.Result, or None when the site could not be read (blocked, network error)."""
        url = compare.first_url(src, num, self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE), None)
        fallback = None
        for step in range(compare.MAX_STEPS):
            if not url:
                break
            status, page, error = await self.fetcher.get_page(src, url, note_block=step == 0)
            self._scan_count()
            if status in (404, 410):
                return compare.Result("missing")
            if status == 0 or status >= 400:
                if fallback is not None:
                    return fallback
                self.scan_info["error"] = f"{compare.SOURCES[src][0]}: {error or status}"[:150]
                return None
            res = compare.parse(src, page, num, url, all_domains(), step)
            if res.kind == "follow" and res.url and compare.is_compare_url(res.url) and step < compare.MAX_STEPS - 1:
                if res.shops:
                    fallback = compare.Result("offers", shops=res.shops)
                url = res.url
                continue
            if res.kind not in ("offers", "data") and fallback is not None:
                return fallback
            self.scan_info["error"] = None
            return res
        return fallback or compare.Result("missing")

    def _scan_count(self) -> None:
        day = dt_util.now().date().isoformat()
        if self.scan_info["day"] != day:
            self.scan_info.update(day=day, today=0)
        self.scan_info["today"] += 1

    async def scan_set(self, num: str) -> dict[str, Any]:
        """Look up one set of the database: its retirement now and then (market value page), and its lowest
        shop price on a comparison site. A deal is remembered and, the first time or when cheaper, notified."""
        now = time.time()
        e = self.scan.setdefault(num, {})
        if self.market_enabled and scan.needs_retired_check(e, now) and self.fetcher.cooldown_left(MARKET) <= 0:
            res = await self._scan_page(MARKET, num)
            if res is not None:
                e["rts"] = now
                d = res.data if res.kind == "data" and res.data else {}
                e["retired"] = (str(d["retired"])[:40] if d.get("retired") else None)
                if d.get("retail"):
                    e["rrp"] = d["retail"]
        if not e.get("rrp") and (cat := catalog.get(num)) and cat.get("rrp"):
            e["rrp"] = cat["rrp"]
        if not e.get("retired") and (src := self._scan_source(num)):
            res = await self._scan_page(src, num)
            e["ts"], e["src"] = now, src
            if res is not None:
                if res.rrp and not e.get("rrp") and 1 < res.rrp < 2000:
                    e["rrp"] = res.rrp
                best = scan.best_offer(res.shops if res.kind == "offers" else [], RETAILERS, e.get("rrp"))
                if best:
                    e.update(best, miss=0)
                else:
                    for k in ("price", "shop", "url"):
                        e.pop(k, None)
                    e["miss"] = e.get("miss", 0) + 1
        elif e.get("retired"):
            e["ts"] = now
        self._scan_deal(num, e, now)
        self._scan_save()
        return e

    def _scan_deal(self, num: str, e: dict[str, Any], now: float) -> None:
        """Remember whether the set is a deal now; notify a new deal, or the same deal when 2 % cheaper."""
        d = scan.deal(e, self.threshold, self.deal_filter)
        if d is None or num in self.store["sets"]:
            for k in ("deal", "deal_ts", "notified"):
                e.pop(k, None)
            return
        e["deal"] = d
        e.setdefault("deal_ts", now)
        if e.get("notified") is None or e["price"] < e["notified"] * 0.98:
            e["notified"] = e["price"]
            self.log("ok", "price", T("deal on a set you don't follow: €{price} at {shop} ({discount}% below RRP)",
                                      price=f"{e['price']:.2f}", shop=RETAILERS.get(e.get("shop"), ("?",))[0], discount=f"{d:.0f}"),
                     set_number=num, retailer=e.get("shop"), url=e.get("url"), source="scan")
            self.hass.async_create_task(self.notifier.on_catalog_deal(num, dict(e)))

    def catalog(self, q: str = "", theme: str = "", status: str = "", sort: str = "deal", offset: int = 0,
                limit: int = 120, year: int = 0) -> dict[str, Any]:
        """Deals → All LEGO sets: the whole set database with what is known about each set: followed by you,
        a deal, for sale, retired or not looked up yet."""
        statuses = (self.data or self.compute())["statuses"]
        words = q.strip().lower().split()
        rows = []
        for num, row in self.setdb.items():
            if (theme and row[setdb.THEME] != theme) or (year and row[setdb.YEAR] != year):
                continue
            if words and not all(w in f"{num} {row[setdb.NAME]} {row[setdb.THEME]} {row[setdb.SUB]}".lower() for w in words):
                continue
            tracked = num in self.store["sets"]
            e = self.scan.get(num) or {}
            if tracked:
                st = statuses.get(num, {})
                mk = (self.store["sets"][num].get("market") or {})
                stt = "retired" if (st.get("retired") or mk.get("retired")) else "followed"
                price, shop, url, rrp = st.get("best_price"), st.get("best_retailer"), st.get("best_url"), self.store["sets"][num].get("rrp")
                disc = st.get("discount_rrp")
            else:
                stt = scan.status(e)
                price, shop, url, rrp, disc = e.get("price"), e.get("shop"), e.get("url"), e.get("rrp"), scan.discount(e)
            if status and status != stt and not (status == "deal" and tracked and disc is not None and disc >= self.threshold):
                continue
            rows.append((num, row, stt, price, shop, url, rrp, disc, e, tracked))
        key = {"deal": lambda r: (-(r[7] if r[7] is not None else -999), -(r[1][setdb.YEAR] or 0)),
               "new": lambda r: (-(r[1][setdb.YEAR] or 0), r[0]),
               "price": lambda r: (r[3] is None, r[3] or 0),
               "name": lambda r: r[1][setdb.NAME].lower()}.get(sort) or (lambda r: r[0])
        rows.sort(key=key)
        items = [{**setdb.as_set(num, row), "status": stt, "price": price, "shop": RETAILERS.get(shop, (shop,))[0] if shop else None,
                  "url": url, "rrp": rrp, "discount": disc, "retired": e.get("retired"), "checked": e.get("ts"),
                  "deal_since": e.get("deal_ts"), "tracked": tracked, "owned": num in self.store["collection"],
                  "watched": tracked and self.is_watched(num)}
                 for num, row, stt, price, shop, url, rrp, disc, e, tracked in rows[offset: offset + limit]]
        counts: dict[str, int] = {}
        for e in self.scan.values():
            k = scan.status(e)
            counts[k] = counts.get(k, 0) + 1
        return {"items": items, "total": len(rows), "count": len(self.setdb), "themes": sorted({r[setdb.THEME] for r in self.setdb.values() if r[setdb.THEME]}),
                "years": sorted({r[setdb.YEAR] for r in self.setdb.values() if r[setdb.YEAR]}, reverse=True),
                "scan": {"per_day": self.scan_per_day, "candidates": len(self.scan_candidates()), "looked_up": sum(1 for e in self.scan.values() if e.get("ts")),
                         "counts": counts, "today": self.scan_info["today"] if self.scan_info["day"] == dt_util.now().date().isoformat() else 0,
                         "error": self.scan_info["error"], "compare": self.compare_enabled, "market": self.market_enabled,
                         "threshold": self.threshold}}

    def remove_set(self, set_number: str) -> None:
        num = normalize_set_number(set_number)
        self.log("info", "user", T("set removed"), set_number=num, source="panel")
        for key in ("sets", "offers", "collection"):
            self.store[key].pop(num, None)
        self.push_update()

    def set_offer(self, set_number: str, retailer: str, url: str) -> None:
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise LocalizedError("Set {number} is not tracked yet; add it first.", number=num)
        if retailer not in RETAILERS:
            raise LocalizedError("Unknown shop {shop}.", shop=retailer)
        if is_search_url(url):
            raise LocalizedError("This is a search page, not a product page. Use 🔎 Find to search it, or paste the page of the product itself.")
        url = normalize_url(retailer, url)
        rej = self.store.setdefault("rejected", {}).get(num, [])
        if url_key(retailer, url) in rej:
            rej.remove(url_key(retailer, url))
        self.store["offers"].setdefault(num, {})[retailer] = {
            "url": url, "history": [], "link_status": "confirmed", "link_reason": T("set by hand")}
        self.log("ok", "link", T("link set by hand"), set_number=num, retailer=retailer, url=url, source="panel")
        self.push_update()

    SET_FIELDS = {"name": str, "theme": str, "subtheme": str, "rrp": float, "pieces": int, "year": int,
                  "image": str, "target_price": float, "notes": str, "priority": int, "retiring": bool,
                  "exit_date": str, "watch": bool}
    COLL_FIELDS = {"qty": int, "paid": float, "current_value": float, "added": str, "condition": str,
                   "location": str}
    CLEARABLE = {"target_price", "notes", "priority", "retiring", "exit_date", "subtheme", "watch",
                 "name", "theme", "rrp", "pieces", "year", "image"}      # cleared = automatic again
    SOURCE_KEYS = {"name": "name_source", "rrp": "rrp_source", "image": "image_source", "retiring": "retiring_source",
                   "exit_date": "exit_date_source", "pieces": "pieces_source"}

    @staticmethod
    def _coerce(key: str, typ: type, value: Any) -> Any:
        """Validate a single user-supplied field. Raises ValueError with a readable message."""
        if typ is str:
            value = str(value).strip()[:200]
            if key in ("added", "exit_date") and value:
                try:
                    date.fromisoformat(value)
                except ValueError as err:
                    raise LocalizedError("{field}: invalid date {value}", field=key, value=value) from err
                if key == "added" and value > today_iso():
                    raise LocalizedError("the purchase date is in the future")
            if key == "image" and value and not value.startswith("https://"):
                raise LocalizedError("the image must be an https URL")
            return value
        if typ is bool:
            return bool(value)
        try:
            num = typ(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise LocalizedError("{field}: {value} is not a number", field=key, value=value) from err
        if not math.isfinite(num):                       # nan / inf compare false with every limit
            raise LocalizedError("{field}: {value} is not a number", field=key, value=value)
        limits = {"rrp": 10000, "paid": 10000, "current_value": 10000, "target_price": 10000, "pieces": 12000,
                  "qty": 999, "priority": 3, "year": 2100}
        if num < 0 or num > limits.get(key, 1e9):
            raise LocalizedError("{field}: {value} is out of range", field=key, value=num)
        if key == "qty" and num == 0:
            raise LocalizedError("quantity must be at least 1")
        return num

    def _clean_copies(self, items: Any) -> list[dict[str, Any]]:
        """Validate the copies of a set (1–999), each with its own price paid, purchase date, condition,
        location and notes. Raises LocalizedError."""
        from .csv_import import CONDITIONS as CONDITION_NAMES

        if not isinstance(items, list) or not 1 <= len(items) <= 999:
            raise LocalizedError("A set in your collection has between 1 and 999 copies.")
        allowed = set(CONDITION_NAMES.values())
        out = []
        for i, raw in enumerate(items, 1):
            if not isinstance(raw, dict):
                raise LocalizedError("Copy {n}: invalid data", n=i)
            c: dict[str, Any] = {}
            for key, typ in (("paid", float), ("added", str), ("location", str), ("notes", str)):
                v = raw.get(key)
                if key == "paid" and isinstance(v, str):
                    v = v.strip().replace("€", "").replace(",", ".")      # 25,50 as well as 25.50
                if v not in (None, ""):
                    try:
                        c[key] = self._coerce(key, typ, v)
                    except LocalizedError as err:
                        raise LocalizedError("Copy {n}: {error}", n=i, error=str(err)) from err
            if (cond := str(raw.get("condition") or "").strip()):
                if cond not in allowed:
                    raise LocalizedError("Copy {n}: unknown condition {value}", n=i, value=cond)
                c["condition"] = cond
            for key in ("location", "notes"):
                if key in c:
                    c[key] = c[key][:80 if key == "location" else 200]
                    if not c[key]:
                        del c[key]
            out.append(c)
        return out

    def check_set_fields(self, num: str, fields: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        """Check an edit of a set without changing anything (also for a set that isn't tracked yet): the copies,
        every field and the watchlist limit. Returns (fields without the copies, the cleaned copies or None).
        Raises LocalizedError."""
        copies_new = None
        if "copies" in fields:                         # every copy with its own price, date, condition, ...
            copies_new = self._clean_copies(fields["copies"])
            fields = {k: v for k, v in fields.items() if k not in ("copies", "owned", *self.COLL_FIELDS) or k == "current_value"}
        for key, value in fields.items():
            if (typ := self.SET_FIELDS.get(key) or self.COLL_FIELDS.get(key)) and value not in ("", None) \
                    and not (key == "watch" and value is False) and not (value == 0 and key in self.CLEARABLE):
                self._coerce(key, typ, value)
        if fields.get("watch") not in (None, False, "", 0) and not (num in self.store["sets"] and self.is_watched(num)) \
                and (limit := self.watch_limit) is not None and len(self.watched_sets()) >= limit:
            raise LocalizedError("The watchlist is full ({n} sets): remove a set or move one to your collection first.", n=limit)
        return fields, copies_new

    def update_set(self, set_number: str, fields: dict[str, Any]) -> None:
        num = normalize_set_number(set_number)
        s = self.store["sets"][num]
        refill = False
        # the whole edit is checked before anything changes: a rejected edit leaves the set as it was
        fields, copies_new = self.check_set_fields(num, fields)
        clean_set: dict[str, Any] = {}
        clean_coll: dict[str, Any] = {}
        for key, value in fields.items():
            typ = self.SET_FIELDS.get(key) or self.COLL_FIELDS.get(key)
            if typ is None:
                continue
            if key == "watch" and value is False:
                clean_set["watch"] = False                   # off the watchlist (also a set you don't own)
                continue
            if value in ("", None) or (value == 0 and key in self.CLEARABLE):
                if key in self.CLEARABLE:
                    if s.pop(key, None) is not None and key in self.SOURCE_KEYS:
                        s.pop(self.SOURCE_KEYS[key], None)
                        refill = True
                elif key in self.COLL_FIELDS and num in self.store["collection"]:
                    self.store["collection"][num].pop(key, None)
                continue
            target = clean_set if key in self.SET_FIELDS else clean_coll
            target[key] = self._coerce(key, typ, value)
        if copies_new is not None:
            entry = self.store["collection"].setdefault(num, {"qty": 1})
            entry["items"] = copies_new
            sync_copies(entry)
            self.log("info", "user", T("copies edited: {n}", n=len(copies_new)), set_number=num, source="panel")
        s.update(clean_set)
        if clean_set or clean_coll or "owned" in fields:
            self.log("info", "user", T("details edited: {fields}", fields=", ".join(sorted(set(clean_set) | set(clean_coll) | ({"owned"} if "owned" in fields else set())))),
                     set_number=num, source="panel")
        if clean_set.get("name"):
            s["name_source"] = "user"
        if "rrp" in clean_set:
            s["rrp_source"] = "user"
        if "image" in clean_set:
            s["image_source"] = "user"
        if "theme" in clean_set:
            s["theme_source"] = "user"
        if "pieces" in clean_set:
            s["pieces_source"] = "user"
        if refill and not self.job_running:     # cleared by the user: let the official shop / set data fill it again
            s.pop("lego_checked", None)
            self.entry.async_create_background_task(self.hass, self._refill(num), f"{DOMAIN}_refill_{num}")
        if fields.get("owned") is False:
            self.store["collection"].pop(num, None)
        elif clean_coll or fields.get("owned"):
            entry = self.store["collection"].setdefault(num, {"qty": 1})
            if entry.get("items"):
                # the set-level fields apply to every copy; a new quantity adds or removes copies at the end
                items = entry["items"]
                if (q := clean_coll.get("qty")) and q != len(items):      # first the new number of copies ...
                    last = {k: v for k, v in items[-1].items() if k in ("condition", "location")}
                    items = entry["items"] = items[:q] + [dict(last) for _ in range(q - len(items))]
                for k in COPY_FIELDS:                                       # ... then the fields for all of them
                    if k in clean_coll:
                        for c in items:
                            c[k] = clean_coll[k]
                entry.update({k: v for k, v in clean_coll.items() if k not in COPY_FIELDS and k != "qty"})
                sync_copies(entry)
            else:
                entry.update(clean_coll)
        self.push_update()

    BULK_FIELDS = ("location", "notes", "added", "condition", "sold")

    def bulk_change(self, set_numbers: list[str], field: str, value: Any = "", *, append: bool = False,
                    price: Any = None, sold_on: str = "", which: str = "all") -> dict[str, Any]:
        """Edit all → Bulk change: one field for every copy of the chosen sets of your collection (location, notes,
        purchase date or condition), or sell them. A sold copy leaves the collection and is kept in the sold
        ledger; a set without copies left is no longer tracked unless it is on your watchlist on purpose.
        Every set is checked on its own: a set with a wrong value is reported and left as it was."""
        if field not in self.BULK_FIELDS:
            raise LocalizedError("Unknown field {field}.", field=field)
        sale_price = None
        if field == "sold":
            if price not in (None, ""):
                raw = str(price).strip().replace("€", "").replace(",", ".")
                sale_price = round(self._coerce("paid", float, raw), 2)
            sold_on = (sold_on or today_iso()).strip()
            self._coerce("added", str, sold_on)            # a valid date, not in the future
        done, errors, removed = 0, {}, []
        statuses = (self.data or self.compute())["statuses"]
        for raw_num in set_numbers:
            num = normalize_set_number(raw_num)
            entry = self.store["collection"].get(num)
            if entry is None:
                errors[num] = T("Set {number} is not in your collection.", number=num)
                continue
            try:
                items = [dict(c) for c in copies(entry)]
                if field == "sold":
                    s = self.store["sets"].get(num, {})
                    sell = items if which == "all" else items[:1]
                    ledger = self.store.setdefault("sold", [])
                    for i, c in enumerate(sell):
                        unit, _ = copy_value(c, entry, statuses.get(num, {}), s, self.store.get("value_source") == "import_first")
                        ledger.append(sold_record(num, c, s, unit, sale_price, sold_on, seq=len(ledger) + i))
                    del ledger[: max(0, len(ledger) - SOLD_MAX)]
                    rest = items[len(sell):]
                    self.log("info", "user", T("sold: {n} copies", n=len(sell)), set_number=num, source="panel")
                    if rest:
                        entry["items"] = rest
                        sync_copies(entry)
                    else:
                        self.store["collection"].pop(num, None)
                        if s.get("watch") is not True:          # sold and not watched on purpose: no longer tracked
                            for key in ("sets", "offers"):
                                self.store[key].pop(num, None)
                            removed.append(num)
                else:
                    for c in items:
                        v = str(value or "").strip()
                        if field == "notes" and append and v:
                            v = f"{c['notes']} {v}".strip() if c.get("notes") else v
                        c[field] = v
                    entry["items"] = self._clean_copies(items)
                    sync_copies(entry)
                    self.log("info", "user", T("details edited: {fields}", fields=field), set_number=num, source="panel")
                done += 1
            except ValueError as err:
                errors[num] = str(err)
        self.push_update()
        return {"saved": done, "errors": errors, "removed": removed}

    def _sold(self, rec_id: str) -> dict[str, Any]:
        for rec in self.store.get("sold") or []:
            if rec.get("id") == rec_id:
                return rec
        raise LocalizedError("This sale is not in the list (any more).")

    def sold_update(self, rec_id: str, price: Any = None, sold_on: str | None = None) -> None:
        """Change the price or the date of a sale."""
        rec = self._sold(rec_id)
        if price is not None:
            raw = str(price).strip().replace("€", "").replace(",", ".")
            if raw:
                rec["price"] = round(self._coerce("paid", float, raw), 2)
            else:
                rec.pop("price", None)
        if sold_on:
            rec["date"] = self._coerce("added", str, sold_on)
        self.push_update()

    def sold_delete(self, rec_id: str) -> None:
        self.store["sold"].remove(self._sold(rec_id))
        self.push_update()

    async def sold_restore(self, rec_id: str) -> str:
        """Undo a sale: the copy goes back into your collection (the set is tracked again when it was removed)."""
        rec = self._sold(rec_id)
        num = rec["set_number"]
        if num not in self.store["sets"]:
            await self.add_set(num, name=rec.get("name"), theme=rec.get("theme"), owned={"qty": 1}, discover=False, watch=False)
            self.store["collection"].pop(num, None)           # filled with the restored copy below
        entry = self.store["collection"].get(num)
        items = [dict(c) for c in copies(entry)] if entry else []
        items.append({k: rec[k] for k in COPY_FIELDS if rec.get(k) not in (None, "")})
        new_entry = self.store["collection"].setdefault(num, {"qty": 1})
        new_entry["items"] = items
        sync_copies(new_entry)
        self.store["sold"].remove(rec)
        self.log("info", "user", T("sale undone"), set_number=num, source="panel")
        self.push_update()
        return num

    def sold_info(self, basis: str = "paid") -> dict[str, Any]:
        return sold_summary(self.store, basis)

    def investments(self, basis: str = "paid") -> dict[str, Any]:
        return investment_report(self.store, (self.data or self.compute())["statuses"], basis)

    async def _refill(self, num: str) -> None:
        try:
            await self.enrich_set(num)
        finally:
            self.push_update()

    def shop_detail(self, rid: str) -> dict[str, Any]:
        """Shops → click a shop: its settings, the last requests (what was asked, what came back, why it
        failed), its recent log and a plain diagnosis."""
        from .shops import SEARCH

        stats = self.retailer_stats().get(rid, {})
        site = self._site(rid)
        trace = list(reversed(self.fetcher.trace.get(rid, [])))
        offers = [o for by in self.store["offers"].values() for r, o in by.items() if r == rid]
        log = query_activity(self.store, retailer=rid, limit=40)["entries"]
        via = [((e.get("results") or {}).get(rid) or {}).get("via") for e in log if e["kind"] == "check"]
        hints = []
        if rid not in self.retailers:
            hints.append(T("This shop is switched off (Settings → Shops)."))
        if self.fetcher.cooldown_left(rid) > 0:
            hints.append(T("Paused after a block: the shop refused the requests. It is tried again automatically later."))
        searches = [x for x in trace if x["kind"] == "search"]
        js = sum(1 for x in searches if "JavaScript" in (x.get("error") or ""))
        blocked = sum(1 for x in trace if x.get("status") in (403, 429, 503) or "blocked" in (x.get("error") or ""))
        if (js and js >= len(searches) / 2) or rid in self.store.get("shop_js", {}):
            hints.append(T("The shop's search page loads its results with JavaScript, so the server sees no products there. Links still come from the shop's sitemap, and the userscript can search it in a background tab (Manage → Userscript)."))
        if blocked and blocked >= len(trace) / 2:
            hints.append(T("Most requests are blocked by the shop's bot protection. The browser relay (userscript) or a manual price helps."))
        if offers and via and all(via) and not any(x["kind"] == "page" and not x.get("error") for x in trace):
            hints.append(T("The prices of this shop come from other sources, not from the shop's own pages."))
        if not offers and not searches:
            hints.append(T("No links yet and not searched since the last restart: use Find links, or paste a product link in a set."))
        if not hints and stats.get("errors"):
            hints.append(T("Some links fail: open the failing sets below to see the error per link."))
        return {
            "id": rid, "label": RETAILERS.get(rid, (rid,))[0], "site": site, "stats": stats,
            "enabled": rid in self.retailers, "search": SEARCH.get(rid), "blocks": self.fetcher.blocks.get(rid, 0),
            "paused_until": self.fetcher.blocked_until.get(rid) if self.fetcher.cooldown_left(rid) > 0 else None,
            "last_request": self.fetcher.last_request.get(site), "next_free": self.fetcher.next_free(site),
            "next_search": self.fetcher.next_free(site, search=True), "trace": trace, "log": log, "hints": hints,
            "js": rid in self.store.get("shop_js", {}), "sitemap_ok": rid in self.sitemap_shops(),
            "sitemap": {k: v for k, v in (self.store.get("sitemaps", {}).get(rid) or {}).items() if k != "urls"}
            | {"count": len((self.store.get("sitemaps", {}).get(rid) or {}).get("urls") or []),
               "linked": sum(1 for by in self.store["offers"].values() if (by.get(rid) or {}).get("found_via") == "sitemap"),
               "busy": getattr(self, "_sitemap_busy", False)},
            "now": time.time(),
        }

    def retailer_stats(self) -> dict[str, dict[str, Any]]:
        """Summarize each retailer's offers, errors, cheapest prices, and cooldown."""
        statuses = (self.data or self.compute())["statuses"]
        out: dict[str, dict[str, Any]] = {}
        for rid, (label, _) in RETAILERS.items():
            offers = [(n, o) for n, by in self.store["offers"].items() for r, o in by.items() if r == rid]
            last_ok = max((o.get("last_ok") or 0 for _, o in offers), default=0)
            out[rid] = {
                "label": label, "enabled": rid in self.retailers, "offers": len(offers),
                "ok": sum(1 for _, o in offers if o.get("available")),
                "errors": sum(1 for _, o in offers if o.get("error")),
                "suspect": sum(1 for _, o in offers if o.get("link_status") == "suspect"),
                "cheapest": sum(1 for n, _ in offers if statuses.get(n, {}).get("best_retailer") == rid),
                "last_ok": last_ok or None,
                "paused_hours": round(self.fetcher.cooldown_left(rid) / 3600, 1),
                "failing": [{"set_number": n, "name": self.store["sets"].get(n, {}).get("name"), "error": o["error"],
                             "suspect": (o.get("suspect") or {}).get("price")}
                            for n, o in offers if o.get("error")][:50],
            }
        return out

    def report_price(self, price: float, *, url: str | None = None, set_number: str | None = None,
                     retailer: str | None = None, title: str | None = None, via: str | None = None) -> str:
        """Accept a price observed elsewhere (userscript, n8n, automation). Returns the set number."""
        if url and not retailer:
            retailer = retailer_from_url(url)
        num = normalize_set_number(set_number) if set_number else None
        found: tuple[str, str] | None = None
        if url and retailer:
            key = url_key(retailer, url)
            for n, offers in self.store["offers"].items():
                o = offers.get(retailer)
                if o and o.get("url") and url_key(retailer, o["url"]) == key and (num is None or n == num):
                    found = (n, retailer)
                    break
        if found is None and num and retailer:
            if num not in self.store["sets"]:
                raise LocalizedError("Set {number} is not tracked yet; add it first.", number=num)
            offer = self.store["offers"].setdefault(num, {}).setdefault(retailer, {"url": url or "", "history": []})
            if url and not offer.get("url"):
                offer["url"] = normalize_url(retailer, url)
            found = (num, retailer)
        source = ("relay" if via == "relay" else "userscript") if url else "panel"
        if found is None:
            self.log("warning", "userscript" if url else "user", T("price received for a product that is not tracked"),
                     url=url, retailer=retailer, price=price, source=source, set_number=num)
            raise LocalizedError("No matching link: this page is not tracked (add the set or link it first).")
        num, retailer = found
        others = [o["last_price"] for r, o in self.store["offers"][num].items() if r != retailer and o.get("available") and o.get("last_price")]
        if url and (warn := is_suspicious_price(price, self.store["sets"][num], self.store["offers"][num][retailer], others)):
            self.store["offers"][num][retailer]["suspect"] = {"price": price, "reason": warn, "ts": time.time()}
            self.log("error", "userscript", warn, set_number=num, retailer=retailer, url=url, price=price, source=source)
            raise ValueError(warn)   # already English (panel translates)
        offer = self.store["offers"][num][retailer]
        if title:   # the userscript sends the page title: lets the link check judge marketplace links too
            offer["title"] = title[:300]
            offer["link_status"], offer["link_reason"] = link_check(offer, self.store["sets"][num], num)
        before = self.compute()["statuses"].get(num, {})
        had_error = bool(offer.get("error"))
        record_price(offer, price)
        offer["last_ok"] = offer["last_checked"]
        if url:
            offer["last_via"] = source                       # 'relay' / 'userscript': shown as ⓤ next to the price
        msg = (T("price €{price} fetched by your browser (relay)", price=f"{price:.2f}") if source == "relay"
               else T("price €{price} received via Tampermonkey", price=f"{price:.2f}") if url
               else T("price €{price} entered by hand", price=f"{price:.2f}"))
        if url:          # a price from the shop page itself: the open error is solved (it leaves the error lists)
            offer["error"] = None
            offer.pop("ignored_error", None)
            offer.pop("suspect", None)
        self.log("ok", "userscript" if url else "user", msg,
                 set_number=num, retailer=retailer, url=url or offer.get("url"), price=price, source=source)
        if url and had_error:
            self.log("ok", "userscript", T("open error solved by your browser"), set_number=num, retailer=retailer,
                     url=url, source=source)
        if url:
            self.store["userscript_last"] = {"ts": time.time(), "set_number": num, "retailer": retailer, "price": price}
        self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self.push_update()
        return num

    def export_csv(self) -> str:
        return rows_to_csv(collection_rows(self.store, self.compute()["statuses"]), COLLECTION_COLUMNS)

    def export_backup(self) -> dict[str, Any]:
        return {"version": 1, "exported": today_iso(), **copy.deepcopy(self.store)}

    def import_backup(self, data: Any, merge: bool = False) -> dict[str, int]:
        clean = copy.deepcopy(validate_backup(data))
        if merge:
            for num, s in clean["sets"].items():
                self.store["sets"].setdefault(num, s)
            for num, offers in clean["offers"].items():
                self.store["offers"].setdefault(num, {}).update(
                    {r: o for r, o in offers.items() if r not in self.store["offers"].get(num, {})})
            for num, e in clean["collection"].items():
                self.store["collection"].setdefault(num, e)
            known = {r.get("id") for r in self.store.setdefault("sold", [])}
            self.store["sold"].extend(r for r in clean["sold"] if r.get("id") not in known)
        else:
            self.store = clean
        self._rename_market_source()          # a backup from before 0.9.19 still has the old label
        self.push_update()
        return {"sets": len(self.store["sets"]), "collection": len(self.store["collection"])}

    def series(self) -> list[dict[str, float]]:
        return collection_series(self.store)

    def all_themes(self) -> list[str]:
        return sorted({s.get("theme") for s in self.store["sets"].values() if s.get("theme")})
