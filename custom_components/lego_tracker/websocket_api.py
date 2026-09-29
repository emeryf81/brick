"""Websocket API used by the sidebar panel."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, RETAILERS
from .csv_import import analyze_csv
from .models import combined_history, normalize_set_number


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
            rid: {"label": RETAILERS[rid][0], "url": o.get("url"), "price": o.get("last_price") if o.get("available") else None,
                  "available": o.get("available"), "error": o.get("error"), "checked": o.get("last_checked"),
                  "low": min((p for _, p in o.get("history", [])), default=None),
                  "title": o.get("title"), "link_status": o.get("link_status"), "link_reason": o.get("link_reason")}
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


class UserscriptView(HomeAssistantView):
    """Serves a Tampermonkey userscript generated for this HA instance and the configured shops.

    No auth: Tampermonkey fetches it outside HA. It contains no secrets (the token is entered
    by the user inside Tampermonkey)."""

    url = "/api/lego_tracker/lego-tracker.user.js"
    name = "api:lego_tracker:userscript"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        from . import VERSION
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
        return web.Response(text=body, content_type="text/javascript", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})
