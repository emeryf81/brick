"""HTML price extraction per retailer. Pure functions, regex only (no extra deps).

Retailer markup changes regularly; every parser therefore tries several strategies
(JSON-LD -> meta tags -> retailer specific markup) and returns None when unsure.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

from .const import GENERIC_SHOPS
from .i18n import T
from .models import parse_price
from .shops import BOL_PATH_RE, LOCALE, PROFILE, SEARCH, all_domains, bol_site_url, domain_of, reader_of, site_root


@dataclass
class Parsed:
    price: float | None
    title: str | None = None
    image: str | None = None
    blocked: bool = False
    unavailable: bool = False
    list_price: float | None = None      # LEGO.com: regular price = RRP (price may be a sale price)
    retiring: bool = False
    reason: str | None = None            # why there is no price: "sold_out" or "discontinued" (no error, a warning)


# Why a shop page has no price. The structured availability (JSON-LD / schema.org) counts first; the words
# only when the page has no price at all (they can also appear in menus or translations of every page).
GONE_WORDS_RE = re.compile(r"niet meer (?:leverbaar|verkrijgbaar|beschikbaar|in (?:het )?assortiment)|uit (?:het )?assortiment|"
                           r"uit (?:de )?(?:handel|productie)|no longer (?:available|sold|in stock)|discontinued|"
                           r"nicht mehr (?:lieferbar|erhältlich|verfügbar)|aus dem sortiment|plus disponible|"
                           r"n'est plus (?:vendu|disponible|fabriqué)|ya no está disponible|descatalogado", re.I)
SOLD_OUT_WORDS_RE = re.compile(r"(?:tijdelijk )?uitverkocht|niet (?:op voorraad|leverbaar)|out of stock|sold out|"
                               r"(?:tijdelijk|momenteel) niet (?:beschikbaar|verkrijgbaar)|temporarily (?:unavailable|out of stock)|"
                               r"currently unavailable|ausverkauft|nicht (?:vorrätig|lieferbar|auf lager)|"
                               r"vorübergehend nicht (?:verfügbar|lieferbar)|temporairement (?:indisponible|en rupture)|épuisé|"
                               r"rupture de stock|agotado|sin stock", re.I)


def _own_product_availability(page: str, set_number: str | None) -> str | None:
    """The schema.org availability of the page's own product: the one that names the set (when the set
    number is known), else a top-level product. Recommendations nested in it or elsewhere don't count.
    Returns "" when the product is there but says nothing about availability, None without a product."""
    tops: list[dict] = []
    for block in _jsonld_blocks(page):
        for node in (block.get("@graph") if isinstance(block, dict) and isinstance(block.get("@graph"), list)
                     else block if isinstance(block, list) else [block]):
            if isinstance(node, dict):
                types = node.get("@type")
                if "Product" in (types if isinstance(types, list) else [types]):
                    tops.append(node)
    if set_number:
        num = re.compile(rf"(?<!\d){re.escape(set_number)}(?!\d)")
        named = [n for n in tops if num.search(" ".join(str(n.get(k) or "") for k in ("name", "sku", "mpn", "productID")))]
        tops = named or tops[:1]
    if not tops:
        return None
    offers = tops[0].get("offers")
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    avail = " ".join(str(o.get("availability") or "") for o in offers if isinstance(o, dict))
    return avail or ""


def availability_reason(page: str, set_number: str | None = None) -> str | None:
    """'discontinued', 'sold_out' or None for a page without a price. The structured availability of the
    page's own product decides first; without one, the words on the page."""
    own = _own_product_availability(page, set_number)
    if own is not None:
        if not own:
            return None                                 # the product is there but says nothing: no guessing from text
        if "Discontinued" in own:
            return "discontinued"
        if "OutOfStock" in own or "SoldOut" in own:
            return "sold_out"
        return None                                     # in stock / pre-order: no reason to call it unavailable
    text = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", page[:600000], flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    if GONE_WORDS_RE.search(text):
        return "discontinued"
    if SOLD_OUT_WORDS_RE.search(text):
        return "sold_out"
    return None


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
LEGO_SOLD_OUT_RE = re.compile(r'"(?:availabilityStatus|availability|productStatus|stockStatus)"\s*:\s*"[^"]*'
                              r'(?:SOLD_?OUT|OUT_?OF_?STOCK|OutOfStock|SoldOut|TEMPORARILY_?(?:UNAVAILABLE|OUT))[^"]*"', re.I)
LEGO_CENTS_RE = {k: re.compile(rf'"{k}"\s*:\s*\{{[^{{}}]*?"centAmount"\s*:\s*(\d+)') for k in ("price", "listPrice", "originalPrice")}


def _lego_own_page(page: str, num: str | None) -> bool:
    """The page itself is the product page of this set: its canonical / og:url ends in the set number."""
    if not num:
        return False
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', page, re.I) \
        or re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical', page, re.I)
    urls = [m.group(1) if m else "", _meta(page, "og:url") or ""]
    return any(re.search(rf"/product/(?:[^/?#]*?-)?{re.escape(num)}/?(?:[?#]|$)", u) for u in urls)


def _lego_product(page: str, num: str | None) -> tuple[float | None, str | None, str | None, bool | None, bool]:
    """(price, name, image, in stock, ambiguous) of the page's own product in JSON-LD: the node for this set
    number, also when it is sold out (then its price is the regular price, not a price you can pay now).
    LEGO.com does not always put the set number in it (the sku can be its own article number): then the
    only Product on the set's own page (canonical URL with the number) is the set. ambiguous: several such
    Products, so it can't tell which one is the set (and no price without the set's own code is safe)."""
    nodes = []
    for block in _jsonld_blocks(page):
        for node in _walk(block):
            types = node.get("@type")
            if "Product" in (types if isinstance(types, list) else [types]):
                nodes.append(node)

    def ids(node: dict[str, Any]) -> str:
        """The fields that can name the set: sku, product id, mpn, name and url."""
        return " ".join(str(node.get(k) or "") for k in ("sku", "productID", "mpn", "name", "url"))
    own = next((n for n in nodes if not num or re.search(rf"(?<!\d){re.escape(num)}(?!\d)", ids(n))), None)
    if own is None and nodes and _lego_own_page(page, num):
        # the set's own page: the one Product that carries no other set's number (a recommendation does;
        # LEGO's own article numbers have 7 digits); more than one such Product → can't tell which is the set
        bare = [n for n in nodes if not re.search(r"(?<!\d)\d{4,6}(?!\d)", ids(n))]
        own = bare[0] if len(bare) == 1 else None
        if len(bare) > 1:
            return None, None, None, None, True
    if own is None:
        return None, None, None, None, False           # only other products (e.g. recommendations)
    prices, stock = [], None
    for off in _walk(own.get("offers") or []):
        if (p := parse_price(off.get("price") or off.get("lowPrice"))) is not None:
            prices.append(p)
            avail = str(off.get("availability", ""))
            stock = (stock or False) or not ("OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail)
    img = own.get("image")
    img = img[0] if isinstance(img, list) and img else img
    return (min(prices) if prices else None, own.get("name"), img if isinstance(img, str) else None, stock, False)


def _lego_window(page: str, num: str | None, ld_price: float | None, strict: bool = False,
                 scoped: bool = False) -> str:
    """The part of the page state that belongs to this product: around its price (the one JSON-LD gives),
    or around its product code. A page also lists recommended products with their own prices.
    strict: only prices inside the product's own data (no nearest price before its code).
    scoped: no page-wide prices when the page has no code for this set (it lists several unnamed products)."""
    if ld_price is not None:
        m = re.search(rf'"price"\s*:\s*\{{[^{{}}]*?"centAmount"\s*:\s*{round(ld_price * 100)}\b', page)
        if m:
            return page[max(0, m.start() - 1500):m.end() + 3000]
    if num:
        m = re.search(rf'"(?:productCode|sku)"\s*:\s*"{re.escape(num)}"', page)
        if m:                                              # up to the neighbouring products' codes
            codes = [c.start() for c in re.finditer(r'"productCode"\s*:\s*"', page)]
            end = min([c for c in codes if c > m.start()] + [m.end() + 3000])
            if LEGO_CENTS_RE["price"].search(page, m.start(), end) or strict:
                return page[m.start():end]                 # the prices that follow its code
            start = max([c + 15 for c in codes if c < m.start()] + [m.start() - 3000, 0])
            before = page[start:m.start()]
            last = before.rfind('"price"')                 # else the nearest price just before its code
            return before[last:] + page[m.start():end] if last >= 0 else page[m.start():end]
        if scoped or re.search(r'"productCode"\s*:\s*"\d', page):
            return ""                                      # only other products' codes: none of these prices is ours
    return page[:400000]


def _lego_text_status(page: str, num: str | None) -> str | None:
    """'discontinued' ("Product uit handel") or 'sold_out' ("Tijdelijk niet beschikbaar") from the words you see
    on a LEGO.com page, only when they are about this set: on its own page only in its product block (title to
    the next heading, before the recommendations); on other pages (search) when this set's number is the number
    closest to the words. Words in scripts don't count (translation bundles hold them on every page)."""
    if not num:
        return None
    body = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<noscript\b.*?</noscript>", " ", page[:2000000], flags=re.S | re.I)

    def text(html: str) -> str:
        """Visible text of an HTML fragment: tags removed, whitespace collapsed, entities decoded."""
        return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))
    if _lego_own_page(page, num):
        # the set's own page: only its product block counts, from its title (h1) to the next heading (h2, e.g.
        # "Aanbevolen voor jou") or the page's aside / footer; without a title nothing on it counts
        h1 = re.search(r"<h1\b", body, re.I)
        if not h1:
            return None
        end = re.compile(r"<(?:h2|aside|footer)\b", re.I).search(body, h1.end())
        section = text(body[h1.start():end.start() if end else len(body)])[:3000]
        for rx, why in ((GONE_WORDS_RE, "discontinued"), (SOLD_OUT_WORDS_RE, "sold_out")):
            if rx.search(section):
                return why
        return None
    full = text(body)
    for rx, why in ((GONE_WORDS_RE, "discontinued"), (SOLD_OUT_WORDS_RE, "sold_out")):
        for m in rx.finditer(full):
            # the set number closest to the words is the set they are about
            lo = max(0, m.start() - 400)
            near = [(min(abs(n.start() + lo - m.start()), abs(n.start() + lo - m.end())), n.group(0))
                    for n in re.finditer(r"(?<![\d.,€$])\d{4,7}(?![\d.,%])", full[lo:m.end() + 400])]
            if near and min(near)[1] == num:
                return why
    return None


def parse_lego(page: str, num: str | None = None) -> Parsed:
    """LEGO.com product page: JSON-LD + the Next.js/Apollo state (centAmount prices). Only the prices of the
    product itself count; a sold-out set gives its regular price (RRP) but no price to buy at."""
    if "Access Denied" in page[:3000] or "captcha" in page[:5000].lower():
        return Parsed(None, blocked=True)
    ld_price, ld_name, ld_image, in_stock, ambiguous = _lego_product(page, num)
    # newer pages carry the page state as escaped JSON inside scripts (\"centAmount\":2999): read it unescaped
    state = page.replace('\\"', '"') if '\\"centAmount\\"' in page or '\\"productCode\\"' in page else page
    win = _lego_window(state, num, ld_price, strict=state is not page or ambiguous, scoped=ambiguous)
    cents = {k: int(m.group(1)) / 100 for k, rx in LEGO_CENTS_RE.items() if (m := rx.search(win))}
    price = ld_price or cents.get("price")
    list_price = cents.get("listPrice") or cents.get("originalPrice") or price
    if list_price and price and list_price < price:
        list_price = price
    title = ld_name or _meta(page, "og:title") or _title(page)
    image = ld_image or _meta(page, "og:image")
    head = win if num else page[:400000]
    sold_out = in_stock is False or bool(LEGO_SOLD_OUT_RE.search(win[:20000] if num else ""))
    gone = bool(LEGO_GONE_RE.search(head))
    # what the page says in words ("Product uit handel", "Tijdelijk niet beschikbaar"): then no price is taken
    said = _lego_text_status(page, num) if not gone else None
    if said == "discontinued":
        gone = True
    elif said == "sold_out":
        sold_out = True
    if gone:
        price = None
    own_page = _lego_own_page(page, num)
    avail = (_meta(page, "product:availability", "og:availability") or "") if own_page else ""
    if re.search(r"discontinued|retired", avail, re.I):     # the page's own meta data says it in so many words
        gone, price = True, None
    elif re.search(r"out ?of ?stock|sold ?out", avail, re.I):
        sold_out = True
    if sold_out or (price is None and gone):
        if own_page and not list_price:                     # the regular price, also when it can't be bought now
            list_price = parse_price(_meta(page, "product:price:amount", "og:price:amount"))
        return Parsed(None, title, image, unavailable=True, list_price=list_price, retiring=bool(LEGO_RETIRING_RE.search(head)),
                      reason="discontinued" if gone else "sold_out")
    if own_page and price is None:                          # the set's own page: its price in the page's meta data
        price = parse_price(_meta(page, "product:price:amount", "og:price:amount"))
        list_price = list_price or price
    return Parsed(price, title, image, list_price=list_price, retiring=bool(LEGO_RETIRING_RE.search(head)))


def lego_number(url: str | None) -> str | None:
    """The set number at the end of a LEGO.com product URL (/product/flower-bouquet-10280)."""
    m = re.search(r"/product/(?:[^/?#]*?-)?(\d{3,7})/?(?:[?#]|$)", url or "")
    return m.group(1) if m else None


# how a shop's pages are read: the "reader" of the shop in the shop settings file
PARSERS = {"amazon": parse_amazon, "bol": parse_bol, "kruidvat": parse_kruidvat}


def parse_page(retailer: str, page: str, set_number: str | None = None) -> Parsed:
    if reader_of(retailer) == "lego":
        return parse_lego(page, set_number)       # never the generic fallback: it may read a recommended product
    amazon = is_amazon(retailer)
    parser = parse_amazon if amazon else PARSERS.get(reader_of(retailer), parse_generic)
    result = parser(page)
    if amazon and result.price is None and not result.blocked:
        # an Amazon page without a price in the buy box: not in stock (at Amazon), never an error
        result.unavailable, result.reason = True, "sold_out"
        return result
    if result.price is None and not result.blocked and not result.unavailable:
        fallback = parse_generic(page)
        if fallback.price is not None:
            return fallback
    if result.price is None and not result.blocked and (result.unavailable or result.title) and not result.reason:
        # no price: say why when the page says so (sold out / no longer sold); a warning, not an error
        if (why := availability_reason(page, set_number)) or result.unavailable:
            result.unavailable, result.reason = True, why or "sold_out"
    return result


# --------------------------------------------------------------- URL handling
def is_amazon(retailer: str) -> bool:
    """Any Amazon site: the built-in ones and a shop you add yourself on amazon.fr, amazon.it, amazon.co.uk..."""
    if reader_of(retailer) == "amazon":
        return True
    return bool(re.fullmatch(r"(?:www\.)?amazon\.(?:[a-z]{2,3}|com?\.[a-z]{2})", (domain_of(retailer) or "").lower()))


def amazon_url(retailer: str, asin: str) -> str:
    domain = (domain_of(retailer) or "").removeprefix("www.")
    return f"https://www.{domain}/dp/{asin}"


def normalize_url(retailer: str, url_or_id: str) -> str:
    """Accept a full URL, or for Amazon a bare ASIN."""
    value = url_or_id.strip()
    if is_amazon(retailer):
        m = re.search(r"(?:/dp/|/gp/product/|^)(B[0-9A-Z]{9}|\d{9}[\dX])(?:[/?#]|$)", value)
        if m:
            return amazon_url(retailer, m.group(1))
    if not value.startswith("http"):
        raise ValueError("Provide a full product URL (or an ASIN for Amazon).")
    host = (urlparse(value).hostname or "").lower().rstrip(".")
    expected = (domain_of(retailer) or "").lower().removeprefix("www.")
    if expected and not (host == expected or host.endswith("." + expected)):      # never 'shop.be.evil.example'
        raise ValueError(f"URL host {host!r} does not match retailer {retailer}.")
    return value.split("#")[0]


def search_url(retailer: str, set_number: str) -> str | None:
    tpl = SEARCH.get(retailer)
    if not tpl:
        return None
    return (tpl.replace("{query}", quote_plus(f"LEGO {set_number}")).replace("{number}", quote_plus(set_number))
            .replace("{locale}", LOCALE["lego"]))


def lego_product_url(set_number: str, retailer: str | None = None) -> str | None:
    """The product page of a set at the official LEGO shop, from its "product" address in the shop settings."""
    shop = PROFILE.get(retailer or "") or next((s for s in PROFILE.values() if s["reader"] == "lego"), {})
    tpl = shop.get("product")
    return tpl.replace("{locale}", LOCALE["lego"]).replace("{number}", quote_plus(set_number)) if tpl else None


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


def wrong_product(title: str | None, set_number: str) -> str | None:
    """Why a page is clearly another product (other brand, an accessory, another set's number and not
    this one), or None. Only these reasons make a link go away by itself; anything else is a doubt."""
    if not title:
        return None
    t = htmllib.unescape(title)
    if (m := KNOCKOFF_RE.search(t)):
        return T("other brand: {brand}", brand=m.group(0))
    if (word := accessory_word(t)):
        return T("looks like an accessory ({word})", word=word)
    if not re.search(rf"(?<!\d){re.escape(set_number)}(?!\d)", t) and re.search(r"lego", t, re.I):
        pieces = r"\s*(?:-?\s*(?:pcs|pc|pieces|piece|stukjes|stuks|stenen|delig|onderdelen|teile|teilig|pièces|piezas|pezzi|elements?))\b"
        other = [n for n in re.findall(rf"(?<![\d.,€$])\d{{4,6}}(?![\d.,%])(?!{pieces})", t, re.I)
                 if n != set_number and not 1900 < int(n) < 2100]
        if other:
            return T("another set ({number})", number=other[0])
    return None


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


def find_search_result(retailer: str, page: str, set_number: str, skip: set[str] | frozenset[str] = frozenset()) -> str | None:
    """First search hit whose *title* passes title_check (set number, LEGO, no accessory/knock-off).
    skip: url_keys of links you blocked for this set; the next good hit is taken instead."""
    ok = lambda u: url_key(retailer, u) not in skip  # noqa: E731
    if is_amazon(retailer):
        for asin, title in _amazon_results(page):
            if title_check(title, set_number)[0] == "ok" and ok(amazon_url(retailer, asin)):
                return amazon_url(retailer, asin)
    elif (reader := reader_of(retailer)) == "bol":
        for href in dict.fromkeys(re.findall(r'href="(/(?:nl|be)/nl/p/[^"]+)"', page)):
            url = bol_site_url(f"{site_root(retailer)}{href.split('?')[0]}")
            if title_check(f"lego {slug_title(url) or ''}", set_number)[0] == "ok" and ok(url):
                return url
    elif reader == "kruidvat":
        for href in dict.fromkeys(re.findall(r'href="(/nl/[^"]*?/p/\d+[^"]*)"', page)):
            url = f"{site_root(retailer)}{href.split('?')[0]}"
            if title_check(slug_title(url), set_number)[0] == "ok" and ok(url):
                return url
    elif reader == "lego":
        root = site_root(retailer) or ""
        for href in dict.fromkeys(re.findall(rf'href="((?:{re.escape(root)})?/[a-z]{{2}}-[a-z]{{2}}/product/[^"?#]+)"', page)):
            url = href if href.startswith("http") else root + href
            if re.search(rf"(?<!\d){re.escape(set_number)}/?$", href) and ok(url):
                return url
    elif retailer in GENERIC_SHOPS:
        found = _generic_result(page, GENERIC_SHOPS[retailer]["domain"], set_number)
        return found if found and ok(found) else None
    return None


def _generic_result(page: str, domain: str, set_number: str) -> str | None:
    """Any shop: links on the shop's own domain whose link text or URL passes the title check;
    otherwise the product tile around a link that mentions the set number (shops like Smyths Toys
    link to /p/<their own code> and put the set number elsewhere in the tile)."""
    return _generic_link(page, domain, set_number) or _generic_tile(page, domain, set_number) \
        or _generic_url_text(page, domain, set_number)


def _generic_url_text(page: str, domain: str, set_number: str) -> str | None:
    """A product URL of the shop anywhere in the page, also in the page's own data (JSON, with escaped
    slashes): shops that build the page with JavaScript (e.g. Smyths Toys) often have the product's
    address only there. The URL itself must name the set (its slug) and pass the title check."""
    text = page.replace("\\u002F", "/").replace("\\/", "/")
    num = re.compile(rf"(?<!\d){re.escape(set_number)}(?!\d)")
    # the shop's own absolute URLs, or paths that start a string (not the path of a link to another site)
    for m in re.finditer(rf'(?:https?://(?:www\.)?{re.escape(domain)}|(?<![\w.:/%-]))(/[A-Za-z0-9._~%-][A-Za-z0-9._~%/-]{{5,300}})', text):
        url = m.group(0) if m.group(0).startswith("http") else f"https://www.{domain}{m.group(1)}"
        path = urlparse(url).path
        low = path.lower()
        if not num.search(path) or NAV_PATH_RE.search(path) or any(w in low for w in ("search", "zoek", "/c/", "/cart", "/login")) \
                or low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js", ".pdf", ".json")):
            continue
        words = re.sub(r"[-/_]+", " ", path)
        if title_check(words if "lego" in words.lower() else f"lego {words}", set_number)[0] == "ok":
            return url
    return None


# links inside a product tile that are not another product (cart, wishlist, compare, reviews, account)
# a whole path segment (optionally with an extension), e.g. /cart/add, /wishlist, /account/login.jsp;
# a product slug that merely contains such a word (/products/cart-111) is still another product
NAV_PATH_RE = re.compile(r"/(?:(?:add-?to-?)?cart|basket|winkelwagen|winkelmand(?:je)?|mandje|warenkorb|panier|login|logout|(?:my-?)?account|"
                         r"wish-?list|wishlist|verlanglijst(?:je)?|merkliste|compare|vergelijk(?:en)?|reviews?|"
                         r"beoordelingen|share|delen)(?:\.\w+)?(?:/|$)", re.I)


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
                     and not x.attrs["href"].startswith(("#", "javascript:", "mailto:"))}
            others = {p for p in paths - {product, ""} if not NAV_PATH_RE.search(p)}
            if others:
                break                          # grew into another product: its set number is not this link's
            text = tile.all_text() + " " + " ".join(x.attrs.get("alt", "") + " " + x.attrs.get("title", "")
                                                    for x in tile.iter() if x.tag in ("img", "a"))
            if num_re.search(text):
                # the same rules as a title: LEGO, the set number, no accessory, no other brand
                if title_check(text if "lego" in text.lower() else f"lego {text}", set_number)[0] == "ok":
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
    if is_amazon(retailer):
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        if m:
            return m.group(1)
    if reader_of(retailer) == "bol":                   # the same product on bol.com/nl/nl and bol.com/be/nl
        url = BOL_PATH_RE.sub(r"\1/nl/nl/", url)
    parsed = urlparse(url)
    return parsed.path.rstrip("/").lower()
