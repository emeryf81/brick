"""LEGO Price Tracker: prices, deals and collection value for LEGO sets."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import voluptuous as vol
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME, DOMAIN, EVENT_DIGEST, PANEL_ELEMENT, PANEL_URL, RETAILERS,
    STATIC_URL,
)
from .coordinator import LegoCoordinator
from .csv_import import apply_import, parse_collection_csv
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)
VERSION = json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SET = vol.Required("set_number")
SERVICE_SCHEMAS = {
    "add_set": vol.Schema({
        SET: cv.string, vol.Optional("name"): cv.string, vol.Optional("theme"): cv.string,
        vol.Optional("subtheme"): cv.string, vol.Optional("rrp"): vol.Coerce(float),
        vol.Optional("pieces"): vol.Coerce(int), vol.Optional("target_price"): vol.Coerce(float),
        vol.Optional("owned", default=False): cv.boolean,
        vol.Optional("quantity", default=1): vol.Coerce(int), vol.Optional("paid"): vol.Coerce(float),
        vol.Optional("purchase_date"): cv.string,
    }),
    "add_sets": vol.Schema({
        vol.Required("set_numbers"): cv.string, vol.Optional("theme"): cv.string,
        vol.Optional("owned", default=False): cv.boolean,
    }),
    "remove_set": vol.Schema({SET: cv.string}),
    "discover_offers": vol.Schema({vol.Optional("set_number"): cv.string}),
    "export_collection": vol.Schema({}),
    "export_data": vol.Schema({}),
    "import_data": vol.Schema({
        vol.Optional("data"): dict, vol.Optional("file_path"): cv.string, vol.Optional("merge", default=False): cv.boolean,
    }),
    "set_offer": vol.Schema({SET: cv.string, vol.Required("retailer"): vol.In(list(RETAILERS)),
                             vol.Required("url"): cv.string}),
    "refresh": vol.Schema({vol.Optional("set_number"): cv.string}),
    "import_collection": vol.Schema({
        vol.Optional("csv_text"): cv.string, vol.Optional("file_path"): cv.string,
        vol.Optional("replace", default=False): cv.boolean, vol.Optional("track_prices", default=True): cv.boolean,
    }),
    "report_price": vol.Schema({
        vol.Required("price"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=100000)),
        vol.Optional("url"): cv.string, vol.Optional("set_number"): cv.string,
        vol.Optional("retailer"): vol.In(list(RETAILERS)),
    }),
    "send_digest": vol.Schema({}),
}


def _coordinator(hass: HomeAssistant) -> LegoCoordinator:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise ServiceValidationError("LEGO Price Tracker is not set up")
    return next(iter(entries.values()))


def digest(coord: LegoCoordinator) -> dict:
    """What is interesting today."""
    data = coord.data or coord.compute()
    rows = []
    for num, st in data["statuses"].items():
        if st["is_all_time_low"] or st["high_discount"]:
            s = coord.store["sets"][num]
            rows.append({"set_number": num, "name": s.get("name"), "theme": s.get("theme"),
                         "price": st["best_price"], "retailer": st["best_retailer"],
                         "discount": st["discount_rrp"], "all_time_low": st["is_all_time_low"],
                         "owned": num in coord.store["collection"], "url": st["best_url"]})
    rows.sort(key=lambda r: (not r["all_time_low"], -(r["discount"] or 0)))
    return {"deals": rows, "collection": data["summary"], "threshold": coord.threshold}


async def _send_digest(hass: HomeAssistant, coord: LegoCoordinator) -> None:
    d = digest(coord)
    hass.bus.async_fire(EVENT_DIGEST, d)
    from homeassistant.components import persistent_notification

    if d["deals"]:
        lines = [
            f"- **{r['set_number']} {r['name'] or ''}** €{r['price']:.2f} @ {RETAILERS[r['retailer']][0]}"
            + (f" (-{r['discount']:.0f}%)" if r["discount"] else "") + (" 🔻 record low" if r["all_time_low"] else "")
            for r in d["deals"][:20]
        ]
        await coord.async_notify(f"LEGO deals ({len(d['deals'])})", "\n".join(l.replace("**", "") for l in lines[:8]))
        persistent_notification.async_create(
            hass, "\n".join(lines), title=f"LEGO deals ({len(d['deals'])})", notification_id=f"{DOMAIN}_digest"
        )
    else:
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_digest")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord = LegoCoordinator(hass, entry)
    await coord.async_load()
    _LOGGER.info("LEGO Price Tracker %s starting (request transport: %s)", VERSION, coord.fetcher.transport)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    coord.async_set_updated_data(coord.compute())  # entities are usable before the first (slow) poll
    entry.async_create_background_task(hass, coord.async_refresh(), f"{DOMAIN}_first_refresh")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))

    if not hass.data.get(f"{DOMAIN}_frontend"):
        hass.data[f"{DOMAIN}_frontend"] = True
        await _register_frontend(hass)
        async_register_websocket(hass)
        _register_services(hass)

    t = dt_util.parse_time(coord.opt(entry, CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME)) or dt_util.parse_time(DEFAULT_DIGEST_TIME)

    async def _daily(_now: datetime) -> None:
        await coord.async_request_refresh()
        await _send_digest(hass, coord)

    entry.async_on_unload(async_track_time_change(hass, _daily, t.hour, t.minute, t.second))
    return True


async def _register_frontend(hass: HomeAssistant) -> None:
    panel_dir = Path(__file__).parent / "panel"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(panel_dir), False)])
    version = VERSION
    await panel_custom.async_register_panel(
        hass, webcomponent_name=PANEL_ELEMENT, frontend_url_path=PANEL_URL,
        sidebar_title="LEGO", sidebar_icon="mdi:toy-brick",
        module_url=f"{STATIC_URL}/lego-tracker-panel.js?v={version}", embed_iframe=False, require_admin=False,
    )


@callback
def _register_services(hass: HomeAssistant) -> None:
    async def add_set(call: ServiceCall) -> None:
        c = _coordinator(hass)
        d = call.data
        owned = None
        if d["owned"]:
            owned = {"qty": d["quantity"]}
            if "paid" in d:
                owned["paid"] = d["paid"]
            if "purchase_date" in d:
                owned["added"] = d["purchase_date"]
        await c.add_set(d["set_number"], name=d.get("name"), theme=d.get("theme"), subtheme=d.get("subtheme"),
                        rrp=d.get("rrp"), pieces=d.get("pieces"),
                        target_price=d.get("target_price"), owned=owned)

    async def add_sets(call: ServiceCall) -> dict:
        """Bulk add: numbers separated by spaces, commas or newlines."""
        c = _coordinator(hass)
        nums = list(dict.fromkeys(re.findall(r"\d{4,7}", call.data["set_numbers"])))
        if not nums:
            raise ServiceValidationError("No set numbers found")
        for n in nums:
            await c.add_set(n, theme=call.data.get("theme"), owned={"qty": 1} if call.data["owned"] and n not in c.store["collection"] else None)
        return {"added": len(nums)}

    async def discover_offers(call: ServiceCall) -> dict:
        return {"found": await _coordinator(hass).discover_offers(call.data.get("set_number"))}

    async def export_collection(call: ServiceCall) -> dict:
        return {"csv": _coordinator(hass).export_csv()}

    async def export_data(call: ServiceCall) -> dict:
        return _coordinator(hass).export_backup()

    async def import_data(call: ServiceCall) -> dict:
        data = call.data.get("data")
        if data is None and (path := call.data.get("file_path")):
            if not hass.config.is_allowed_path(path):
                raise ServiceValidationError(f"{path} is not in allowlist_external_dirs")
            data = json.loads(await hass.async_add_executor_job(Path(path).read_text, "utf-8"))
        if data is None:
            raise ServiceValidationError("Provide data or file_path")
        try:
            return _coordinator(hass).import_backup(data, merge=call.data["merge"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def remove_set(call: ServiceCall) -> None:
        _coordinator(hass).remove_set(call.data["set_number"])

    async def set_offer(call: ServiceCall) -> None:
        try:
            _coordinator(hass).set_offer(call.data["set_number"], call.data["retailer"], call.data["url"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def refresh(call: ServiceCall) -> None:
        c = _coordinator(hass)
        if num := call.data.get("set_number"):
            await c.refresh_set(num)
            c.push_update()
        else:
            await c.async_refresh()

    async def import_collection(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        text = call.data.get("csv_text")
        if not text and (path := call.data.get("file_path")):
            if not hass.config.is_allowed_path(path):
                raise ServiceValidationError(f"{path} is not in allowlist_external_dirs")
            text = await hass.async_add_executor_job(Path(path).read_text, "utf-8-sig")
        if not text:
            raise ServiceValidationError("Provide csv_text or file_path")
        rows, warnings = parse_collection_csv(text)
        if not rows:
            raise ServiceValidationError("; ".join(warnings) or "No rows found")
        result = apply_import(c.store, rows, replace=call.data["replace"])
        for r in rows:
            c.store["offers"].setdefault(r["set_number"], {})
        c.push_update()
        if call.data["track_prices"]:
            hass.async_create_task(_discover_offers(c, [r["set_number"] for r in rows]))
        return {**result, "warnings": warnings[:20]}

    async def report_price(call: ServiceCall) -> None:
        try:
            _coordinator(hass).report_price(call.data["price"], url=call.data.get("url"),
                                            set_number=call.data.get("set_number"), retailer=call.data.get("retailer"))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def send_digest(call: ServiceCall) -> None:
        await _send_digest(hass, _coordinator(hass))

    for name, handler in (("add_sets", add_sets), ("discover_offers", discover_offers), ("export_collection", export_collection),
                          ("export_data", export_data), ("import_data", import_data)):
        hass.services.async_register(DOMAIN, name, handler, SERVICE_SCHEMAS[name], supports_response=SupportsResponse.OPTIONAL)
    for name, handler in (("add_set", add_set), ("remove_set", remove_set), ("set_offer", set_offer),
                          ("refresh", refresh), ("report_price", report_price), ("send_digest", send_digest)):
        hass.services.async_register(DOMAIN, name, handler, SERVICE_SCHEMAS[name])
    hass.services.async_register(DOMAIN, "import_collection", import_collection,
                                 SERVICE_SCHEMAS["import_collection"], supports_response=SupportsResponse.OPTIONAL)


async def _discover_offers(c: LegoCoordinator, numbers: list[str]) -> None:
    """Background: find retailer URLs for freshly imported sets (slow on purpose)."""
    for num in numbers:
        offers = c.store["offers"].setdefault(num, {})
        for rid in c.retailers:
            if rid in offers:
                continue
            try:
                if url := await c.fetcher.discover(rid, num):
                    offers[rid] = {"url": url, "history": []}
            except Exception:  # noqa: BLE001 - never let discovery kill the task
                _LOGGER.debug("discovery failed for %s/%s", num, rid, exc_info=True)
        c.push_update()


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord: LegoCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.async_shutdown()
    return ok
