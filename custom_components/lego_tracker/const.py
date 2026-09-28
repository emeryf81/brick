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

DEFAULT_DISCOUNT_THRESHOLD = 25
DEFAULT_UPDATE_HOURS = 6
DEFAULT_DIGEST_TIME = "08:00:00"
DEFAULT_MIN_HISTORY_DAYS = 3

# retailer id -> (label, currency)
RETAILERS: dict[str, tuple[str, str]] = {
    "amazon_nl": ("Amazon.nl", "EUR"),
    "amazon_de": ("Amazon.de", "EUR"),
    "amazon_be": ("Amazon.com.be", "EUR"),
    "bol": ("bol.com", "EUR"),
    "kruidvat_be": ("Kruidvat.be", "EUR"),
}
DEFAULT_RETAILERS = list(RETAILERS)

EVENT_DIGEST = f"{DOMAIN}_daily_digest"
EVENT_NEW_LOW = f"{DOMAIN}_new_all_time_low"
EVENT_HIGH_DISCOUNT = f"{DOMAIN}_high_discount"

# Max price observations kept per offer (one per day is ~10 years).
MAX_HISTORY = 4000
