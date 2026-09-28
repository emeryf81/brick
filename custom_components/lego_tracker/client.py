"""Network access: retailer pages and optional Brickset metadata."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import aiohttp

from .models import normalize_set_number
from .parsers import Parsed, find_search_result, parse_page, search_url

_LOGGER = logging.getLogger(__name__)

HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    },
]


class Fetcher:
    """Polite fetcher: one request at a time per retailer, jittered delay."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._locks: dict[str, asyncio.Lock] = {}
        self.min_delay = 4.0

    async def _get(self, retailer: str, url: str) -> tuple[int, str]:
        lock = self._locks.setdefault(retailer, asyncio.Lock())
        async with lock:
            await asyncio.sleep(self.min_delay + random.random() * 3)
            async with self._session.get(
                url, headers=random.choice(HEADERS_POOL), timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True
            ) as resp:
                return resp.status, await resp.text(errors="replace")

    async def fetch_offer(self, retailer: str, url: str) -> tuple[Parsed | None, str | None]:
        """Returns (parsed, error)."""
        try:
            status, page = await self._get(retailer, url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            return None, f"network: {err}"
        if status in (403, 429, 503):
            return None, f"blocked (HTTP {status})"
        if status == 404:
            return None, "not found (HTTP 404)"
        if status >= 400:
            return None, f"HTTP {status}"
        parsed = parse_page(retailer, page)
        if parsed.blocked:
            return None, "blocked (captcha / bot protection)"
        if parsed.price is None and not parsed.unavailable:
            return None, "price not found on page (markup changed?)"
        return parsed, None

    async def discover(self, retailer: str, set_number: str) -> str | None:
        url = search_url(retailer, set_number)
        if not url:
            return None
        try:
            status, page = await self._get(retailer, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
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
    }
