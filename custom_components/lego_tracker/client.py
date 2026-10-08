"""Network access: retailer pages and optional set data."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from urllib.parse import urljoin, urlparse
from typing import Any

import aiohttp

from .models import normalize_set_number
from .i18n import T
from .shops import partner_site_url, domain_of, home_of, ready, reader_of, source_url
from .parsers import Parsed, bot_wall, find_search_result, is_search_url, lego_number, title_check, lego_product_url, parse_set_data_page, parse_page, search_url, url_key

_LOGGER = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}
# To protect the traffic to and the load on the shops and comparison sites: at most 2 requests a minute
# per site, and a search at most once every 2 minutes per site.
DOMAIN_GAP = 30.0
SEARCH_GAP = 120.0


class Aborted(Exception):
    """The running job was stopped while a request was waiting for its turn."""


class NotReady(Exception):
    """No shop settings imported, or their terms not accepted: no website is contacted."""


def site_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
MAX_BODY = 25_000_000            # bytes: a sitemap file or page larger than this is refused


def _too_big(size: Any) -> None:
    """Refuse a response (header value or bytes read so far) over MAX_BODY: pages, search results and sitemaps alike."""
    try:
        n = int(size) if size is not None else 0
    except (TypeError, ValueError):
        return
    if n > MAX_BODY:
        raise ValueError(f"response over {MAX_BODY // 1_000_000} MB")


def registrable(host: str) -> str:
    """'www.shop.com' → 'shop.com', 'www.shop.com.be' → 'shop.com.be' (an IP stays itself)."""
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if not labels or host.replace(".", "").isdigit() or ":" in host:
        return host
    keep = 3 if len(labels) >= 3 and labels[-2] in ("com", "co", "org", "net", "gov", "ac", "edu") else 2
    return ".".join(labels[-keep:])


def redirect_home(retailer: str, url: str) -> str:
    """The site a request (and its redirects) must stay on: a shop's own domain for its pages (so a suffix like
    ne.jp or co.uk is never "the site"); for comparison sites, which have no shop domain, the registrable part."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    dom = (domain_of(retailer) or "").lower()
    return dom if dom and (host == dom or host.endswith("." + dom)) else registrable(host)


def check_url(url: str, home: str) -> None:
    """Only http(s), and only the site the request started on (its own subdomains included)."""
    p = urlparse(url)
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme not in ("http", "https") or not host or not (host == home or host.endswith("." + home)):
        raise ValueError(f"redirect to another site blocked ({host or url[:60]})")


# After a block we stop asking that retailer for a while: hammering makes bot protection stricter.
COOLDOWN_HOURS = (1, 3, 6, 12, 24)
CURL_REQUIREMENT = "curl_cffi>=0.7.0"


class Fetcher:
    """Polite fetcher: one request at a time per retailer, jittered delay, block cooldown.

    Transport is either aiohttp, or curl_cffi impersonating Chrome's TLS/HTTP2 fingerprint
    (much less likely to be blocked). curl_cffi is installed on demand and is optional.
    """

    def __init__(self, hass: Any, use_impersonation: bool = True) -> None:
        self._hass = hass
        self._use_impersonation = use_impersonation
        self._curl_ok: bool | None = None
        self._sessions: dict[str, Any] = {}
        self._warmed: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self.min_delay = 4.0
        self.blocks: dict[str, int] = {}
        self.no_autopause: set[str] = set()
        self.on_pause = None   # callback(retailer, hours), set by the coordinator
        self.blocked_until: dict[str, float] = {}
        self.discover_error: dict[str, str | None] = {}   # why the last search found nothing, per shop
        self.domain_gap, self.search_gap = DOMAIN_GAP, SEARCH_GAP
        self.last_request: dict[str, float] = {}           # per site: when it was last asked anything
        self.last_search: dict[str, float] = {}            # per site: when it was last searched
        self.abort: Any = None                             # callable: True = stop waiting (the job was stopped)
        self.trace: dict[str, deque] = {}                  # per shop: the last requests, what came back and why
        self._search_meta: dict[str, tuple[int | None, int]] = {}
        self.final_url: dict[str, str] = {}               # per shop: where the last request ended up (redirects)

    def _trace(self, key: str, kind: str, url: str, t0: float, status: int | None = None, size: int = 0,
               result: str | None = None, error: str | None = None, set_number: str | None = None) -> None:
        self.trace.setdefault(key, deque(maxlen=40)).append(
            {"ts": time.time(), "kind": kind, "url": url, "status": status, "size": size, "set_number": set_number,
             "ms": int((time.time() - t0) * 1000), "result": result, "error": error})

    @property
    def transport(self) -> str:
        return "curl_cffi (Chrome impersonation)" if self._curl_ok else "aiohttp"

    async def async_setup(self) -> None:
        if not self._use_impersonation:
            self._curl_ok = False
            return
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            try:
                from homeassistant.requirements import async_process_requirements

                await async_process_requirements(self._hass, "lego_tracker", [CURL_REQUIREMENT], is_built_in=False)
                import curl_cffi  # noqa: F401
            except Exception as err:  # noqa: BLE001 - optional dependency, never fatal
                _LOGGER.warning("curl_cffi unavailable, falling back to aiohttp (more likely to be blocked): %s", err)
                self._curl_ok = False
                return
        self._curl_ok = True

    async def async_close(self) -> None:
        for sess in self._sessions.values():
            try:
                res = sess.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()

    def _session(self, retailer: str) -> Any:
        """One cookie jar per retailer, so consent/session cookies persist between polls."""
        if retailer not in self._sessions:
            if self._curl_ok:
                from curl_cffi.requests import AsyncSession

                self._sessions[retailer] = AsyncSession(impersonate="chrome", timeout=30)
            else:
                from homeassistant.helpers.aiohttp_client import async_create_clientsession

                self._sessions[retailer] = async_create_clientsession(self._hass)
        return self._sessions[retailer]

    async def _request(self, retailer: str, url: str, referer: str | None = None, binary: bool = False) -> tuple[int, Any]:
        """One page. Redirects are followed by hand and only within the same site (e.g. www.shop.be → shop.be/nl/…):
        a page can never send the server to another host or into the local network."""
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers.update({"Referer": referer, "Sec-Fetch-Site": "same-origin"})
        sess = self._session(retailer)
        home = redirect_home(retailer, url)
        for _hop in range(MAX_REDIRECTS + 1):
            check_url(url, home)
            if self._curl_ok:
                # streamed: a body over MAX_BODY is never held in memory as a whole
                resp = await sess.get(url, headers=headers, allow_redirects=False, stream=True)
                complete = False
                try:
                    status, loc = resp.status_code, resp.headers.get("location")
                    if status in REDIRECTS and loc:
                        url = urljoin(url, loc)
                        continue
                    _too_big(resp.headers.get("content-length"))
                    chunks, size = [], 0
                    async for chunk in resp.aiter_content():
                        size += len(chunk)
                        _too_big(size)
                        chunks.append(chunk)
                    raw, charset = b"".join(chunks), resp.encoding
                    complete = True
                finally:
                    if not complete and getattr(resp, "quit_now", None) is not None:
                        resp.quit_now.set()       # stop the transfer itself (redirect body, too big, error)
                    await resp.aclose()
            else:
                async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60 if binary else 30),
                                    allow_redirects=False) as resp:
                    status, loc = resp.status, resp.headers.get("Location")
                    if status in REDIRECTS and loc:
                        url = urljoin(url, loc)
                        continue
                    _too_big(resp.headers.get("Content-Length"))
                    buf = bytearray()             # read(n) may return early: read until the end or past the limit
                    while len(buf) <= MAX_BODY and (chunk := await resp.content.read(min(65536, MAX_BODY + 1 - len(buf)))):
                        buf += chunk
                    _too_big(len(buf))
                    raw = bytes(buf)
                    try:
                        charset = resp.get_encoding()
                    except Exception:  # noqa: BLE001 - unknown / undetectable charset
                        charset = resp.charset
            self.final_url[retailer] = url
            if binary:
                return status, raw
            try:
                return status, raw.decode(charset or "utf-8", errors="replace")
            except LookupError:                       # a charset name Python doesn't know
                return status, raw.decode("utf-8", errors="replace")
        raise ValueError("too many redirects")

    def next_free(self, url_or_site: str, search: bool = False) -> float:
        """Seconds until this site may be asked again (searches: also the search gap)."""
        site = site_of(url_or_site) if "/" in url_or_site else url_or_site
        due = self.last_request.get(site, 0) + self.domain_gap
        if search:
            due = max(due, self.last_search.get(site, 0) + self.search_gap)
        return max(0.0, due - time.time())

    async def _wait_turn(self, site: str, search: bool) -> None:
        while (left := self.next_free(site, search)) > 0:
            if self.abort and self.abort():
                raise Aborted
            await asyncio.sleep(min(left, 1.0))

    async def _get(self, retailer: str, url: str, search: bool = False, binary: bool = False) -> tuple[int, Any]:
        if not ready():          # the one gate every request to a shop or comparison site passes
            raise NotReady(T("no shop settings imported (or their terms not accepted): nothing is fetched"))
        if self._curl_ok is None:
            await self.async_setup()
        site = site_of(url)
        lock = self._locks.setdefault(site, asyncio.Lock())      # one request at a time per site
        async with lock:
            await self._wait_turn(site, search)
            origin = home_of(retailer)
            if not ready():      # withdrawn while waiting for this site's turn
                raise NotReady(T("no shop settings imported (or their terms not accepted): nothing is fetched"))
            if origin and retailer not in self._warmed:   # look like a visitor: home page first
                self._warmed.add(retailer)
                try:
                    await self._request(retailer, origin)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2 + random.random() * 2)
            await asyncio.sleep(self.min_delay + random.random() * 3)
            if not ready():      # withdrawn while waiting for this site's turn
                raise NotReady(T("no shop settings imported (or their terms not accepted): nothing is fetched"))
            try:
                return await self._request(retailer, url, referer=None if binary else origin, binary=binary)
            finally:
                self.last_request[site] = time.time()
                if search:
                    self.last_search[site] = time.time()

    def reset_cooldowns(self, retailer: str | None = None) -> None:
        if retailer is None:
            self.blocked_until.clear()
            self.blocks.clear()
        else:
            self.blocked_until.pop(retailer, None)
            self.blocks.pop(retailer, None)

    def paused(self) -> dict[str, float]:
        return {r: round(self.cooldown_left(r) / 3600, 2) for r in self.blocked_until if self.cooldown_left(r) > 0}

    def cooldown_left(self, retailer: str) -> float:
        return max(0.0, self.blocked_until.get(retailer, 0) - time.time())

    def _note_block(self, retailer: str) -> None:
        n = self.blocks.get(retailer, 0)
        if retailer in self.no_autopause:       # user chose: never pause this shop
            self.blocks[retailer] = n + 1
            return
        hours = COOLDOWN_HOURS[min(n, len(COOLDOWN_HOURS) - 1)]
        self.blocked_until[retailer] = time.time() + hours * 3600
        if self.on_pause:
            self.on_pause(retailer, hours)
        self.blocks[retailer] = n + 1

    async def fetch_offer(self, retailer: str, url: str, force: bool = False) -> tuple[Parsed | None, str | None]:
        """Returns (parsed, error). force: also try a paused shop (manual action from the panel)."""
        t0 = time.time()
        parsed, status, size, error = await self._fetch_offer(retailer, url, force)
        if not (error or "").startswith("paused"):
            res = None if not parsed else (f"€{parsed.price:.2f}" if parsed.price is not None else "unavailable")
            self._trace(retailer, "page", url, t0, status, size, res, error)
        return parsed, error

    async def _fetch_offer(self, retailer: str, url: str, force: bool) -> tuple[Parsed | None, int | None, int, str | None]:
        if reader_of(retailer) == "lego" and not lego_number(url):
            # without the set number the page's own product can't be told from recommendations
            return None, None, 0, T("not a product page of the official shop with a set number in the address")
        if not force and (left := self.cooldown_left(retailer)) > 0:
            return None, None, 0, T("paused {hours} h after being blocked", hours=f"{left / 3600:.1f}")
        if reader_of(retailer) == "partner":
            url = partner_site_url(url)                         # the partner shop's site chosen in Settings (NL or BE)
        try:
            status, page = await self._get(retailer, url)
        except Aborted:
            return None, None, 0, T("paused: the job was stopped")
        except Exception as err:  # noqa: BLE001 - aiohttp and curl_cffi raise different types
            return None, None, 0, T("network error: {error}", error=str(err)[:120])
        size = len(page or "")
        if status in (403, 429, 503):
            self._note_block(retailer)
            return None, status, size, T("blocked (HTTP {status})", status=status)
        if status == 404:
            return None, status, size, T("page not found (HTTP 404)")
        if status >= 400:
            return None, status, size, T("HTTP error {status}", status=status)
        parsed = parse_page(retailer, page, lego_number(url) if reader_of(retailer) == "lego" else None)
        if parsed.blocked:
            self._note_block(retailer)
            return None, status, size, T("blocked (captcha / bot protection)")
        if parsed.price is None and not parsed.unavailable:
            return None, status, size, T("price not found on the page")
        self.blocks[retailer] = 0
        return parsed, status, size, None

    async def get_page(self, key: str, url: str, force: bool = False, note_block: bool = True) -> tuple[int, str, str | None]:
        """(status, html, error) for an extra source (a comparison site), with the same politeness and pauses.
        note_block=False: a refusal doesn't pause the whole site (e.g. only its product pages are blocked)."""
        if not force and (left := self.cooldown_left(key)) > 0:
            return 0, "", T("paused {hours} h after being blocked", hours=f"{left / 3600:.1f}")
        try:
            status, page = await self._get(key, url)
        except Aborted:
            return 0, "", T("paused: the job was stopped")
        except Exception as err:  # noqa: BLE001
            return 0, "", T("network error: {error}", error=str(err)[:120])
        if status in (403, 429, 503) or bot_wall(page):
            if note_block:
                self._note_block(key)
            return status, "", T("blocked (HTTP {status})", status=status) if status >= 400 else T("blocked (captcha / bot protection)")
        return status, page, None

    def _landed_on_product(self, retailer: str, url: str, page: str, set_number: str) -> str | None:
        """A search for one exact product often jumps straight to its product page (redirect): that page is the link."""
        final = (self.final_url.get(retailer) or "").split("#")[0]
        if not final or final.split("?")[0] == url.split("?")[0] or is_search_url(final) or site_of(final) != site_of(url):
            return None
        parsed = parse_page(retailer, page, set_number)
        title = parsed.title or ""
        if title_check(title if "lego" in title.lower() else f"lego {title}", set_number)[0] == "ok":
            return final
        return None

    async def get_raw(self, retailer: str, url: str) -> tuple[int, bytes, str | None]:
        """(status, body bytes, error) for a sitemap / robots.txt of a shop, with the same pacing and pauses."""
        t0 = time.time()
        if self.cooldown_left(retailer) > 0:
            return 0, b"", T("paused after being blocked")
        try:
            status, body = await self._get(retailer, url, binary=True)
        except Aborted:
            return 0, b"", T("paused: the job was stopped")
        except Exception as err:  # noqa: BLE001
            self._trace(retailer, "sitemap", url, t0, None, 0, None, T("network error: {error}", error=str(err)[:120]))
            return 0, b"", T("network error: {error}", error=str(err)[:120])
        body = body if isinstance(body, (bytes, bytearray)) else str(body or "").encode()
        error = T("HTTP error {status}", status=status) if status >= 400 else None
        self._trace(retailer, "sitemap", url, t0, status, len(body), None, error)
        return status, bytes(body), error

    async def discover(self, retailer: str, set_number: str, force: bool = False, url: str | None = None,
                       skip: set[str] | frozenset[str] = frozenset()) -> str | None:
        """Search the shop for the set; every attempt is kept in the shop's trace (Shops → click a shop).
        skip: url_keys of links blocked for this set (the next good search hit is taken)."""
        t0 = time.time()
        self._search_meta[retailer] = (None, 0)
        found = await self._discover(retailer, set_number, force, url, skip)
        err = self.discover_error.get(retailer)
        if not (err or "").startswith("paused"):
            status, size = self._search_meta.get(retailer, (None, 0))
            self._trace(retailer, "search", url or search_url(retailer, set_number) or "", t0, status, size,
                        found, err, set_number)
        return found

    async def _discover(self, retailer: str, set_number: str, force: bool = False, url: str | None = None,
                        skip: set[str] | frozenset[str] = frozenset()) -> str | None:
        """Search the shop for the set (url: a search page to use instead of the shop's search URL).
        On failure the reason is kept in self.discover_error[retailer] (blocked, HTTP error, results
        loaded by JavaScript, or really nothing matching), so the panel can say what happened."""
        self.discover_error[retailer] = None
        url = url or search_url(retailer, set_number)
        if not url or (not force and self.cooldown_left(retailer) > 0):
            self.discover_error[retailer] = T("paused after being blocked") if url else T("this shop has no search URL")
            return None
        try:
            status, page = await self._get(retailer, url, search=True)
            self._search_meta[retailer] = (status, len(page or ""))
        except Aborted:
            self.discover_error[retailer] = T("paused: the job was stopped")
            return None
        except Exception as err:  # noqa: BLE001
            self.discover_error[retailer] = T("could not reach the shop: {error}", error=str(err)[:100])
            return None
        if status in (403, 429, 503) or (status < 400 and parse_page(retailer, page).blocked):
            self._note_block(retailer)
            self.discover_error[retailer] = T("the shop blocked the search (HTTP {status})", status=status) if status >= 400 \
                else T("the shop blocked the search (captcha / bot protection)")
            return None
        if status >= 400:
            self.discover_error[retailer] = T("search page: HTTP error {status}", status=status)
            return None
        found = find_search_result(retailer, page, set_number, skip) or self._landed_on_product(retailer, url, page, set_number)
        if found and url_key(retailer, found) in skip:
            found = None                                   # the search jumped to a page you blocked: look elsewhere
        if found or reader_of(retailer) != "lego":
            if not found:
                self.discover_error[retailer] = T("no matching product found") if set_number in page else \
                    T("the search page does not contain {number}: this shop probably loads its results with JavaScript. Paste the product page URL instead.", number=set_number)
            return found
        # The official shop's search is partly rendered in the browser: try the product URL directly (its own trace entry)
        t0, purl = time.time(), lego_product_url(set_number, retailer)
        if not purl or url_key(retailer, purl) in skip:                # you blocked this page: never visit it again
            self.discover_error[retailer] = T("no matching product found")
            return None
        try:
            status, page = await self._get(retailer, purl)
        except Exception as err:  # noqa: BLE001 - also Aborted
            self._trace(retailer, "page", purl, t0, None, 0, None, T("network error: {error}", error=str(err)[:120]), set_number)
            return None
        if status in (403, 429, 503) or (status < 400 and parse_page(retailer, page, set_number).blocked):
            self._note_block(retailer)                     # a refusal or a bot wall here pauses the shop too
            self.discover_error[retailer] = T("blocked (HTTP {status})", status=status) if status >= 400 \
                else T("blocked (captcha / bot protection)")
            self._trace(retailer, "page", purl, t0, status, len(page or ""), None, self.discover_error[retailer], set_number)
            return None
        if status < 400:
            parsed = parse_page(retailer, page, set_number)
            if parsed.price or parsed.list_price or (parsed.title and set_number in (parsed.title + page[:200000])):
                self._trace(retailer, "page", purl, t0, status, len(page or ""), purl, None, set_number)
                return purl
        self.discover_error[retailer] = T("no matching product found")
        self._trace(retailer, "page", purl, t0, status, len(page or ""), None,
                    T("HTTP error {status}", status=status) if status >= 400 else T("no matching product found"), set_number)
        return None


async def set_data_lookup(session: aiohttp.ClientSession, api_key: str, set_number: str) -> dict[str, Any] | None:
    """Metadata from the set data API (free key). Returns None on any problem."""
    if not api_key or not (url := source_url("set_data_api")):
        return None
    params = {"apiKey": api_key, "userHash": "", "params": json.dumps({"setNumber": f"{normalize_set_number(set_number)}-1"})}
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    sets = data.get("sets") or []
    if data.get("status") != "success" or not sets:
        return None
    s = sets[0]
    prices = (s.get("LEGOCom") or {})
    rrp = None
    for region in ("DE", "UK", "US"):  # DE is EUR; others are a fallback
        rrp = (prices.get(region) or {}).get("retailPrice")
        if rrp:
            break
    return {
        "name": s.get("name"), "theme": s.get("theme"), "subtheme": s.get("subtheme"),
        "year": s.get("year"), "pieces": s.get("pieces"),
        "image": (s.get("image") or {}).get("imageURL"), "rrp": rrp,
        "themeGroup": s.get("themeGroup"),
        "exit_date": (s.get("exitDate") or "")[:10] or None,
    }


_RB_THEMES: dict[int, dict[str, Any]] = {}


async def parts_lookup(session: aiohttp.ClientSession, api_key: str, set_number: str) -> dict[str, Any] | None:
    """Metadata from the parts database API (free key). No RRP there."""
    if not api_key or not (base := source_url("parts_api")):
        return None
    base = base.rstrip("/")
    headers = {"Authorization": f"key {api_key}", "Accept": "application/json"}
    try:
        async with session.get(f"{base}/sets/{normalize_set_number(set_number)}-1/", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            s = await resp.json(content_type=None)
        out = {"name": s.get("name"), "year": s.get("year"), "pieces": s.get("num_parts") or None,
               "image": s.get("set_img_url")}
        tid = s.get("theme_id")
        chain: list[str] = []
        while tid and len(chain) < 4:
            if tid not in _RB_THEMES:
                async with session.get(f"{base}/themes/{tid}/", headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        break
                    _RB_THEMES[tid] = await resp.json(content_type=None)
            chain.insert(0, _RB_THEMES[tid].get("name"))
            tid = _RB_THEMES[tid].get("parent_id")
        if chain:
            out["theme"] = chain[0]
            if len(chain) > 1:
                out["subtheme"] = chain[-1]
        return {k: v for k, v in out.items() if v}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def set_data_page_lookup(session: aiohttp.ClientSession, set_number: str) -> dict[str, Any] | None:
    """Fallback without any key: the public set page of the set data source."""
    if not (tpl := source_url("set_data_page")):
        return None
    url = tpl.replace("{number}", normalize_set_number(set_number))
    try:
        async with session.get(url, headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return parse_set_data_page(await resp.text(errors="replace")) or None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


# where set data comes from (stored as the source of a field): the set data API, its public set page, the parts database
SET_DATA_API, SET_DATA_PAGE, PARTS_API = "set_data_api", "set_data_page", "parts_api"
OFFICIAL_COUNT = (SET_DATA_API, SET_DATA_PAGE)      # these give the official piece count


async def lookup_metadata(session: aiohttp.ClientSession, set_data_key: str, parts_key: str,
                          set_number: str) -> tuple[dict[str, Any], str | None]:
    """Set data API -> parts database API -> public set data page.

    A source that fails or lacks fields is complemented by the next one; the name comes from the
    first source that has it. Returns (data, "Source1+Source2")."""
    wanted = ("name", "theme", "subtheme", "year", "pieces", "image", "rrp", "exit_date")
    merged: dict[str, Any] = {"_from": {}}           # _from: which source gave each field
    used: list[str] = []
    sources = []
    if set_data_key:
        sources.append((SET_DATA_API, lambda: set_data_lookup(session, set_data_key, set_number)))
    if parts_key:
        sources.append((PARTS_API, lambda: parts_lookup(session, parts_key, set_number)))
    if source_url("set_data_page"):
        sources.append((SET_DATA_PAGE, lambda: set_data_page_lookup(session, set_number)))
    official = OFFICIAL_COUNT
    for name, fetch in sources:
        if all(merged.get(k) for k in wanted if k not in ("subtheme", "exit_date")) \
                and merged["_from"].get("pieces") in official:
            break
        try:
            data = await fetch()
        except Exception:  # noqa: BLE001 - a broken source must not stop the others
            _LOGGER.debug("metadata source %s failed for %s", name, set_number, exc_info=True)
            data = None
        if not data:
            continue
        added = False
        for k in wanted:
            # the official piece count (set data) wins over a parts count (the parts database counts differently)
            better = k == "pieces" and name in official and merged["_from"].get("pieces") not in official
            if data.get(k) and (not merged.get(k) or better):
                merged[k] = data[k]
                merged["_from"][k] = name
                added = True
        if added:
            used.append(name)
    return (merged if used else {}), ("+".join(used) or None)


async def test_metadata_source(session: aiohttp.ClientSession, source: str, key: str) -> tuple[bool, str]:
    """Settings panel 'test' button: try a well known set."""
    fn = {"set_data": set_data_lookup, "parts": parts_lookup}[source]
    if not key:
        return False, T("no key entered")
    if not source_url(f"{source}_api"):
        return False, T("this source is not in your shop settings (or their terms are not accepted)")
    data = await fn(session, key, "10281")
    if data and data.get("name"):
        return True, T("works: 10281 = {name}", name=data["name"])
    return False, T("no answer or invalid key")
