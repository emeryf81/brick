import time
import re

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


def test_combined_history_forward_fill_and_duplicate_timestamps():
    offers = {
        "a": {"history": [[1, 50], [3, 20], [3, 60], [5, 10], [6, 50]]},
        "b": {"history": [[2, 40], [3, 30], [4, 70], [6, 80]], "available": False},
        "empty": {},
        "suspect": {"history": [[0, 1]], "link_status": "suspect"},
        "manual": {"history": [[7, 45]], "link_status": "suspect", "manual_price": {"price": 99}},
    }
    assert models.combined_history(offers) == [
        [1, 50], [2, 40], [3, 30], [4, 60], [5, 10], [6, 50], [7, 45],
    ]
    assert models.combined_history({}) == []
    assert models.combined_history({"empty": {}, "suspect": offers["suspect"]}) == []


@pytest.mark.parametrize("shops,points", [(1, 8000), (64, 64)])
def test_combined_history_does_not_rescan_observations(shops, points):
    class CountingHistory(list):
        visits = 0

        def __iter__(self):
            for observation in super().__iter__():
                type(self).visits += 1
                yield observation

    offers = {str(i): {"history": CountingHistory([[j * shops + i, 50] for j in range(points)])}
              for i in range(shops)}
    assert models.combined_history(offers) == [[t, 50] for t in range(shops * points)]
    assert CountingHistory.visits <= 2 * shops * points


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
    assert rows == [] and "set numbers" in warnings[0]


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
    az = ('<div data-asin="B0LEDLEDLE"><h2><span>Led-verlichting voor LEGO 10281 Bonsai Boom (geen LEGO)</span></h2></div>'
          '<div data-asin="B0KEEPPLEY"><h2><span>Keeppley 10281 bonsai compatibel</span></h2></div>'
          '<div data-asin="B0ABCDEFGH"><h2 class="a-size-base"><span>LEGO Icons Bonsai Boom 10281</span></h2><span>€ 39,99</span></div>'
          '<div data-asin="B0ZZZZZZZZ"><h2><span>Other</span></h2></div>')
    assert parsers.find_search_result("amazon_be", az, "10281") == "https://www.amazon.com.be/dp/B0ABCDEFGH"
    assert parsers.find_search_result("bol", '<a href="/nl/nl/p/lego-icons-bonsai-10281/9300000?x=1">', "10281") == "https://www.bol.com/nl/nl/p/lego-icons-bonsai-10281/9300000"
    assert parsers.find_search_result("bol", '<a href="/nl/nl/p/led-verlichting-voor-lego-10281/93/">', "10281") is None
    # old bug: kruidvat accepted any LEGO product
    assert parsers.find_search_result("kruidvat_be", '<a href="/nl/lego-city-politiebureau-60316/p/123">', "10281") is None
    assert parsers.find_search_result("kruidvat_be", '<a href="/nl/lego-icons-bonsai-10281/p/456">', "10281") == "https://www.kruidvat.be/nl/lego-icons-bonsai-10281/p/456"


def test_title_check_and_clean_title():
    tc = parsers.title_check
    assert tc("Led-verlichting voor Lego 21028 Architecture New York", "21028")[0] == "suspect"
    assert tc("Acryl transparante vitrine voor LEGO 42043", "42043")[0] == "suspect"
    assert "other brand" in tc("Keeppley City Corner compatibel met LEGO 356", "356")[1]
    assert "is not in the title" in tc("LEGO Speed Champions BMW M3 (E30) 77263", "358")[1]
    assert tc("LEGO 102810 something", "10281")[0] == "suspect"            # no partial number match
    assert tc("LEGO Icons 10311 Orchidee", "10311") == ("ok", "title contains the set number")
    assert tc(None, "1")[0] is None
    assert parsers.clean_title("LEGO Icons 10311 Orchidee, kunstplanten | bol.com", "10311") == "Icons Orchidee"


def test_link_check():
    lc = models.link_check
    assert lc({"title": "LEGO 10311 Orchidee"}, {}, "10311")[0] == "ok"
    assert lc({"title": "LED kit for LEGO 10311"}, {}, "10311")[0] == "suspect"
    assert lc({"url": "https://www.bol.com/nl/nl/p/vitrine-voor-lego-10311/93/"}, {}, "10311")[0] == "suspect"
    assert lc({"url": "https://www.amazon.nl/dp/B0ABCDEFGH"}, {}, "10311")[0] is None
    assert "far too low" in lc({"title": "LEGO 10311", "history": [[1, 9.99]]}, {"rrp": 49.99}, "10311")[1]
    assert lc({"title": "LED kit", "link_status": "confirmed"}, {}, "10311")[0] == "confirmed"


def test_suspect_offers_do_not_count():
    now = time.time()
    good, bad = {}, {"link_status": "suspect"}
    models.record_price(good, 45.0, now=now)
    models.record_price(bad, 12.0, now=now)
    st = models.compute_set_status({"rrp": 50}, {"bol": good, "amazon_nl": bad}, threshold=25, min_history_days=0, now=now)
    assert st["best_price"] == 45.0 and st["all_time_low"] == 45.0 and st["offers_suspect"] == 1


def test_url_key_and_retailer_detection():
    assert parsers.retailer_from_url("https://www.amazon.com.be/dp/B08XYZ1234?th=1") == "amazon_be"
    assert parsers.retailer_from_url("https://www.bol.com/nl/nl/p/x/1/") == "bol"
    assert parsers.retailer_from_url("https://example.com/") is None
    a = parsers.url_key("amazon_nl", "https://www.amazon.nl/LEGO-Bonsai/dp/B08XYZ1234/ref=x?y=1")
    assert a == parsers.url_key("amazon_nl", "https://www.amazon.nl/dp/B08XYZ1234")
    assert parsers.url_key("bol", "https://www.bol.com/nl/nl/p/x/1/?bltgh=q") == parsers.url_key("bol", "https://www.bol.com/nl/nl/p/x/1")


def test_trend_price_per_piece_and_target():
    now = time.time()
    offer = {}
    for days_ago, price in ((40, 100.0), (20, 90.0), (6, 80.0), (0, 60.0)):
        models.record_price(offer, price, now=now - days_ago * DAY)
    st = models.compute_set_status({"rrp": 100, "pieces": 600, "target_price": 65}, {"bol": offer},
                                   threshold=25, min_history_days=3, now=now)
    assert st["price_per_piece"] == 0.1
    assert st["change_7d"] == -33.3 and st["change_30d"] == -40.0
    assert st["target_hit"] is True
    st = models.compute_set_status({"target_price": 50}, {"bol": offer}, threshold=25, min_history_days=3, now=now)
    assert st["target_hit"] is False and st["price_per_piece"] is None


def test_wishlist_summary_excludes_owned():
    store = models.new_store()
    store["sets"] = {"1": {"rrp": 100}, "2": {"rrp": 50}, "3": {"rrp": 20}}
    store["collection"]["1"] = {"qty": 1}
    statuses = {"1": {"best_price": 10}, "2": {"best_price": 40}, "3": {}}
    w = models.wishlist_summary(store, statuses)
    assert w == {"sets": 2, "priced": 1, "cost": 40.0, "rrp": 50.0, "saving": 10.0}


def test_export_import_roundtrip():
    store = models.new_store()
    store["sets"]["10281"] = {"set_number": "10281", "name": "Bonsai; Tree", "theme": "Botanicals", "pieces": 878, "rrp": 49.99}
    store["collection"]["10281"] = {"qty": 2, "paid": 39.99, "added": "2024-05-01"}
    text = models.rows_to_csv(models.collection_rows(store, {}), models.COLLECTION_COLUMNS)
    rows, warnings = csv_import.parse_collection_csv(text)
    assert not warnings and rows[0]["set_number"] == "10281" and rows[0]["qty"] == 2
    assert rows[0]["paid"] == 39.99 and rows[0]["name"] == "Bonsai; Tree" and rows[0]["added"] == "2024-05-01"


def test_validate_backup():
    good = {"sets": {"10281": {}}, "offers": {}, "collection": {}, "snapshots": []}
    assert models.validate_backup(good)["sets"] == {"10281": {}}
    for bad in ([], {"nope": 1}, {"sets": {"abc": {}}}, {"sets": {}, "offers": []}):
        with pytest.raises(ValueError):
            models.validate_backup(bad)


# ------------------------------------------------------------------ 0.4.0: import checks
from datetime import date as _date  # noqa: E402

MESSY = """Setnummer;Naam;Thema;Aantal;Betaald;Aankoopdatum;Jaar;Staat
10281-1;Bonsai;Botanicals;1;39,99;24/12/2021;2021;NISB
abc;Rommel;;1;;;;
10311;Orchidee;Botanicals;0;45;;2022;
42143;Ferrari;Technic;1;-5;;2022;
10281;Bonsai;Botanicals;1;49,99;31/02/2022;2021;gebouwd
21330;Home Alone;Ideas;1;999;2030-01-01;1800;
75192;;Star Wars;60;;;;
"""


def test_analyze_flags_every_problem():
    store = models.new_store()
    store["sets"]["21330"] = {"rrp": 299.99}
    store["collection"]["75192"] = {"qty": 1}
    a = csv_import.analyze_csv(MESSY, store, today=_date(2026, 9, 29))
    by = {r["line"]: r for r in a["rows"]}
    txt = lambda n: " | ".join(i["text"] for i in by[n]["issues"])  # noqa: E731
    assert by[2]["status"] == "ok" and by[2]["condition"] == "Sealed" and by[2]["added"] == "2021-12-24"
    assert by[3]["status"] == "error" and "invalid set number" in txt(3)
    assert by[4]["status"] == "error" and "quantity is 0" in txt(4)
    assert by[5]["status"] == "error" and "negative" in txt(5)
    assert by[6]["status"] == "warning" and "31/02/2022" in txt(6) and "extra copy" in txt(6)
    assert by[6]["condition"] == "Built" and "added" not in by[6]
    assert by[7]["status"] == "warning" and "future" in txt(7) and "1800" in txt(7) and "3× the RRP" in txt(7)
    assert by[8]["status"] == "warning" and "very high" in txt(8) and "updated" in txt(8)
    assert a["summary"]["error"] == 3 and a["summary"]["merged"] == 1 and a["summary"]["update"] == 1
    assert a["columns"]["Setnummer"] == "Set number" and a["ignored_columns"] == []
    rows = csv_import.importable_rows(a)
    res = csv_import.apply_import(store, rows)
    assert store["collection"]["10281"]["qty"] == 2 and store["collection"]["10281"]["paid"] == 44.99
    assert "10311" not in store["collection"] and "42143" not in store["collection"]
    assert res["added"] == 2 and res["updated"] == 1


def test_analyze_fatal_cases():
    assert "set numbers" in csv_import.analyze_csv("foo;bar\n1;2\n")["fatal"]
    assert csv_import.analyze_csv("   ")["fatal"] == "Empty file."
    assert "too large" in csv_import.analyze_csv("Number\n" + "1" * 2_100_000)["fatal"]
    a = csv_import.analyze_csv("Number,Foo,Name\n10281,x,Bonsai\n")
    assert a["ignored_columns"] == ["Foo"]


def test_paid_average_ignores_missing_prices():
    store = models.new_store()
    csv_import.apply_import(store, [{"set_number": "1", "qty": 1, "paid": 40.0}, {"set_number": "1", "qty": 1}])
    # two lines = two copies, each its own price; the set-level average only counts the known price
    assert store["collection"]["1"] == {"qty": 2, "paid": 40.0, "items": [{"paid": 40.0}, {}]}


def test_csv_export_neutralises_formulas():
    out = models.rows_to_csv([{"Name": "=HYPERLINK(\"x\")", "Qty": 1}], ["Name", "Qty"])
    assert "'=HYPERLINK" in out


# ------------------------------------------------------------------ 0.4.0: deals & sanity
def test_deal_score_ranges():
    assert models.deal_score({"best_price": None}) == 0
    top = models.deal_score({"best_price": 50, "discount_rrp": 50, "all_time_low": 50, "history_days": 30,
                             "discount_avg": 25, "target_hit": True})
    meh = models.deal_score({"best_price": 95, "discount_rrp": 5, "all_time_low": 60, "history_days": 30})
    assert top == 100 and meh < 20


def test_retirement_status():
    now = time.mktime((2026, 9, 29, 12, 0, 0, 0, 0, -1))
    assert models.retirement_status({"exit_date": "2026-12-31"}, now)["retiring_soon"]
    assert models.retirement_status({"exit_date": "2025-12-31"}, now)["retired"]
    assert not models.retirement_status({"exit_date": "2028-01-01"}, now)["retiring_soon"]
    assert models.retirement_status({"retiring": True}, now)["retiring_soon"]


def test_suspicious_price_guard():
    assert "RRP" in models.is_suspicious_price(4.99, {"rrp": 49.99}, {})
    assert models.is_suspicious_price(39.99, {"rrp": 49.99}, {}) is None
    offer = {"history": [[1, 100.0], [2, 98.0], [3, 101.0]]}
    assert models.is_suspicious_price(9.0, {}, offer) and models.is_suspicious_price(95.0, {}, offer) is None


def test_collection_analytics():
    store = models.new_store()
    store["sets"] = {"1": {"theme": "Icons", "year": 2021, "pieces": 1000}, "2": {"theme": "City", "year": 2022}}
    store["collection"] = {"1": {"qty": 1, "paid": 100, "condition": "Sealed"}, "2": {"qty": 2, "paid": 50}}
    st = {"1": {"best_price": 150}, "2": {"best_price": 40}}
    a = models.collection_analytics(store, st)
    assert list(a["by_theme"]) == ["Icons", "City"] and a["by_theme"]["City"]["count"] == 2
    assert a["by_year"]["2022"]["value"] == 80 and a["by_condition"] == {"Sealed": 1, "Unknown": 2}
    assert a["top_gainers"][0]["pct"] == 50.0 and a["top_losers"][0]["set_number"] == "2"
    assert all(m["pct"] >= 0 for m in a["top_gainers"])
    assert a["avg_paid_per_piece"] == 0.1


def test_backup_validation_rejects_bad_offers():
    base = {"sets": {"10281": {}}, "offers": {"10281": {"bol": {"url": "javascript:alert(1)", "history": []}}}}
    with pytest.raises(ValueError, match="URL"):
        models.validate_backup(base)
    base["offers"]["10281"]["bol"] = {"url": "https://x", "history": [["a", 1]]}
    with pytest.raises(ValueError, match="price history"):
        models.validate_backup(base)
    with pytest.raises(ValueError, match="unknown set"):
        models.validate_backup({"sets": {}, "offers": {"1": {}}})


@pytest.mark.parametrize("history", [
    [[i, 50] for i in range(models.MAX_HISTORY + 1)],
    [[2, 50], [1, 40]], [[float("nan"), 50]], [[1, float("inf")]],
    [[float("-inf"), 50]], [[True, 50]], [[1, False]],
])
def test_backup_validation_rejects_unsafe_histories(history):
    with pytest.raises(ValueError, match="invalid price history"):
        models.validate_backup({"sets": {"10281": {}}, "offers": {"10281": {"bol": {"history": history}}}})


def test_backup_validation_accepts_history_limit_and_duplicate_timestamps():
    history = [[i // 2, 50] for i in range(models.MAX_HISTORY)]
    data = {"sets": {"10281": {}}, "offers": {"10281": {"bol": {"history": history}}}}
    assert models.validate_backup(data)["offers"]["10281"]["bol"]["history"] == history


# ---------------------------------------------------------------- 0.6.0
def test_parse_times():
    assert models.parse_times("19.30, 7:05 en 23u00, 25:00, 7:05") == ["07:05", "19:30", "23:00"]
    assert models.parse_times("") == []


def test_value_source_and_value_history():
    store = models.new_store()
    store["sets"]["1"] = {"rrp": 100}
    rows = [{"set_number": "1", "qty": 1, "paid": 80, "current_value": 150.0}]
    csv_import.apply_import(store, rows, now=1_000_000)
    csv_import.apply_import(store, [dict(rows[0], current_value=150.0)], now=1_100_000)   # unchanged: no new point
    csv_import.apply_import(store, [dict(rows[0], current_value=175.0)], now=1_200_000)
    assert store["collection"]["1"]["value_history"] == [[1_000_000, 150.0], [1_200_000, 175.0]]
    st = {"1": {"best_price": 90.0}}
    assert models.collection_summary(store, st)["value"] == 90.0          # shop first (default)
    store["value_source"] = "import_first"
    assert models.collection_summary(store, st)["value"] == 175.0
    # replace keeps the history of sets that are in the new file, and drops the others
    csv_import.apply_import(store, [dict(rows[0], current_value=180.0), {"set_number": "2", "qty": 1}],
                            replace=True, now=1_300_000)
    assert len(store["collection"]["1"]["value_history"]) == 3
    csv_import.apply_import(store, [{"set_number": "2", "qty": 1}], replace=True)
    assert "1" not in store["collection"]


def test_custom_shops_registry_and_generic_search():
    from lego_pkg import shops
    from lego_pkg.const import RETAILERS, GENERIC_SHOPS
    with pytest.raises(ValueError):
        shops.validate_custom_shop({"name": "x", "domain": "not a domain"})
    with pytest.raises(ValueError):
        shops.validate_custom_shop({"name": "x", "domain": "shop.be", "search": "https://evil.com/?q={query}"})
    shops.apply_shop_options({"custom_shops": [{"name": "Speelgoed Van Dijk", "domain": "https://www.vandijk.be/",
                                                "search": "https://www.vandijk.be/zoek?q={query}"}],
                              "shop_search": {"dreamland_be": "https://www.dreamland.be/zoeken?term={query}"}})
    assert RETAILERS["c_speelgoed_van_dijk"][0] == "Speelgoed Van Dijk"
    assert parsers.search_url("c_speelgoed_van_dijk", "10311") == "https://www.vandijk.be/zoek?q=LEGO+10311"
    assert parsers.search_url("dreamland_be", "10311") == "https://www.dreamland.be/zoeken?term=LEGO+10311"
    assert parsers.retailer_from_url("https://www.vandijk.be/p/lego-10311") == "c_speelgoed_van_dijk"
    assert parsers.normalize_url("c_speelgoed_van_dijk", "https://www.vandijk.be/p/1#x") == "https://www.vandijk.be/p/1"
    page = ('<a href="/zoek?q=lego">zoek</a><a href="/p/led-set-lego-10311">LED verlichting LEGO 10311</a>'
            '<a href="https://www.vandijk.be/p/lego-icons-orchidee-10311-123"><span>LEGO Icons 10311 Orchidee</span></a>')
    assert parsers.find_search_result("c_speelgoed_van_dijk", page, "10311") == "https://www.vandijk.be/p/lego-icons-orchidee-10311-123"
    shops.apply_shop_options({})                      # removing custom shops cleans the registry
    assert "c_speelgoed_van_dijk" not in RETAILERS and "dreamland_be" in RETAILERS
    assert GENERIC_SHOPS["dreamland_be"]["search"].startswith("https://www.dreamland.be/")


# ---------------------------------------------------------------- 0.7.0
AMAZON_B06W2KC5R5_LIKE = """
<div id="sims-carousel"><span class="a-price" data-a-size="l"><span class="a-offscreen">13,69&nbsp;€</span><span aria-hidden="true">13,69 €</span></span></div>
<span id="productTitle"> LEGO 42082 Technic Rough Terrain Crane </span>
<div id="corePriceDisplay_desktop_feature_div"><div class="a-section">
 <span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">119,99&nbsp;€</span></span>
 <span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay"><span class="a-offscreen">99,95&nbsp;€</span><span aria-hidden="true"><span class="a-price-whole">99<span class="a-price-decimal">,</span></span><span class="a-price-fraction">95</span></span></span>
</div></div>
<div id="similar"><span class="a-price"><span class="a-offscreen">5,99 €</span></span></div>
"""


def test_amazon_uses_buybox_not_first_price():
    p = parsers.parse_page("amazon_de", AMAZON_B06W2KC5R5_LIKE)
    assert p.price == 99.95 and "42082" in p.title
    hidden = '<span class="a-price"><span class="a-offscreen">13,69 €</span></span><input type="hidden" name="items[0.base][customerVisiblePrice][amount]" value="99.95">'
    assert parsers.parse_page("amazon_de", hidden).price == 99.95
    # no buy box at all: no price rather than a wrong one
    only_carousel = '<span id="productTitle">LEGO 42082</span><span class="a-price"><span class="a-offscreen">13,69 €</span></span>'
    assert parsers.parse_page("amazon_de", only_carousel).price is None


LEGO_PAGE = """<html><head><meta property="og:image" content="https://www.lego.com/cdn/og.png">
<title>Orchidee 10311 | LEGO® Icons | Officiële LEGO® winkel BE</title>
<script type="application/ld+json">{"@type":"Product","name":"Orchidee","sku":"10311","image":["https://www.lego.com/cdn/cs/set/assets/10311.png"],
"offers":{"@type":"Offer","price":"39.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}</script></head>
<script id="__NEXT_DATA__">{"product":{"price":{"formattedAmount":"€ 39,99","centAmount":3999},"listPrice":{"formattedAmount":"€ 49,99","centAmount":4999},
"availabilityStatus":"E_RETIRING_SOON"}}</script></html>"""


def test_parse_lego_sale_and_retiring():
    p = parsers.parse_page("lego_com", LEGO_PAGE)
    assert p.price == 39.99 and p.list_price == 49.99 and p.retiring and p.image.endswith("10311.png")
    plain = LEGO_PAGE.replace('"listPrice":{"formattedAmount":"€ 49,99","centAmount":4999},', "").replace("E_RETIRING_SOON", "E_AVAILABLE")
    p = parsers.parse_page("lego_com", plain)
    assert p.list_price == 39.99 and not p.retiring
    # the word "retiring" in a translation bundle must not flag every set
    assert not parsers.parse_page("lego_com", plain + '<script>{"i18n":{"retiring":"Retiring soon"}}</script>').retiring
    assert parsers.clean_title(p.title, "10311") == "Orchidee"


def test_search_templates_and_lego_links():
    from lego_pkg import shops
    shops.apply_shop_options({})
    assert parsers.search_url("lego_com", "10311") == "https://www.lego.com/nl-be/search?q=10311"
    assert parsers.search_url("bol", "10311") == "https://www.bol.com/nl/nl/s/?searchtext=LEGO+10311"
    shops.apply_shop_options({"lego_locale": "de-de", "shop_search": {"bol": "https://www.bol.com/be/nl/s/?searchtext={query}",
                                                                      "amazon_de": "javascript:alert(1)"}})
    assert parsers.search_url("lego_com", "10311") == "https://www.lego.com/de-de/search?q=10311"
    assert parsers.search_url("bol", "10311") == "https://www.bol.com/be/nl/s/?searchtext=LEGO+10311"
    assert parsers.search_url("amazon_de", "1").startswith("https://www.amazon.de/s?k=")     # invalid override ignored
    shops.apply_shop_options({})
    page = '<a href="/nl-be/product/orchidee-103110">x</a><a href="/nl-be/product/orchidee-10311">Orchidee</a>'
    assert parsers.find_search_result("lego_com", page, "10311") == "https://www.lego.com/nl-be/product/orchidee-10311"
    assert parsers.retailer_from_url("https://www.lego.com/nl-be/product/orchidee-10311") == "lego_com"
    assert models.link_check({"url": "https://www.lego.com/nl-be/product/orchidee-10311", "title": "Orchidee | LEGO"}, {}, "10311")[0] == "ok"


def test_clean_history_drops_impossible_points():
    offer = {"history": [[1, 229.99], [2, 13.69], [3, 219.0]], "last_price": 219.0, "available": True}
    assert models.clean_history(offer, 229.99) == 1 and [p for _, p in offer["history"]] == [229.99, 219.0]
    offer = {"history": [[1, 229.99], [2, 13.69]], "last_price": 13.69, "available": True}
    models.clean_history(offer, 229.99)
    assert offer["last_price"] == 229.99
    assert models.clean_history({"history": [[1, 5.0]]}, None) == 0


def test_activity_log_collapse_and_query():
    store = models.new_store()
    for i in range(3):   # the same failure three times -> one entry, count 3
        models.add_activity(store, "error", "fetch", "blocked (HTTP 403)", set_number="10311", retailer="bol", now=100 + i)
    models.add_activity(store, "ok", "price", "€49.99 → €39.99", set_number="10311", retailer="amazon_nl", source="server", now=200)
    models.add_activity(store, "ok", "userscript", "prijs €42.00 ontvangen via Tampermonkey", set_number="42143", retailer="amazon_de",
                        url="https://www.amazon.de/dp/B06W2KC5R5", source="userscript", now=300)
    assert len(store["activity"]) == 3 and store["activity"][0]["count"] == 3 and store["activity"][0]["ts"] == 102
    q = models.query_activity
    assert [e["kind"] for e in q(store)["entries"]] == ["userscript", "price", "fetch"]          # newest first
    assert q(store, level="problems")["total"] == 1 and q(store, level="events")["total"] == 2
    assert q(store, retailer="bol")["entries"][0]["message"].startswith("blocked")
    assert q(store, source="userscript")["total"] == 1 and q(store, set_number="10311-1")["total"] == 2
    assert q(store, q="B06W2KC5R5")["total"] == 1 and q(store, before=250)["total"] == 2
    r = q(store, limit=1)
    assert r["more"] and r["facets"]["retailer"] == {"bol": 1, "amazon_nl": 1, "amazon_de": 1}
    for i in range(models.ACTIVITY_MAX + 10):
        models.add_activity(store, "info", "job", f"x{i}")
    assert len(store["activity"]) == models.ACTIVITY_MAX


def test_parse_lego_sold_out_never_takes_a_recommended_products_price():
    sold_out = """<html><title>Boeket bloemen 10280 | LEGO® Icons</title>
<script type="application/ld+json">{"@type":"Product","name":"Boeket bloemen","sku":"10280","image":"https://www.lego.com/cdn/10280.png",
"offers":{"@type":"Offer","price":"59.99","priceCurrency":"EUR","availability":"https://schema.org/OutOfStock"}}</script>
<script type="application/ld+json">{"@type":"Product","name":"Mini bloemen","sku":"40646","offers":{"@type":"Offer","price":"14.99","availability":"https://schema.org/InStock"}}</script>
<script id="__NEXT_DATA__">{"recommendations":[{"productCode":"40646","price":{"formattedAmount":"€ 14,99","centAmount":1499},"availabilityStatus":"E_AVAILABLE"}],
"product":{"productCode":"10280","price":{"formattedAmount":"€ 59,99","centAmount":5999},"availabilityStatus":"H_OUT_OF_STOCK"}}</script></html>"""
    p = parsers.parse_page("lego_com", sold_out, "10280")
    assert p.price is None and p.unavailable and p.list_price == 59.99 and p.title == "Boeket bloemen"
    # without JSON-LD: only the state around this product's code counts
    no_ld = re.sub(r'<script type="application/ld\+json">.*?</script>', "", sold_out, flags=re.S)
    p = parsers.parse_page("lego_com", no_ld, "10280")
    assert p.price is None and p.unavailable and p.list_price == 59.99
    in_stock = sold_out.replace("OutOfStock", "InStock").replace("H_OUT_OF_STOCK", "E_AVAILABLE")
    p = parsers.parse_page("lego_com", in_stock, "10280")
    assert p.price == 59.99 and not p.unavailable and p.list_price == 59.99
    # a page with only other products' codes gives no price at all
    other = '<script>{"productCode":"40646","price":{"centAmount":1499}}</script>'
    assert parsers.parse_page("lego_com", other, "10280").price is None
    assert parsers.lego_number("https://www.lego.com/nl-be/product/flower-bouquet-10280") == "10280"
    assert parsers.lego_number("https://www.lego.com/nl-be/product/10280?x=1") == "10280"


def test_suspicious_price_without_rrp_uses_the_other_shops():
    from custom_components.lego_tracker.models import is_suspicious_price

    new_set = {"set_number": "75192"}                                     # no RRP, no history yet
    assert is_suspicious_price(75000.0, new_set, {}, [749.99, 759.0])     # cents read without the comma
    assert is_suspicious_price(75000.0, new_set, {}, [749.99])
    assert not is_suspicious_price(749.99, new_set, {}, [75000.0])        # one other shop: never blame the low one
    assert is_suspicious_price(19.99, new_set, {}, [749.99, 759.0])       # an accessory, two shops agree
    assert not is_suspicious_price(729.0, new_set, {}, [749.99, 759.0])
    assert not is_suspicious_price(75000.0, new_set, {}, [])              # nothing to compare with


def test_custom_shop_search_must_be_on_the_shop_host():
    from lego_pkg import shops
    with pytest.raises(ValueError):            # the domain text in the query string is not the host
        shops.validate_custom_shop({"name": "x", "domain": "shop.be", "search": "https://evil.example/?u=shop.be&q={query}"})
    with pytest.raises(ValueError):
        shops.validate_custom_shop({"name": "x", "domain": "shop.be", "search": "https://shop.be.evil.example/?q={query}"})
    assert shops.validate_custom_shop({"name": "x", "domain": "shop.be", "search": "https://www.shop.be/zoek?q={query}"})["domain"] == "shop.be"




def test_export_safety_apostrophe_is_removed_on_import():
    """Our export puts an apostrophe before =, +, -, @ and ' (spreadsheet safety); importing the file again gives
    the original text back, also for text that itself starts with an apostrophe. Other leading apostrophes stay."""
    res = csv_import.analyze_csv("Number;Name;Location;Notes\n10281;'=Bonsai;'-shelf 2;'quoted\n")
    item = res["rows"][0]
    assert item["name"] == "=Bonsai" and item["location"] == "-shelf 2" and item["notes"] == "'quoted"

    rows = [{"Number": "10281", "Name": "'=Bonsai", "Location": "=A1", "Notes": "'t Huis"}]
    text = models.rows_to_csv(rows, ["Number", "Name", "Location", "Notes"])
    item = csv_import.analyze_csv(text)["rows"][0]
    assert item["name"] == "'=Bonsai" and item["location"] == "=A1" and item["notes"] == "'t Huis"


def test_wrong_product_only_for_clearly_other_products():
    """Only another brand, an accessory or another set's number make a link go away by itself."""
    wp = parsers.wrong_product
    assert wp("LEGO Icons 10281 Bonsai", "10281") is None
    assert "75192" in wp("LEGO Star Wars 75192 Millennium Falcon", "10281")
    assert wp("LEGO Bonsai boompje 1200 stukjes", "10281") is None             # pieces, not a set number
    assert wp("LEGO Bonsai 878-delig (2021)", "10281") is None
    assert wp("LEGO Architecture New York", "21028") is None                   # number missing: only a doubt
    assert wp("Led-verlichting voor LEGO 21028", "21028")                         # accessory
    assert wp(None, "10281") is None


def test_parse_lego_new_page_layouts():
    """LEGO.com pages without the set number in JSON-LD, with the page state as escaped JSON, or with only
    meta data still give the set's own price, never a recommended product's."""
    canon = '<link rel="canonical" href="https://www.lego.com/nl-be/product/the-lego-van-60500">'
    # JSON-LD sku is LEGO's own article number; the only Product on the set's own page is the set
    ld = ('<script type="application/ld+json">{"@type":"Product","name":"De LEGO® bestelwagen","sku":"6570123",'
          '"offers":{"@type":"Offer","price":"29.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}</script>')
    p = parsers.parse_page("lego_com", f"<html><head>{canon}{ld}</head></html>", "60500")
    assert p.price == 29.99 and p.title == "De LEGO® bestelwagen" and not p.unavailable
    # the same JSON-LD on another set's page is not ours
    other = canon.replace("the-lego-van-60500", "police-car-60400")
    assert parsers.parse_page("lego_com", f"<html><head>{other}{ld}</head></html>", "60500").price is None
    # page state as escaped JSON (Next.js app router), with a recommended product first
    rsc = ('<script>self.__next_f.push([1,"{\\"recs\\":[{\\"productCode\\":\\"40646\\",\\"price\\":{\\"formattedAmount\\":\\"€ 14,99\\",'
           '\\"centAmount\\":1499}}],\\"product\\":{\\"productCode\\":\\"60500\\",\\"price\\":{\\"formattedAmount\\":\\"€ 29,99\\",'
           '\\"centAmount\\":2999},\\"listPrice\\":{\\"centAmount\\":2999},\\"availabilityStatus\\":\\"E_AVAILABLE\\"}}"])</script>')
    p = parsers.parse_page("lego_com", f"<html><head><title>De LEGO® bestelwagen 60500</title></head><body>{rsc}</body></html>", "60500")
    assert p.price == 29.99 and p.list_price == 29.99 and not p.unavailable
    sold = rsc.replace("E_AVAILABLE", "H_OUT_OF_STOCK")
    p = parsers.parse_page("lego_com", f"<html><body>{sold}</body></html>", "60500")
    assert p.price is None and p.unavailable and p.list_price == 29.99
    # only meta data on the set's own page
    meta = f'<html><head>{canon}<meta property="product:price:amount" content="29.99"><meta property="og:title" content="De LEGO® bestelwagen"></head></html>'
    assert parsers.parse_page("lego_com", meta, "60500").price == 29.99
    assert parsers.parse_page("lego_com", meta.replace("the-lego-van-60500", "police-car-60400"), "60500").price is None
