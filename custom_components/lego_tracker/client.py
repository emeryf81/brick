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
from .i18n import T
from .shops import domain_of
from .parsers import Parsed, find_search_result, lego_product_url, parse_brickset_page, parse_page, search_url

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
    "kruidvat_be": "https://www.kruidvat.be/nl/", "brickwatch": "https://www.brickwatch.net/",
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
        self.no_autopause: set[str] = set()
        self.on_pause = None   # callback(retailer, hours), set by the coordinator
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
            origin = ORIGINS.get(retailer) or (f"https://www.{d}/" if (d := domain_of(retailer)) else None)
            if origin and retailer not in self._warmed:   # look like a visitor: home page first
                self._warmed.add(retailer)
                try:
                    await self._request(retailer, origin)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2 + random.random() * 2)
            await asyncio.sleep(self.min_delay + random.random() * 3)
            return await self._request(retailer, url, referer=origin)

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
        if not force and (left := self.cooldown_left(retailer)) > 0:
            return None, T("paused {hours} h after being blocked", hours=f"{left / 3600:.1f}")
        try:
            status, page = await self._get(retailer, url)
        except Exception as err:  # noqa: BLE001 - aiohttp and curl_cffi raise different types
            return None, T("network error: {error}", error=str(err)[:120])
        if status in (403, 429, 503):
            self._note_block(retailer)
            return None, T("blocked (HTTP {status})", status=status)
        if status == 404:
            return None, T("page not found (HTTP 404)")
        if status >= 400:
            return None, T("HTTP error {status}", status=status)
        parsed = parse_page(retailer, page)
        if parsed.blocked:
            self._note_block(retailer)
            return None, T("blocked (captcha / bot protection)")
        if parsed.price is None and not parsed.unavailable:
            return None, T("price not found on the page")
        self.blocks[retailer] = 0
        return parsed, None

    async def get_page(self, key: str, url: str, force: bool = False) -> tuple[int, str, str | None]:
        """(status, html, error) for an extra source (e.g. Brickwatch), with the same politeness and pauses."""
        if not force and (left := self.cooldown_left(key)) > 0:
            return 0, "", T("paused {hours} h after being blocked", hours=f"{left / 3600:.1f}")
        try:
            status, page = await self._get(key, url)
        except Exception as err:  # noqa: BLE001
            return 0, "", T("network error: {error}", error=str(err)[:120])
        if status in (403, 429, 503):
            self._note_block(key)
            return status, "", T("blocked (HTTP {status})", status=status)
        return status, page, None

    async def discover(self, retailer: str, set_number: str, force: bool = False) -> str | None:
        url = search_url(retailer, set_number)
        if not url or (not force and self.cooldown_left(retailer) > 0):
            return None
        try:
            status, page = await self._get(retailer, url)
        except Exception:  # noqa: BLE001
            return None
        if status in (403, 429, 503) or (status < 400 and parse_page(retailer, page).blocked):
            self._note_block(retailer)
            return None
        found = find_search_result(retailer, page, set_number) if status < 400 else None
        if found or retailer != "lego_com":
            return found
        # LEGO.com search is partly rendered in the browser: try the product URL directly
        try:
            status, page = await self._get(retailer, lego_product_url(set_number))
        except Exception:  # noqa: BLE001
            return None
        if status < 400:
            parsed = parse_page(retailer, page)
            if parsed.price or (parsed.title and set_number in (parsed.title + page[:200000])):
                return lego_product_url(set_number)
        return None


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
    """Brickset API -> Rebrickable API -> public Brickset page.

    A source that fails or lacks fields is complemented by the next one; the name comes from the
    first source that has it. Returns (data, "Source1+Source2")."""
    wanted = ("name", "theme", "subtheme", "year", "pieces", "image", "rrp", "exit_date")
    merged: dict[str, Any] = {}
    used: list[str] = []
    sources = []
    if brickset_key:
        sources.append(("Brickset", lambda: brickset_lookup(session, brickset_key, set_number)))
    if rebrickable_key:
        sources.append(("Rebrickable", lambda: rebrickable_lookup(session, rebrickable_key, set_number)))
    sources.append(("brickset.com", lambda: brickset_page_lookup(session, set_number)))
    for name, fetch in sources:
        if all(merged.get(k) for k in wanted if k not in ("subtheme", "exit_date")):
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
            if data.get(k) and not merged.get(k):
                merged[k] = data[k]
                added = True
        if added:
            used.append(name)
    return merged, ("+".join(used) or None)


async def test_metadata_source(session: aiohttp.ClientSession, source: str, key: str) -> tuple[bool, str]:
    """Settings panel 'test' button: try a well known set."""
    fn = {"brickset": brickset_lookup, "rebrickable": rebrickable_lookup}[source]
    if not key:
        return False, T("no key entered")
    data = await fn(session, key, "10281")
    if data and data.get("name"):
        return True, T("works: 10281 = {name}", name=data["name"])
    return False, T("no answer or invalid key")
