import time

import pytest
from lego_pkg import csv_import, models, parsers

DAY = 86400


@pytest.mark.parametrize("raw,expected", [
    ("39,99", 39.99), ("€ 1.234,56", 1234.56), ("1,234.56", 1234.56), ("39.99", 39.99),
    ("1.299", 1299.0), ("EUR 249", 249.0), ("", None), (None, None), (0, None), (12.5, 12.5),
])
def test_parse_price(raw, expected):
    assert models.parse_price(raw) == expected


def test_normalize_set_number():
    assert models.normalize_set_number("10281-1") == "10281"
    assert models.normalize_set_number(" 75192 ") == "75192"


def _offer(prices, start=None, now=None):
    now = now or time.time()
    start = start or now - (len(prices) - 1) * 5 * DAY
    o = {}
    for i, p in enumerate(prices):
        models.record_price(o, p, now=start + i * 5 * DAY)
    return o


def test_history_dedupes_and_overwrites_same_day():
    o = {}
    t = 1_700_000_000
    assert models.record_price(o, 50, now=t)
    assert not models.record_price(o, 50, now=t + DAY)          # unchanged
    assert models.record_price(o, 45, now=t + DAY + 60)         # new day, changed
    assert models.record_price(o, 44, now=t + DAY + 3600)       # same day -> overwrite
    assert [p for _, p in o["history"]] == [50, 44]
    models.record_price(o, None, now=t + 3 * DAY, error="blocked")
    assert o["available"] is False and o["error"] == "blocked"


def test_all_time_low_and_discount():
    now = time.time()
    offers = {"bol": _offer([100, 90, 80, 70], now=now)}
    st = models.compute_set_status({"rrp": 100}, offers, threshold=25, min_history_days=3, now=now)
    assert st["best_price"] == 70 and st["is_all_time_low"] and st["high_discount"]
    assert st["discount_rrp"] == 30.0


def test_no_record_low_without_history():
    now = time.time()
    st = models.compute_set_status({"rrp": 100}, {"bol": _offer([70], now=now)}, threshold=25, min_history_days=3, now=now)
    assert not st["is_all_time_low"] and st["high_discount"]


def test_not_low_when_price_rose_again():
    now = time.time()
    st = models.compute_set_status({"rrp": 100}, {"bol": _offer([60, 90], now=now)}, threshold=25, min_history_days=3, now=now)
    assert not st["is_all_time_low"] and st["all_time_low"] == 60 and not st["high_discount"]


def test_best_offer_picks_cheapest_available():
    now = time.time()
    a, b = _offer([50, 60], now=now), _offer([70, 40], now=now)
    models.record_price(b, None, now=now + 1, error="x")  # b currently unavailable
    st = models.compute_set_status({}, {"amazon_nl": a, "bol": b}, threshold=25, min_history_days=3, now=now)
    assert st["best_retailer"] == "amazon_nl" and st["best_price"] == 60


def test_collection_series_and_summary():
    now = time.time()
    store = models.new_store()
    store["sets"]["1"] = {"set_number": "1", "theme": "Icons", "rrp": 100, "pieces": 500}
    store["offers"]["1"] = {"bol": _offer([100, 90, 120], now=now)}
    store["collection"]["1"] = {"qty": 2, "paid": 80}
    series = models.collection_series(store, now=now)
    assert series[0]["cost"] == 160 and series[-1]["value"] == 240 and series[0]["value"] == 200
    statuses = {"1": models.compute_set_status(store["sets"]["1"], store["offers"]["1"], threshold=25, min_history_days=3, now=now)}
    summ = models.collection_summary(store, statuses)
    assert summ["value"] == 240 and summ["cost"] == 160 and summ["growth_pct"] == 50.0 and summ["pieces"] == 1000


BRICKECONOMY_LIKE = """﻿Number;Name;Theme;Subtheme;Year;Pieces;Condition;Paid;Value;Purchase Date
10281-1;Bonsai Tree;Botanicals;;2021;878;New;"39,99";"49,50";24/12/2021
10281-1;Bonsai Tree;Botanicals;;2021;878;New;"49,99";"49,50";2022-02-01
42143;Ferrari Daytona SP3;Technic;;2022;3778;New;349.99;399;
abc;Junk;;;;;;;;
"""


def test_csv_import_european_and_duplicates():
    rows, warnings = csv_import.parse_collection_csv(BRICKECONOMY_LIKE)
    assert len(rows) == 3 and len(warnings) == 1
    assert rows[0]["paid"] == 39.99 and rows[0]["added"] == "2021-12-24" and rows[0]["current_value"] == 49.5
    store = models.new_store()
    res = csv_import.apply_import(store, rows)
    assert res == {"added": 2, "updated": 0}
    bonsai = store["collection"]["10281"]
    assert bonsai["qty"] == 2 and bonsai["paid"] == 44.99 and bonsai["added"] == "2021-12-24"
    assert store["sets"]["10281"]["theme"] == "Botanicals"
    res = csv_import.apply_import(store, rows)          # re-import updates, does not double
    assert res == {"added": 0, "updated": 2} and store["collection"]["10281"]["qty"] == 2


def test_csv_without_number_column():
    rows, warnings = csv_import.parse_collection_csv("a,b\n1,2\n")
    assert rows == [] and "set-number" in warnings[0]


def test_url_normalization():
    assert parsers.normalize_url("amazon_nl", "B08XYZ1234") == "https://www.amazon.nl/dp/B08XYZ1234"
    assert parsers.normalize_url("amazon_de", "https://www.amazon.de/LEGO-Bonsai/dp/B08XYZ1234/ref=sr_1_1?x=1") == "https://www.amazon.de/dp/B08XYZ1234"
    with pytest.raises(ValueError):
        parsers.normalize_url("bol", "https://www.amazon.nl/dp/B08XYZ1234")
    with pytest.raises(ValueError):
        parsers.normalize_url("bol", "bonsai")


JSONLD = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"LEGO Bonsai","image":["https://x/i.jpg"],
"offers":{"@type":"Offer","price":"39.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}</script></head></html>"""


def test_jsonld_parse_for_bol_and_kruidvat():
    for r in ("bol", "kruidvat_be"):
        p = parsers.parse_page(r, JSONLD)
        assert p.price == 39.99 and p.title == "LEGO Bonsai" and p.image == "https://x/i.jpg"


def test_bol_promo_price_markup():
    page = '<span class="promo-price" data-test="price">39<sup class="promo-price__fraction" data-test="price-fraction">99</sup></span>'
    assert parsers.parse_page("bol", page).price == 39.99


def test_amazon_offscreen_and_block():
    page = '<span id="productTitle"> LEGO Bonsai </span><div id="corePrice_feature_div"><div><div><span class="a-price"><span class="a-offscreen">€39,99</span></span></div></div></div>'
    p = parsers.parse_page("amazon_nl", page)
    assert p.price == 39.99 and p.title == "LEGO Bonsai"
    assert parsers.parse_page("amazon_nl", "<html>Type the characters you see in this image</html>").blocked


def test_search_result_finders():
    az = '<div data-asin="B0ABCDEFGH"><span>LEGO Icons Bonsai Boom 10281</span></div><div data-asin="B0ZZZZZZZZ"><span>Other</span></div>'
    assert parsers.find_search_result("amazon_be", az, "10281") == "https://www.amazon.com.be/dp/B0ABCDEFGH"
    assert parsers.find_search_result("bol", '<a href="/nl/nl/p/lego-icons-bonsai-10281/9300000?x=1">', "10281") == "https://www.bol.com/nl/nl/p/lego-icons-bonsai-10281/9300000"


def test_url_key_and_retailer_detection():
    assert parsers.retailer_from_url("https://www.amazon.com.be/dp/B08XYZ1234?th=1") == "amazon_be"
    assert parsers.retailer_from_url("https://www.bol.com/nl/nl/p/x/1/") == "bol"
    assert parsers.retailer_from_url("https://example.com/") is None
    a = parsers.url_key("amazon_nl", "https://www.amazon.nl/LEGO-Bonsai/dp/B08XYZ1234/ref=x?y=1")
    assert a == parsers.url_key("amazon_nl", "https://www.amazon.nl/dp/B08XYZ1234")
    assert parsers.url_key("bol", "https://www.bol.com/nl/nl/p/x/1/?bltgh=q") == parsers.url_key("bol", "https://www.bol.com/nl/nl/p/x/1")
