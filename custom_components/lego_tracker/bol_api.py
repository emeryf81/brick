"""bol.com Marketing Catalog API (free with a bol.com affiliate / partner account).

Official route to bol.com prices: no bot protection, no scraping. Needs a client id + secret from
the bol.com affiliate program (Partnerplatform → API credentials).

- token:  POST https://login.bol.com/token?grant_type=client_credentials (HTTP basic auth)
- search: GET  https://api.bol.com/marketing/catalog/v1/products/search?search-term=…&country-code=NL|BE
- offer:  GET  https://api.bol.com/marketing/catalog/v1/products/{ean}/offers/best?country-code=NL|BE

The response parsing is deliberately tolerant (walks the JSON for product-like and price-like
objects) so small schema differences don't break it.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp

from .i18n import T

TOKEN_URL = "https://login.bol.com/token"
API = "https://api.bol.com/marketing/catalog/v1"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class BolApiError(Exception):
    """Readable (English, translatable) error from the bol.com API."""


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        try:
            f = float(v.replace(",", "."))
            return f if f > 0 else None
        except ValueError:
            return None
    if isinstance(v, dict):                       # {"amount": 39.99} / {"value": 39.99}
        for k in ("amount", "value", "price"):
            if k in v:
                return _num(v[k])
    return None


def offer_price(obj: Any) -> float | None:
    """Price from an offer-like object: {"price": 39.99, "strikethroughPrice": …}."""
    if not isinstance(obj, dict):
        return None
    for key in ("price", "sellingPrice", "offerPrice", "amount"):
        if key in obj and (p := _num(obj[key])):
            return p
    for key in ("offer", "bestOffer", "offers"):
        sub = obj.get(key)
        if isinstance(sub, list):
            sub = sub[0] if sub else None
        if (p := offer_price(sub)) is not None:
            return p
    return None


def _image(obj: dict[str, Any]) -> str | None:
    img = obj.get("image") or obj.get("images") or obj.get("mainImage")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("href")
    return img if isinstance(img, str) and img.startswith("https://") else None


def _url(obj: dict[str, Any]) -> str | None:
    url = obj.get("url") or obj.get("productUrl") or obj.get("link")
    if isinstance(url, list):
        url = url[0] if url else None
    if isinstance(url, dict):
        url = url.get("href") or url.get("url")
    return url if isinstance(url, str) and url.startswith("https://") else None


def products(data: Any) -> list[dict[str, Any]]:
    """Every product-like object (has an EAN and a title) with its price, url and image."""
    out: list[dict[str, Any]] = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        ean = node.get("ean") or node.get("EAN")
        title = node.get("title") or node.get("name")
        if ean and isinstance(title, str):
            out.append({"ean": str(ean), "title": title, "price": offer_price(node), "url": _url(node),
                        "image": _image(node)})
            continue
        # a search result can be {"product": {...}, "offer": {...}}
        if isinstance(node.get("product"), dict) and ("offer" in node or "bestOffer" in node):
            prod = dict(node["product"])
            prod.setdefault("offer", node.get("offer") or node.get("bestOffer"))
            stack.append(prod)
            continue
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


class BolApi:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str, country: str = "NL") -> None:
        self._session, self._id, self._secret = session, client_id, client_secret
        self.country = "BE" if country.upper() == "BE" else "NL"
        self._token: str | None = None
        self._expires = 0.0

    async def _auth(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        try:
            async with self._session.post(TOKEN_URL, params={"grant_type": "client_credentials"}, timeout=TIMEOUT,
                                          headers={"Authorization": f"Basic {basic}", "Accept": "application/json"}) as r:
                if r.status in (400, 401, 403):
                    raise BolApiError(T("bol.com API: client id or secret not accepted"))
                if r.status >= 400:
                    raise BolApiError(T("bol.com API: login failed (HTTP {status})", status=r.status))
                data = await r.json(content_type=None)
        except aiohttp.ClientError as err:
            raise BolApiError(T("network error: {error}", error=str(err)[:120])) from err
        self._token = data.get("access_token")
        if not self._token:
            raise BolApiError(T("bol.com API: login failed (HTTP {status})", status=200))
        self._expires = time.time() + float(data.get("expires_in") or 300)
        return self._token

    async def _get(self, path: str, **params: Any) -> Any:
        for attempt in (1, 2):
            token = await self._auth()
            try:
                async with self._session.get(f"{API}{path}", params={"country-code": self.country, **params}, timeout=TIMEOUT,
                                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                      "Accept-Language": "nl"}) as r:
                    if r.status == 401 and attempt == 1:   # expired token: log in again once
                        self._token = None
                        continue
                    if r.status == 404:
                        return None
                    if r.status == 429:
                        raise BolApiError(T("bol.com API: too many requests, try again later"))
                    if r.status >= 400:
                        raise BolApiError(T("bol.com API: error (HTTP {status})", status=r.status))
                    return await r.json(content_type=None)
            except aiohttp.ClientError as err:
                raise BolApiError(T("network error: {error}", error=str(err)[:120])) from err
        return None

    async def search(self, term: str) -> list[dict[str, Any]]:
        data = await self._get("/products/search", **{"search-term": term, "include-offer": "true", "include-image": "true"})
        return products(data) if data else []

    async def best_price(self, ean: str) -> float | None:
        """Price of the best offer, or None when bol.com has no offer (not available)."""
        data = await self._get(f"/products/{ean}/offers/best")
        return offer_price(data) if data else None

    async def product(self, ean: str) -> dict[str, Any] | None:
        data = await self._get(f"/products/{ean}", **{"include-offer": "true", "include-image": "true"})
        found = products(data) if data else []
        return found[0] if found else None
