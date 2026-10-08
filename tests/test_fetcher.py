"""Fetcher against a local server: block cooldown, transports."""
import asyncio
import time

import pytest

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])
from aiohttp import web
from custom_components.lego_tracker.client import Fetcher

PAGE_OK = '<script type="application/ld+json">{"@type":"Product","name":"x","offers":{"price":"12.34"}}</script>'


@pytest.fixture
async def server(aiohttp_server):
    state = {"status": 200, "body": PAGE_OK, "hits": 0, "ua": None}

    async def handler(request):
        state["hits"] += 1
        state["ua"] = request.headers.get("User-Agent")
        return web.Response(status=state["status"], text=state["body"], content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    srv = await aiohttp_server(app)
    state["url"] = str(srv.make_url("/p"))
    return state


@pytest.mark.parametrize("impersonate", [True, False])
async def test_fetch_and_cooldown(hass, server, impersonate):
    f = Fetcher(hass, impersonate)
    f.min_delay = 0
    f.domain_gap = f.search_gap = 0
    await f.async_setup()
    assert f.transport.startswith("curl_cffi") == impersonate
    parsed, err = await f.fetch_offer("bol", server["url"])
    assert err is None and parsed.price == 12.34

    server["status"] = 403
    hits = server["hits"]
    parsed, err = await f.fetch_offer("bol", server["url"])
    assert parsed is None and "blocked (HTTP 403)" in err
    hits_after_block = server["hits"]
    parsed, err = await f.fetch_offer("bol", server["url"])      # cooling down: no request sent
    assert "paused" in err and server["hits"] == hits_after_block
    assert f.cooldown_left("bol") > 3000
    f.blocked_until["bol"] = time.time() - 1                     # cooldown over
    server["status"] = 200
    parsed, err = await f.fetch_offer("bol", server["url"])
    assert parsed.price == 12.34 and f.blocks["bol"] == 0
    await f.async_close()


async def test_captcha_page_counts_as_block(hass, server):
    f = Fetcher(hass, False)
    f.min_delay = 0
    f.domain_gap = f.search_gap = 0
    server["body"] = "<html>Type the characters you see in this image</html>"
    parsed, err = await f.fetch_offer("amazon_nl", server["url"])
    assert "captcha" in err and f.cooldown_left("amazon_nl") > 0
    await f.async_close()


@pytest.mark.parametrize("impersonate", [True, False])
async def test_redirects_stay_on_the_same_site(hass, aiohttp_server, impersonate):
    async def handler(request):
        if request.path == "/same":
            raise web.HTTPFound("/p")
        if request.path == "/away":
            raise web.HTTPFound(f"http://localhost:{request.url.port}/p")    # another host: never followed
        return web.Response(text=PAGE_OK, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    srv = await aiohttp_server(app)
    f = Fetcher(hass, impersonate)
    f.min_delay = 0
    f.domain_gap = f.search_gap = 0
    await f.async_setup()
    parsed, err = await f.fetch_offer("bol", str(srv.make_url("/same")))
    assert err is None and parsed.price == 12.34 and f.final_url["bol"].endswith("/p")
    parsed, err = await f.fetch_offer("bol", str(srv.make_url("/away")))
    assert parsed is None and "another site blocked" in err
    await f.async_close()


async def test_lego_offer_without_set_number_is_refused(hass):
    f = Fetcher(hass, False)
    parsed, err = await f.fetch_offer("lego_com", "https://www.lego.com/nl-be/product/flower-bouquet")
    assert parsed is None and "set number" in err


@pytest.mark.parametrize("impersonate", [True, False])
async def test_oversized_pages_are_refused(hass, aiohttp_server, impersonate, monkeypatch):
    from custom_components.lego_tracker import client

    monkeypatch.setattr(client, "MAX_BODY", 5000)

    async def handler(request):
        if request.path in ("/big", "/endless"):        # no Content-Length: only the streamed count can stop it
            resp = web.StreamResponse()
            await resp.prepare(request)
            for _ in range(20 if request.path == "/big" else 400):
                await resp.write(b"x" * 1000)
                await asyncio.sleep(0.01)                   # in portions: read(n) may return before the end
            await resp.write_eof()
            return resp
        return web.Response(text=PAGE_OK, content_type="text/html", charset="utf-8")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    srv = await aiohttp_server(app)
    f = Fetcher(hass, impersonate)
    f.min_delay = 0
    f.domain_gap = f.search_gap = 0
    await f.async_setup()
    parsed, err = await f.fetch_offer("bol", str(srv.make_url("/big")))
    assert parsed is None and "over" in err
    t0 = time.time()                                       # the transfer itself stops: no waiting for 4 s of data
    parsed, err = await f.fetch_offer("bol", str(srv.make_url("/endless")))
    assert parsed is None and "over" in err and time.time() - t0 < 3
    parsed, err = await f.fetch_offer("bol", str(srv.make_url("/p")))
    assert err is None and parsed.price == 12.34
    await f.async_close()


def test_redirects_are_anchored_to_the_shops_own_domain():
    """A shop's pages may only redirect within the shop's domain, not within a shared suffix like ne.jp."""
    from custom_components.lego_tracker import client, shops
    shops.apply_shop_options({"custom_shops": [{"name": "Foo JP", "domain": "foo.ne.jp", "search": ""}]})
    try:
        rid = shops.shop_id("Foo JP")
        home = client.redirect_home(rid, "https://www.foo.ne.jp/p/1")
        assert home == "foo.ne.jp"
        client.check_url("https://shop.foo.ne.jp/x", home)
        with pytest.raises(ValueError):
            client.check_url("https://bar.ne.jp/x", home)
        assert client.redirect_home("bol", "https://www.bol.com/nl/nl/p/1") == "bol.com"
        assert client.redirect_home("kieskeurig", "https://www.kieskeurig.nl/x") == "kieskeurig.nl"   # no shop domain
    finally:
        shops.apply_shop_options({})


async def test_bot_wall_on_the_lego_product_page_pauses_the_shop(hass):
    """When the LEGO.com search finds nothing and the direct product page shows a bot-protection wall, that is a
    block (the shop is paused), not 'no matching product found'."""
    from unittest.mock import AsyncMock

    wall = "<html><head><title>Pardon Our Interruption</title><script>window.isImpervaSpaSupport</script></head></html>"
    f = Fetcher(hass, False)
    f.min_delay = 0
    f.domain_gap = f.search_gap = 0
    f._get = AsyncMock(side_effect=[(200, "<html><body>no results</body></html>"), (200, wall)])
    assert await f.discover("lego_com", "10311", force=True) is None
    assert "bot protection" in f.discover_error["lego_com"] and f.cooldown_left("lego_com") > 0
    await f.async_close()
