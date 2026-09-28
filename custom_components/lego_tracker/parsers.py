"""HTML price extraction per retailer. Pure functions, regex only (no extra deps).

Retailer markup changes regularly; every parser therefore tries several strategies
(JSON-LD -> meta tags -> retailer specific markup) and returns None when unsure.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

from .models import parse_price


@dataclass
class Parsed:
    price: float | None
    title: str | None = None
    image: str | None = None
    blocked: bool = False
    unavailable: bool = False


AMAZON_DOMAINS = {"amazon_nl": "amazon.nl", "amazon_de": "amazon.de", "amazon_be": "amazon.com.be"}
BLOCK_MARKERS = (
    "api-services-support@amazon", "Type the characters you see", "Voer de tekens in",
    "Geben Sie die Zeichen", "/errors/validateCaptcha", "captcha", "Access Denied",
    "Just a moment...", "Attention Required",
)


def _jsonld_blocks(page: str):
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            yield json.loads(raw.strip())
        except ValueError:
            continue


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _from_jsonld(page: str) -> Parsed | None:
    for block in _jsonld_blocks(page):
        for node in _walk(block):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers")
            prices = []
            for off in _walk(offers) if offers else []:
                for key in ("price", "lowPrice"):
                    if (p := parse_price(off.get(key))) is not None:
                        avail = str(off.get("availability", ""))
                        if "OutOfStock" in avail or "SoldOut" in avail:
                            continue
                        prices.append(p)
            img = node.get("image")
            img = img[0] if isinstance(img, list) and img else img
            if prices:
                return Parsed(min(prices), node.get("name"), img if isinstance(img, str) else None)
    return None


def _meta(page: str, *props: str) -> str | None:
    for prop in props:
        m = re.search(
            rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']', page, re.I
        ) or re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\']{re.escape(prop)}["\']', page, re.I
        )
        if m:
            return htmllib.unescape(m.group(1))
    return None


def _title(page: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return htmllib.unescape(m.group(1)).strip() if m else None


def parse_generic(page: str) -> Parsed:
    if (p := _from_jsonld(page)) is not None:
        return p
    price = parse_price(_meta(page, "product:price:amount", "og:price:amount", "price"))
    return Parsed(price, _meta(page, "og:title") or _title(page), _meta(page, "og:image"))


def parse_amazon(page: str) -> Parsed:
    if any(marker.lower() in page[:200000].lower() for marker in BLOCK_MARKERS[:6]):
        return Parsed(None, blocked=True)
    title = None
    m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
    if m:
        title = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    image = None
    m = re.search(r'"hiRes"\s*:\s*"(https:[^"]+)"', page) or re.search(r'id="landingImage"[^>]+src="([^"]+)"', page)
    if m:
        image = m.group(1)
    # Buy-box / core price blocks first, then any first .a-price
    for scope_pattern in (
        r'id="(?:corePrice_feature_div|corePriceDisplay_desktop_feature_div|apex_desktop|buybox)".*?</div>\s*</div>\s*</div>',
        r'class="a-price[^"]*"[^>]*>.*?</span>\s*</span>',
    ):
        for scope in re.findall(scope_pattern, page, re.S)[:3]:
            m = re.search(r'class="a-offscreen"[^>]*>([^<]+)<', scope)
            if m and (price := parse_price(m.group(1))):
                return Parsed(price, title, image)
    m = re.search(r'"priceAmount"\s*:\s*([\d.]+)', page)
    if m and (price := parse_price(m.group(1))):
        return Parsed(price, title, image)
    unavailable = bool(re.search(r'id="outOfStock"|Momenteel niet verkrijgbaar|Derzeit nicht verfügbar|Currently unavailable', page))
    return Parsed(None, title, image, unavailable=unavailable)


def parse_bol(page: str) -> Parsed:
    if "Access Denied" in page[:2000] or "Just a moment" in page[:3000]:
        return Parsed(None, blocked=True)
    if (p := _from_jsonld(page)) is not None:
        return p
    m = re.search(
        r'class="promo-price"[^>]*>\s*([\d.]+)\s*<sup[^>]*class="promo-price__fraction"[^>]*>\s*(\d{2}|-)\s*</sup>', page, re.S
    )
    if m:
        frac = "00" if m.group(2) == "-" else m.group(2)
        price = parse_price(f"{m.group(1)},{frac}")
        if price:
            return Parsed(price, _meta(page, "og:title") or _title(page), _meta(page, "og:image"))
    price = parse_price(_meta(page, "product:price:amount", "og:price:amount"))
    unavailable = bool(re.search(r"Niet leverbaar|Tijdelijk uitverkocht|Uitverkocht", page))
    return Parsed(price, _meta(page, "og:title") or _title(page), _meta(page, "og:image"), unavailable=unavailable and price is None)


def parse_kruidvat(page: str) -> Parsed:
    if "Access Denied" in page[:2000]:
        return Parsed(None, blocked=True)
    if (p := _from_jsonld(page)) is not None:
        return p
    for pattern in (
        r'"price"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)',
        r'"priceValue"\s*:\s*"?([\d.]+)',
        r'itemprop="price"[^>]*content="([\d.,]+)"',
    ):
        m = re.search(pattern, page)
        if m and (price := parse_price(m.group(1))):
            return Parsed(price, _meta(page, "og:title") or _title(page), _meta(page, "og:image"))
    return Parsed(parse_price(_meta(page, "product:price:amount", "og:price:amount")),
                  _meta(page, "og:title") or _title(page), _meta(page, "og:image"))


PARSERS = {
    "amazon_nl": parse_amazon, "amazon_de": parse_amazon, "amazon_be": parse_amazon,
    "bol": parse_bol, "kruidvat_be": parse_kruidvat,
}


def parse_page(retailer: str, page: str) -> Parsed:
    parser = PARSERS.get(retailer, parse_generic)
    result = parser(page)
    if result.price is None and not result.blocked and not result.unavailable:
        fallback = parse_generic(page)
        if fallback.price is not None:
            return fallback
    return result


# --------------------------------------------------------------- URL handling
def amazon_url(retailer: str, asin: str) -> str:
    return f"https://www.{AMAZON_DOMAINS[retailer]}/dp/{asin}"


def normalize_url(retailer: str, url_or_id: str) -> str:
    """Accept a full URL, or for Amazon a bare ASIN."""
    value = url_or_id.strip()
    if retailer in AMAZON_DOMAINS:
        m = re.search(r"(?:/dp/|/gp/product/|^)(B[0-9A-Z]{9}|\d{9}[\dX])(?:[/?#]|$)", value)
        if m:
            return amazon_url(retailer, m.group(1))
    if not value.startswith("http"):
        raise ValueError("Provide a full product URL (or an ASIN for Amazon).")
    host = urlparse(value).netloc.lower()
    expected = {"bol": "bol.com", "kruidvat_be": "kruidvat.be", **AMAZON_DOMAINS}.get(retailer)
    if expected and expected not in host:
        raise ValueError(f"URL host {host!r} does not match retailer {retailer}.")
    return value.split("#")[0]


def search_url(retailer: str, set_number: str) -> str | None:
    q = quote_plus(f"LEGO {set_number}")
    if retailer in AMAZON_DOMAINS:
        return f"https://www.{AMAZON_DOMAINS[retailer]}/s?k={q}"
    if retailer == "bol":
        return f"https://www.bol.com/nl/nl/s/?searchtext={q}"
    if retailer == "kruidvat_be":
        return f"https://www.kruidvat.be/nl/search?text={q}"
    return None


def find_search_result(retailer: str, page: str, set_number: str) -> str | None:
    """Best-effort: first search hit whose text mentions the set number."""
    if retailer in AMAZON_DOMAINS:
        for asin, chunk in re.findall(r'data-asin="(B[0-9A-Z]{9})"(.*?)(?=data-asin=|$)', page, re.S):
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", chunk))
            if set_number in text and re.search(r"lego", text, re.I):
                return amazon_url(retailer, asin)
    elif retailer == "bol":
        for href in re.findall(r'href="(/nl/nl/p/[^"]+)"', page):
            if set_number in href:
                return "https://www.bol.com" + href.split("?")[0]
    elif retailer == "kruidvat_be":
        for href in re.findall(r'href="(/nl/[^"]*?/p/\d+[^"]*)"', page):
            if set_number in href or "lego" in href.lower():
                return "https://www.kruidvat.be" + href.split("?")[0]
    return None
