"""Built-in set catalogue (data/sets.json): sets already read on the official shop, shipped with the integration.

A set in the catalogue needs no lookup online when it is added: name, RRP, theme, year, pieces, image, EAN
and exit date come from here (the link to the official shop is built from your shop settings). Only unknown
sets are looked up online. Values you typed yourself always win; the catalogue only fills gaps.
Rebuild it with scripts/build_catalog.py.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .models import set_pieces

_LOGGER = logging.getLogger(__name__)
PATH = Path(__file__).parent / "data" / "sets.json"
SOURCE = "LEGO.com"                    # the catalogue holds data of the official shop: counts as such (no re-fetch)
_SETS: dict[str, dict[str, Any]] | None = None


def load() -> dict[str, dict[str, Any]]:
    """Read the file once (call from an executor)."""
    global _SETS
    if _SETS is None:
        try:
            _SETS = json.loads(PATH.read_text("utf-8")).get("sets") or {}
        except (OSError, ValueError) as err:
            _LOGGER.warning("Set catalogue not readable: %s", err)
            _SETS = {}
    return _SETS


def get(num: str) -> dict[str, Any] | None:
    return (_SETS or {}).get(num)


def apply(num: str, s: dict[str, Any], offers: dict[str, Any]) -> bool:
    """Fill empty set fields (and the LEGO.com link) from the catalogue. True if the set is in it."""
    c = get(num)
    if not c:
        return False
    if c.get("name") and not s.get("name"):
        s["name"], s["name_source"] = c["name"], SOURCE
    if c.get("rrp") and not s.get("rrp"):
        s["rrp"], s["rrp_source"] = c["rrp"], SOURCE
    if c.get("image") and not s.get("image"):
        s["image"], s["image_source"] = c["image"], SOURCE
    if c.get("pieces"):
        set_pieces(s, c["pieces"], "catalog")
    for key in ("theme", "subtheme", "year", "ean"):
        if c.get(key) and not s.get(key):
            s[key] = c[key]
    if c.get("exit_date") and not s.get("exit_date"):
        s["exit_date"], s["exit_date_source"] = c["exit_date"], SOURCE
    if c.get("status") and not s.get("availability"):
        s["availability"] = c["status"]                       # 'retail' | 'retired'
    from .parsers import lego_product_url                       # the address comes from your shop settings

    if c.get("status") != "retired" and not (offers.get("lego_com") or {}).get("url") and (url := lego_product_url(num)):
        old = offers.get("lego_com") or {}
        offers["lego_com"] = {**old, "url": url, "history": old.get("history", []), "found": time.time(), "via": "catalog"}
    s["catalog"] = True
    return True


def complete(s: dict[str, Any]) -> bool:
    """Everything a lookup would give is there: no need to ask Brickset/Rebrickable/LEGO.com."""
    return all(s.get(k) for k in ("name", "theme", "year", "image")) and s.get("rrp_source") in (SOURCE, "user")
