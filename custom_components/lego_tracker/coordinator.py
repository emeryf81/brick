"""Data coordinator: owns the store, polls retailers, computes statuses."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import Fetcher, brickset_lookup
from .const import (
    CONF_BRICKSET_KEY, CONF_DISCOUNT_THRESHOLD, CONF_IMPERSONATE, CONF_MIN_HISTORY_DAYS, CONF_RETAILERS,
    CONF_UPDATE_HOURS, DEFAULT_DISCOUNT_THRESHOLD, DEFAULT_MIN_HISTORY_DAYS, DEFAULT_RETAILERS,
    DEFAULT_UPDATE_HOURS, DOMAIN, EVENT_HIGH_DISCOUNT, EVENT_NEW_LOW, RETAILERS, STORAGE_KEY,
    STORAGE_VERSION,
)
from .models import (
    collection_series, collection_summary, compute_set_status, new_store, normalize_set_number,
    record_price, today_iso,
)
from .parsers import normalize_url, retailer_from_url, url_key

_LOGGER = logging.getLogger(__name__)


class LegoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """data = {"statuses": {set: status}, "summary": {...}}"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(hours=self.opt(entry, CONF_UPDATE_HOURS, DEFAULT_UPDATE_HOURS)))
        self.entry = entry
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self.store: dict[str, Any] = new_store()
        self.fetcher = Fetcher(hass, bool(self.opt(entry, CONF_IMPERSONATE, True)))
        self._alerted: set[tuple[str, str]] = set()

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
    async def async_load(self) -> None:
        await self.fetcher.async_setup()
        if (data := await self._store.async_load()):
            self.store = {**new_store(), **data}

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.store, 5)

    async def async_shutdown(self) -> None:
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
        return {"statuses": statuses, "summary": collection_summary(self.store, statuses)}

    async def _async_update_data(self) -> dict[str, Any]:
        await self.refresh_all()
        return self.compute()

    def push_update(self) -> None:
        """Recompute without hitting the network (after edits/imports)."""
        self._save()
        self.async_set_updated_data(self.compute())

    # ---------------------------------------------------------------- refresh
    async def refresh_set(self, num: str) -> None:
        before = self.compute()["statuses"].get(num, {})
        for rid, offer in list(self.store["offers"].get(num, {}).items()):
            if rid not in self.retailers or not offer.get("url"):
                continue
            parsed, error = await self.fetcher.fetch_offer(rid, offer["url"])
            record_price(offer, parsed.price if parsed else None, error=error)
            if parsed:
                s = self.store["sets"][num]
                if parsed.image and not s.get("image"):
                    s["image"] = parsed.image
                if parsed.title and not s.get("name"):
                    s["name"] = parsed.title[:120]
            if error:
                _LOGGER.debug("%s/%s: %s", num, rid, error)
        after = self.compute()["statuses"].get(num, {})
        self._fire_events(num, before, after)

    async def refresh_all(self) -> None:
        for num in list(self.store["offers"]):
            if num in self.store["sets"]:
                await self.refresh_set(num)
        self._snapshot()
        self._save()

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
                   "url": after.get("best_url"), "discount": after.get("discount_rrp")}
        day = today_iso()
        if after.get("is_all_time_low") and not before.get("is_all_time_low") and (num, "low" + day) not in self._alerted:
            self._alerted.add((num, "low" + day))
            self.hass.bus.async_fire(EVENT_NEW_LOW, payload)
        if after.get("high_discount") and not before.get("high_discount") and (num, "disc" + day) not in self._alerted:
            self._alerted.add((num, "disc" + day))
            self.hass.bus.async_fire(EVENT_HIGH_DISCOUNT, payload)

    # ------------------------------------------------------------------ edits
    async def add_set(self, set_number: str, *, name: str | None = None, theme: str | None = None,
                      subtheme: str | None = None, rrp: float | None = None, pieces: int | None = None,
                      owned: dict | None = None, discover: bool = True) -> str:
        num = normalize_set_number(set_number)
        s = self.store["sets"].setdefault(num, {"set_number": num})
        meta = await brickset_lookup(async_get_clientsession(self.hass),
                                     self.opt(self.entry, CONF_BRICKSET_KEY, ""), num) or {}
        for key, val in {"name": name, "theme": theme, "subtheme": subtheme, "rrp": rrp, "pieces": pieces}.items():
            if val:
                s[key] = val
        for key in ("name", "theme", "subtheme", "year", "pieces", "image", "rrp"):
            if meta.get(key) and not s.get(key):
                s[key] = meta[key]
        self.store["offers"].setdefault(num, {})
        if owned is not None:
            self.store["collection"][num] = owned
        if discover:
            for rid in self.retailers:
                if rid not in self.store["offers"][num]:
                    if url := await self.fetcher.discover(rid, num):
                        self.store["offers"][num][rid] = {"url": url, "history": []}
        self.push_update()
        return num

    def remove_set(self, set_number: str) -> None:
        num = normalize_set_number(set_number)
        for key in ("sets", "offers", "collection"):
            self.store[key].pop(num, None)
        self.push_update()

    def set_offer(self, set_number: str, retailer: str, url: str) -> None:
        num = normalize_set_number(set_number)
        if num not in self.store["sets"]:
            raise ValueError(f"Set {num} is not tracked yet; add it first.")
        if retailer not in RETAILERS:
            raise ValueError(f"Unknown retailer {retailer}")
        url = normalize_url(retailer, url)
        self.store["offers"].setdefault(num, {})[retailer] = {"url": url, "history": []}
        self.push_update()

    def update_set(self, set_number: str, fields: dict[str, Any]) -> None:
        num = normalize_set_number(set_number)
        allowed = {"name", "theme", "subtheme", "rrp", "pieces", "year", "image"}
        self.store["sets"][num].update({k: v for k, v in fields.items() if k in allowed and v not in (None, "")})
        coll = {k: fields[k] for k in ("qty", "paid", "current_value", "added", "condition") if k in fields}
        if fields.get("owned") is False:
            self.store["collection"].pop(num, None)
        elif coll or fields.get("owned"):
            self.store["collection"].setdefault(num, {"qty": 1}).update(coll)
        self.push_update()

    def report_price(self, price: float, *, url: str | None = None, set_number: str | None = None,
                     retailer: str | None = None) -> str:
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
                raise ValueError(f"Set {num} is not tracked yet; add it first.")
            offer = self.store["offers"].setdefault(num, {}).setdefault(retailer, {"url": url or "", "history": []})
            if url and not offer.get("url"):
                offer["url"] = normalize_url(retailer, url)
            found = (num, retailer)
        if found is None:
            raise ValueError("No matching offer: pass the product url of a tracked offer, or set_number + retailer.")
        num, retailer = found
        before = self.compute()["statuses"].get(num, {})
        record_price(self.store["offers"][num][retailer], price)
        self._fire_events(num, before, self.compute()["statuses"].get(num, {}))
        self.push_update()
        return num

    def series(self) -> list[dict[str, float]]:
        return collection_series(self.store)

    def all_themes(self) -> list[str]:
        return sorted({s.get("theme") for s in self.store["sets"].values() if s.get("theme")})
