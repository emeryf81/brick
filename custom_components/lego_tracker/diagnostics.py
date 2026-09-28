"""Diagnostics download (Settings -> Devices & services -> ... -> Download diagnostics)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BRICKSET_KEY, CONF_NOTIFY, DOMAIN

REDACT = {CONF_BRICKSET_KEY, CONF_NOTIFY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coord = hass.data[DOMAIN][entry.entry_id]
    offers = coord.store["offers"]
    per_retailer: dict[str, dict[str, int]] = {}
    for by_retailer in offers.values():
        for rid, o in by_retailer.items():
            r = per_retailer.setdefault(rid, {"offers": 0, "ok": 0, "errors": 0})
            r["offers"] += 1
            r["ok" if o.get("available") else "errors"] += 1
    return {
        "options": async_redact_data(dict(entry.options), REDACT),
        "transport": coord.fetcher.transport,
        "counts": {"sets": len(coord.store["sets"]), "collection": len(coord.store["collection"]),
                   "snapshots": len(coord.store["snapshots"])},
        "per_retailer": per_retailer,
        "cooldown_hours": {r: round(coord.fetcher.cooldown_left(r) / 3600, 2) for r in coord.retailers},
        "errors": sorted({o.get("error") for by in offers.values() for o in by.values() if o.get("error")}),
    }
