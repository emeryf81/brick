"""Websocket API used by the sidebar panel."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, RETAILERS
from .csv_import import analyze_csv
from .models import combined_history, normalize_set_number, offer_price


def _languages() -> dict[str, str]:
    from .i18n import LANGUAGES

    return LANGUAGES


def _coord(hass: HomeAssistant):
    entries = hass.data.get(DOMAIN, {})
    return next(iter(entries.values()), None)


def _card(coord, num: str, with_history: bool = False) -> dict[str, Any]:
    s = coord.store["sets"][num]
    st = (coord.data or coord.compute())["statuses"].get(num, {})
    offers = coord.store["offers"].get(num, {})
    coll = coord.store["collection"].get(num)
    series = combined_history(offers)
    card = {
        **s, **st,
        "watched": coll is None,
        "owned": coll is not None,
        "collection": coll,
        "spark": [p for _, p in series][-60:],
        "offers": {
            rid: {"label": RETAILERS[rid][0], "url": o.get("url"), "price": offer_price(o), "auto_price": o.get("auto_price") if o.get("manual_price") else None,
                  "manual_price": (o.get("manual_price") or {}).get("price"), "manual_url": bool(o.get("manual_url")),
                  "available": o.get("available"), "error": o.get("error"), "checked": o.get("last_checked"),
                  "low": min((p for _, p in o.get("history", [])), default=None),
                  "title": o.get("title"), "link_status": o.get("link_status"), "link_reason": o.get("link_reason"),
                  "ignored": bool(o.get("error") and o.get("ignored_error") == o.get("error"))}
            for rid, o in offers.items() if rid in RETAILERS
        },
    }
    if with_history:
        card["history"] = {rid: o.get("history", []) for rid, o in offers.items()}
        card["combined"] = series
    return card


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
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
    websocket_api.async_register_command(hass, ws_notify_set)
    websocket_api.async_register_command(hass, ws_notify_test)
    hass.http.register_view(UserscriptView())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def ws_overview(hass, connection, msg):
    coord = _coord(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_loaded", "LEGO Price Tracker is not loaded")
        return
    from . import VERSION

    connection.send_result(msg["id"], {
        "version": VERSION, "transport": coord.fetcher.transport,
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
        "userscript_last": coord.store.get("userscript_last"),
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
                                  vol.Required("source"): vol.In(["brickset", "rebrickable"]),
                                  vol.Optional("key"): str})
@websocket_api.async_response
async def ws_test_key(hass, connection, msg):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .client import test_metadata_source

    coord = _coord(hass)
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
        body = (template.replace("{{VERSION}}", VERSION).replace("{{MATCHES}}", matches)
                .replace("{{HA_URL}}", base).replace("{{SELF_URL}}", base + self.url))
        # {{t:English}} -> translated text, {{tj:English}} -> translated JS string literal
        body = re.sub(r"\{\{tj:(.+?)\}\}", lambda m: json.dumps(tr(m.group(1))), body)
        body = re.sub(r"\{\{t:(.+?)\}\}", lambda m: tr(m.group(1)), body)
        return web.Response(text=body, content_type="text/javascript", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})
