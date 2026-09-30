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

from .const import GENERIC_SHOPS
from .i18n import T
from .models import parse_price
from .shops import LOCALE, SEARCH, all_domains, domain_of


@dataclass
class Parsed:
    price: float | None
    title: str | None = None
    image: str | None = None
    blocked: bool = False
    unavailable: bool = False
    list_price: float | None = None      # LEGO.com: regular price = RRP (price may be a sale price)
    retiring: bool = False


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
    price = amazon_buybox_price(page)
    if price is not None:
        return Parsed(price, title, image)
    unavailable = bool(re.search(r'id="outOfStock"|Momenteel niet verkrijgbaar|Derzeit nicht verfügbar|Currently unavailable', page))
    return Parsed(None, title, image, unavailable=unavailable)


AMAZON_BUYBOX_IDS = ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div", "apex_desktop",
                     "corePrice_desktop", "buybox", "desktop_buybox", "newAccordionRow")


def _offscreen_prices(block: str) -> list[float]:
    """Prices inside a block, skipping struck-through list prices (a-text-price / data-a-strike)."""
    out = []
    for cls, inner in re.findall(r'<span class="(a-price[^"]*)"[^>]*>(.*?)</span>\s*</span>', block, re.S):
        if "a-text-price" in cls or "a-size-small" in cls:
            continue
        m = re.search(r'class="a-offscreen"[^>]*>([^<]+)', inner)
        if m and (p := parse_price(m.group(1))):
            out.append(p)
    return out


def amazon_buybox_price(page: str) -> float | None:
    """The price in the buy box, never just the first price on the page (that can be an
    accessory, a unit price, a coupon or a struck-through list price)."""
    # 1. structured data Amazon ships for the buy box / twister
    for pat in (r'name="items\[0\.base\]\[customerVisiblePrice\]\[amount\]"\s+value="([\d.]+)"',
                r'id="twister-plus-price-data-price"\s+value="([\d.]+)"',
                r'"priceToPay"\s*:\s*\{[^{}]*?"(?:amount|price)"\s*:\s*"?([\d.]+)',
                r'"desktop_buybox_group_1"\s*:\s*\[\s*\{[^\]]*?"priceAmount"\s*:\s*([\d.]+)'):
        m = re.search(pat, page)
        if m and (p := parse_price(float(m.group(1)))):
            return p
    # 2. "price to pay" inside the buy box
    for bid in AMAZON_BUYBOX_IDS:
        i = page.find(f'id="{bid}"')
        if i < 0:
            continue
        block = page[i:i + 8000]
        m = re.search(r'class="a-price[^"]*priceToPay[^"]*"[^>]*>.*?class="a-offscreen"[^>]*>([^<]+)<', block, re.S)
        if m and (p := parse_price(m.group(1))):
            return p
        if prices := _offscreen_prices(block):
            return prices[0]
    # 3. older layouts
    for pid in ("priceblock_dealprice", "priceblock_ourprice", "priceblock_saleprice", "price_inside_buybox",
                "newBuyBoxPrice", "kindle-price"):
        m = re.search(rf'id="{pid}"[^>]*>\s*([^<]+)<', page)
        if m and (p := parse_price(m.group(1))):
            return p
    return None


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


# Only structured availability values count; plain words can appear in translation bundles on every page.
LEGO_RETIRING_RE = re.compile(r'"(?:availabilityStatus|availability|productStatus|stockStatus)"\s*:\s*"[^"]*RETIRING[^"]*"', re.I)
LEGO_GONE_RE = re.compile(r'"(?:availabilityStatus|availability|productStatus)"\s*:\s*"[^"]*(?:RETIRED|Discontinued)[^"]*"', re.I)


def parse_lego(page: str) -> Parsed:
    """LEGO.com product page: JSON-LD + the Next.js/Apollo state (centAmount prices)."""
    if "Access Denied" in page[:3000] or "captcha" in page[:5000].lower():
        return Parsed(None, blocked=True)
    ld = _from_jsonld(page)
    cents = {k: int(m.group(1)) / 100 for k in ("price", "listPrice", "originalPrice")
             if (m := re.search(rf'"{k}"\s*:\s*\{{[^{{}}]*?"centAmount"\s*:\s*(\d+)', page))}
    price = (ld.price if ld else None) or cents.get("price")
    list_price = cents.get("listPrice") or cents.get("originalPrice") or price
    if list_price and price and list_price < price:
        list_price = price
    title = (ld.title if ld else None) or _meta(page, "og:title") or _title(page)
    image = (ld.image if ld else None) or _meta(page, "og:image")
    head = page[:400000]
    return Parsed(price, title, image, unavailable=price is None and bool(LEGO_GONE_RE.search(head)),
                  list_price=list_price, retiring=bool(LEGO_RETIRING_RE.search(head)))


PARSERS = {
    "lego_com": parse_lego,
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
    expected = domain_of(retailer)
    if expected and expected not in host:
        raise ValueError(f"URL host {host!r} does not match retailer {retailer}.")
    return value.split("#")[0]


def search_url(retailer: str, set_number: str) -> str | None:
    tpl = SEARCH.get(retailer)
    if not tpl:
        return None
    return (tpl.replace("{query}", quote_plus(f"LEGO {set_number}")).replace("{number}", quote_plus(set_number))
            .replace("{locale}", LOCALE["lego"]))


def lego_product_url(set_number: str) -> str:
    return f"https://www.lego.com/{LOCALE['lego']}/product/{set_number}"


# ------------------------------------------------------------ product matching
# Accessories and look-alikes that mention a LEGO set number but are not the set itself.
ACCESSORY_RE = re.compile(
    r"\b(leds?|led[- ]?(?:verlichting|licht|light|strip|set|kit)\w*|lmb|verlichting\w*|beleuchtung\w*|"
    r"licht|lichtjes|licht(?:set|kit|snoer)\w*|lights?|lighting|light ?(?:kit|set)|lampen|"
    r"vitrines?|displays?|display ?case|schaukasten|acryl\w*|acrylic|plexi\w*|showcase|stofkap|staubschutz|dust ?cover|"
    r"wandhalter|wall mount|halterung|"
    r"sticker|aufkleber|poster|puzzle|sokken|socks|t-shirt|mok|mug|sleutelhanger|schl[uü]sselanh[aä]nger|keychain|"
    r"magneet|magnet|handleiding|instructions only|anleitung|bauanleitung|ersatzteile|onderdelen los|spare parts|"
    r"compatibel|compatible|kompatibel|niet van lego|kein lego|not lego|geen lego|"
    # 'geschikt voor LEGO 10368' is an accessory; 'geschikt voor kinderen vanaf 8 jaar' is a normal set title
    r"geschikt\s+(?:voor|met|bij)\s+(?:(?:de|het|alle|jouw|je)\s+)?(?:lego|\d{4,6})|passend\s+(?:voor|bij)\s+(?:lego|\d{4,6})|"
    r"suitable\s+for\s+(?:lego|\d{4,6})|for\s+lego|voor\s+lego|für\s+lego|pour\s+lego)\b", re.I)
KNOCKOFF_RE = re.compile(r"\b(keeppley|mould ?king|cada|lepin|bluebrixx|cobi|sluban|qman|wange|reobrix|pantasy|"
                         r"funwhole|jmbricklayer|lumibricks|briksmax|light my bricks|lightailing|kyglaring|brickbling)\b", re.I)


# your own words (Manage → Settings → Product filter): extra words that are never the set, and exceptions
_USER_BLOCK: re.Pattern | None = None
_USER_ALLOW: re.Pattern | None = None


def _word_re(words: list[str]) -> re.Pattern | None:
    """Whole words / phrases, case-insensitive; a trailing * matches any ending ('verlicht*')."""
    parts = []
    for w in words:
        w = re.sub(r"\s+", " ", str(w)).strip()
        if not w:
            continue
        star = w.endswith("*")
        body = r"\s+".join(re.escape(x) for x in w.rstrip("*").split(" "))
        parts.append(rf"(?<!\w){body}{r'\w*' if star else r'(?!\w)'}")
    return re.compile("|".join(parts), re.I) if parts else None


def set_custom_words(block: list[str] | None, allow: list[str] | None) -> None:
    global _USER_BLOCK, _USER_ALLOW
    _USER_BLOCK, _USER_ALLOW = _word_re(block or []), _word_re(allow or [])


# the built-in list in readable form (shown in the settings)
BUILTIN_WORDS = ("LED", "LMB", "verlichting*", "licht", "lichtjes", "lichtset", "light(s)", "lighting", "display", "vitrine",
                 "acryl*", "plexi*", "showcase", "stofkap", "dust cover", "wall mount", "sticker", "poster", "puzzle", "sokken",
                 "t-shirt", "mok", "sleutelhanger", "magneet", "handleiding", "onderdelen los", "compatibel", "niet van lego",
                 "geschikt voor LEGO / <nummer>", "passend bij LEGO / <nummer>", "voor LEGO", "for LEGO")


def strip_allowed(text: str) -> str:
    return _USER_ALLOW.sub(" ", text) if _USER_ALLOW and text else text


def accessory_word(text: str | None) -> str | None:
    """The word that makes a product title an accessory (not the set itself), or None.
    Exceptions are removed first, then your own words and the built-in list are checked."""
    if not text:
        return None
    t = strip_allowed(htmllib.unescape(text))
    for rx in (_USER_BLOCK, ACCESSORY_RE):
        if rx and (m := rx.search(t)):
            return m.group(0)
    return None


def title_check(title: str | None, set_number: str) -> tuple[str | None, str]:
    """('ok' | 'suspect' | None, reason). None = nothing to judge."""
    if not title:
        return None, T("no product title yet")
    t = htmllib.unescape(title)
    if KNOCKOFF_RE.search(t):
        return "suspect", T("other brand: {brand}", brand=KNOCKOFF_RE.search(t).group(0))
    if (word := accessory_word(t)):
        return "suspect", T("looks like an accessory ({word})", word=word)
    if not re.search(rf"(?<!\d){re.escape(set_number)}(?!\d)", t):
        return "suspect", T("set number {number} is not in the title", number=set_number)
    if not re.search(r"lego", t, re.I):
        return "suspect", T("no LEGO in the title")
    return "ok", T("title contains the set number")


def is_search_url(url: str | None) -> bool:
    """A shop's search results page (not a product page)."""
    if not url or not url.startswith("http"):
        return False
    u = urlparse(url)
    return bool(re.search(r"/(?:search|zoeken|zoek|suche|recherche|buscar|s)(?:/|$)|/search\b", u.path, re.I)
                or re.search(r"(?:^|&)(?:q|text|query|searchtext|search|k|keyword|zoekterm)=", u.query, re.I)) \
        and not re.search(r"/(?:p|dp|product|gp/product)/", u.path)


def slug_title(url: str) -> str | None:
    """bol/kruidvat URLs carry the product name in the path; Amazon /dp/ URLs do not."""
    path = urlparse(url).path
    m = re.search(r"/p/([^/]+)/", path) or re.search(r"/nl/([^/]+)/p/", path)
    return m.group(1).replace("-", " ") if m else None


def clean_title(title: str, set_number: str) -> str:
    """'LEGO Icons 10311 Orchidee, Kunstplanten ... | bol.com' -> 'Orchidee'-ish short name."""
    t = htmllib.unescape(title)
    t = re.split(r"\s[|:]\s|: Amazon|\s-\s(?:Amazon|bol\.com|Kruidvat)", t)[0]
    t = re.sub(rf"(?<!\d){re.escape(set_number)}(?!\d)", "", t)
    t = re.sub(r"\bLEGO\b\s*®?", "", t, flags=re.I)
    t = re.split(r",|\s\(|\s–\s", t)[0]
    t = re.sub(r"\s{2,}", " ", t).strip(" -–:")
    return t[:80] or title[:80]


def _amazon_results(page: str) -> list[tuple[str, str]]:
    out = []
    for asin, chunk in re.findall(r'data-asin="(B[0-9A-Z]{9})"(.*?)(?=data-asin="B|$)', page, re.S):
        m = (re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.S) or re.search(r'aria-label="([^"]+)"', chunk)
             or re.search(r'class="[^"]*a-text-normal[^"]*"[^>]*>(.*?)<', chunk, re.S))
        if m:
            out.append((asin, re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()))
    return out


def find_search_result(retailer: str, page: str, set_number: str) -> str | None:
    """First search hit whose *title* passes title_check (set number, LEGO, no accessory/knock-off)."""
    if retailer in AMAZON_DOMAINS:
        for asin, title in _amazon_results(page):
            if title_check(title, set_number)[0] == "ok":
                return amazon_url(retailer, asin)
    elif retailer == "bol":
        for href in dict.fromkeys(re.findall(r'href="(/nl/nl/p/[^"]+)"', page)):
            url = "https://www.bol.com" + href.split("?")[0]
            if title_check(f"lego {slug_title(url) or ''}", set_number)[0] == "ok":
                return url
    elif retailer == "kruidvat_be":
        for href in dict.fromkeys(re.findall(r'href="(/nl/[^"]*?/p/\d+[^"]*)"', page)):
            url = "https://www.kruidvat.be" + href.split("?")[0]
            if title_check(slug_title(url), set_number)[0] == "ok":
                return url
    elif retailer == "lego_com":
        for href in dict.fromkeys(re.findall(r'href="((?:https://www\.lego\.com)?/[a-z]{2}-[a-z]{2}/product/[^"?#]+)"', page)):
            if re.search(rf"(?<!\d){re.escape(set_number)}/?$", href):
                return href if href.startswith("http") else "https://www.lego.com" + href
    elif retailer in GENERIC_SHOPS:
        return _generic_result(page, GENERIC_SHOPS[retailer]["domain"], set_number)
    return None


def _generic_result(page: str, domain: str, set_number: str) -> str | None:
    """Any shop: links on the shop's own domain whose link text or URL passes the title check;
    otherwise the product tile around a link that mentions the set number (shops like Smyths Toys
    link to /p/<their own code> and put the set number elsewhere in the tile)."""
    return _generic_link(page, domain, set_number) or _generic_tile(page, domain, set_number)


def _generic_tile(page: str, domain: str, set_number: str) -> str | None:
    from .compare import _dom            # small DOM helper (import here: compare imports this module)

    root = _dom(page)
    num_re = re.compile(rf"(?<!\d){re.escape(set_number)}(?!\d)")
    for a in (n for n in root.iter() if n.tag == "a" and n.attrs.get("href")):
        href = a.attrs["href"]
        url = href if href.startswith("http") else f"https://www.{domain}{href if href.startswith('/') else '/' + href}"
        low = url.lower()
        if domain not in urlparse(url).netloc or any(w in low for w in ("search", "zoek", "/cart", "/login", "/c/", "wishlist")):
            continue
        tile, product = a, urlparse(url).path
        for _ in range(5):                 # the smallest block around the link that names the set
            if tile.parent is None:
                break
            tile = tile.parent
            paths = {urlparse(x.attrs.get("href", "")).path for x in tile.iter() if x.tag == "a" and x.attrs.get("href")
                     and not x.attrs["href"].startswith(("#", "javascript:"))}
            if len(paths - {product}) > 1:
                break                          # grew into the next product
            text = tile.all_text() + " " + " ".join(x.attrs.get("alt", "") + " " + x.attrs.get("title", "")
                                                    for x in tile.iter() if x.tag in ("img", "a"))
            if num_re.search(text):
                if title_check(text if "lego" in text.lower() else f"lego {text}", set_number)[0] == "ok" \
                        or (num_re.search(text) and "lego" in text.lower() and not accessory_word(text)):
                    return url.split("?")[0]
                break
    return None


def _generic_link(page: str, domain: str, set_number: str) -> str | None:
    for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        url = href if href.startswith("http") else f"https://www.{domain}{href if href.startswith('/') else '/' + href}"
        if domain not in urlparse(url).netloc or "search" in url.lower() or "zoek" in url.lower():
            continue
        label = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
        path = urlparse(url).path.replace("-", " ").replace("/", " ")
        for candidate in (label, f"lego {path}"):
            if candidate and title_check(candidate if "lego" in candidate.lower() else f"lego {candidate}",
                                         set_number)[0] == "ok" and set_number in (label + path):
                return url.split("?")[0]
    return None


def parse_brickset_page(page: str) -> dict:
    """Best effort metadata from a public brickset.com/sets/<n>-1 page (fallback when no API key)."""
    out: dict = {}
    t = _meta(page, "og:title") or _title(page) or ""
    m = re.match(r"\s*\d+-\d+:\s*(.*?)\s*(?:\|.*)?$", t)
    if m:
        out["name"] = m.group(1)
    img = _meta(page, "og:image")
    if img and img.startswith("https://"):
        out["image"] = img
    for dt, dd in re.findall(r"<dt>\s*(.*?)\s*</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S):
        key = re.sub(r"<[^>]+>", "", dt).strip().lower()
        val = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", dd))).strip()
        if key == "theme":
            out["theme"] = val
        elif key == "subtheme":
            out["subtheme"] = val
        elif key == "year released" and val[:4].isdigit():
            out["year"] = int(val[:4])
        elif key == "pieces" and re.match(r"\d", val):
            out["pieces"] = int(re.match(r"[\d,]+", val).group(0).replace(",", ""))
        elif key == "rrp":
            eur = re.search(r"([\d.,]+)\s*€|€\s*([\d.,]+)", val)
            if eur:
                out["rrp"] = parse_price(eur.group(1) or eur.group(2))
    return out


def retailer_from_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    for rid, dom in all_domains().items():
        if host == dom or host.endswith("." + dom):
            return rid
    return None


def url_key(retailer: str, url: str) -> str:
    """Stable identity of a product page (ASIN for Amazon, path without query otherwise)."""
    if retailer in AMAZON_DOMAINS:
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        if m:
            return m.group(1)
    parsed = urlparse(url)
    return parsed.path.rstrip("/").lower()
