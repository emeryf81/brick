"""Websocket API used by the sidebar panel."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
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
