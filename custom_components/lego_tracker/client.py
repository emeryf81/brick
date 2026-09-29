"""Network access: retailer pages and optional Brickset metadata."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import aiohttp

from .models import normalize_set_number
from .parsers import Parsed, find_search_result, parse_brickset_page, parse_page, search_url

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
ORIGINS = {
    "amazon_nl": "https://www.amazon.nl/", "amazon_de": "https://www.amazon.de/",
    "amazon_be": "https://www.amazon.com.be/", "bol": "https://www.bol.com/nl/nl/",
    "kruidvat_be": "https://www.kruidvat.be/nl/",
}
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
        self.blocked_until: dict[str, float] = {}

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

    async def _request(self, retailer: str, url: str, referer: str | None = None) -> tuple[int, str]:
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers.update({"Referer": referer, "Sec-Fetch-Site": "same-origin"})
        sess = self._session(retailer)
        if self._curl_ok:
            resp = await sess.get(url, headers=headers, allow_redirects=True)
            return resp.status_code, resp.text
        async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as resp:
            return resp.status, await resp.text(errors="replace")

    async def _get(self, retailer: str, url: str) -> tuple[int, str]:
        if self._curl_ok is None:
            await self.async_setup()
        lock = self._locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            origin = ORIGINS.get(retailer)
            if origin and retailer not in self._warmed:   # look like a visitor: home page first
                self._warmed.add(retailer)
                try:
                    await self._request(retailer, origin)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2 + random.random() * 2)
            await asyncio.sleep(self.min_delay + random.random() * 3)
            return await self._request(retailer, url, referer=origin)

    def reset_cooldowns(self) -> None:
        self.blocked_until.clear()
        self.blocks.clear()

    def paused(self) -> dict[str, float]:
        return {r: round(self.cooldown_left(r) / 3600, 2) for r in self.blocked_until if self.cooldown_left(r) > 0}

    def cooldown_left(self, retailer: str) -> float:
        return max(0.0, self.blocked_until.get(retailer, 0) - time.time())

    def _note_block(self, retailer: str) -> None:
        n = self.blocks.get(retailer, 0)
        self.blocked_until[retailer] = time.time() + COOLDOWN_HOURS[min(n, len(COOLDOWN_HOURS) - 1)] * 3600
        self.blocks[retailer] = n + 1

    async def fetch_offer(self, retailer: str, url: str) -> tuple[Parsed | None, str | None]:
        """Returns (parsed, error)."""
        if (left := self.cooldown_left(retailer)) > 0:
            return None, f"paused {left / 3600:.1f} h after being blocked (use report_price / userscript, or wait)"
        try:
            status, page = await self._get(retailer, url)
        except Exception as err:  # noqa: BLE001 - aiohttp and curl_cffi raise different types
            return None, f"network: {err}"
        if status in (403, 429, 503):
            self._note_block(retailer)
            return None, f"blocked (HTTP {status})"
        if status == 404:
            return None, "not found (HTTP 404)"
        if status >= 400:
            return None, f"HTTP {status}"
        parsed = parse_page(retailer, page)
        if parsed.blocked:
            self._note_block(retailer)
            return None, "blocked (captcha / bot protection)"
        if parsed.price is None and not parsed.unavailable:
            return None, "price not found on page (markup changed?)"
        self.blocks[retailer] = 0
        return parsed, None

    async def discover(self, retailer: str, set_number: str) -> str | None:
        url = search_url(retailer, set_number)
        if not url or self.cooldown_left(retailer) > 0:
            return None
        try:
            status, page = await self._get(retailer, url)
        except Exception:  # noqa: BLE001
            return None
        if status in (403, 429, 503) or (status < 400 and parse_page(retailer, page).blocked):
            self._note_block(retailer)
            return None
        return find_search_result(retailer, page, set_number) if status < 400 else None


async def brickset_lookup(session: aiohttp.ClientSession, api_key: str, set_number: str) -> dict[str, Any] | None:
    """Metadata from Brickset API v3 (free key). Returns None on any problem."""
    if not api_key:
        return None
    params = {"apiKey": api_key, "userHash": "", "params": json.dumps({"setNumber": f"{normalize_set_number(set_number)}-1"})}
    try:
        async with session.get("https://brickset.com/api/v3.asmx/getSets", params=params,
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


async def rebrickable_lookup(session: aiohttp.ClientSession, api_key: str, set_number: str) -> dict[str, Any] | None:
    """Metadata from the Rebrickable API v3 (free key at rebrickable.com/api). No RRP there."""
    if not api_key:
        return None
    headers = {"Authorization": f"key {api_key}", "Accept": "application/json"}
    base = "https://rebrickable.com/api/v3/lego"
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


async def brickset_page_lookup(session: aiohttp.ClientSession, set_number: str) -> dict[str, Any] | None:
    """Fallback without any key: the public brickset.com set page."""
    url = f"https://brickset.com/sets/{normalize_set_number(set_number)}-1"
    try:
        async with session.get(url, headers=BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return parse_brickset_page(await resp.text(errors="replace")) or None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def lookup_metadata(session: aiohttp.ClientSession, brickset_key: str, rebrickable_key: str,
                          set_number: str) -> tuple[dict[str, Any], str | None]:
    """Try Brickset API, then Rebrickable API, then the Brickset web page. Returns (data, source)."""
    for source, coro in (("Brickset", brickset_lookup(session, brickset_key, set_number) if brickset_key else None),
                         ("Rebrickable", rebrickable_lookup(session, rebrickable_key, set_number) if rebrickable_key else None)):
        if coro is not None and (data := await coro):
            return {k: v for k, v in data.items() if v}, source
    data = await brickset_page_lookup(session, set_number)
    return (data or {}), ("brickset.com" if data else None)
