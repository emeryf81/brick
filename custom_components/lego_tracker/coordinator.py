"""Data coordinator: owns the store, polls retailers, computes statuses."""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .client import Fetcher, lookup_metadata
from .const import (
    CONF_BRICKWATCH, CONF_COMPARE_SOURCES, COMPARE_FRESH_HOURS, COMPARE_MISSING_HOURS, COMPARE_NET_ERRORS, COMPARE_PAUSE_HOURS,
    CONF_BOL_CLIENT_ID, CONF_BOL_CLIENT_SECRET, CONF_BOL_COUNTRY, CONF_RELAY, CONF_RELAY_HOURS, DEFAULT_RELAY_HOURS,
    CONF_AUTO_REFRESH, CONF_BRICKSET_KEY, CONF_LANGUAGE, CONF_REFRESH_MODE, CONF_SPREAD_HOURS, DEFAULT_REFRESH_MODE, DEFAULT_SPREAD_HOURS, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE, DEFAULT_SEARCH, CONF_CUSTOM_SHOPS, CONF_DIGEST_TIME, CONF_NO_AUTOPAUSE, CONF_SHOP_SEARCH, CONF_VALUE_SOURCE, DEFAULT_DIGEST_TIME, GENERIC_SHOPS, CONF_DISCOUNT_THRESHOLD, CONF_REBRICKABLE_KEY, CONF_REFRESH_TIMES, CONF_IMPERSONATE, CONF_NOTIFY, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    DEFAULT_REFRESH_TIMES, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS, DEFAULT_RETAILERS,
    DOMAIN, EVENT_JOB_DONE, EVENT_HIGH_DISCOUNT, EVENT_NEW_LOW, EVENT_TARGET_HIT, RETAILERS, STORAGE_KEY,
    STORAGE_VERSION,
)
from .models import (
    add_activity, add_event, clean_history, collection_analytics, link_check, collection_rows, collection_series, is_suspicious_price, collection_summary, COLLECTION_COLUMNS, rows_to_csv, validate_backup, wishlist_summary, compute_set_status, new_store, normalize_set_number,
    offer_price, record_price, today_iso,
)
from .i18n import DEFAULT_LANGUAGE, LANGUAGES, LocalizedError, T, resolve, set_language
from .notifications import Notifier, default_rules
from .bol_api import BolApi, BolApiError
from . import compare
from .shops import all_domains
from .parsers import Parsed, title_check
from .shops import SEARCH, valid_search
from .parsers import ACCESSORY_RE, KNOCKOFF_RE, clean_title, normalize_url, retailer_from_url, url_key

_LOGGER = logging.getLogger(__name__)


class LegoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """data = {"statuses": {set: status}, "summary": {...}}"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # No polling interval: shop rounds run as background jobs (buttons or the schedule).
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.entry = entry
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self.store: dict[str, Any] = new_store()
        self.fetcher = Fetcher(hass, bool(self.opt(entry, CONF_IMPERSONATE, True)))
        self.fetcher.no_autopause = set(self.opt(entry, CONF_NO_AUTOPAUSE, []) or [])
        self._bol_api: BolApi | None = None
        self._bol_found: dict[str, dict[str, Any]] = {}
        self._alerted: set[tuple[str, str]] = set()
        self.job: dict[str, Any] | None = None
        self.last_job: dict[str, Any] | None = None
        self._job_task: asyncio.Task | None = None
        self._cancel = False
        self._job_source = "panel"
        self.notifier = Notifier(self)
        self._net_errors: dict[str, int] = {}
        self._compare_debug: dict[str, dict[str, Any]] = {}
        self._compare_retry_unsub: Callable[[], None] | None = None
        def _paused(rid: str, hours: float) -> None:
            if rid in compare.SOURCES:
                self.log("error", "shop", T("{shop} blocked us: paused for {hours} h", shop=compare.SOURCES[rid][0], hours=hours),
                         source=compare.SOURCES[rid][0])
                return
            self.log("error", "shop", T("{shop} blocked us: paused for {hours} h", shop=RETAILERS.get(rid, (rid,))[0], hours=hours), retailer=rid)
            self.hass.async_create_task(self.notifier.on_shop_paused(rid, hours))
        self.fetcher.on_pause = _paused

    # ---------------------------------------------------------------- options
    @staticmethod
    def opt(entry: ConfigEntry, key: str, default: Any) -> Any:
        return entry.options.get(key, entry.data.get(key, default))

    @property
    def threshold(self) -> float:
        return float(self.opt(self.entry, CONF_DISCOUNT_THRESHOLD, DEFAULT_DISCOUNT_THRESHOLD))

    @property
    def retailers(self) -> list[str]:
        return [r for r in self.opt(self.entry, CONF_RETAILERS, DEFAULT_RETAILERS) if r in RETAILERS]

    # ---------------------------------------------------------------- storage
    @property
    def language(self) -> str:
        return resolve(self.opt(self.entry, CONF_LANGUAGE, DEFAULT_LANGUAGE), self.hass.config.language)

    async def async_load(self) -> None:
        set_language(self.language)
        await self.fetcher.async_setup()
        if (data := await self._store.async_load()):
            self.store = {**new_store(), **data}
        if "brickwatch" in self.store:                         # 0.9.4: Brickwatch was the only comparison site
            self.store.setdefault("compare", {}).setdefault("brickwatch", self.store.pop("brickwatch") or {})
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

    def _save(self) -> None:
        # pauses survive restarts/reloads, otherwise a reload would hammer a shop that just blocked us
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

    def push_update(self) -> None:
        """Recompute without hitting the network (after edits/imports)."""
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

    @property
    def refresh_mode(self) -> str:
        mode = self.opt(self.entry, CONF_REFRESH_MODE, None)
        if mode is None:   # older installs: auto_refresh off = off, else the new spread mode
            mode = DEFAULT_REFRESH_MODE if self.opt(self.entry, CONF_AUTO_REFRESH, True) else "off"
        return mode if mode in ("spread", "times", "off") else DEFAULT_REFRESH_MODE

    @property
    def auto_refresh(self) -> bool:
        return self.refresh_mode == "times" and bool(self.refresh_times)

    @property
    def spread_hours(self) -> float:
        try:
            return max(1.0, min(168.0, float(self.opt(self.entry, CONF_SPREAD_HOURS, DEFAULT_SPREAD_HOURS))))
        except (TypeError, ValueError):
            return DEFAULT_SPREAD_HOURS

    # ------------------------------------------------------------ spread checks
    def _spread_candidates(self) -> list[str]:
        if self.brickwatch_enabled:          # Brickwatch can also find prices for sets without links
            return list(self.store["sets"])
        live = set(self._live_retailers(False))
        return [n for n, offers in self.store["offers"].items() if n in self.store["sets"]
                and any(r in live and o.get("url") for r, o in offers.items())]

    def spread_interval(self) -> float:
        """Seconds between two set checks: cycle / number of sets (each set once per cycle)."""
        n = len(self.store["sets"]) if self.brickwatch_enabled else len([n for n, o in self.store["offers"].items() if n in self.store["sets"] and o])
        return max(20.0, self.spread_hours * 3600 / max(1, n))

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
        num = min(cands, key=lambda n: self.store["sets"][n].get("checked", 0))
        if time.time() - self.store["sets"][num].get("checked", 0) < self.spread_hours * 3600 * 0.5:
            return None        # everything was checked recently (e.g. after a full manual round)
        return num

    async def _spread_tick(self, _now: Any = None) -> None:
        from homeassistant.helpers.event import async_call_later

        self._spread_unsub = None
        try:
            if not self.job_running and (num := self.next_spread_set()):
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
            info.update(cycle_hours=self.spread_hours, interval=round(interval), per_hour=round(3600 / interval, 1),
                        next=getattr(self, "_spread_next", None), next_set=self.next_spread_set(),
                        last=getattr(self, "_spread_last", None),
                        checked_24h=sum(1 for s in self.store["sets"].values() if s.get("checked", 0) > day),
                        total=len(self._spread_candidates()))
        return info

    # ------------------------------------------------------------------ logbook
    def log(self, level: str, kind: str, message: str, **fields: Any) -> None:
        """Everything the integration does ends up here (Beheer → Logboek)."""
        fields.setdefault("source", "server")
        add_activity(self.store, level, kind, message, **fields)

    # ---------------------------------------------------------------------- jobs
    def job_info(self) -> dict[str, Any]:
        return {"job": dict(self.job) if self.job else None, "last": self.last_job,
                "paused": {RETAILERS[r][0]: h for r, h in self.fetcher.paused().items() if r in RETAILERS},
                "schedule": self.schedule_info()}

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
                self.log("ok" if not st["err"] else "warning" if st["ok"] else "error", "fetch",
                         T("{shop}: {ok} succeeded, {failed} failed", shop=RETAILERS.get(rid, (rid,))[0], ok=st["ok"], failed=st["err"]),
                         retailer=rid)
            if job["kind"] == "refresh":
                self._snapshot()
            if job["kind"] in ("enrich", "update"):
                self.verify_links()      # new RRPs from LEGO.com: re-judge links, drop impossible prices
            self.push_update()
            self.hass.async_create_task(self.notifier.on_job_done(dict(job)))
            self.hass.bus.async_fire(EVENT_JOB_DONE, {k: job[k] for k in ("kind", "total", "done", "found", "updated",
                                                                         "errors", "skipped", "cancelled")})

    # ------------------------------------------------------------ bol.com API
    @property
    def bol_country(self) -> str:
        c = str(self.opt(self.entry, CONF_BOL_COUNTRY, "auto") or "auto").upper()
        if c in ("NL", "BE"):
            return c
        return "BE" if str(self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE)).lower().endswith("-be") else "NL"

    @property
    def bol_api(self) -> BolApi | None:
        """The official bol.com API when the user entered affiliate credentials (else: scraping)."""
        cid, secret = self.opt(self.entry, CONF_BOL_CLIENT_ID, ""), self.opt(self.entry, CONF_BOL_CLIENT_SECRET, "")
        if not (cid and secret):
            return None
        if self._bol_api is None:
            self._bol_api = BolApi(async_get_clientsession(self.hass), cid, secret, self.bol_country)
        return self._bol_api

    async def _bol_match(self, num: str, url: str | None = None) -> dict[str, Any] | None:
        """Find the set in the bol.com catalog: title must pass the link check; same product id wins."""
        found = [p for p in await self.bol_api.search(f"LEGO {num}") if title_check(p["title"], num)[0] == "ok"]
        pid = re.search(r"/(\d{13,17})/?", url or "")
        for p in found:
            if pid and p.get("url") and pid.group(1) in p["url"]:
                return p
        return found[0] if found else None

    async def _discover(self, rid: str, num: str, force: bool = False) -> str | None:
        if rid == "bol" and self.bol_api:
            try:
                match = await self._bol_match(num)
            except BolApiError as err:
                self.log("error", "discover", str(err), set_number=num, retailer=rid, source="bol.com API")
                return None
            if not match:
                return None
            self._bol_found[num] = match
            return match.get("url") or f"https://www.bol.com/{'be' if self.bol_country == 'BE' else 'nl'}/nl/s/?searchtext={match['ean']}"
        return await self.fetcher.discover(rid, num, force=force)

    async def _fetch(self, rid: str, offer: dict[str, Any], num: str, force: bool = False) -> tuple[Any, str | None]:
        if compare.is_compare_url(offer.get("url")):
            return None, T("no comparison-site price for this shop")
        if rid == "bol" and self.bol_api:
            try:
                ean, title, image = offer.get("ean"), offer.get("title"), None
                if not ean:
                    match = self._bol_found.pop(num, None) or await self._bol_match(num, offer.get("url"))
                    if not match:
                        return None, T("bol.com API: set not found in the catalog")
                    ean, title, image = match["ean"], match["title"], match.get("image")
                    offer["ean"] = ean
                    if num in self.store["sets"] and str(ean).isdigit():
                        self.store["sets"][num].setdefault("ean", str(ean).zfill(13))   # also used by Producthero
                price = await self.bol_api.best_price(ean)
                return Parsed(price=price, title=title, image=image, unavailable=price is None), None
            except BolApiError as err:
                return None, str(err)
        return await self.fetcher.fetch_offer(rid, offer["url"], force=force)

    # ------------------------------------------------------------ price-comparison sites (hidden option)
    @property
    def brickwatch_enabled(self) -> bool:
        """The hidden option: price-comparison sites (Brickwatch and others) as extra price sources."""
        return bool(self.opt(self.entry, CONF_BRICKWATCH, False))

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
        for src in sources or self.compare_sources:
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
        if entry and entry.get("status") == "ok" and now - entry["ts"] < COMPARE_FRESH_HOURS * 3600 and not refresh:
            return entry
        if not force and self.fetcher.cooldown_left(src) > 0:
            return entry if entry and entry.get("status") == "ok" else None
        s = self.store["sets"].get(num) or {}
        url = compare.first_url(src, num, self.opt(self.entry, CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE), s.get("ean"))
        if not url:
            return None                                      # site doesn't cover this country / needs an EAN
        step, kind = 0, "error"
        while url:
            status, page, error = await self.fetcher.get_page(src, url, force=True)
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

        def fail(msg: str) -> tuple[str, None]:
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
            res = compare.parse(src, page, num, url, all_domains(), step)
        if res.kind == "follow" and res.url and compare.is_compare_url(res.url) and step < compare.MAX_STEPS - 1:
            return "follow", res.url
        if res.kind != "offers" and res.note == "js":
            msg = T("{source} loads its results with JavaScript: this page has no results to read", source=name)
            st[num] = {"status": "unreadable", "ts": now, "url": url, "error": msg}
            self.log("warning", "fetch", msg, set_number=num, url=url, source=name)
            return "missing", None
        if res.kind != "offers":
            st[num] = {"status": "missing", "ts": now, "url": url, "note": res.note}
            self.log("info", "fetch", T("not on {source}: next try tomorrow", source=name), set_number=num, url=url, source=name)
            return "missing", None
        st[num] = {"status": "ok", "ts": now, "url": url, "name": res.name, "image": res.image, "rrp": res.rrp,
                   "ean": res.ean, "shops": res.shops, "via": via}
        s = self.store["sets"].get(num)
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
                if is_suspicious_price(shop["price"], self.store["sets"][num], o):
                    continue
                record_price(o, shop["price"])
                o["last_ok"], o["via"] = o["last_checked"], shop["source"]
                n += 1
        return n

    def start_brickwatch(self, nums: list[str] | None = None) -> dict[str, Any]:
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
        return self.start_job("brickwatch", T("Fetching prices from comparison sites"), items, work)

    def _compare_retry(self, rest: list[str]) -> None:
        from homeassistant.helpers.event import async_call_later

        wait = max([self.fetcher.cooldown_left(src) for src in self.compare_sources] + [60.0])
        self.log("warning", "job", T("All comparison sites are paused: stopped, the other {n} sets follow in {minutes} min",
                                     n=len(rest), minutes=round(wait / 60)))
        if self._compare_retry_unsub:
            self._compare_retry_unsub()

        def _go(_now: Any) -> None:
            self._compare_retry_unsub = None
            if not self.brickwatch_enabled:
                return
            if self.job_running:
                self._compare_retry_unsub = async_call_later(self.hass, 600, _go)
                return
            self._job_source = "auto"
            self.start_brickwatch(rest)
        self._compare_retry_unsub = async_call_later(self.hass, wait + 30, _go)

    def _live_retailers(self, force: bool) -> list[str]:
        if force:
            self.fetcher.reset_cooldowns()
        return [r for r in self.retailers if self.fetcher.cooldown_left(r) <= 0]

    # ------------------------------------------------------------------- refresh
    def start_refresh(self, force: bool = False) -> dict[str, Any]:
        live = self._live_retailers(force)
        nums = list(self.store["sets"]) if self.brickwatch_enabled else [
            n for n, offers in self.store["offers"].items()
            if n in self.store["sets"] and any(r in live and o.get("url") for r, o in offers.items())]
        paused = [RETAILERS[r][0] for r in self.retailers if r not in live]
        note = T("paused and skipped: {shops}", shops=", ".join(paused)) if paused else None
        return self.start_job("refresh", T("Refreshing shop prices"), nums,
                              lambda n: self.refresh_set(n, live), note)

    async def refresh_set(self, num: str, retailers: list[str] | None = None, source: str = "server",
                          force: bool = False) -> dict[str, int]:
        """Fetch all shop pages of one set, shops in parallel (each shop stays sequential and polite).

        Writes one 'check' entry to the logbook with a green/red result per shop, plus separate
        'price' entries for changes. A manual price always wins over the automatic one."""
        live = retailers if retailers is not None else self._live_retailers(False)
        s = self.store["sets"][num]
        bw_prices: dict[str, dict[str, Any]] = {}
        if self.brickwatch_enabled:
            try:
                await self.compare_refresh(num, force=force)
                bw_prices = self.compare_prices(num)
            except Exception:  # noqa: BLE001 - an extra source must never break the check
                _LOGGER.exception("Comparison sites failed for %s", num)
        before = self.compute()["statuses"].get(num, {})
        offers = [(rid, o) for rid, o in self.store["offers"].get(num, {}).items()
                  if (rid in live or (rid in bw_prices and rid in self.retailers)) and o.get("url") and o.get("link_status") != "rejected"]
        results = await asyncio.gather(*(self._fetch(rid, o, num, force) for rid, o in offers))
        counts = {"updated": 0, "errors": 0, "skipped": 0}
        shop_results: dict[str, dict[str, Any]] = {}
        for (rid, offer), (parsed, error) in zip(offers, results):
            via = None
            if (bwp := bw_prices.get(rid)) and (error or not parsed or parsed.price is None):
                parsed, error, via = Parsed(price=bwp["price"], title=offer.get("title")), None, bwp["source"]
            if error and error.startswith("paused"):
                counts["skipped"] += 1        # keep the last known price, just skip
                shop_results[rid] = {"ok": None, "error": error}
                continue
            if parsed and parsed.title:
                offer["title"] = parsed.title[:300]
            status, reason = link_check(offer, s, num)
            offer["link_status"], offer["link_reason"] = status, reason
            price = parsed.price if parsed else None
            if price is not None and (warn := is_suspicious_price(price, s, offer)):
                price, error = None, warn
            manual = offer.get("manual_price")
            if manual:                        # manual price has priority: only remember what the shop said
                offer["auto_price"], offer["last_checked"], offer["error"] = price, time.time(), error
            else:
                old_price = offer.get("last_price") if offer.get("available") else None
                record_price(offer, price, error=error)
                if price is not None and (old_price is None or abs(old_price - price) >= 0.01):
                    self.log("ok", "price", T("€{old} → €{new}", old=f"{old_price:.2f}", new=f"{price:.2f}") if old_price
                             else T("first price €{price}", price=f"{price:.2f}"),
                             set_number=num, retailer=rid, url=offer.get("url"), price=price, old_price=old_price, source=source)
            shop_results[rid] = {"ok": not error, "price": price, "error": error, "manual": bool(manual)}
            if via:
                shop_results[rid]["via"] = via
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

    async def fetch_shop(self, set_number: str, retailer: str) -> dict[str, Any]:
        """Panel button per shop: fetch this shop for this set now, also when the shop is paused.
        Without a link the shop is searched first. LEGO.com also fills in image, RRP and name."""
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise LocalizedError("Set {number} is not tracked.", number=num)
        if retailer not in RETAILERS:
            raise LocalizedError("Unknown shop {shop}.", shop=retailer)
        offers = self.store["offers"].setdefault(num, {})
        found = False
        if not (offers.get(retailer) or {}).get("url"):
            url = await self._discover(retailer, num, force=True)
            rejected = set(self.store.setdefault("rejected", {}).get(num, []))
            if not url or url_key(retailer, url) in rejected:
                reason = T("found a link you rejected earlier; not linked again") if url else T("no matching product found")
                self.log("warning", "discover", reason, set_number=num, retailer=retailer, url=url, source="panel")
                self.push_update()
                return {"ok": False, "found": False, "error": reason}
            offers[retailer] = {"url": url, "history": [], "found": time.time()}
            if retailer == "bol" and num in self._bol_found:
                offers[retailer]["ean"] = self._bol_found[num]["ean"]
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
        return self.start_job("discover", T("Finding missing shop links"), nums,
                              lambda n: self.discover_set(n, live),
                              T("paused and skipped: {shops}", shops=", ".join(paused)) if paused else None)

    async def discover_set(self, num: str, retailers: list[str] | None = None) -> dict[str, int]:
        live = retailers if retailers is not None else self._live_retailers(False)
        todo = self._missing(num, live)
        urls = await asyncio.gather(*(self._discover(r, num) for r in todo))
        rejected = set(self.store.setdefault("rejected", {}).get(num, []))
        found = 0
        for rid, url in zip(todo, urls):
            if url and url_key(rid, url) not in rejected:
                self.store["offers"].setdefault(num, {})[rid] = {"url": url, "history": [], "found": time.time()}
                if rid == "bol" and num in self._bol_found:
                    self.store["offers"][num][rid]["ean"] = self._bol_found[num]["ean"]
                found += 1
                self.log("ok", "discover", T("link found"), set_number=num, retailer=rid, url=url)
            elif url:
                self.log("info", "discover", T("found a link you rejected earlier; not linked again"), set_number=num, retailer=rid, url=url)
            elif self.job and self.job.get("running"):
                st = self.job["shops"].setdefault(rid, {"ok": 0, "err": 0})
                st["err"] += 1                  # summarised per shop at the end of the job
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
        server or weren't fetched in the last 20 h, oldest first. bol.com via the API is left out."""
        items: list[tuple[float, dict[str, Any]]] = []
        if self.relay_enabled:
            day = time.time() - 20 * 3600
            for num, offers in self.store["offers"].items():
                if num not in self.store["sets"]:
                    continue
                for rid, o in offers.items():
                    if (rid not in self.retailers or rid == "lego_com" or not o.get("url") or o.get("manual_price")
                            or o.get("link_status") == "rejected" or (rid == "bol" and self.bol_api)):
                        continue
                    last = o.get("last_ok") or 0
                    if o.get("error") or last < day:
                        items.append((last, {"set_number": num, "retailer": rid, "shop": RETAILERS[rid][0], "url": o["url"]}))
            if self.brickwatch_enabled:
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

    def relay_result(self, item: dict[str, Any]) -> str:
        """One page fetched by the user's browser: a price (stored like the userscript) or a failure.
        A comparison-site page comes back as HTML and is read here ('follow' adds the next page to fetch)."""
        if item.get("kind") == "page":
            return self._relay_page(item)
        url, price, error = item.get("url"), item.get("price"), item.get("error")
        rid = item.get("retailer") or (retailer_from_url(url) if url else None)
        num = normalize_set_number(item["set_number"]) if item.get("set_number") else None
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
        src, url = item.get("source"), str(item.get("url") or "")
        num = normalize_set_number(str(item.get("set_number") or ""))
        if src not in compare.SOURCES or not compare.is_compare_url(url) or num not in self.store["sets"]:
            raise ValueError(f"{src}: {url}: not a comparison page of a tracked set")
        html = item.get("html") if isinstance(item.get("html"), str) else ""
        try:
            status, step = int(item.get("status") or 0), max(0, min(compare.MAX_STEPS - 1, int(item.get("step") or 0)))
        except (TypeError, ValueError):
            status, step = 0, 0
        if status == 0:        # the browser couldn't reach it either: log, but don't count it towards a server pause
            self.log("warning", "userscript", T("your browser could not fetch the price either: {error}", error=str(item.get("error") or "")[:120] or "?"),
                     set_number=num, url=url, source="relay")
            kind, nxt = "error", None
        else:
            kind, nxt = self.compare_page(src, num, url, status, html, None, step, via="relay")
        if kind == "ok":
            self._compare_links(num)
            self._compare_apply_prices(num)
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
        if src in ("user", "import") or (src and src[0].isupper() or src == "brickset.com" or "+" in (src or "")):
            return False
        # names from older versions: replace the ones that look like a shop title
        return bool(re.search(r"\blego\b", name, re.I) or ACCESSORY_RE.search(name) or KNOCKOFF_RE.search(name)
                    or len(name) > 70)

    def needs_enrich(self, num: str) -> bool:
        s = self.store["sets"][num]
        lego_missing = s.get("rrp_source") not in ("LEGO.com", "user") or s.get("image_source") not in ("LEGO.com", "user")
        lego_due = lego_missing and time.time() - s.get("lego_checked", 0) > 7 * 86400
        return lego_due or self._name_replaceable(s) or not all(s.get(k) for k in ("theme", "year", "pieces", "image"))

    def _apply_lego(self, num: str, parsed: Any) -> bool:
        """LEGO.com is the first source for RRP, image and name. Values the user typed win."""
        s = self.store["sets"][num]
        before = (s.get("rrp"), s.get("image"), s.get("name"), s.get("retiring"))
        if parsed.list_price and s.get("rrp_source") != "user":
            s["rrp"], s["rrp_source"] = round(parsed.list_price, 2), "LEGO.com"
        if parsed.image and parsed.image.startswith("https://") and s.get("image_source") != "user":
            s["image"], s["image_source"] = parsed.image, "LEGO.com"
        if parsed.title and (self._name_replaceable(s) or s.get("name_source") == "LEGO.com"):
            s["name"], s["name_source"] = clean_title(parsed.title, num), "LEGO.com"
        if parsed.retiring:
            s["retiring"], s["retiring_source"] = True, "LEGO.com"
        elif s.get("retiring_source") == "LEGO.com":
            s.pop("retiring", None)
            s.pop("retiring_source", None)
        s["lego_checked"] = time.time()
        return before != (s.get("rrp"), s.get("image"), s.get("name"), s.get("retiring"))

    async def lego_lookup(self, num: str, force: bool = False) -> bool:
        """Find + read the set's LEGO.com page (also kept as a 'LEGO.com' shop link)."""
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
        parsed, error = await self.fetcher.fetch_offer("lego_com", url, force=force)
        if error and error.startswith("paused"):
            return False
        offer["link_status"], offer["link_reason"] = link_check(offer, s, num)
        if parsed and parsed.title:
            offer["title"] = parsed.title[:300]
        record_price(offer, parsed.price if parsed else None, error=error)
        if parsed and parsed.price:
            offer["last_ok"] = offer["last_checked"]
        if error:
            self.log("error", "fetch", error, set_number=num, retailer="lego_com", url=url)
        return self._apply_lego(num, parsed) if parsed else False

    def start_enrich(self, all_sets: bool = False) -> dict[str, Any]:
        nums = [n for n in self.store["sets"] if all_sets or self.needs_enrich(n)]
        has_key = bool(self.opt(self.entry, CONF_BRICKSET_KEY, "") or self.opt(self.entry, CONF_REBRICKABLE_KEY, ""))
        return self.start_job("enrich", T("Filling in set data"), nums, self.enrich_set,
                              None if has_key else T("no API key set: public Brickset pages are used"))

    async def enrich_set(self, num: str, force: bool = False) -> dict[str, int]:
        s = self.store["sets"][num]
        lego_changed = await self.lego_lookup(num, force=force)       # 1st source: RRP, image, name
        meta, source = await lookup_metadata(async_get_clientsession(self.hass),
                                             self.opt(self.entry, CONF_BRICKSET_KEY, ""),
                                             self.opt(self.entry, CONF_REBRICKABLE_KEY, ""), num)
        await asyncio.sleep(1.0)   # be gentle with the metadata sources
        if not meta:
            return {"updated": 1} if lego_changed else {"errors": 1}
        changed = lego_changed
        if meta.get("name") and self._name_replaceable(s) and s.get("name") != meta["name"]:
            s["name"], s["name_source"] = meta["name"], source
            changed = True
        for key in ("theme", "subtheme", "year", "pieces", "image", "rrp", "exit_date"):
            if meta.get(key) and not s.get(key):
                s[key] = meta[key]
                if key in self.SOURCE_KEYS:
                    s[self.SOURCE_KEYS[key]] = source or "meta"
                changed = True
        if changed:
            self.log("ok", "meta", T("set data filled in from {source}", source=source or "LEGO.com"), set_number=num,
                     source=(source or "LEGO.com"))
        return {"updated": 1} if changed else {}

    # ------------------------------------------------------------ update (CSV)
    def start_update(self, nums: list[str], force: bool = False) -> dict[str, Any]:
        """After a CSV re-import: per set fill in metadata, find missing links and fetch prices."""
        live = self._live_retailers(force)

        async def work(num: str) -> dict[str, int]:
            out = {"updated": 0, "found": 0, "errors": 0, "skipped": 0}
            if self.needs_enrich(num):
                out["updated"] += (await self.enrich_set(num)).get("updated", 0)
            out["found"] += (await self.discover_set(num, live))["found"]
            res = await self.refresh_set(num, live)
            out["errors"] += res["errors"]
            out["skipped"] += res["skipped"]
            return out

        nums = [n for n in dict.fromkeys(nums) if n in self.store["sets"]]
        return self.start_job("update", T("Updating collection"), nums, work)

    # ---------------------------------------------------------------- settings
    SECRET_KEYS = (CONF_BRICKSET_KEY, CONF_REBRICKABLE_KEY, CONF_BOL_CLIENT_ID, CONF_BOL_CLIENT_SECRET)

    def settings_get(self) -> dict[str, Any]:
        o = {**self.entry.data, **self.entry.options}
        mask = lambda v: f"••••{v[-4:]}" if v and len(v) > 4 else ("••••" if v else "")  # noqa: E731
        shops = []
        for rid, (label, _) in RETAILERS.items():
            shops.append({
                "id": rid, "label": label, "builtin": not rid.startswith("c_"),
                "generic": rid in GENERIC_SHOPS, "domain": GENERIC_SHOPS.get(rid, {}).get("domain"),
                "search": SEARCH.get(rid, ""), "default_search": DEFAULT_SEARCH.get(rid, GENERIC_SHOPS.get(rid, {}).get("search", "")),
                "enabled": rid in self.retailers,
                "paused_hours": round(self.fetcher.cooldown_left(rid) / 3600, 2),
                "blocks": self.fetcher.blocks.get(rid, 0), "autopause": rid not in self.fetcher.no_autopause,
            })
        return {
            "discount_threshold": self.threshold, "min_history_days": int(self.opt(self.entry, CONF_MIN_HISTORY_DAYS, DEFAULT_MIN_HISTORY_DAYS)),
            "auto_refresh": bool(o.get(CONF_AUTO_REFRESH, True)), "refresh_times": ", ".join(f"{h:02d}:{m:02d}" for h, m in self.refresh_times),
            "digest_time": str(o.get(CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME))[:5], "use_impersonation": bool(o.get(CONF_IMPERSONATE, True)),
            "notify_service": o.get(CONF_NOTIFY, "") or "", "value_source": o.get(CONF_VALUE_SOURCE, "shop_first"),
            "keys": {k: {"set": bool(o.get(k)), "masked": mask(o.get(k) or "")} for k in self.SECRET_KEYS},
            "shops": shops, "transport": self.fetcher.transport,
            "lego_locale": o.get(CONF_LEGO_LOCALE, DEFAULT_LEGO_LOCALE),
            "refresh_mode": self.refresh_mode, "spread_hours": self.spread_hours,
            "language": o.get(CONF_LANGUAGE, DEFAULT_LANGUAGE), "languages": LANGUAGES,
            "bol_country": o.get(CONF_BOL_COUNTRY, "auto"), "bol_api": bool(self.bol_api),
            "browser_relay": bool(o.get(CONF_RELAY, True)), "brickwatch": bool(o.get(CONF_BRICKWATCH, False)),
            "compare_sources": self.compare_sources, "relay_hours": int(o.get(CONF_RELAY_HOURS, DEFAULT_RELAY_HOURS)),
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
        for key in ("auto_refresh", "use_impersonation", CONF_RELAY, CONF_BRICKWATCH):
            if key in fields:
                opts[key] = bool(fields[key])
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
                if val and not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}" if key in (CONF_BRICKSET_KEY, CONF_REBRICKABLE_KEY) else r"[\x21-\x7e]{8,256}", val):
                    raise LocalizedError("{field}: invalid key", field=key)
                opts[key] = val
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
        if "bol_country" in fields:
            if str(fields["bol_country"]) not in ("auto", "NL", "BE"):
                raise LocalizedError("{field}: invalid value", field="bol_country")
            opts[CONF_BOL_COUNTRY] = str(fields["bol_country"])
        if "relay_hours" in fields:
            num(CONF_RELAY_HOURS, 1, 168)
        if "lego_locale" in fields:
            loc = str(fields["lego_locale"] or "").strip().lower()
            if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", loc):
                raise LocalizedError("LEGO.com country like en-gb, nl-be, de-de")
            opts[CONF_LEGO_LOCALE] = loc
        valid_ids = set(RETAILERS) | {s["id"] for s in opts.get(CONF_CUSTOM_SHOPS, [])}
        if "retailers" in fields:
            opts[CONF_RETAILERS] = [r for r in fields["retailers"] if r in valid_ids]
        if "no_autopause" in fields:
            opts[CONF_NO_AUTOPAUSE] = [r for r in fields["no_autopause"] if r in valid_ids]
        return opts

    async def test_bol(self, client_id: str | None = None, secret: str | None = None) -> tuple[bool, str]:
        """Settings test button: log in and look up set 10281 in the bol.com catalog."""
        cid = client_id or self.opt(self.entry, CONF_BOL_CLIENT_ID, "")
        sec = secret or self.opt(self.entry, CONF_BOL_CLIENT_SECRET, "")
        if not (cid and sec):
            return False, T("no key entered")
        api = BolApi(async_get_clientsession(self.hass), cid, sec, self.bol_country)
        try:
            found = [p for p in await api.search("LEGO 10281") if title_check(p["title"], "10281")[0] == "ok"]
        except BolApiError as err:
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
        s = self.store["sets"][num]
        payload = {"set_number": num, "name": s.get("name"), "theme": s.get("theme"),
                   "price": after.get("best_price"), "retailer": after.get("best_retailer"),
                   "url": after.get("best_url"), "discount": after.get("discount_rrp"),
                   "target_price": s.get("target_price")}
        day = today_iso()
        for flag, event in (("is_all_time_low", EVENT_NEW_LOW), ("high_discount", EVENT_HIGH_DISCOUNT),
                            ("target_hit", EVENT_TARGET_HIT)):
            key = (num, flag + day)
            if after.get(flag) and not before.get(flag) and key not in self._alerted:
                self._alerted.add(key)
                self.hass.bus.async_fire(event, payload)
                add_event(self.store, flag, {k: payload[k] for k in ("set_number", "name", "price", "retailer")}
                          | {"discount": payload["discount"]})
        if before.get("best_price") != after.get("best_price") or any(
                before.get(k) != after.get(k) for k in ("is_all_time_low", "target_hit", "retiring_soon", "deal_score")):
            self.hass.async_create_task(self.notifier.on_set_change(num, dict(before), dict(after)))

    # ------------------------------------------------------------------ edits
    async def add_set(self, set_number: str, *, name: str | None = None, theme: str | None = None,
                      subtheme: str | None = None, rrp: float | None = None, pieces: int | None = None, target_price: float | None = None,
                      owned: dict | None = None, discover: bool = True) -> str:
        num = normalize_set_number(set_number)
        s = self.store["sets"].setdefault(num, {"set_number": num})
        meta, source = await lookup_metadata(async_get_clientsession(self.hass),
                                             self.opt(self.entry, CONF_BRICKSET_KEY, ""),
                                             self.opt(self.entry, CONF_REBRICKABLE_KEY, ""), num)
        if name:
            s["name_source"] = "user"
        elif meta.get("name") and self._name_replaceable(s):
            s["name"], s["name_source"] = meta["name"], source
        for key, val in {"name": name, "theme": theme, "subtheme": subtheme, "rrp": rrp, "pieces": pieces,
                         "target_price": target_price}.items():
            if val:
                s[key] = val
        for key in ("name", "theme", "subtheme", "year", "pieces", "image", "rrp", "exit_date"):
            if meta.get(key) and not s.get(key):
                s[key] = meta[key]
        self.store["offers"].setdefault(num, {})
        if owned is not None:
            self.store["collection"][num] = owned
        if discover:
            await self.discover_set(num)
        self.push_update()
        return num

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
                  "exit_date": str}
    COLL_FIELDS = {"qty": int, "paid": float, "current_value": float, "added": str, "condition": str,
                   "location": str}
    CLEARABLE = {"target_price", "notes", "priority", "retiring", "exit_date", "subtheme",
                 "name", "theme", "rrp", "pieces", "year", "image"}      # cleared = automatic again
    SOURCE_KEYS = {"name": "name_source", "rrp": "rrp_source", "image": "image_source", "retiring": "retiring_source"}

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
        except (TypeError, ValueError) as err:
            raise LocalizedError("{field}: {value} is not a number", field=key, value=value) from err
        limits = {"rrp": 10000, "paid": 10000, "current_value": 10000, "target_price": 10000, "pieces": 12000,
                  "qty": 999, "priority": 3, "year": 2100}
        if num < 0 or num > limits.get(key, 1e9):
            raise LocalizedError("{field}: {value} is out of range", field=key, value=num)
        if key == "qty" and num == 0:
            raise LocalizedError("quantity must be at least 1")
        return num

    def update_set(self, set_number: str, fields: dict[str, Any]) -> None:
        num = normalize_set_number(set_number)
        s = self.store["sets"][num]
        refill = False
        clean_set: dict[str, Any] = {}
        clean_coll: dict[str, Any] = {}
        for key, value in fields.items():
            typ = self.SET_FIELDS.get(key) or self.COLL_FIELDS.get(key)
            if typ is None:
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
        if refill and not self.job_running:     # cleared by the user: let LEGO.com/Brickset fill it again
            s.pop("lego_checked", None)
            self.entry.async_create_background_task(self.hass, self._refill(num), f"{DOMAIN}_refill_{num}")
        if fields.get("owned") is False:
            self.store["collection"].pop(num, None)
        elif clean_coll or fields.get("owned"):
            self.store["collection"].setdefault(num, {"qty": 1}).update(clean_coll)
        self.push_update()

    async def _refill(self, num: str) -> None:
        try:
            await self.enrich_set(num)
        finally:
            self.push_update()

    def retailer_stats(self) -> dict[str, dict[str, Any]]:
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
                "failing": [{"set_number": n, "name": self.store["sets"].get(n, {}).get("name"), "error": o["error"]}
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
        if url and (warn := is_suspicious_price(price, self.store["sets"][num], self.store["offers"][num][retailer])):
            self.log("error", "userscript", warn, set_number=num, retailer=retailer, url=url, price=price, source=source)
            raise ValueError(warn)   # already English (panel translates)
        offer = self.store["offers"][num][retailer]
        if title:   # the userscript sends the page title: lets the link check judge Amazon links too
            offer["title"] = title[:300]
            offer["link_status"], offer["link_reason"] = link_check(offer, self.store["sets"][num], num)
        before = self.compute()["statuses"].get(num, {})
        record_price(offer, price)
        offer["last_ok"] = offer["last_checked"]
        msg = (T("price €{price} fetched by your browser (relay)", price=f"{price:.2f}") if source == "relay"
               else T("price €{price} received via Tampermonkey", price=f"{price:.2f}") if url
               else T("price €{price} entered by hand", price=f"{price:.2f}"))
        if url:
            offer["error"] = None
        self.log("ok", "userscript" if url else "user", msg,
                 set_number=num, retailer=retailer, url=url or offer.get("url"), price=price, source=source)
        if url:
            self.store["userscript_last"] = {"ts": time.time(), "set_number": num, "retailer": retailer, "price": price}
        self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self.push_update()
        return num

    def _relay_page(self, item: dict[str, Any]) -> str | dict[str, Any]:
        src, url = item.get("source"), str(item.get("url") or "")
        num = normalize_set_number(str(item.get("set_number") or ""))
        if src not in compare.SOURCES or not compare.is_compare_url(url) or num not in self.store["sets"]:
            raise ValueError(f"{src}: {url}: not a comparison page of a tracked set")
        html = item.get("html") if isinstance(item.get("html"), str) else ""
        try:
            status, step = int(item.get("status") or 0), max(0, min(compare.MAX_STEPS - 1, int(item.get("step") or 0)))
        except (TypeError, ValueError):
            status, step = 0, 0
        if status == 0:        # the browser couldn't reach it either: log, but don't count it towards a server pause
            self.log("warning", "userscript", T("your browser could not fetch the price either: {error}", error=str(item.get("error") or "")[:120] or "?"),
                     set_number=num, url=url, source="relay")
            kind, nxt = "error", None
        else:
            kind, nxt = self.compare_page(src, num, url, status, html, None, step, via="relay")
        if kind == "ok":
            self._compare_links(num)
            self._compare_apply_prices(num)
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
        """(Re)try to find shop pages for sets that have no offer at some retailer."""
        nums = [normalize_set_number(set_number)] if set_number else list(self.store["sets"])
        found = 0
        for num in nums:
            offers = self.store["offers"].setdefault(num, {})
            for rid in self.retailers:
                if rid not in offers and (url := await self.fetcher.discover(rid, num)):
                    offers[rid] = {"url": url, "history": []}
                    found += 1
        self.push_update()
        return found

    def export_csv(self) -> str:
        return rows_to_csv(collection_rows(self.store, self.compute()["statuses"]), COLLECTION_COLUMNS)

    def export_backup(self) -> dict[str, Any]:
        return {"version": 1, "exported": today_iso(), **copy.deepcopy(self.store)}

    def import_backup(self, data: Any, merge: bool = False) -> dict[str, int]:
        clean = validate_backup(copy.deepcopy(data))
        if merge:
            for num, s in clean["sets"].items():
                self.store["sets"].setdefault(num, s)
            for num, offers in clean["offers"].items():
                self.store["offers"].setdefault(num, {}).update(
                    {r: o for r, o in offers.items() if r not in self.store["offers"].get(num, {})})
            for num, e in clean["collection"].items():
                self.store["collection"].setdefault(num, e)
        else:
            self.store = clean
        self.push_update()
        return {"sets": len(self.store["sets"]), "collection": len(self.store["collection"])}

    def series(self) -> list[dict[str, float]]:
        return collection_series(self.store)

    def all_themes(self) -> list[str]:
        return sorted({s.get("theme") for s in self.store["sets"].values() if s.get("theme")})
