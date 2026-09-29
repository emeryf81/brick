"""Brickwatch (brickwatch.net) as an extra price source: one page per set with the prices of many shops.

Hidden option. One request per set (https://www.brickwatch.net/<locale>/set/<number>/) gives the
prices of every shop Brickwatch follows. Missing pages (404) are not retried within a day.

The page markup is not documented, so parsing is tolerant: JSON-LD offers first, then an HTML
heuristic (a block with an outgoing shop link and a euro price is a shop row).
"""
from __future__ import annotations

import html as htmllib
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from .models import parse_price

BASE = "https://www.brickwatch.net"
LOCALES = {"nl-be": "nl-BE", "fr-be": "fr-BE", "en-be": "en-BE", "nl-nl": "nl-NL", "en-nl": "en-NL", "fr-fr": "fr-FR",
           "en-gb": "en-GB", "de-de": "de-DE", "en-de": "en-DE"}
PRICE_RE = re.compile(r"(?:€\s*(\d{1,4}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?|\d{1,4}[.,]-)|(\d{1,4}(?:[.\s]\d{3})*(?:[.,]\d{1,2})?)\s*€)")
OLD_PRICE = re.compile(r"old|strike|was|rrp|advies|retail|list-?price|msrp|original|crossed|uvp", re.I)
RRP_RE = re.compile(r"(?:adviesprijs|winkelprijs|verkoopprijs lego|rrp|prix conseillé|prix public|uvp|retail price)[^€\d]{0,60}"
                    r"(?:€\s*([\d.,]+)|([\d.,]+)\s*€)", re.I)
# shop name / domain -> our retailer id (custom shops are matched by their domain)
SHOP_ALIASES = (
    ("amazon.com.be", "amazon_be"), ("amazon.be", "amazon_be"), ("amazon be", "amazon_be"), ("amazon belgi", "amazon_be"),
    ("amazon.nl", "amazon_nl"), ("amazon nl", "amazon_nl"), ("amazon nederland", "amazon_nl"),
    ("amazon.de", "amazon_de"), ("amazon de", "amazon_de"), ("amazon duitsland", "amazon_de"),
    ("bol.com", "bol"), ("bol", "bol"), ("lego.com", "lego_com"), ("lego shop", "lego_com"), ("lego store", "lego_com"),
    ("kruidvat", "kruidvat_be"), ("dreamland", "dreamland_be"),
)


def locale_for(lego_locale: str | None) -> str:
    return LOCALES.get((lego_locale or "nl-be").lower(), "nl-BE")


def set_url(set_number: str, lego_locale: str | None = None) -> str:
    return f"{BASE}/{locale_for(lego_locale)}/set/{set_number}/"


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag, self.attrs, self.parent, self.children, self.text = tag, attrs, parent, [], []

    def all_text(self) -> str:
        out: list[str] = []
        stack: list[Any] = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                out.append(n)
            elif n.tag not in ("script", "style"):
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

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if data.strip():
            self.cur.children.append(data)


def _prices(node: _Node) -> list[float]:
    """Euro prices in a block, skipping struck-through / 'old' / RRP prices."""
    out: list[float] = []
    stack: list[tuple[Any, bool]] = [(node, False)]
    while stack:
        n, old = stack.pop()
        if isinstance(n, str):
            if not old:
                for m in PRICE_RE.finditer(n):
                    raw = (m.group(1) or m.group(2) or "").replace(" ", "")
                    if raw.endswith(",-") or raw.endswith(".-"):
                        raw = raw[:-2]
                    if (p := parse_price(raw)) is not None and 0.5 <= p <= 10000:
                        out.append(p)
            continue
        if n.tag in ("script", "style"):
            continue
        is_old = old or n.tag in ("s", "del", "strike") or bool(OLD_PRICE.search(n.attrs.get("class", "") + " " + n.attrs.get("data-test", "")))
        stack.extend((c, is_old) for c in reversed(n.children))
    return out


def shop_retailer(name: str, href: str | None, domains: dict[str, str]) -> str | None:
    """Map a Brickwatch shop (name / outgoing link) to one of our retailer ids."""
    host = urlparse(href or "").netloc.lower().removeprefix("www.")
    for rid, dom in domains.items():
        if host and (host == dom or host.endswith("." + dom)):
            return rid
    n = name.lower().strip()
    for rid, dom in domains.items():               # custom shops: their domain in the shop name
        if dom and dom in n:
            return rid
    for alias, rid in SHOP_ALIASES:
        if n == alias or n.startswith(alias + " ") or alias in n and "." in alias:
            return rid
    return None


def _shop_name(row: _Node, link: _Node) -> str:
    for n in row.iter():
        if n.tag == "img" and (alt := (n.attrs.get("alt") or n.attrs.get("title") or "").strip()):
            if not re.search(r"lego set|\d{4,}", alt, re.I):
                return alt[:60]
    for key in ("title", "data-shop", "data-site", "aria-label"):
        if (v := link.attrs.get(key, "").strip()):
            return v[:60]
    text = link.all_text()
    if text and len(text) <= 40 and not PRICE_RE.search(text):
        return text
    for n in row.iter():
        if n.attrs.get("class") and re.search(r"shop|site|store|retailer|merchant|winkel", n.attrs["class"], re.I):
            t = n.all_text()
            if t and len(t) <= 40:
                return t
    return urlparse(link.attrs.get("href", "")).netloc.removeprefix("www.") or "?"


def _is_shop_link(href: str) -> bool:
    if not href or href.startswith(("#", "mailto:", "javascript:")):
        return False
    u = urlparse(href)
    if u.netloc and "brickwatch" not in u.netloc:
        return True                                   # direct outgoing link
    return bool(re.search(r"/(?:go|out|redirect|click|buy|visit|link|r)/", u.path, re.I))


def _from_jsonld(page: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    info: dict[str, Any] = {}
    shops: list[dict[str, Any]] = []
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, list):
                stack.extend(n)
                continue
            if not isinstance(n, dict):
                continue
            types = n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]
            if "Product" in types:
                info.setdefault("name", n.get("name"))
                img = n.get("image")
                info.setdefault("image", img[0] if isinstance(img, list) and img else img)
            if "Offer" in types and n.get("price") is not None:
                seller = n.get("seller") or {}
                name = seller.get("name") if isinstance(seller, dict) else str(seller)
                if name and (p := parse_price(n.get("price"))) and "OutOfStock" not in str(n.get("availability", "")):
                    shops.append({"name": str(name)[:60], "price": p, "url": n.get("url")})
                continue
            stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
    return info, shops


def parse_set_page(page: str, set_number: str, page_url: str, domains: dict[str, str]) -> dict[str, Any] | None:
    """{'name', 'image', 'rrp', 'shops': [{'name', 'retailer', 'price', 'url'}]} or None if this is not the set's page."""
    info, shops = _from_jsonld(page)
    if not shops:
        b = _Builder()
        try:
            b.feed(page)
        except Exception:  # noqa: BLE001 - broken markup: use what was parsed
            pass
        seen: set[int] = set()
        for link in (n for n in b.root.iter() if n.tag == "a" and _is_shop_link(n.attrs.get("href", ""))):
            row, depth = link, 0
            while row.parent is not None and depth < 6 and not _prices(row):
                row, depth = row.parent, depth + 1
            if not _prices(row) or id(row) in seen:
                continue
            # a row with several shop links is a whole list, not one shop: skip it
            if sum(1 for n in row.iter() if n.tag == "a" and _is_shop_link(n.attrs.get("href", ""))) > 2:
                continue
            seen.add(id(row))
            prices = _prices(row)
            shops.append({"name": _shop_name(row, link), "price": min(prices),
                          "url": urljoin(page_url, htmllib.unescape(link.attrs["href"]))})
    title = info.get("name") or (re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I) or [None, None])[1]
    title = re.sub(r"<[^>]+>|\s+", " ", htmllib.unescape(title or "")).strip()
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
    if not shops and set_number not in (title + (_page_title(page) or "")):
        return None                                   # e.g. the home page after a redirect
    rrp = None
    if (m := RRP_RE.search(re.sub(r"<[^>]+>", " ", page))):
        rrp = parse_price(m.group(1) or m.group(2))
    out_shops, keyed = [], {}
    for s in shops:
        s["retailer"] = shop_retailer(s["name"], s.get("url"), domains)
        key = s["retailer"] or s["name"].lower()
        if key not in keyed or s["price"] < keyed[key]["price"]:      # one row per shop, cheapest
            keyed[key] = s
    out_shops = sorted(keyed.values(), key=lambda s: s["price"])
    image = info.get("image") or (htmllib.unescape(og.group(1)) if og else None)
    return {"name": title or None, "image": image if isinstance(image, str) and image.startswith("https://") else None,
            "rrp": rrp, "shops": out_shops}


def _page_title(page: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return htmllib.unescape(m.group(1)).strip() if m else None
