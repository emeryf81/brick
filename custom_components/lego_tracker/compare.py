"""Price-comparison sites as extra price sources (hidden option).

One source = one site that lists the prices of many shops for a set. Every source works the same way:
a first URL (set page, product page or search), then at most a few 'follow' steps (search result ->
product page), then a list of shop offers.

The markup of these sites is not documented and changes, so extraction is layered and tolerant:
  1. JSON-LD (schema.org Product / Offer / AggregateOffer)
  2. JSON embedded in the page (__NEXT_DATA__, application/json scripts, Nuxt/Apollo states)
  3. an HTML heuristic: a small block with a link and a euro price is one offer
Results that are not the LEGO set itself (LED / lighting kits, display cases, knock-offs...) are dropped.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

from .models import parse_price
from .parsers import KNOCKOFF_RE, accessory_word

# id -> (display name, host); order = order in which the sources are tried
SOURCES: dict[str, tuple[str, str]] = {
    "kieskeurig": ("Kieskeurig", "www.kieskeurig.be"),
    "shoparize": ("Shoparize", "www.shoparize.com"),
    "channable": ("Channable Shopping", "shopping.channable.com"),
    "producthero": ("Producthero", "shopping.producthero.com"),
    "brickeconomy": ("Market value", "www.brickeconomy.com"),     # market value + retirement, not shop prices
}
FRESH_HOURS = {"brickeconomy": 24}          # market values move slowly: once a day is enough
HOSTS = {"www.brickeconomy.com", "www.kieskeurig.be", "www.kieskeurig.nl", "www.shoparize.com",
         "shopping.channable.com", "shopping.producthero.com"}
MAX_STEPS = 3

PRICE_RE = re.compile(r"(?:€\s*(\d{1,4}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?|\d{1,4}[.,]-)|(\d{1,4}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?)\s*€"
                      r"|EUR\s*(\d{1,4}(?:[.,]\d{1,2})?))")
# class words of struck-through / old / advisory / shipping prices (whole words: 'font-bold' is not 'old')
OLD_PRICE = re.compile(r"(?:^|[\s_:-])(?:old|strike|strikethrough|line-through|was|rrp|advies|adviesprijs|retail|list-?price|msrp|"
                       r"original|crossed|uvp|shipping|verzend|verzendkosten|delivery)(?:$|[\s_:-])", re.I)
RRP_RE = re.compile(r"(?:adviesprijs|winkelprijs|verkoopprijs lego|rrp|prix conseillé|prix public|uvp|retail price)[^€\d]{0,60}"
                    r"(?:€\s*([\d.,]+)|([\d.,]+)\s*€)", re.I)
# shop name / domain -> our retailer id (custom shops are matched by their domain)
SHOP_ALIASES = (
    ("amazon.com.be", "amazon_be"), ("amazon.be", "amazon_be"), ("amazon be", "amazon_be"), ("amazon belgi", "amazon_be"),
    ("amazon.nl", "amazon_nl"), ("amazon nl", "amazon_nl"), ("amazon nederland", "amazon_nl"),
    ("amazon.de", "amazon_de"), ("amazon de", "amazon_de"), ("amazon duitsland", "amazon_de"),
    ("bol.com", "bol"), ("bol", "bol"), ("lego.com", "lego_com"), ("lego shop", "lego_com"), ("lego store", "lego_com"),
    ("lego", "lego_com"), ("kruidvat", "kruidvat_be"), ("dreamland", "dreamland_be"), ("smyths", "smyths_be"), ("smythstoys", "smyths_be"),
)


def is_accessory(text: str | None) -> bool:
    """True when a title / description is not the LEGO set itself (e.g. an LED kit for that set)."""
    return bool(text and (accessory_word(text) or KNOCKOFF_RE.search(text)))


def has_number(text: str | None, num: str) -> bool:
    return bool(text and re.search(rf"(?<![\d]){re.escape(num)}(?![\d])", text))


def is_compare_url(url: str | None) -> bool:
    return bool(url) and urlparse(url).netloc.lower() in HOSTS


# ------------------------------------------------------------------ locale
def _country(lego_locale: str | None) -> tuple[str, str]:
    """('nl', 'BE') from 'nl-be'."""
    parts = (lego_locale or "nl-be").lower().split("-")
    return parts[0], (parts[1] if len(parts) > 1 else "be").upper()


def first_url(source: str, num: str, lego_locale: str | None = None, ean: str | None = None) -> str | None:
    """Where a source starts for a set; None when the source does not cover this country (or needs an EAN)."""
    lang, cc = _country(lego_locale)
    q = quote_plus(f"lego {num}")
    if source == "kieskeurig":
        host = {"BE": "www.kieskeurig.be", "NL": "www.kieskeurig.nl"}.get(cc)
        return f"https://{host}/search?q={q}" if host else None
    if source == "shoparize":
        return f"https://www.shoparize.com/{'uk' if cc == 'GB' else cc.lower()}/q?q={q}"
    if source == "channable":
        return f"https://shopping.channable.com/?country={cc}&search={q}"
    if source == "brickeconomy":
        return f"https://www.brickeconomy.com/set/{num}-1/"
    if source == "producthero":
        if not ean or not ean.isdigit():
            return None
        return f"https://shopping.producthero.com/{lang if lang in ('nl', 'fr', 'de', 'en') else 'en'}/product/{ean.zfill(14)}?country={cc.lower()}"
    return None


# ------------------------------------------------------------------ small DOM
class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def all_text(self) -> str:
        out: list[str] = []
        stack: list[Any] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                out.append(n)
            elif n.tag not in ("script", "style", "noscript"):
                stack.extend(reversed(n.children))
        return re.sub(r"\s+", " ", " ".join(out)).strip()

    def iter(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(c for c in reversed(n.children) if isinstance(c, _Node))


VOID = {"img", "br", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self.cur = self.root
        self._open_tags: dict[str, int] = {}

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node
            self._open_tags[tag] = self._open_tags.get(tag, 0) + 1

    def handle_endtag(self, tag):
        # Unmatched closes must not repeatedly walk the same ancestor chain.
        if tag not in self._open_tags:
            return
        # A matching close removes each visited node from the open chain, so
        # total ancestor work is bounded by the number of non-void start tags.
        while self.cur is not self.root:
            node = self.cur
            self.cur = node.parent
            self._open_tags[node.tag] -= 1
            if not self._open_tags[node.tag]:
                del self._open_tags[node.tag]
            if node.tag == tag:
                break

    def handle_data(self, data):
        if data.strip():
            self.cur.children.append(data)


def _dom(page: str) -> _Node:
    b = _Builder()
    try:
        b.feed(page)
    except Exception:  # noqa: BLE001 - broken markup: use what was parsed
        pass
    return b.root


def _offer_summary(node: _Node, host: str, cache: dict[_Node, tuple[set[float], int]]) -> tuple[set[float], int]:
    """Cache subtree prices and outgoing-link counts once per extraction.

    Four distinct prices or three outgoing links already disqualify a row, so
    summaries stay constant-sized even for deeply nested or price-heavy pages.
    Prices are relative to each node: an old-price parent suppresses its children
    when merged, without changing their independently usable summaries.
    """
    stack = [(node, False)]
    while stack:
        n, ready = stack.pop()
        if n in cache:
            continue
        if not ready:
            stack.append((n, True))
            stack.extend((c, False) for c in reversed(n.children) if isinstance(c, _Node) and c not in cache)
            continue
        prices: set[float] = set()
        links = int(_out(n, host))
        old = n.tag in ("script", "style", "s", "del", "strike") or bool(
            OLD_PRICE.search(n.attrs.get("class", "") + " " + n.attrs.get("data-test", "")))
        for child in n.children:
            if isinstance(child, _Node):
                child_prices, child_links = cache[child]
                links = min(3, links + child_links)
                if not old:
                    for price in child_prices:
                        if len(prices) == 4:
                            break
                        prices.add(price)
            elif not old and len(prices) < 4:
                for m in PRICE_RE.finditer(child):
                    raw = (m.group(1) or m.group(2) or m.group(3) or "").replace(" ", "")
                    if raw.endswith(",-") or raw.endswith(".-"):
                        raw = raw[:-2]
                    if (p := parse_price(raw)) is not None and 0.5 <= p <= 10000:
                        prices.add(p)
                        if len(prices) == 4:
                            break
        cache[n] = prices, links
    return cache[node]


def shop_retailer(name: str, href: str | None, domains: dict[str, str]) -> str | None:
    """Map a shop (name / outgoing link) to one of our retailer ids."""
    host = urlparse(href or "").netloc.lower().removeprefix("www.")
    if host and host not in HOSTS and not host.endswith(tuple(h.removeprefix("www.") for h in HOSTS)):
        for rid, dom in domains.items():
            if host == dom or host.endswith("." + dom):
                return rid
    n = re.sub(r"\s+logo$", "", (name or "").lower().strip()).strip(" .")     # 'bol. logo' -> 'bol'
    for rid, dom in domains.items():               # custom shops: their domain in the shop name
        if dom and dom in n:
            return rid
    for alias, rid in SHOP_ALIASES:
        if n == alias or n.startswith(alias + " ") or ("." in alias and alias in n):
            return rid
    return None


def _shop_name(row: _Node, link: _Node) -> str:
    for n in row.iter():
        if n.tag == "img" and (alt := (n.attrs.get("alt") or n.attrs.get("title") or "").strip()):
            if not re.search(r"lego|\d{4,}", alt, re.I):
                return alt[:60]
    for key in ("data-shop", "data-shop-name", "data-merchant", "data-site", "data-retailer", "title", "aria-label"):
        if (v := link.attrs.get(key, "").strip()) and len(v) <= 60:
            return v
    for n in row.iter():
        if n.attrs.get("class") and re.search(r"shop|site|store|retailer|merchant|winkel|seller|vendor", n.attrs["class"], re.I):
            t = n.all_text()
            if t and len(t) <= 40 and not PRICE_RE.search(t):
                return t
    text = link.all_text()
    if text and len(text) <= 40 and not PRICE_RE.search(text) and not re.search(r"lego", text, re.I):
        return text
    return urlparse(link.attrs.get("href", "")).netloc.removeprefix("www.") or "?"


def _is_out_link(href: str, host: str, rel: str = "") -> bool:
    """A link to a shop: 'sponsored' links, other domains, and the site's own click-out redirects
    (e.g. ocean.kieskeurig.be/e/c/..., /go/..., ?url=...)."""
    if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
        return False
    if "sponsored" in rel.lower():
        return True
    u = urlparse(href)
    net = u.netloc.lower()
    base = host.removeprefix("www.")
    if net and net != host and not net.endswith(base):
        return True                                   # direct outgoing link
    if net and net != host and re.match(r"(?:ocean|click|clicks|out|go|redirect|track|tracking|r)\.", net):
        return True                                   # the site's own click-out subdomain
    return bool(re.search(r"/(?:go|out|redirect|click|clickout|buy|visit|link|r|goto|naar|shop|e/c)/|[?&](?:url|u|target)=", href, re.I))


def _out(n: _Node, host: str) -> bool:
    return n.tag == "a" and _is_out_link(n.attrs.get("href", ""), host, n.attrs.get("rel", ""))


# ------------------------------------------------------------------ extraction layers
def _jsonld(page: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    info: dict[str, Any] = {}
    offers: list[dict[str, Any]] = []
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack: list[tuple[Any, str | None]] = [(data, None)]
        while stack:
            n, product = stack.pop()
            if isinstance(n, list):
                stack.extend((x, product) for x in n)
                continue
            if not isinstance(n, dict):
                continue
            types = n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]
            if "Product" in types:
                product = str(n.get("name") or "") or product
                if not info.get("name"):
                    info["name"] = n.get("name")
                    img = n.get("image")
                    info["image"] = img[0] if isinstance(img, list) and img else img
                    info["description"] = n.get("description")
                for k in ("gtin13", "gtin", "gtin14", "ean", "gtin12"):
                    if n.get(k) and str(n[k]).strip().isdigit():
                        info.setdefault("ean", str(n[k]).strip().lstrip("0").zfill(13))
            if "Offer" in types and n.get("price") is not None:
                seller = n.get("seller") or n.get("offeredBy") or {}
                name = seller.get("name") if isinstance(seller, dict) else str(seller)
                item = n.get("itemOffered") if isinstance(n.get("itemOffered"), dict) else {}
                if name and (p := parse_price(n.get("price"))) and "OutOfStock" not in str(n.get("availability", "")):
                    offers.append({"name": str(name)[:60], "price": p, "url": n.get("url"),
                                   "title": item.get("name") or n.get("name") or product})
                continue
            stack.extend((v, product) for v in n.values() if isinstance(v, (dict, list)))
    return info, offers


PRICE_KEYS = ("price", "priceValue", "priceAmount", "amount", "salePrice", "currentPrice", "lowestPrice", "minPrice",
              "sellingPrice", "offerPrice", "totalPrice", "finalPrice", "value")
SHOP_KEYS = ("shop", "shopName", "merchant", "merchantName", "seller", "sellerName", "retailer", "retailerName", "store",
             "storeName", "vendor", "vendorName", "webshop", "webshopName", "shopTitle", "domain", "source")
URL_KEYS = ("clickoutUrl", "clickOutUrl", "clickout", "deeplink", "deepLink", "outUrl", "redirectUrl", "trackingUrl",
            "affiliateUrl", "offerUrl", "productUrl", "url", "link", "href")
TITLE_KEYS = ("title", "productTitle", "productName", "name", "offerTitle")


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        for k in ("amount", "value", "price", "raw", "current"):
            if k in v:
                return _num(v[k])
        return None
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int) and v > 10000:          # cents
        v = v / 100
    p = parse_price(v) if not isinstance(v, (int, float)) else float(v)
    return p if p is not None and 0.5 <= p <= 10000 else None


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def _embedded(page: str) -> list[dict[str, Any]]:
    """Offer-like objects in JSON embedded in the page."""
    blobs: list[str] = re.findall(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    blobs += re.findall(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    for m in re.finditer(r"(?:__NUXT__|__APOLLO_STATE__|__INITIAL_STATE__|__PRELOADED_STATE__|__DATA__)\s*=\s*", page):
        start = m.end()
        if page[start:start + 1] in "{[":
            blobs.append(_balanced(page, start))
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for raw in blobs:
        try:
            data = json.loads(htmllib.unescape(raw.strip()) if raw.strip().startswith("&") else raw.strip())
        except ValueError:
            continue
        stack = [data]
        count = 0
        while stack and count < 200000:
            n = stack.pop()
            count += 1
            if isinstance(n, list):
                stack.extend(n)
                continue
            if not isinstance(n, dict):
                continue
            price = next((p for k in PRICE_KEYS if k in n and (p := _num(n[k])) is not None), None)
            shop = _first(n, SHOP_KEYS)
            if isinstance(shop, dict):
                shop = _first(shop, ("name", "displayName", "title", "domain"))
            url = _first(n, URL_KEYS)
            url = url if isinstance(url, str) else None
            if price is not None and (isinstance(shop, str) and shop.strip() or url):
                title = _first(n, TITLE_KEYS)
                name = shop.strip()[:60] if isinstance(shop, str) and shop.strip() else urlparse(url or "").netloc.removeprefix("www.")
                key = (name, price, url)
                if key not in seen:
                    seen.add(key)
                    out.append({"name": name or "?", "price": price, "url": url, "title": title if isinstance(title, str) else None})
            stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
    return out


def _balanced(s: str, start: int) -> str:
    depth, i, instr, esc = 0, start, False, False
    while i < len(s) and i - start < 3_000_000:
        c = s[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        i += 1
    return ""


# button texts, not shop names
CTA_RE = re.compile(r"^(?:naar|bekijk|ga naar|bezoek|koop|kopen|bestel|buy|view|visit|go to|zum|voir|ver|shop now|meer)\b|goedkoopste|cheapest|günstigsten|moins cher", re.I)


def _dom_offers(root: _Node, page_url: str, search: bool) -> list[dict[str, Any]]:
    """HTML heuristic: the smallest block around a (shop) link that holds a euro price."""
    host = urlparse(page_url).netloc.lower()
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    summaries: dict[_Node, tuple[set[float], int]] = {}
    for link in (n for n in root.iter() if n.tag == "a" and n.attrs.get("href")):
        href = link.attrs["href"]
        if not search and not _out(link, host):
            continue
        row, depth = link, 0
        while row.parent is not None and depth < 6 and not _offer_summary(row, host, summaries)[0]:
            row, depth = row.parent, depth + 1
        if row.tag in ("root", "body", "html", "main"):
            continue
        prices, links = _offer_summary(row, host, summaries)
        if not prices or id(row) in seen:
            continue
        # a block with several offers is a whole list, not one offer
        if links > 2 or len(prices) > 3:
            continue
        seen.add(id(row))
        text = row.all_text()
        name = _shop_name(row, link)
        if not search and CTA_RE.search(name):
            continue                                  # "Naar goedkoopste shop": a button, not a shop
        if is_accessory(text) or is_accessory(link.attrs.get("title")):
            continue                                  # an LED kit / display case offer: take the next one
        out.append({"name": name, "price": min(prices), "url": urljoin(page_url, htmllib.unescape(href)),
                    "title": text[:300] if search else None})
    return out


# ------------------------------------------------------------------ page results
@dataclass
class Result:
    kind: str                          # "offers" | "follow" | "missing"
    url: str | None = None             # follow: next URL
    name: str | None = None
    image: str | None = None
    rrp: float | None = None
    ean: str | None = None
    shops: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None
    data: dict[str, Any] | None = None       # set data without shop prices (market value)


def _page_title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip() if m else ""


def _h1(page: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    return re.sub(r"<[^>]+>|\s+", " ", htmllib.unescape(m.group(1))).strip() if m else ""


def _meta(page: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)', page, re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']', page, re.I)
    return htmllib.unescape(m.group(1)).strip() if m else None


def _links(root: _Node, page_url: str, pattern: str, num: str) -> list[str]:
    """Links on the page (matching a path pattern) that are this set and not an accessory, best first."""
    cands: list[tuple[int, str]] = []
    cur = page_url.rstrip("/")
    for a in (n for n in root.iter() if n.tag == "a" and re.search(pattern, n.attrs.get("href", ""))):
        href = urljoin(page_url, htmllib.unescape(a.attrs["href"]))
        text = " ".join(filter(None, (a.all_text(), a.attrs.get("title"), a.attrs.get("aria-label"))))
        blob = text + " " + href
        if href.rstrip("/") == cur or not has_number(blob, num) or is_accessory(blob):
            continue
        score = (2 if has_number(urlparse(href).path, num) else 0) + (1 if re.search(r"lego", blob, re.I) else 0)
        cands.append((-score, href))
    seen: set[str] = set()
    return [h for _, h in sorted(cands) if not (h in seen or seen.add(h))]


def _card_offers(root: _Node, page_url: str, product_url: str) -> list[dict[str, Any]]:
    """Offers inside the search-result card of one product (the block around its link that holds
    shop links, but no other product)."""
    host = urlparse(page_url).netloc.lower()
    pid = re.search(r"/product/(\d+)", product_url)
    for a in (n for n in root.iter() if n.tag == "a" and pid and f"/product/{pid.group(1)}" in n.attrs.get("href", "")):
        card = a
        for _ in range(12):
            if card.parent is None:
                break
            card = card.parent
            ids = {m.group(1) for n in card.iter() if n.tag == "a" and (m := re.search(r"/product/(\d+)", n.attrs.get("href", "")))}
            if len(ids) > 1:
                break                                  # grew into the next product
            if any(_out(n, host) for n in card.iter()):
                return _dom_offers(card, page_url, False)
    return []


def _collect(page: str, page_url: str, search: bool, root: _Node | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    info, offers = _jsonld(page)
    if not offers:
        offers = _embedded(page)
    if not offers:
        offers = _dom_offers(root or _dom(page), page_url, search)
    return info, offers


def _finish(info: dict[str, Any], offers: list[dict[str, Any]], page: str, num: str, domains: dict[str, str],
            search: bool, page_url: str) -> Result:
    shops: dict[str, dict[str, Any]] = {}
    for o in offers:
        title = o.get("title")
        if title and is_accessory(title):
            continue                       # an LED kit / accessory for this set, not the set
        if search and not has_number(title, num):
            continue                       # search results: only this set
        url = urljoin(page_url, o["url"]) if o.get("url") else None
        rid = shop_retailer(o["name"], url, domains)
        key = rid or o["name"].lower()
        if key not in shops or o["price"] < shops[key]["price"]:     # one row per shop, cheapest
            shops[key] = {"name": o["name"], "retailer": rid, "price": round(o["price"], 2), "url": url}
    rrp = None
    if (m := RRP_RE.search(re.sub(r"<[^>]+>", " ", page))):
        rrp = parse_price(m.group(1) or m.group(2))
    name = info.get("name") or _h1(page) or _meta(page, "og:title") or _page_title(page)
    image = info.get("image") or _meta(page, "og:image")
    return Result("offers" if shops else "missing", name=re.sub(r"\s+", " ", str(name or "")).strip()[:160] or None,
                  image=image if isinstance(image, str) and image.startswith("https://") else None,
                  rrp=rrp, ean=info.get("ean"), shops=sorted(shops.values(), key=lambda s: s["price"]))


def _producthero(page: str, num: str, domains: dict[str, str], page_url: str) -> Result | None:
    """Producthero renders with Inertia: the product and its shops sit in <script data-page="app">.
    Per shop the sale price counts when there is one (0 = none); only euro offers."""
    m = re.search(r'<script[^>]*data-page=["\']app["\'][^>]*>(.*?)</script>', page, re.S | re.I)
    if not m:
        return None
    try:
        data = ((json.loads(m.group(1)).get("props") or {}).get("product") or {}).get("data") or {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or not data.get("shops"):
        return None
    title = str(data.get("title") or "")
    if title and not has_number(title, num) and not any(has_number(str(x.get("product_title") or ""), num)
                                                        for x in data["shops"] if isinstance(x, dict)):
        return Result("missing")                       # another product
    offers = []
    for x in data["shops"]:
        if not isinstance(x, dict) or (x.get("currency_code_google") or "EUR") != "EUR":
            continue
        base, sale = _num(x.get("product_price")), _num(x.get("product_sale_price"))
        price = sale if sale and (base is None or sale <= base) else base
        if price is None:
            continue
        offers.append({"name": str(x.get("title") or x.get("shop_alias") or "?").strip(), "price": price,
                       "url": x.get("checkout_link"), "title": x.get("product_title") or title})
    images = data.get("images") or []
    info = {"name": title or None, "image": images[0] if images and isinstance(images[0], str) else None,
            "ean": str(data["eancode"]).strip().lstrip("0").zfill(13) if str(data.get("eancode") or "").strip().isdigit() else None}
    return _finish(info, offers, page, num, domains, False, page_url)


def parse(source: str, page: str, num: str, page_url: str, domains: dict[str, str], step: int = 0) -> Result:
    """What one fetched page of a source gives: offers, a URL to follow, or 'not there'."""
    if source == "brickeconomy":
        return parse_brickeconomy(page, num)
    root = _dom(page)
    path = urlparse(page_url).path
    if source == "kieskeurig":
        if "/product/" not in path:
            links = _links(root, page_url, r"/product/\d+", num)
            if not links:
                return Result("missing")
            # the result card already lists the cheapest shops: kept as a fallback for the product page
            card = _card_offers(root, page_url, links[0])
            fallback = _finish({}, card, "", num, domains, False, page_url).shops if card else []
            if step < MAX_STEPS - 1:
                return Result("follow", url=links[0], shops=fallback)
            return Result("offers" if fallback else "missing", shops=fallback)
        head = _h1(page) + " " + _page_title(page)
        if is_accessory(head):
            return Result("missing", note="only accessories")
        info, offers = _collect(page, page_url, False, root)
        return _finish(info, offers, page, num, domains, False, page_url)
    if source == "producthero":
        if (res := _producthero(page, num, domains, page_url)) is not None:
            return res
        head = _h1(page) + " " + _page_title(page)
        if is_accessory(head):
            return Result("missing", note="only accessories")
        info, offers = _collect(page, page_url, False, root)
        return _finish(info, offers, page, num, domains, False, page_url)
    # search result pages (Shoparize, Channable): every result is one shop's offer
    if not has_number(page, num):
        # a server-rendered result page always repeats the query; without it the results are
        # loaded afterwards by JavaScript and there is nothing in this HTML to read
        return Result("missing", note="js")
    info, offers = _jsonld(page)
    offers = [o for o in offers if o.get("title")] or _embedded(page) or _dom_offers(root, page_url, True)
    res = _finish({}, offers, page, num, domains, True, page_url)
    res.name = res.image = res.rrp = None            # a search page says nothing about the set itself
    return res


# ------------------------------------------------------------------ market value (value, not shop prices)
SEASONS = (("early to mid", 5, 31), ("mid to late", 9, 30), ("early", 3, 31), ("mid", 6, 30), ("late", 12, 31))
MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                          "september", "october", "november", "december"))}


def _eur(text: str | None) -> float | None:
    """A euro amount; None for other currencies (the site follows the visitor's region)."""
    if not text or "€" not in text:
        return None
    m = re.search(r"€\s*([\d.,]+)", text)
    return parse_price(m.group(1)) if m else None


def forecast_date(text: str | None) -> str | None:
    """'Early to mid 2027' -> '2027-05-31', 'December 2024' -> '2024-12-31' (end of the period)."""
    if not text or not (y := re.search(r"(20\d\d)", text)):
        return None
    t, year = text.lower(), int(y.group(1))
    for word, month in MONTHS.items():
        if word in t:
            nxt = date(year + (month == 12), month % 12 + 1, 1)
            return (nxt - timedelta(days=1)).isoformat()
    for word, month, day in SEASONS:
        if word in t:
            return f"{year}-{month:02d}-{day:02d}"
    return f"{year}-12-31"


def parse_brickeconomy(page: str, num: str) -> Result:
    """Set page: market value (new / used), retail price, retirement (forecast), set data."""
    info, _ = _jsonld(page)
    body = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", page, flags=re.S | re.I)
    lines = [x for x in (re.sub(r"\s+", " ", htmllib.unescape(l)).strip() for l in re.split(r"<[^>]+>", body)) if x]

    def after(label: str, start: int = 0, want: str | None = None) -> str | None:
        """The first line after a label line (optionally the first one matching a pattern, within 4 lines)."""
        for i in range(start, len(lines)):
            if lines[i].lower() == label.lower():
                for nxt in lines[i + 1:i + 5]:
                    if want is None or re.search(want, nxt):
                        return nxt
                return None
        return None

    details = next((i for i, x in enumerate(lines) if x.lower() == "set details"), 0)
    setno = after("Set number", details) or ""
    if not re.fullmatch(rf"{re.escape(num)}(?:-\d+)?", setno) and not has_number(info.get("name") or "", num):
        return Result("missing")                        # search / home page: not this set
    pricing = next((i for i, x in enumerate(lines) if x.lower() == "set pricing"), 0)
    predictions = next((i for i, x in enumerate(lines) if x.lower() == "set predictions"), len(lines))
    used = None
    for i in range(pricing, predictions):               # retired sets: separate new / used values
        if lines[i].lower() in ("used", "used value", "value used") and (v := _eur(" ".join(lines[i + 1:i + 3]))):
            used = v
            break
    five = after("5 years retired", want=r"€\s*\d+\s*-\s*€\s*\d+")
    pieces = re.match(r"\d+", after("Pieces", details) or "")
    availability = after("Availability", details) or ""
    retired = after("Retired", details, want=r"20\d\d") if "retired" in availability.lower() else None
    data = {
        "market_new": _eur(after("Market price", pricing, want="€")) or _eur(after("Value", pricing, want="€")) or _eur(after("New/Sealed", pricing, want="€")),
        "market_used": used,
        "retail": _eur(after("Retail price", pricing, want="€")) or _eur(after("Europe", want="€")),
        "availability": availability or None,
        "retired": retired,
        "retirement": None if retired else after("Retirement", predictions),
        "forecast_1y": _eur(after("1 year retired", want="€")),
        "forecast_5y": [parse_price(x) for x in re.findall(r"€\s*([\d.,]+)", five)] if five else None,
        "theme": after("Theme", details), "subtheme": after("Subtheme", details),
        "year": int(y) if (y := after("Year", details) or "").isdigit() else None,
        "pieces": int(pieces.group(0)) if pieces else None,
        "ean": (e if (e := after("EAN") or "").isdigit() and len(e) == 13 else None) or info.get("ean"),
    }
    if "retired" in availability.lower() and not data["retired"]:
        data["retired"] = availability
    name = info.get("name") or after("Name", details)
    image = info.get("image")
    return Result("data", name=re.sub(r"\s+", " ", str(name or "")).strip()[:160] or None,
                  image=image if isinstance(image, str) and image.startswith("https://") else None,
                  rrp=data["retail"], ean=data["ean"], data=data)
