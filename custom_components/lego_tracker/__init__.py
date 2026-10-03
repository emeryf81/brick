"""LEGO Price Tracker: prices, deals and collection value for LEGO sets."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    BUILTIN_RETAILERS, CONF_DIGEST_TIME, CONF_KNOWN_SHOPS, DEFAULT_DIGEST_TIME, DEFAULT_RETAILERS, DOMAIN, EVENT_DIGEST, PANEL_ELEMENT, PANEL_URL, RETAILERS,
    STATIC_URL,
)
from .coordinator import LegoCoordinator
from .i18n import T
from .shops import apply_shop_options
from .csv_import import analyze_csv, apply_import, importable_rows
from .models import normalize_set_number
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
    "discover_offers": vol.Schema({vol.Optional("set_number"): cv.string, vol.Optional("force", default=False): cv.boolean}),
    "enrich_sets": vol.Schema({vol.Optional("set_number"): cv.string, vol.Optional("all", default=False): cv.boolean}),
    "verify_links": vol.Schema({}),
    "confirm_offer": vol.Schema({SET: cv.string, vol.Required("retailer"): cv.string}),
    "remove_offer": vol.Schema({SET: cv.string, vol.Required("retailer"): cv.string,
                                vol.Optional("block", default=True): cv.boolean}),
    "cancel_job": vol.Schema({}),
    "fix_offer": vol.Schema({SET: cv.string, vol.Required("retailer"): cv.string, vol.Optional("url"): cv.string,
                             vol.Optional("price"): vol.Coerce(float)}),
    "export_collection": vol.Schema({}),
    "export_data": vol.Schema({}),
    "import_data": vol.Schema({
        vol.Optional("data"): dict, vol.Optional("file_path"): cv.string, vol.Optional("merge", default=False): cv.boolean,
    }),
    "set_offer": vol.Schema({SET: cv.string, vol.Required("retailer"): cv.string,
                             vol.Required("url"): cv.string}),
    "refresh": vol.Schema({vol.Optional("set_number"): cv.string, vol.Optional("force", default=False): cv.boolean}),
    "import_collection": vol.Schema({
        vol.Optional("csv_text"): cv.string, vol.Optional("file_path"): cv.string,
        vol.Optional("replace", default=False): cv.boolean, vol.Optional("track_prices", default=True): cv.boolean,
        vol.Optional("update_after", default=False): cv.boolean,
    }),
    "report_price": vol.Schema({
        vol.Required("price"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=100000)),
        vol.Optional("url"): cv.string, vol.Optional("set_number"): cv.string,
        vol.Optional("retailer"): cv.string, vol.Optional("title"): vol.All(cv.string, vol.Length(max=400)),
        vol.Optional("via"): vol.In(["userscript", "relay"]),
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
            price, retailer, url = coord.notifier.shop_offer(num, st)
            rows.append({"set_number": num, "name": s.get("name"), "theme": s.get("theme"),
                         "price": price, "retailer": retailer,
                         "discount": st["discount_rrp"], "all_time_low": st["is_all_time_low"],
                         "owned": num in coord.store["collection"], "url": url})
    rows.sort(key=lambda r: (not r["all_time_low"], -(r["discount"] or 0)))
    return {"deals": rows, "collection": data["summary"], "threshold": coord.threshold}


async def _send_digest(hass: HomeAssistant, coord: LegoCoordinator) -> None:
    """Daily digest: event for automations + every notification rule with the 'digest' trigger."""
    d = digest(coord)
    hass.bus.async_fire(EVENT_DIGEST, d)
    await coord.notifier.on_digest(d)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the coordinator, platforms, panel, services, and scheduled refreshes."""
    apply_shop_options(dict(entry.options))
    # New built-in shops (e.g. Dreamland) are switched on once; afterwards the user's choice wins.
    known = set(entry.options.get(CONF_KNOWN_SHOPS) or ("amazon_nl", "amazon_de", "amazon_be", "bol", "kruidvat_be"))
    if new_shops := [r for r in BUILTIN_RETAILERS if r not in known]:
        enabled = list(entry.options.get("retailers", DEFAULT_RETAILERS))
        enabled += [r for r in new_shops if r not in enabled]
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "retailers": enabled, CONF_KNOWN_SHOPS: list(BUILTIN_RETAILERS)})
    coord = LegoCoordinator(hass, entry)
    await coord.async_load()
    _LOGGER.info("LEGO Price Tracker %s starting (request transport: %s)", VERSION, coord.fetcher.transport)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    coord.async_set_updated_data(coord.compute())
    # No shop round at start-up: hammering 5 shops on every HA restart gets us blocked.

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))

    if not hass.data.get(f"{DOMAIN}_frontend"):
        hass.data[f"{DOMAIN}_frontend"] = True
        await _register_frontend(hass)
        async_register_websocket(hass)
        _register_services(hass)

    t = dt_util.parse_time(coord.opt(entry, CONF_DIGEST_TIME, DEFAULT_DIGEST_TIME)) or dt_util.parse_time(DEFAULT_DIGEST_TIME)

    async def _daily(_now: datetime) -> None:
        await _send_digest(hass, coord)

    entry.async_on_unload(async_track_time_change(hass, _daily, t.hour, t.minute, t.second))
    entry.async_on_unload(async_track_time_interval(hass, coord.notifier.flush_queues, timedelta(minutes=5)))

    @callback
    def _scheduled_refresh(_now: datetime) -> None:
        if coord.job_running:
            _LOGGER.info("Scheduled price round skipped: %s is still running", coord.job["label"])
            return
        coord._job_source = "schedule"
        coord.start_refresh()

    if coord.refresh_mode == "spread":
        coord.start_spread()
    # product links from the shops' sitemaps: first look 15 minutes after start, then every 6 hours (weekly per shop)
    entry.async_on_unload(async_call_later(hass, 900, coord.sitemap_tick))
    # the LEGO set database (all sets, new sets): first look 10 minutes after start, then every 6 hours (daily download)
    entry.async_on_unload(async_call_later(hass, 600, coord.setdb_tick))
    entry.async_on_unload(async_track_time_interval(hass, coord.setdb_tick, timedelta(hours=6)))
    # market values: one set at a time, spread over the day
    entry.async_on_unload(async_track_time_interval(hass, coord.market_tick, timedelta(minutes=1)))
    # deals on every LEGO set: one set of the database at a time, spread over the day
    entry.async_on_unload(async_track_time_interval(hass, coord.scan_tick, timedelta(minutes=1)))
    entry.async_on_unload(async_track_time_interval(hass, coord.sitemap_tick, timedelta(hours=6)))
    if coord.auto_refresh:
        for hour, minute in coord.refresh_times:
            entry.async_on_unload(async_track_time_change(hass, _scheduled_refresh, hour, minute, 0))
    return True


async def _register_frontend(hass: HomeAssistant) -> None:
    panel_dir = Path(__file__).parent / "panel"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(panel_dir), False)])
    # the file's own fingerprint in the URL: after an update the browser always loads the new panel
    import hashlib

    digest = await hass.async_add_executor_job(
        lambda: hashlib.sha1((panel_dir / "lego-tracker-panel.js").read_bytes()).hexdigest()[:10])
    version = f"{VERSION}-{digest}"
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
        try:
            await c.add_set(d["set_number"], name=d.get("name"), theme=d.get("theme"), subtheme=d.get("subtheme"),
                            rrp=d.get("rrp"), pieces=d.get("pieces"),
                            target_price=d.get("target_price"), owned=owned)
        except ValueError as err:            # e.g. the watchlist is full
            raise ServiceValidationError(str(err)) from err

    async def add_sets(call: ServiceCall) -> dict:
        """Bulk add: numbers separated by spaces, commas or newlines."""
        c = _coordinator(hass)
        nums = list(dict.fromkeys(re.findall(r"\d{4,7}", call.data["set_numbers"])))
        if not nums:
            raise ServiceValidationError(T("No set numbers found"))
        added = 0
        for n in nums:
            try:
                await c.add_set(n, theme=call.data.get("theme"), owned={"qty": 1} if call.data["owned"] and n not in c.store["collection"] else None)
            except ValueError as err:        # the watchlist is full: stop, say how far we got
                raise ServiceValidationError(f"{err} ({T('{n} of {total} added', n=added, total=len(nums))})") from err
            added += 1
        return {"added": added}

    async def discover_offers(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        if num := call.data.get("set_number"):
            live = c._live_retailers(call.data["force"])
            try:
                c.manual_gate("find")
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            c.mark_sites(c._missing(normalize_set_number(num), live))
            res = await c.discover_set(normalize_set_number(num), live)
            c.push_update()
            return {"started": False, **res}
        return _job(c.start_discover, call.data["force"])

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

    def _job(fn, *args) -> dict:
        try:
            return {"started": True, **fn(*args)}
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def refresh(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        if num := call.data.get("set_number"):
            num = normalize_set_number(num)
            if num not in c.store["sets"]:
                raise ServiceValidationError(T("Set {number} is not tracked.", number=num))
            live = c._live_retailers(call.data["force"])
            try:
                c.manual_gate("prices")
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            c.mark_sites([r for r in (c.store["offers"].get(num) or {}) if r in live])
            res = await c.refresh_set(num, live)
            c.push_update()
            return {"started": False, **res}
        return _job(c.start_full_refresh, call.data["force"])

    async def enrich_sets(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        if num := call.data.get("set_number"):
            res = await c.enrich_set(normalize_set_number(num))
            c.push_update()
            return {"started": False, **res}
        return _job(c.start_enrich, call.data["all"])

    async def verify_links(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        res = c.verify_links()
        c.push_update()
        return res

    async def confirm_offer(call: ServiceCall) -> None:
        try:
            _coordinator(hass).confirm_offer(call.data["set_number"], call.data["retailer"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def remove_offer(call: ServiceCall) -> None:
        try:
            _coordinator(hass).remove_offer(call.data["set_number"], call.data["retailer"], call.data["block"])
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def fix_offer(call: ServiceCall) -> None:
        try:
            _coordinator(hass).fix_offer(call.data["set_number"], call.data["retailer"], call.data.get("url"), call.data.get("price"))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def cancel_job(call: ServiceCall) -> dict:
        return {"cancelled": _coordinator(hass).cancel_job()}

    async def import_collection(call: ServiceCall) -> dict:
        c = _coordinator(hass)
        text = call.data.get("csv_text")
        if not text and (path := call.data.get("file_path")):
            if not hass.config.is_allowed_path(path):
                raise ServiceValidationError(f"{path} is not in allowlist_external_dirs")
            text = await hass.async_add_executor_job(Path(path).read_text, "utf-8-sig")
        if not text:
            raise ServiceValidationError("Provide csv_text or file_path")
        analysis = analyze_csv(text, c.store, replace=call.data["replace"])
        rows = importable_rows(analysis)
        if not rows:
            raise ServiceValidationError(analysis["fatal"] or T("No importable lines (every line has errors)."))
        result = apply_import(c.store, rows, replace=call.data["replace"])
        c.log("ok" if not analysis["summary"]["error"] else "warning", "import",
              T("CSV imported: {added} new, {updated} updated, {skipped} lines skipped", added=result["added"],
                updated=result["updated"], skipped=analysis["summary"]["error"])
              + (" " + T("(collection replaced)") if call.data["replace"] else ""), source="import")
        warnings = [T("line {line}: {issues}", line=r["line"], issues="; ".join(x["text"] for x in r["issues"] if x["level"] != "info"))
                    for r in analysis["rows"] if r["status"] != "ok"]
        for r in rows:
            c.store["offers"].setdefault(r["set_number"], {})
        c.push_update()
        if not c.job_running and call.data["update_after"]:
            c.start_update([r["set_number"] for r in rows])
        elif call.data["track_prices"] and not c.job_running:
            c.start_discover()
        return {**result, "skipped": analysis["summary"]["error"], "warnings": warnings[:50]}

    async def report_price(call: ServiceCall) -> None:
        try:
            _coordinator(hass).report_price(call.data["price"], url=call.data.get("url"),
                                            set_number=call.data.get("set_number"), retailer=call.data.get("retailer"),
                                            title=call.data.get("title"), via=call.data.get("via"))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def send_digest(call: ServiceCall) -> None:
        await _send_digest(hass, _coordinator(hass))

    for name, handler in (("add_sets", add_sets), ("discover_offers", discover_offers), ("export_collection", export_collection),
                          ("export_data", export_data), ("import_data", import_data), ("refresh", refresh),
                          ("enrich_sets", enrich_sets), ("verify_links", verify_links), ("cancel_job", cancel_job)):
        hass.services.async_register(DOMAIN, name, handler, SERVICE_SCHEMAS[name], supports_response=SupportsResponse.OPTIONAL)
    for name, handler in (("add_set", add_set), ("remove_set", remove_set), ("set_offer", set_offer),
                          ("confirm_offer", confirm_offer), ("remove_offer", remove_offer), ("fix_offer", fix_offer),
                          ("report_price", report_price), ("send_digest", send_digest)):
        hass.services.async_register(DOMAIN, name, handler, SERVICE_SCHEMAS[name])
    hass.services.async_register(DOMAIN, "import_collection", import_collection,
                                 SERVICE_SCHEMAS["import_collection"], supports_response=SupportsResponse.OPTIONAL)


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord: LegoCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.async_shutdown()
    return ok
