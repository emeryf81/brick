"""Built-in set catalogue (data/sets.json): sets already read on LEGO.com, shipped with the integration.

A set in the catalogue needs no LEGO.com lookup and no Brickset/Rebrickable request when it is added:
name, RRP, theme, year, pieces, image, EAN, LEGO.com link and exit date come from here. Only unknown
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
SOURCE = "LEGO.com"                    # the catalogue holds LEGO.com data: counts as such (no re-fetch)
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
        set_pieces(s, c["pieces"], SOURCE)
    for key in ("theme", "subtheme", "year", "ean"):
        if c.get(key) and not s.get(key):
            s[key] = c[key]
    if c.get("exit_date") and not s.get("exit_date"):
        s["exit_date"], s["exit_date_source"] = c["exit_date"], SOURCE
    if c.get("status") and not s.get("availability"):
        s["availability"] = c["status"]                       # 'retail' | 'retired'
    if c.get("lego_url") and not (offers.get("lego_com") or {}).get("url"):
        old = offers.get("lego_com") or {}
        offers["lego_com"] = {**old, "url": c["lego_url"], "history": old.get("history", []), "found": time.time(), "via": "catalog"}
    s["catalog"] = True
    return True


def complete(s: dict[str, Any]) -> bool:
    """Everything a lookup would give is there: no need to ask Brickset/Rebrickable/LEGO.com."""
    return all(s.get(k) for k in ("name", "theme", "year", "image")) and s.get("rrp_source") in (SOURCE, "user")
