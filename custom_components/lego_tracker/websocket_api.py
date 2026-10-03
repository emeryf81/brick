"""Websocket API used by the sidebar panel."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import API_LEVEL, CONF_DEV_FULL_REFRESH, DOMAIN, PANEL_URL, RETAILERS
from .csv_import import analyze_csv
from .themes import THEMES as LEGO_THEMES
from .i18n import tr
from .models import combined_history, normalize_set_number, offer_price


def _languages() -> dict[str, str]:
    from .i18n import LANGUAGES

    return LANGUAGES


def _coord(hass: HomeAssistant):
    entries = hass.data.get(DOMAIN, {})
    return next(iter(entries.values()), None)


def _compare_status(coord) -> dict[str, Any] | None:
    """Hidden option: state per price-comparison site."""
    if not coord.compare_enabled:
        return None
    from . import compare

    out = {}
    for src, (name, _host) in compare.SOURCES.items():
        st = (coord.store.get("compare") or {}).get(src) or {}
        out[src] = {"name": name, "enabled": src in coord.compare_sources,
                    "sets": sum(1 for e in st.values() if e.get("status") == "ok"),
                    "missing": sum(1 for e in st.values() if e.get("status") == "missing"),
                    "errors": sum(1 for e in st.values() if e.get("status") in ("error", "unreadable") or e.get("last_error")),
                    "last": max((e.get("ts", 0) for e in st.values()), default=None) or None,
                    "paused_until": coord.fetcher.blocked_until.get(src) if coord.fetcher.cooldown_left(src) > 0 else None,
                    "net_errors": coord._net_errors.get(src, 0)}
    return {"sources": out, "sets": sum(v["sets"] for v in out.values()), "missing": sum(v["missing"] for v in out.values()),
            "last": max((v["last"] or 0 for v in out.values()), default=0) or None}


def _card(coord, num: str, with_history: bool = False) -> dict[str, Any]:
    """Build a panel set card with offers and optional price history and comparison data."""
    s = coord.store["sets"][num]
    st = (coord.data or coord.compute())["statuses"].get(num, {})
    offers = coord.store["offers"].get(num, {})
    coll = coord.store["collection"].get(num)
    series = combined_history(offers)
    card = {
        **s, **st,
        "watched": coord.is_watched(num),
        "owned": coll is not None,
        "collection": coll,
        "spark": [p for _, p in series][-60:],
        "offers": {
            rid: {"label": RETAILERS[rid][0], "url": o.get("url"), "price": offer_price(o), "auto_price": o.get("auto_price") if o.get("manual_price") else None,
                  "manual_price": (o.get("manual_price") or {}).get("price"), "manual_url": bool(o.get("manual_url")),
                  "available": o.get("available"), "error": o.get("error"), "checked": o.get("last_checked"),
                  "low": min((p for _, p in o.get("history", [])), default=None),
                  "title": o.get("title"), "link_status": o.get("link_status"), "link_reason": o.get("link_reason"),
                  "via": o.get("last_via") if o.get("available") and not o.get("manual_price") else None, "found_via": o.get("found_via"),
                  "suspect": o.get("suspect"), "approved": o.get("approved"),
                  "ignored": bool(o.get("error") and o.get("ignored_error") == o.get("error"))}
            for rid, o in offers.items() if rid in RETAILERS
        },
    }
    if with_history:
        card["history"] = {rid: o.get("history", []) for rid, o in offers.items()}
        if coord.compare_enabled:
            from .compare import SOURCES

            entries = coord.compare_entries(num)
            card["compare"] = {src: {**(entries.get(src) or {"status": "none"}), "name": SOURCES[src][0]}
                               for src in coord.compare_sources}
        card["combined"] = series
    return card


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register panel websocket commands and the userscript and relay HTTP views."""
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_set_detail)
    websocket_api.async_register_command(hass, ws_collection)
    websocket_api.async_register_command(hass, ws_update_set)
    websocket_api.async_register_command(hass, ws_import_preview)
    websocket_api.async_register_command(hass, ws_job)
    websocket_api.async_register_command(hass, ws_settings_get)
    websocket_api.async_register_command(hass, ws_settings_set)
    websocket_api.async_register_command(hass, ws_test_key)
    websocket_api.async_register_command(hass, ws_shop_action)
    websocket_api.async_register_command(hass, ws_notify_get)
    websocket_api.async_register_command(hass, ws_ignore_error)
    websocket_api.async_register_command(hass, ws_log)
    websocket_api.async_register_command(hass, ws_offer_update)
    websocket_api.async_register_command(hass, ws_offer_fetch)
    websocket_api.async_register_command(hass, ws_set_enrich)
    websocket_api.async_register_command(hass, ws_compare_fetch)
    websocket_api.async_register_command(hass, ws_compare_test)
    websocket_api.async_register_command(hass, ws_compare_html)
    websocket_api.async_register_command(hass, ws_filter_test)
    websocket_api.async_register_command(hass, ws_client_error)
    websocket_api.async_register_command(hass, ws_logs_export)
    websocket_api.async_register_command(hass, ws_report)
    websocket_api.async_register_command(hass, ws_shop_detail)
    websocket_api.async_register_command(hass, ws_offer_approve)
    websocket_api.async_register_command(hass, ws_shop_sitemap)
    websocket_api.async_register_command(hass, ws_notify_set)
    websocket_api.async_register_command(hass, ws_notify_test)
    websocket_api.async_register_command(hass, ws_dev_tool)
    websocket_api.async_register_command(hass, ws_ticker)
    websocket_api.async_register_command(hass, ws_new_sets)
    websocket_api.async_register_command(hass, ws_catalog)
    websocket_api.async_register_command(hass, ws_setdb_search)
    hass.http.register_view(UserscriptView())
    hass.http.register_view(RelayView())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def ws_overview(hass, connection, msg):
    """Send panel overview data, or a not-loaded error when no coordinator is available."""
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    from . import VERSION

    connection.send_result(msg["id"], {
        "version": VERSION, "api": API_LEVEL, "transport": coord.fetcher.transport,
        "new_sets_week": sum(1 for ts in coord.store.get("new_sets", {}).values() if time.time() - ts < 7 * 86400),
        "threshold": coord.threshold,
        "themes": coord.all_themes(),
        "retailers": {k: v[0] for k, v in RETAILERS.items()},
        "sets": [_card(coord, n) for n in coord.store["sets"]],
        "summary": (coord.data or coord.compute())["summary"],
        "wishlist": (coord.data or coord.compute())["wishlist"],
        "analytics": (coord.data or coord.compute())["analytics"],
        "events": list(reversed(coord.store.get("events", [])[-40:])),
        "retailer_stats": coord.retailer_stats(),
        **coord.job_info(),
        "language": coord.language, "languages": _languages(),
        "userscript_last": coord.store.get("userscript_last"), "relay_last": coord.store.get("relay_last"),
        "relay": {"enabled": coord.relay_enabled, "pending": coord.relay_items(100)["total"] if coord.relay_enabled else 0,
                  "continuous": coord.continuous_items(1)["counts"] if coord.relay_enabled else {},
                  "heartbeat": coord.store.get("relay_heartbeat")},
        "bol_api": bool(coord.bol_api),
        "compare": _compare_status(coord), "deal_rules": coord.deal_rules | {"filter": coord.deal_filter},
        "deal_blocked": {n: why for n in coord.store["sets"]
                         if (why := coord.deal_blocked(n, (coord.data or coord.compute())["statuses"].get(n, {})))},
        "lego_themes": list(LEGO_THEMES),
        "watch_limit": coord.watch_limit, "full_refresh": coord.dev(CONF_DEV_FULL_REFRESH), "manual": coord.manual_status(),
        "value_source": coord.store.get("value_source", "shop_first"),
        "health": {
            "errors": sum(s["offers_error"] for s in (coord.data or coord.compute())["statuses"].values()),
            "paused_hours": {RETAILERS[r][0]: round(coord.fetcher.cooldown_left(r) / 3600, 1)
                             for r in coord.retailers if coord.fetcher.cooldown_left(r) > 0},
        },
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/set", vol.Required("set_number"): str})
@callback
def ws_set_detail(hass, connection, msg):
    coord = _coord(hass)
    num = normalize_set_number(msg["set_number"])
    if coord is None or num not in coord.store["sets"]:
        connection.send_error(msg["id"], "not_found", f"Set {num} not tracked")
        return
    connection.send_result(msg["id"], _card(coord, num, with_history=True))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/collection"})
@callback
def ws_collection(hass, connection, msg):
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], {
        "series": coord.series(),
        "snapshots": coord.store["snapshots"],
        "summary": (coord.data or coord.compute())["summary"],
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/update_set", vol.Required("set_number"): str,
    vol.Required("fields"): dict,
})
@callback
def ws_update_set(hass, connection, msg):
    coord = _coord(hass)
    num = normalize_set_number(msg["set_number"])
    if coord is None or num not in coord.store["sets"]:
        connection.send_error(msg["id"], "not_found", f"Set {num} not tracked")
        return
    try:
        coord.update_set(num, msg["fields"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], _card(coord, num))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/import_preview", vol.Required("csv_text"): str,
    vol.Optional("replace", default=False): bool,
})
@callback
def ws_import_preview(hass, connection, msg):
    """Dry run: parse + validate a CSV without changing anything."""
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], analyze_csv(msg["csv_text"], coord.store, replace=msg["replace"]))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/job"})
@callback
def ws_job(hass, connection, msg):
    """Progress of the running background job (polled by the panel)."""
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], coord.job_info())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings/get"})
@callback
def ws_settings_get(hass, connection, msg):
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], coord.settings_get())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings/set", vol.Required("fields"): dict})
@websocket_api.async_response
async def ws_settings_set(hass, connection, msg):
    """Validate and store settings; the entry reloads itself (update listener) to apply them."""
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    try:
        options = coord.settings_validate(msg["fields"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    coord.log("info", "settings", "instellingen gewijzigd: " + ", ".join(sorted(k for k in msg["fields"] if not k.endswith("_api_key") or msg["fields"][k] is not None)), source="panel")
    coord._save()
    hass.config_entries.async_update_entry(coord.entry, options=options)
    connection.send_result(msg["id"], {"saved": True})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings/test_key",
                                  vol.Required("source"): vol.In(["brickset", "rebrickable", "bol"]),
                                  vol.Optional("key"): str, vol.Optional("secret"): str})
@websocket_api.async_response
async def ws_test_key(hass, connection, msg):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .client import test_metadata_source

    coord = _coord(hass)
    if msg["source"] == "bol":
        ok, text = await coord.test_bol(msg.get("key"), msg.get("secret"))
        connection.send_result(msg["id"], {"ok": ok, "message": text})
        return
    key = msg.get("key") or (coord.opt(coord.entry, f"{msg['source']}_api_key", "") if coord else "")
    ok, text = await test_metadata_source(async_get_clientsession(hass), msg["source"], key)
    connection.send_result(msg["id"], {"ok": ok, "message": text})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/shop_action",
                                  vol.Required("action"): vol.In(["resume", "resume_all"]),
                                  vol.Optional("retailer"): str})
@callback
def ws_shop_action(hass, connection, msg):
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    coord.resume_shop(msg.get("retailer") if msg["action"] == "resume" else None)
    connection.send_result(msg["id"], coord.job_info())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/ignore_error", vol.Required("set_number"): str,
                                  vol.Required("retailer"): str, vol.Optional("ignore", default=True): bool})
@callback
def ws_ignore_error(hass, connection, msg):
    """Hide a known error in the errors tab until a different error occurs."""
    coord = _coord(hass)
    offer = (coord.store["offers"].get(normalize_set_number(msg["set_number"])) or {}).get(msg["retailer"]) if coord else None
    if offer is None:
        connection.send_error(msg["id"], "not_found", "Unknown link")
        return
    if msg["ignore"]:
        offer["ignored_error"] = offer.get("error")
    else:
        offer.pop("ignored_error", None)
    coord.push_update()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/notify/get"})
@callback
def ws_notify_get(hass, connection, msg):
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], {"rules": coord.notifier.rules, "options": coord.notifier.ha_options(),
                                       "log": list(reversed(coord.store.get("notify_log", [])[-30:])),
                                       "queued": {k: len(v) for k, v in (coord.store.get("notify_queue") or {}).items() if v}})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/notify/set", vol.Required("rules"): list})
@callback
def ws_notify_set(hass, connection, msg):
    from .notifications import validate_rules

    coord = _coord(hass)
    try:
        rules = validate_rules(msg["rules"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    coord.store["notify_rules"] = rules
    coord.push_update()
    connection.send_result(msg["id"], {"rules": rules})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/notify/test", vol.Required("rule"): dict})
@websocket_api.async_response
async def ws_notify_test(hass, connection, msg):
    """Send a sample message to every target of a (possibly unsaved) rule."""
    from .i18n import tr
    from .notifications import validate_rules

    coord = _coord(hass)
    try:
        rule = validate_rules([msg["rule"]])[0]
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    sample = next((s for s in coord.store["sets"].values() if s.get("image")), {})
    res = await coord.notifier.send(
        rule, "🧱 " + tr("Test notification LEGO Price Tracker"),
        tr("This is what a notification of '{rule}' looks like.", rule=rule["name"])
        + (" " + tr("Example: {set}", set=f"{sample.get('set_number')} {sample.get('name') or ''}".strip()) if sample else ""),
        url="https://www.lego.com" if rule.get("link") else None, image=sample.get("image") if rule.get("image") else None,
        force=True)
    connection.send_result(msg["id"], {"results": res})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/log", vol.Optional("level", default=""): str, vol.Optional("kind", default=""): str,
    vol.Optional("retailer", default=""): str, vol.Optional("source", default=""): str,
    vol.Optional("set_number", default=""): str, vol.Optional("q", default=""): str, vol.Optional("status", default=""): str,
    vol.Optional("before"): vol.Any(None, vol.Coerce(float)), vol.Optional("limit", default=100): vol.All(int, vol.Range(1, 500)),
})
@callback
def ws_log(hass, connection, msg):
    from .models import query_activity

    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    connection.send_result(msg["id"], query_activity(coord.store, **{k: msg[k] for k in (
        "level", "kind", "retailer", "source", "set_number", "q", "status", "limit")}, before=msg.get("before")))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/offer/update", vol.Required("set_number"): str, vol.Required("retailer"): str,
    vol.Optional("url"): vol.Any(None, str), vol.Optional("manual_price"): vol.Any(None, str, int, float),
})
@callback
def ws_offer_update(hass, connection, msg):
    """Set or clear a manual link / manual price (empty = back to automatic)."""
    coord = _coord(hass)
    kwargs = {k: msg[k] for k in ("url", "manual_price") if k in msg}
    try:
        coord.update_offer(msg["set_number"], msg["retailer"], **kwargs)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], _card(coord, normalize_set_number(msg["set_number"]), with_history=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/offer/fetch", vol.Required("set_number"): str, vol.Required("retailer"): str,
    vol.Optional("url"): str,
})
@websocket_api.async_response
async def ws_offer_fetch(hass, connection, msg):
    """Fetch one shop for one set now (searching a link first if there is none). Works for paused shops."""
    coord = _coord(hass)
    try:
        result = await coord.fetch_shop(msg["set_number"], msg["retailer"], msg.get("url"))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], {"result": result, "set": _card(coord, normalize_set_number(msg["set_number"]), with_history=True)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/set/enrich", vol.Required("set_number"): str})
@websocket_api.async_response
async def ws_set_enrich(hass, connection, msg):
    """Fill in the set data now: LEGO.com first (image, RRP, name), then Brickset / Rebrickable."""
    coord = _coord(hass)
    num = normalize_set_number(msg["set_number"])
    if num not in coord.store["sets"]:
        connection.send_error(msg["id"], "not_found", tr("Set {number} is not tracked.", number=num))
        return
    res = await coord.enrich_set(num, force=True)
    coord._save()
    coord.push_update()
    connection.send_result(msg["id"], {"result": res, "set": _card(coord, num, with_history=True)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/compare/fetch", vol.Optional("set_number"): str,
                                  vol.Optional("retry_missing", default=False): bool})
@websocket_api.async_response
async def ws_compare_fetch(hass, connection, msg):
    """Hidden option: one set's comparison pages now (and a price check), or a job for all sets."""
    coord = _coord(hass)
    if not coord.compare_enabled:
        connection.send_error(msg["id"], "not_enabled", "Comparison sites are not enabled")
        return
    if not msg.get("set_number"):
        try:
            connection.send_result(msg["id"], {"job": coord.start_compare()})
        except ValueError as err:
            connection.send_error(msg["id"], "busy", str(err))
        return
    num = normalize_set_number(msg["set_number"])
    if num not in coord.store["sets"]:
        connection.send_error(msg["id"], "not_found", tr("Set {number} is not tracked.", number=num))
        return
    found = await coord.compare_refresh(num, force=True, retry_missing=msg["retry_missing"])
    if found:
        await coord.refresh_set(num, source="panel")
    coord._save()
    coord.push_update()
    connection.send_result(msg["id"], {"found": bool(found), "set": _card(coord, num, with_history=True)})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/compare/test", vol.Required("source"): str,
                                  vol.Required("set_number"): str})
@websocket_api.async_response
async def ws_compare_test(hass, connection, msg):
    """Hidden option: try one site for one set (any set number) and show every step, for diagnosis."""
    from . import compare

    coord = _coord(hass)
    src, num = msg["source"], normalize_set_number(msg["set_number"])
    if src not in compare.SOURCES or not num:
        connection.send_error(msg["id"], "invalid", "unknown source")
        return
    steps: list[dict[str, Any]] = []
    tracked = num in coord.store["sets"]
    if not tracked:                      # a set that isn't tracked: a temporary entry, removed afterwards
        coord.store["sets"][num] = {"set_number": num}
    try:
        await coord._compare_one(src, num, True, True, True, steps)
        entry = coord._cstore(src).get(num)
    finally:
        if not tracked:
            coord.store["sets"].pop(num, None)
            coord._cstore(src).pop(num, None)
    dbg = coord._compare_debug.get(src) or {}
    connection.send_result(msg["id"], {"steps": steps, "entry": entry, "size": len(dbg.get("html") or "")})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/logs/export", vol.Required("parts"): [vol.In(["checks", "history", "total", "reports"])],
    vol.Optional("start"): vol.Coerce(float), vol.Optional("end"): vol.Coerce(float), vol.Optional("filters", default={}): dict,
})
@callback
def ws_logs_export(hass, connection, msg):
    """Logbook export as CSV: shop checks, price history per set and/or in total, with filters."""
    from . import log_export
    from .models import retirement_status

    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    if not msg["parts"]:
        connection.send_error(msg["id"], "invalid", tr("Choose at least one part to export."))
        return
    prio = (msg.get("filters") or {}).get("priority")
    if prio not in (None, "", 0):
        try:
            if isinstance(prio, bool) or float(prio) != int(float(prio)) or not 1 <= int(float(prio)) <= 3:
                raise ValueError
        except (TypeError, ValueError):
            connection.send_error(msg["id"], "invalid", tr("Invalid priority filter."))
            return
    text, counts = log_export.build(coord.store, msg, coord.is_watched, lambda s: retirement_status(s)["retiring_soon"])
    connection.send_result(msg["id"], {"csv": text, "counts": counts})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/report", vol.Required("set_number"): str,
    vol.Optional("problems", default=[]): [str], vol.Optional("shops", default=[]): [str],
    vol.Optional("comment", default=""): str, vol.Optional("save", default=True): bool,
})
@callback
def ws_report(hass, connection, msg):
    """'Problem with this set': everything known about the set plus what is wrong; to the logbook and/or as CSV."""
    coord = _coord(hass)
    num = normalize_set_number(msg["set_number"])
    if coord is None or num not in coord.store["sets"]:
        connection.send_error(msg["id"], "not_found", tr("Set {number} is not tracked.", number=num))
        return
    rep, text = coord.report_problem(num, msg["problems"], msg["shops"], msg["comment"], msg["save"])
    connection.send_result(msg["id"], {"id": rep["id"], "saved": msg["save"], "csv": text})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/shop/detail", vol.Required("retailer"): str})
@callback
def ws_shop_detail(hass, connection, msg):
    """Shops → click a shop: last requests, results, errors and a diagnosis (polled while open)."""
    coord = _coord(hass)
    if coord is None or msg["retailer"] not in RETAILERS:
        connection.send_error(msg["id"], "not_found", "Unknown shop")
        return
    connection.send_result(msg["id"], coord.shop_detail(msg["retailer"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/shop/sitemap", vol.Required("retailer"): str})
@callback
def ws_shop_sitemap(hass, connection, msg):
    """Read a shop's sitemap now (in the background) and link the sets found there."""
    coord = _coord(hass)
    try:
        coord.start_sitemap(msg["retailer"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"started": True})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/offer/approve", vol.Required("set_number"): str,
                                  vol.Required("retailer"): str})
@callback
def ws_offer_approve(hass, connection, msg):
    """Approve a price that was held back as suspicious: it counts, and prices like it count from now on."""
    coord = _coord(hass)
    try:
        price = coord.approve_price(msg["set_number"], msg["retailer"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], {"price": price, "set": _card(coord, normalize_set_number(msg["set_number"]), with_history=True)})


_CLIENT_ERRORS: list[float] = []


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/client_error", vol.Required("message"): str,
                                  vol.Optional("stack", default=""): str, vol.Optional("ua", default=""): str,
                                  vol.Optional("view", default=""): str})
@callback
def ws_client_error(hass, connection, msg):
    """The panel failed to draw (e.g. in the iPhone app): log it with the stack, at most 20 per hour."""
    import logging
    import time

    now = time.time()
    _CLIENT_ERRORS[:] = [t for t in _CLIENT_ERRORS if now - t < 3600]
    if len(_CLIENT_ERRORS) < 20:
        _CLIENT_ERRORS.append(now)
        logging.getLogger(__package__).warning("Panel error in %s (view %s): %s\n%s", msg["ua"][:300], msg["view"][:60],
                                                msg["message"][:500], msg["stack"][:3000])
    connection.send_result(msg["id"], {"logged": len(_CLIENT_ERRORS) <= 20})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/filter/test", vol.Required("title"): str,
                                  vol.Optional("block_words"): [str], vol.Optional("allow_words"): [str]})
@callback
def ws_filter_test(hass, connection, msg):
    """Product filter: is this title an accessory? With the given (unsaved) word lists, else the saved ones."""
    from . import parsers
    from .const import CONF_ALLOW_WORDS, CONF_BLOCK_WORDS

    coord = _coord(hass)
    saved = (coord.opt(coord.entry, CONF_BLOCK_WORDS, []), coord.opt(coord.entry, CONF_ALLOW_WORDS, []))
    parsers.set_custom_words(msg.get("block_words", saved[0]), msg.get("allow_words", saved[1]))
    try:
        word = parsers.accessory_word(msg["title"]) or ((m := parsers.KNOCKOFF_RE.search(msg["title"])) and m.group(0))
    finally:
        parsers.set_custom_words(*saved)
    connection.send_result(msg["id"], {"word": word or None})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/compare/html", vol.Required("source"): str})
@callback
def ws_compare_html(hass, connection, msg):
    """The last page a comparison site returned (to see why reading it fails)."""
    dbg = _coord(hass)._compare_debug.get(msg["source"]) or {}
    connection.send_result(msg["id"], {k: dbg.get(k) for k in ("url", "status", "error", "set", "ts", "via", "html")})


class RelayView(HomeAssistantView):
    """Browser relay for the userscript (needs the user's long-lived token).

    GET  -> shop pages the browser should fetch (failed or stale on the server)
    POST -> {"results": [{"set_number", "retailer", "url", "price"?, "title"?, "error"?}]}"""

    url = "/api/lego_tracker/relay"
    name = "api:lego_tracker:relay"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        coord = _coord(request.app[KEY_HASS])
        if coord is None:
            return self.json_message("not loaded", 503)
        try:
            limit = max(1, min(100, int(request.query.get("limit", "40"))))
        except ValueError:
            limit = 40
        if request.query.get("mode") == "continuous":          # the userscript's continuous check
            return self.json(coord.continuous_items(min(limit, 50)))
        return self.json(coord.relay_items(limit))

    async def post(self, request: web.Request) -> web.Response:
        coord = _coord(request.app[KEY_HASS])
        if coord is None:
            return self.json_message("not loaded", 503)
        try:
            body = await request.json()
            results = body.get("results") or []
            assert isinstance(results, list) and len(results) <= 100
            if isinstance(body.get("heartbeat"), dict):
                coord.relay_heartbeat(body["heartbeat"])
        except Exception:  # noqa: BLE001 - any malformed body
            return self.json_message("invalid body", 400)
        out: dict[str, Any] = {"ok": 0, "fail": 0, "rejected": [], "follow": []}
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                res = coord.relay_result(item)
                if isinstance(res, dict):
                    out["follow"].append(res)
                else:
                    out[res] += 1
            except ValueError as err:           # not tracked / suspicious price
                out["rejected"].append(str(err))
        coord.push_update()
        return self.json(out)


class UserscriptView(HomeAssistantView):
    """Serves a Tampermonkey userscript generated for this HA instance and the configured shops.

    No auth: Tampermonkey fetches it outside HA. It contains no secrets (the token is entered
    by the user inside Tampermonkey)."""

    url = "/api/lego_tracker/lego-tracker.user.js"
    name = "api:lego_tracker:userscript"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        from . import VERSION
        from .i18n import tr
        from .shops import all_domains

        hass = request.app[KEY_HASS]
        template = await hass.async_add_executor_job(
            (Path(__file__).parent / "userscript.template.js").read_text, "utf-8")
        base = f"{request.scheme}://{request.host}"
        if request.headers.get("X-Forwarded-Proto") in ("http", "https"):   # behind a reverse proxy / Nabu Casa
            base = f"{request.headers['X-Forwarded-Proto']}://{request.host}"
        matches = "\n".join(f"// @match        https://www.{d}/*\n// @match        https://{d}/*"
                            for d in sorted(set(all_domains().values())))
        # Home Assistant itself (browser relay): this address and the panel on any address
        matches += f"\n// @include      {base}/*\n// @include      *://*/{PANEL_URL}*"
        body = (template.replace("{{VERSION}}", VERSION).replace("{{MATCHES}}", matches)
                .replace("{{HA_URL}}", base).replace("{{SELF_URL}}", base + self.url))
        # {{t:English}} -> translated text, {{tj:English}} -> translated JS string literal
        body = re.sub(r"\{\{tj:(.+?)\}\}", lambda m: json.dumps(tr(m.group(1))), body)
        body = re.sub(r"\{\{t:(.+?)\}\}", lambda m: tr(m.group(1)), body)
        return web.Response(text=body, content_type="text/javascript", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/dev/tool",
                                  vol.Required("action"): vol.In(["stats", "reset", "parse", "fetch", "queue", "outliers",
                                                                  "debug", "dump"]),
                                  vol.Optional("what", default=""): str, vol.Optional("retailer"): str,
                                  vol.Optional("html", default=""): vol.All(str, vol.Length(max=5_000_000)),
                                  vol.Optional("url", default=""): str, vol.Optional("set_number"): str,
                                  vol.Optional("apply", default=False): bool, vol.Optional("on", default=False): bool})
@websocket_api.async_response
async def ws_dev_tool(hass, connection, msg):
    """Developer tools (see devtools.py)."""
    from . import devtools

    coord = _coord(hass)
    a, num = msg["action"], normalize_set_number(msg["set_number"]) if msg.get("set_number") else None
    try:
        if a == "stats":
            out: Any = devtools.stats(coord)
        elif a == "reset":
            if msg["what"] not in devtools.RESETS:
                raise ValueError(msg["what"])
            out = {"removed": devtools.reset(coord, msg["what"])}
            coord.push_update()
        elif a == "parse":
            out = await hass.async_add_executor_job(devtools.parse, msg.get("retailer"), msg["html"], msg["url"], num)
        elif a == "fetch":
            if not msg["url"].startswith(("https://", "http://")):
                raise ValueError("URL")
            out = await devtools.fetch(coord, msg["url"], num)
        elif a == "queue":
            out = coord.continuous_items(100)
        elif a == "outliers":
            out = {"points": devtools.outliers(coord, msg["apply"])}
            if msg["apply"]:
                coord.push_update()
        elif a == "debug":
            out = {"debug": devtools.set_debug(msg["on"])}
        else:
            out = json.loads(json.dumps(devtools.dump(coord), default=str))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/ticker", vol.Optional("lang", default="en"): str})
@websocket_api.async_response
async def ws_ticker(hass, connection, msg):
    """The ticker at the bottom of the panel: latest prices, deals and news."""
    coord = _coord(hass)
    if coord is None:
        connection.send_result(msg["id"], {"items": [], "news": []})
        return
    connection.send_result(msg["id"], await coord.ticker_data(msg["lang"]))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/new_sets"})
@callback
def ws_new_sets(hass, connection, msg):
    """Deals → New sets: sets that appeared in the LEGO set database."""
    coord = _coord(hass)
    if coord is None:
        connection.send_result(msg["id"], {"items": [], "count": 0})
        return
    connection.send_result(msg["id"], coord.new_sets())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/catalog", vol.Optional("q", default=""): vol.All(str, vol.Length(max=80)),
                                  vol.Optional("theme", default=""): vol.All(str, vol.Length(max=80)),
                                  vol.Optional("status", default=""): vol.In(["", "deal", "sale", "none", "unknown", "retired", "followed"]),
                                  vol.Optional("sort", default="deal"): vol.In(["deal", "new", "price", "name"]),
                                  vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0, max=100000)),
                                  vol.Optional("limit", default=120): vol.All(int, vol.Range(min=1, max=500))})
@callback
def ws_catalog(hass, connection, msg):
    """Deals → All LEGO sets: the whole set database with prices, deals and retirement."""
    coord = _coord(hass)
    if coord is None:
        connection.send_result(msg["id"], {"items": [], "total": 0, "count": 0, "themes": [], "scan": {}})
        return
    connection.send_result(msg["id"], coord.catalog(msg["q"], msg["theme"], msg["status"], msg["sort"], msg["offset"], msg["limit"]))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/setdb/search", vol.Required("q"): vol.All(str, vol.Length(max=80)),
                                  vol.Optional("limit", default=20): vol.All(int, vol.Range(min=1, max=50))})
@callback
def ws_setdb_search(hass, connection, msg):
    """Add a set: search the LEGO set database by number or name."""
    from .setdb import as_set, search

    coord = _coord(hass)
    if coord is None:
        connection.send_result(msg["id"], {"items": []})
        return
    nums = search(coord.setdb, msg["q"], msg["limit"])
    connection.send_result(msg["id"], {"items": [{**as_set(n, coord.setdb[n]), "tracked": n in coord.store["sets"],
                                                  "owned": n in coord.store["collection"]} for n in nums],
                                       "count": len(coord.setdb)})
