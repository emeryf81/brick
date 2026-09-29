"""Shop registry: built-in shops plus Dreamland and user-defined shops (generic parser).

RETAILERS (label, currency) and GENERIC_SHOPS (domain, search template) are module-level dicts
shared by the whole integration; apply_shop_options() syncs them with the config entry options.
"""
from __future__ import annotations

import re
from typing import Any

from .const import BUILTIN_RETAILERS, GENERIC_SHOPS, RETAILERS

_BUILTIN_GENERIC = {k: dict(v) for k, v in GENERIC_SHOPS.items()}
FIXED_DOMAINS = {"amazon_nl": "amazon.nl", "amazon_de": "amazon.de", "amazon_be": "amazon.com.be",
                 "bol": "bol.com", "kruidvat_be": "kruidvat.be"}


def shop_id(name: str) -> str:
    return "c_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "shop")[:30]


def validate_custom_shop(shop: dict[str, Any]) -> dict[str, str]:
    name = str(shop.get("name", "")).strip()[:40]
    domain = re.sub(r"^https?://(www\.)?", "", str(shop.get("domain", "")).strip().lower()).split("/")[0]
    search = str(shop.get("search", "")).strip()
    if not name:
        raise ValueError("Geef de winkel een naam.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise ValueError(f"Ongeldig domein {domain!r} (bv. dreamland.be).")
    if search and (not search.startswith("https://") or "{query}" not in search or domain not in search):
        raise ValueError("Zoek-URL moet met https:// beginnen, op het domein van de winkel liggen en {query} bevatten.")
    return {"id": str(shop.get("id") or shop_id(name)), "name": name, "domain": domain, "search": search}


def apply_shop_options(options: dict[str, Any]) -> None:
    """Rebuild the registry from the options (custom shops + search overrides)."""
    for rid in [r for r in RETAILERS if r not in BUILTIN_RETAILERS]:
        del RETAILERS[rid]
    GENERIC_SHOPS.clear()
    GENERIC_SHOPS.update({k: dict(v) for k, v in _BUILTIN_GENERIC.items()})
    for shop in options.get("custom_shops", []) or []:
        try:
            s = validate_custom_shop(shop)
        except ValueError:
            continue
        RETAILERS[s["id"]] = (s["name"], "EUR")
        GENERIC_SHOPS[s["id"]] = {"domain": s["domain"], "search": s["search"]}
    for rid, tpl in (options.get("shop_search", {}) or {}).items():
        if rid in GENERIC_SHOPS and tpl:
            GENERIC_SHOPS[rid]["search"] = tpl


def domain_of(rid: str) -> str | None:
    return FIXED_DOMAINS.get(rid) or GENERIC_SHOPS.get(rid, {}).get("domain")


def all_domains() -> dict[str, str]:
    return {rid: d for rid in RETAILERS if (d := domain_of(rid))}
