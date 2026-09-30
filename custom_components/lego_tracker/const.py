"""Constants for the LEGO Price Tracker integration."""
from __future__ import annotations

DOMAIN = "lego_tracker"
STORAGE_KEY = f"{DOMAIN}.data"
STORAGE_VERSION = 1

PANEL_URL = "lego-tracker"
PANEL_ELEMENT = "lego-tracker-panel"
STATIC_URL = f"/{DOMAIN}_static"

CONF_DISCOUNT_THRESHOLD = "discount_threshold"
CONF_UPDATE_HOURS = "update_hours"
CONF_RETAILERS = "retailers"
CONF_DIGEST_TIME = "digest_time"
CONF_BRICKSET_KEY = "brickset_api_key"
CONF_MIN_HISTORY_DAYS = "min_history_days"
CONF_IMPERSONATE = "use_impersonation"
CONF_NOTIFY = "notify_service"
CONF_REBRICKABLE_KEY = "rebrickable_api_key"
CONF_BOL_CLIENT_ID = "bol_client_id"        # bol.com affiliate / Marketing Catalog API
CONF_BOL_CLIENT_SECRET = "bol_client_secret"
CONF_BOL_COUNTRY = "bol_country"            # auto | NL | BE
CONF_RELAY = "browser_relay"                # userscript fetches shop pages from the user's browser
CONF_RELAY_HOURS = "relay_hours"
DEFAULT_RELAY_HOURS = 6
CONF_BRICKWATCH = "brickwatch"              # hidden option: price-comparison sites as extra price sources
CONF_BLOCK_WORDS = "block_words"            # your own words: a product with one of these is never the set
CONF_ALLOW_WORDS = "allow_words"            # exceptions: words/phrases that may appear in a real set's title
CONF_COMPARE_SOURCES = "compare_sources"    # which comparison sites (default: all)
COMPARE_FRESH_HOURS = 6                     # re-use a comparison page this long
COMPARE_MISSING_HOURS = 24                  # a set a site doesn't have: no retry within a day
COMPARE_NET_ERRORS = 5                      # this many network errors in a row ...
COMPARE_PAUSE_HOURS = 1                     # ... pause that site this long
CONF_AUTO_REFRESH = "auto_refresh"
CONF_REFRESH_TIMES = "refresh_times"
CONF_REFRESH_MODE = "refresh_mode"          # spread | times | off
CONF_SPREAD_HOURS = "spread_hours"
DEFAULT_REFRESH_MODE = "spread"
DEFAULT_SPREAD_HOURS = 24
CONF_LANGUAGE = "language"
DEFAULT_REFRESH_TIMES = "07:30, 19:30"

DEFAULT_DISCOUNT_THRESHOLD = 25
DEFAULT_UPDATE_HOURS = 6
DEFAULT_DIGEST_TIME = "08:00:00"
DEFAULT_MIN_HISTORY_DAYS = 3

# retailer id -> (label, currency)
RETAILERS: dict[str, tuple[str, str]] = {
    "lego_com": ("LEGO.com", "EUR"),
    "amazon_nl": ("Amazon.nl", "EUR"),
    "amazon_de": ("Amazon.de", "EUR"),
    "amazon_be": ("Amazon.com.be", "EUR"),
    "bol": ("bol.com", "EUR"),
    "kruidvat_be": ("Kruidvat.be", "EUR"),
    "dreamland_be": ("Dreamland.be", "EUR"),
}
DEFAULT_RETAILERS = list(RETAILERS)
BUILTIN_RETAILERS = tuple(RETAILERS)

# Shops handled by the generic parser (JSON-LD / meta tags) and a search URL template.
# {query} is replaced by the url-encoded "LEGO <set number>". Editable in the settings panel.
GENERIC_SHOPS: dict[str, dict[str, str]] = {
    "dreamland_be": {"domain": "dreamland.be", "search": "https://www.dreamland.be/e/nl/search?q={query}"},
}
# Search URL per shop. {query} = url-encoded "LEGO <set number>", {number} = set number,
# {locale} = LEGO.com locale (e.g. nl-be). All editable in the settings panel.
DEFAULT_SEARCH: dict[str, str] = {
    "lego_com": "https://www.lego.com/{locale}/search?q={number}",
    "amazon_nl": "https://www.amazon.nl/s?k={query}",
    "amazon_de": "https://www.amazon.de/s?k={query}",
    "amazon_be": "https://www.amazon.com.be/s?k={query}",
    "bol": "https://www.bol.com/nl/nl/s/?searchtext={query}",
    "kruidvat_be": "https://www.kruidvat.be/nl/search?text={query}",
    "dreamland_be": "https://www.dreamland.be/e/nl/search?q={query}",
}
CONF_LEGO_LOCALE = "lego_locale"
DEFAULT_LEGO_LOCALE = "nl-be"
CONF_CUSTOM_SHOPS = "custom_shops"          # [{"id","name","domain","search"}]
CONF_SHOP_SEARCH = "shop_search"            # {shop_id: template} overrides for generic shops
CONF_NO_AUTOPAUSE = "no_autopause"          # [shop_id] never auto-paused after a block
CONF_KNOWN_SHOPS = "known_shops"            # shops the user has seen (new built-ins get enabled once)
CONF_VALUE_SOURCE = "value_source"          # "shop_first" | "import_first"

EVENT_DIGEST = f"{DOMAIN}_daily_digest"
EVENT_NEW_LOW = f"{DOMAIN}_new_all_time_low"
EVENT_HIGH_DISCOUNT = f"{DOMAIN}_high_discount"
EVENT_TARGET_HIT = f"{DOMAIN}_target_price_reached"
EVENT_JOB_DONE = f"{DOMAIN}_job_finished"

# Max price observations kept per offer (one per day is ~10 years).
MAX_HISTORY = 4000
