"""Shop registry, built from the shop settings file the user imports (plus shops added by hand).

The integration contains no list of shops or websites: until a settings file is imported and its terms are
accepted, there are no shops and nothing is fetched (see ready()). RETAILERS (label, currency), GENERIC_SHOPS
(domain, search template) and SEARCH are module-level dicts shared by the whole integration; apply_shop_options()
rebuilds them from the config entry options.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlparse
from typing import Any

from .i18n import LocalizedError
from .const import DEFAULT_LEGO_LOCALE, DEFAULT_SEARCH, GENERIC_SHOPS, RETAILERS

SETTINGS_FORMAT = "brick-shop-settings"
OLD_FORMATS = ("lot-shop-settings",)            # files written before the name B.R.I.C.K. are read as well
SETTINGS_VERSION = 1
LEGAL_VERSION = 1                 # raise when the terms change: everyone accepts them again at the next import
CONF_SHOP_PROFILE = "shop_profile"
CONF_LEGAL = "legal"
CONF_SETUP_VERSION = "setup_version"   # 1: installed (or settings withdrawn) since shop settings exist: nothing to carry over
# how a shop's pages are read (the page structure the parser knows), not a website:
#   lego         the official LEGO shop (RRP, image, name, retirement)
#   marketplace  a marketplace with a "buy box" (the price of the seller that wins it) and product codes in the address
#   partner      a shop with a Dutch and a Belgian site and a partner API for prices
#   retail       a shop whose product data sits in JSON-LD and data attributes
#   generic      any other shop (JSON-LD / meta data)
READERS = ("lego", "marketplace", "partner", "retail", "generic")
FIXED_IDS = {"lego": "lego_com"}   # the official shop has its own logic (RRP): one shop, this id
SINGLE_READERS = ("lego", "partner")   # at most one shop each
# how a comparison site is read: a page with the market value of a set, a search with links to a product page with
# its shops, a page whose data sits in an Inertia "data-page" attribute, or a search result list (one shop per result)
COMPARE_READERS = ("listing", "product_list", "inertia", "market")
MARKET = "market"                  # internal id of the site read as "market" (market value + retirement date)

SEARCH: dict[str, str] = dict(DEFAULT_SEARCH)      # effective search template per shop
LOCALE = {"lego": DEFAULT_LEGO_LOCALE, "partner": "nl"}  # "partner": which of its sites is asked, "nl" (/nl/nl/) or "be" (/be/nl/)

# other websites in the settings file: set data, the set database download and the partner API
DATA_SOURCES = {"set_data_api": "Set data API", "set_data_page": "Set data page", "parts_api": "Parts database API",
                "set_database_sets": "Set database (sets)", "set_database_themes": "Set database (themes)",
                "partner_api_token": "Partner API login", "partner_api": "Partner API"}
_LEGACY: dict[str, Any] = {}


def legacy() -> dict[str, Any]:
    """How identifiers of versions before 1.1 translate to the current ones (in data/previous_shop_settings.json)."""
    if not _LEGACY:
        import json
        from pathlib import Path

        try:
            _LEGACY.update(json.loads((Path(__file__).parent / "data" / "previous_shop_settings.json").read_text("utf-8")).get("legacy") or {})
        except (OSError, ValueError):
            pass
        _LEGACY.setdefault("readers", {})
    return _LEGACY


def _upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """A settings file written for a version before 1.1: its old reader names and data source keys, translated."""
    old = legacy()
    data = dict(data)
    if isinstance(data.get("shops"), list):
        data["shops"] = [dict(x, reader=old["readers"].get(x.get("reader"), x.get("reader"))) if isinstance(x, dict) else x
                         for x in data["shops"]]
    if isinstance(data.get("comparison_sites"), list):
        data["comparison_sites"] = [dict(x, reader=x.get("reader") or (old.get("comparison_readers") or {}).get(x.get("id"), "listing"))
                                    if isinstance(x, dict) else x for x in data["comparison_sites"]]
    if isinstance(data.get("data_sources"), dict):
        data["data_sources"] = {(old.get("data_sources") or {}).get(k, k): v for k, v in data["data_sources"].items()}
    return data
PROFILE: dict[str, dict[str, Any]] = {}            # shop id -> its entry in the imported settings file
SOURCES: dict[str, str] = {}                        # data source key -> its address in the settings file
COMPARE: dict[str, dict[str, Any]] = {}            # internal source id -> comparison site entry
ALIASES: list[tuple[str, str]] = []                 # (shop name as comparison sites write it, shop id), longest first
STATE = {"profile": False, "legal": False}


def shop_id(name: str) -> str:
    return "c_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "shop")[:30]


def _domain(value: Any) -> str:
    return re.sub(r"^https?://(www\.)?", "", str(value or "").strip().lower()).split("/")[0]


def _on(url: str, domain: str) -> bool:
    host = (urlparse(url.replace("{query}", "x").replace("{number}", "1").replace("{locale}", "nl-be")).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def validate_custom_shop(shop: dict[str, Any]) -> dict[str, str]:
    name = str(shop.get("name", "")).strip()[:40]
    domain = _domain(shop.get("domain"))
    search = str(shop.get("search", "")).strip()
    if not name:
        raise LocalizedError("Give the shop a name.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise LocalizedError("Invalid domain {domain} (e.g. example-shop.be).", domain=domain)
    if search and (not valid_search(search) or not _on(search, domain)):   # the shop's own host
        raise LocalizedError("The search URL must start with https://, be on the shop's domain and contain {query} or {number}.")
    return {"id": str(shop.get("id") or shop_id(name)), "name": name, "domain": domain, "search": search}


def valid_search(tpl: str) -> bool:
    return tpl.startswith("https://") and ("{query}" in tpl or "{number}" in tpl) and " " not in tpl


def _https_on(url: Any, domain: str) -> bool:
    return isinstance(url, str) and url.startswith("https://") and " " not in url and _on(url, domain)


def validate_settings(data: Any) -> dict[str, Any]:
    """A shop settings file, checked and cleaned. Raises LocalizedError with what is wrong."""
    if not isinstance(data, dict) or data.get("format") not in (SETTINGS_FORMAT, *OLD_FORMATS):
        raise LocalizedError("This is not a shop settings file for this integration.")
    if data.get("version") != SETTINGS_VERSION:
        raise LocalizedError("Shop settings file version {version} is not supported.", version=data.get("version"))
    data = _upgrade(data)
    for key, kind in (("shops", list), ("comparison_sites", list), ("data_sources", dict)):
        if data.get(key) is not None and not isinstance(data[key], kind):
            raise LocalizedError("{field}: wrong type in the shop settings file.", field=key)
    shops, seen = [], set()
    for raw in data.get("shops") or []:
        if not isinstance(raw, dict):
            raise LocalizedError("Every shop must be an object with id, name, domain and reader.")
        rid, name, domain = str(raw.get("id") or "").strip(), str(raw.get("name") or "").strip()[:40], _domain(raw.get("domain"))
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", rid) or rid.startswith("c_") or rid in seen:
            raise LocalizedError("Shop id {id}: use lowercase letters, digits and _, unique, not starting with c_.", id=rid)
        if not name or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
            raise LocalizedError("Shop {id}: a name and a valid domain are needed.", id=rid)
        reader = raw.get("reader") or "generic"
        if reader not in READERS:
            raise LocalizedError("Shop {id}: unknown reader {reader} (one of: {readers}).", id=rid, reader=reader, readers=", ".join(READERS))
        if (fixed := FIXED_IDS.get(reader)) and rid != fixed:
            raise LocalizedError("Shop {id}: a shop read as {reader} must have the id {fixed}.", id=rid, reader=reader, fixed=fixed)
        if reader in SINGLE_READERS and any(x["reader"] == reader for x in shops):
            raise LocalizedError("Shop {id}: only one shop can be read as {reader}.", id=rid, reader=reader)
        shop = {"id": rid, "name": name, "domain": domain, "reader": reader}
        for key in ("search", "home", "product"):
            if (v := raw.get(key)) in (None, ""):
                continue
            if not _https_on(v, domain) or (key == "search" and not valid_search(v)) or (key == "product" and "{number}" not in v):
                raise LocalizedError("Shop {id}: {field} must be an https:// address on {domain}.", id=rid, field=key, domain=domain)
            shop[key] = v
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list) or not all(isinstance(x, str) for x in aliases):
            raise LocalizedError("{field}: wrong type in the shop settings file.", field=f"{rid}.aliases")
        shop["aliases"] = [a for a in (x.strip().lower()[:40] for x in aliases) if len(a) >= 3][:20]
        seen.add(rid)
        shops.append(shop)
    if not shops:
        raise LocalizedError("The shop settings file contains no shops.")
    if len(shops) > 50:
        raise LocalizedError("At most 50 shops in one settings file.")
    sites = []
    for raw in data.get("comparison_sites") or []:
        if not isinstance(raw, dict):
            raise LocalizedError("{field}: wrong type in the shop settings file.", field="comparison_sites")
        sid = str(raw.get("id") or "").strip()
        raw_hosts, raw_langs = raw.get("hosts"), raw.get("langs")
        if not isinstance(raw_hosts, list) or not all(isinstance(x, str) for x in raw_hosts) or (
                raw_langs is not None and (not isinstance(raw_langs, list) or not all(isinstance(x, str) for x in raw_langs))):
            raise LocalizedError("Comparison site {id}: an id, hosts and start addresses are needed.", id=sid or "?")
        hosts = [h for h in (x.lower().strip() for x in raw_hosts) if h]
        start = raw.get("start") if isinstance(raw.get("start"), dict) else {}
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", sid) or not hosts or not start or sid in {s["id"] for s in sites}:
            raise LocalizedError("Comparison site {id}: an id, hosts and start addresses are needed.", id=sid or "?")
        for tpl in start.values():
            if not isinstance(tpl, str) or not tpl.startswith("https://") or (urlparse(tpl).hostname or "") not in hosts:
                raise LocalizedError("Comparison site {id}: every start address must be https:// on one of its hosts.", id=sid)
        reader = raw.get("reader") or "listing"
        if reader not in COMPARE_READERS or (reader == MARKET and any(x["reader"] == MARKET for x in sites)):
            raise LocalizedError("Comparison site {id}: unknown reader {reader} (one of: {readers}; market only once).",
                                 id=sid, reader=reader, readers=", ".join(COMPARE_READERS))
        sites.append({"id": sid, "name": str(raw.get("name") or sid)[:40], "reader": reader, "hosts": hosts,
                      "start": {str(k).upper() if k != "*" else "*": v for k, v in start.items()},
                      "langs": [x.lower() for x in raw_langs or []][:10]})
    raw_sources = data.get("data_sources") or {}
    if not isinstance(raw_sources, dict):
        raise LocalizedError("data_sources must be an object of addresses.")
    sources = {}
    for key, url in raw_sources.items():
        if key not in DATA_SOURCES:
            raise LocalizedError("Unknown data source {key} (one of: {keys}).", key=key, keys=", ".join(DATA_SOURCES))
        if not isinstance(url, str) or not url.startswith("https://") or " " in url or not urlparse(url).hostname \
                or (key == "set_data_page" and "{number}" not in url):
            raise LocalizedError("Data source {key}: must be an https:// address.", key=key)
        sources[key] = url
    locale = str(data.get("lego_locale") or "").lower()
    out = {"format": SETTINGS_FORMAT, "version": SETTINGS_VERSION, "shops": shops, "comparison_sites": sites, "data_sources": sources}
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", locale):
        out["lego_locale"] = locale
    return out


def previous_settings(options: dict[str, Any]) -> dict[str, Any] | None:
    """An installation from before version 1.0.0 keeps the shops it used: they are carried over once (the terms
    still have to be accepted before anything is fetched). New installations start without shops."""
    import json
    from pathlib import Path

    if options.get(CONF_SHOP_PROFILE) or options.get(CONF_SETUP_VERSION):
        return None
    try:
        return validate_settings(json.loads((Path(__file__).parent / "data" / "previous_shop_settings.json").read_text("utf-8")))
    except (OSError, ValueError):
        return None


# API keys and the data sources they are sent to: a key is only ever sent to the host(s) it was entered or used with
CONF_KEY_HOSTS = "key_hosts"
KEY_SOURCES = {"set_data_api_key": ("set_data_api",), "parts_api_key": ("parts_api",),
               "partner_client_id": ("partner_api_token", "partner_api"), "partner_client_secret": ("partner_api_token", "partner_api")}


def upgrade_options(options: dict[str, Any]) -> dict[str, Any] | None:
    """Options of a version before 1.1 with the current names (API keys, the hosts they may go to, the
    comparison sites in use, the imported settings). None when nothing changes."""
    old = legacy()
    names, ids = old.get("options") or {}, old.get("compare_ids") or {}
    new = {k: v for k, v in options.items() if k not in names}
    for k, v in options.items():
        if k in names:
            new.setdefault(names[k], v)            # an option under its current name wins over its old name
    if isinstance(new.get(CONF_KEY_HOSTS), dict):
        new[CONF_KEY_HOSTS] = {names.get(k, k): v for k, v in new[CONF_KEY_HOSTS].items()}
    if isinstance(new.get("compare_sources"), list):
        new["compare_sources"] = [ids.get(x, x) for x in new["compare_sources"]]
    if isinstance(new.get(CONF_SHOP_PROFILE), dict):
        try:
            new[CONF_SHOP_PROFILE] = validate_settings(new[CONF_SHOP_PROFILE])
        except (ValueError, TypeError, AttributeError):
            pass
    return new if new != options else None


def source_hosts(sources: tuple[str, ...], profile: dict[str, Any] | None = None) -> list[str]:
    """The hosts of these data sources: in a settings file, or (without one) in the settings in force."""
    data = (profile or {}).get("data_sources") or {} if profile is not None else SOURCES
    return [(urlparse(data[s]).hostname or "").lower() if data.get(s) else "" for s in sources]


def keys_for_moved_sources(options: dict[str, Any], profile: dict[str, Any]) -> set[str]:
    """Stored API keys that an imported file would send to another host than the one they were entered or used with,
    or that have no known host at all: they are removed, so importing a file can never hand a key to a new address
    (you enter it again if you trust the new address)."""
    known = options.get(CONF_KEY_HOSTS) or {}
    out = set()
    for key, sources in KEY_SOURCES.items():
        if not options.get(key):
            continue
        before = known.get(key) or source_hosts(sources, options.get(CONF_SHOP_PROFILE) or {})
        if not any(before) or before != source_hosts(sources, profile):
            out.add(key)
    return out


def key_hosts(profile: dict[str, Any] | None, options: dict[str, Any]) -> dict[str, list[str]]:
    """The hosts every stored key may be sent to (recorded at import, at the carry-over and when you enter a key)."""
    return {key: source_hosts(sources, profile) for key, sources in KEY_SOURCES.items() if options.get(key)}


def key_allowed(options: dict[str, Any], key: str) -> bool:
    """May this stored key be sent to the sources in force? Only to the hosts it was recorded with."""
    recorded = (options.get(CONF_KEY_HOSTS) or {}).get(key)
    return bool(recorded) and any(recorded) and recorded == source_hosts(KEY_SOURCES[key])


def legal_ok(options: dict[str, Any]) -> bool:
    legal = options.get(CONF_LEGAL) or {}
    return bool(legal.get("accepted")) and legal.get("version") == LEGAL_VERSION


def revoke() -> None:
    """Withdrawn: from this moment on nothing is fetched, also before the integration has reloaded."""
    STATE["profile"] = STATE["legal"] = False


def ready() -> bool:
    """Shop settings imported and their terms accepted: only then does the integration contact any website."""
    return STATE["profile"] and STATE["legal"]


def source_url(key: str) -> str | None:
    """The address of a data source from the shop settings, only when they are imported and their terms accepted."""
    return SOURCES.get(key) if ready() else None


def profile_ids() -> list[str]:
    return list(PROFILE)


def reader_of(rid: str) -> str:
    return (PROFILE.get(rid) or {}).get("reader") or "generic"


def apply_shop_options(options: dict[str, Any]) -> None:
    """Rebuild the registry from the options (imported settings + custom shops + search overrides)."""
    from . import compare        # compare imports parsers, which imports this module

    profile = options.get(CONF_SHOP_PROFILE)
    try:
        profile = validate_settings(profile) if profile else None
    except (ValueError, TypeError, AttributeError):
        profile = None
    STATE["profile"], STATE["legal"] = profile is not None, legal_ok(options)
    RETAILERS.clear()
    GENERIC_SHOPS.clear()
    DEFAULT_SEARCH.clear()
    PROFILE.clear()
    COMPARE.clear()
    ALIASES.clear()
    SOURCES.clear()
    SOURCES.update((profile or {}).get("data_sources", {}))
    for shop in (profile or {}).get("shops", []):
        PROFILE[shop["id"]] = shop
        RETAILERS[shop["id"]] = (shop["name"], "EUR")
        if shop.get("search"):
            DEFAULT_SEARCH[shop["id"]] = shop["search"]
        if shop["reader"] == "generic":
            GENERIC_SHOPS[shop["id"]] = {"domain": shop["domain"], "search": shop.get("search", "")}
        ALIASES.extend((a, shop["id"]) for a in shop["aliases"])
    for site in (profile or {}).get("comparison_sites", []):
        COMPARE[MARKET if site["reader"] == MARKET else site["id"]] = site
    ALIASES.sort(key=lambda a: -len(a[0]))
    compare.load_sites(COMPARE)
    for shop in options.get("custom_shops", []) or []:
        try:
            s = validate_custom_shop(shop)
        except ValueError:
            continue
        if s["id"] in PROFILE:
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
    LOCALE["lego"] = (options.get("lego_locale") or (profile or {}).get("lego_locale") or DEFAULT_LEGO_LOCALE).lower()
    LOCALE["partner"] = partner_site(options)
    for rid in PROFILE:
        if reader_of(rid) == "partner" and SEARCH.get(rid) == DEFAULT_SEARCH.get(rid):   # your own search URL stays as you wrote it
            SEARCH[rid] = partner_site_url(SEARCH[rid])


def export_settings(options: dict[str, Any]) -> dict[str, Any]:
    """The settings in force, as a file you can import again (imported shops, shops added by hand, your search URLs)."""
    from .i18n import T

    profile = options.get(CONF_SHOP_PROFILE) or {"format": SETTINGS_FORMAT, "version": SETTINGS_VERSION, "shops": [], "comparison_sites": []}
    out = {"format": SETTINGS_FORMAT, "version": SETTINGS_VERSION, "notice": T("You decide yourself which websites your Home Assistant contacts, and you alone are responsible for everything that happens between your Home Assistant and those websites: respecting their terms of use, robots.txt and the law that applies to you, and all legal consequences. The software is provided as is, without any warranty; to the fullest extent permitted by law, its author and contributors cannot be held liable. Use it only for personal, non-commercial purposes and moderately. LEGO and the names of shops are trademarks of their owners; this project is not affiliated with or endorsed by any of them.")}
    if LOCALE["lego"]:
        out["lego_locale"] = LOCALE["lego"]
    shops = []
    for shop in profile.get("shops", []):
        s = dict(shop)
        if (tpl := (options.get("shop_search") or {}).get(s["id"])) and valid_search(tpl):
            s["search"] = tpl
        shops.append(s)
    out["shops"] = shops
    out["comparison_sites"] = profile.get("comparison_sites", [])
    out["data_sources"] = profile.get("data_sources", {})
    out["custom_shops"] = [s for s in options.get("custom_shops", []) or []]
    return out


def compare_start(source: str, num: str, lego_locale: str | None, ean: str | None) -> str | None:
    """Where a comparison site starts for a set; None when it does not cover this country (or needs an EAN)."""
    site = COMPARE.get(source)
    if not site:
        return None
    parts = (lego_locale or "nl-be").lower().split("-")
    lang, cc = parts[0], (parts[1] if len(parts) > 1 else "be").upper()
    tpl = site["start"].get(cc) or site["start"].get("*")
    if not tpl or ("{ean14}" in tpl and not (ean and ean.isdigit())):
        return None
    if site.get("langs") and lang not in site["langs"]:
        lang = site["langs"][-1]
    return (tpl.replace("{query}", quote_plus(f"lego {num}")).replace("{number}", quote_plus(num)).replace("{lang}", lang)
            .replace("{cc_lower}", cc.lower()).replace("{cc}", cc).replace("{ean14}", (ean or "").zfill(14)))


def partner_site(options: dict[str, Any]) -> str:
    """The site of the partner shop to ask: "be" (/be/nl/) or "nl" (/nl/nl/), as chosen in Settings.
    Not chosen yet ("auto"): /nl/nl/, the site that was always asked before the choice existed."""
    return "be" if str(options.get("partner_country") or "").upper() == "BE" else "nl"


def partner_path_re() -> re.Pattern[str] | None:
    """/nl/nl/ or /be/nl/ in an address of the partner shop."""
    rid = shop_with_reader("partner")
    dom = domain_of(rid) if rid else None
    return re.compile(rf"({re.escape(dom)})/(?:nl|be)/nl/", re.I) if dom else None


def partner_site_url(url: str) -> str:
    """The same page of the partner shop on the chosen site (/nl/nl/p/... ↔ /be/nl/p/...)."""
    rx = partner_path_re()
    return rx.sub(lambda m: f"{m.group(1)}/{LOCALE['partner']}/nl/", url or "") if rx else (url or "")


def domain_of(rid: str) -> str | None:
    return (PROFILE.get(rid) or {}).get("domain") or GENERIC_SHOPS.get(rid, {}).get("domain")


def home_of(rid: str) -> str | None:
    """The page a visit to this shop starts on (as a visitor would): from the settings, else the shop's root."""
    home = (PROFILE.get(rid) or {}).get("home")
    if home:
        return partner_site_url(home) if reader_of(rid) == "partner" else home
    return f"https://www.{d}/" if (d := domain_of(rid)) else None


def site_root(rid: str) -> str | None:
    """https://www.<domain>, for links in search results that are relative to the shop."""
    return f"https://www.{d}" if (d := domain_of(rid)) else None


def shop_with_reader(reader: str) -> str | None:
    return next((rid for rid, s in PROFILE.items() if s["reader"] == reader), None)


def all_domains() -> dict[str, str]:
    return {rid: d for rid in RETAILERS if (d := domain_of(rid))}


def readers_by_domain() -> dict[str, str]:
    return {d: reader_of(rid) for rid, d in all_domains().items()}

