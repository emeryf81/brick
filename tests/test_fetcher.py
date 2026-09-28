"""Fetcher against a local server: block cooldown, transports."""
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
    server["body"] = "<html>Type the characters you see in this image</html>"
    parsed, err = await f.fetch_offer("amazon_nl", server["url"])
    assert "captcha" in err and f.cooldown_left("amazon_nl") > 0
    await f.async_close()
