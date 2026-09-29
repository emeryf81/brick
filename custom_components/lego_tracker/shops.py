"""Shop registry: built-in shops plus Dreamland and user-defined shops (generic parser).

RETAILERS (label, currency) and GENERIC_SHOPS (domain, search template) are module-level dicts
shared by the whole integration; apply_shop_options() syncs them with the config entry options.
"""
from __future__ import annotations

import re
from typing import Any

from .i18n import LocalizedError
from .const import BUILTIN_RETAILERS, DEFAULT_LEGO_LOCALE, DEFAULT_SEARCH, GENERIC_SHOPS, RETAILERS

SEARCH: dict[str, str] = dict(DEFAULT_SEARCH)      # effective search template per shop
LOCALE = {"lego": DEFAULT_LEGO_LOCALE}

_BUILTIN_GENERIC = {k: dict(v) for k, v in GENERIC_SHOPS.items()}
FIXED_DOMAINS = {"lego_com": "lego.com", "amazon_nl": "amazon.nl", "amazon_de": "amazon.de", "amazon_be": "amazon.com.be",
                 "bol": "bol.com", "kruidvat_be": "kruidvat.be"}


def shop_id(name: str) -> str:
    return "c_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "shop")[:30]


def validate_custom_shop(shop: dict[str, Any]) -> dict[str, str]:
    name = str(shop.get("name", "")).strip()[:40]
    domain = re.sub(r"^https?://(www\.)?", "", str(shop.get("domain", "")).strip().lower()).split("/")[0]
    search = str(shop.get("search", "")).strip()
    if not name:
        raise LocalizedError("Give the shop a name.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise LocalizedError("Invalid domain {domain} (e.g. dreamland.be).", domain=domain)
    if search and (not valid_search(search) or domain not in search):
        raise LocalizedError("The search URL must start with https://, be on the shop's domain and contain {query} or {number}.")
    return {"id": str(shop.get("id") or shop_id(name)), "name": name, "domain": domain, "search": search}


def valid_search(tpl: str) -> bool:
    return tpl.startswith("https://") and ("{query}" in tpl or "{number}" in tpl) and " " not in tpl


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
    SEARCH.clear()
    SEARCH.update(DEFAULT_SEARCH)
    for rid, g in GENERIC_SHOPS.items():
        if g.get("search"):
            SEARCH[rid] = g["search"]
    for rid, tpl in (options.get("shop_search", {}) or {}).items():
        if rid in RETAILERS and tpl and valid_search(tpl):
            SEARCH[rid] = tpl
            if rid in GENERIC_SHOPS:
                GENERIC_SHOPS[rid]["search"] = tpl
    LOCALE["lego"] = (options.get("lego_locale") or DEFAULT_LEGO_LOCALE).lower()


def domain_of(rid: str) -> str | None:
    return FIXED_DOMAINS.get(rid) or GENERIC_SHOPS.get(rid, {}).get("domain")


def all_domains() -> dict[str, str]:
    return {rid: d for rid in RETAILERS if (d := domain_of(rid))}
