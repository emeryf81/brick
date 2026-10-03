"""Product links from a shop's sitemap (robots.txt → sitemap index → product sitemaps).

Shops publish every product URL in their sitemap for search engines. Product URLs usually carry the
product name, so 'lego-icons-chrysant-10368' gives the set number without any search. This works for
shops whose search page is built with JavaScript (nothing to read for the server), costs only a few
requests per week, and the same title check as everywhere else keeps LED kits and knock-offs out.
"""
from __future__ import annotations

import gzip
import html as htmllib
import re
from urllib.parse import urlparse

from .parsers import is_search_url, title_check

MAX_FILES = 40                 # sitemap files per shop and round (each is a request, spaced like any other)
MAX_URLS = 20000               # LEGO product URLs kept per shop
REFRESH_DAYS = 7

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
# sitemap files that list products; files with these words are visited first, others only if needed
PRODUCT_HINT = re.compile(r"product|prod|artikel|article|item|catalog|/p[-_./]|lego|toy|speelgoed|spielzeug", re.I)
SKIP_HINT = re.compile(r"categor|cms|content|image|video|blog|store|winkel|brand|merk|page|static|faq|news|"
                       r"recipe|press|career|vacature|landing", re.I)


def body_text(body: bytes) -> str:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return ""
    return body.decode("utf-8", errors="replace")


def robots_sitemaps(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^\s*sitemap:\s*(\S+)", text, re.I | re.M)]


def parse(text: str) -> tuple[list[str], list[str]]:
    """(child sitemap URLs, page URLs) of one sitemap file."""
    locs = [htmllib.unescape(x) for x in LOC_RE.findall(text)]
    if re.search(r"<sitemapindex", text[:5000], re.I):
        return locs, []
    return [], locs


def order_children(children: list[str]) -> list[str]:
    """Product sitemaps first, obvious non-product files last."""
    def rank(u: str) -> int:
        return 0 if PRODUCT_HINT.search(u) and not SKIP_HINT.search(u) else 2 if SKIP_HINT.search(u) else 1
    return sorted(dict.fromkeys(children), key=rank)


def lego_urls(urls: list[str], domain: str) -> list[str]:
    """Only LEGO product pages on the shop's own domain (the rest of the shop is not needed)."""
    out = []
    for u in urls:
        p = urlparse(u)
        if domain in p.netloc and "lego" in u.lower() and not is_search_url(u) and re.search(r"\d{3,7}", p.path):
            out.append(u.split("#")[0])
    return out


def _words(url: str) -> str:
    path = urlparse(url).path
    return re.sub(r"[-_/.+]+", " ", re.sub(r"\.(html?|aspx|php)$", "", path))


def match(urls: list[str], num: str) -> str | None:
    """The product URL for this set: the set number in the URL as a whole number and a URL that passes the
    title check (LEGO, the set number, no accessory, no other brand). The most specific URL wins."""
    rx = re.compile(rf"(?<!\d){re.escape(num)}(?!\d)")
    hits = []
    for u in urls:
        words = _words(u)
        if rx.search(words) and title_check(words if "lego" in words.lower() else f"lego {words}", num)[0] == "ok":
            hits.append(u)
    # a product code that merely equals the number (…/p/10368) is weaker than the number in the name
    hits.sort(key=lambda u: (not rx.search(re.sub(r"/p/\d+.*$", "", urlparse(u).path)), len(u)))
    return hits[0] if hits else None
