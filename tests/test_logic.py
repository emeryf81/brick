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
    assert rows == [] and "setnummers" in warnings[0]


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
    assert "ander merk" in tc("Keeppley City Corner compatibel met LEGO 356", "356")[1]
    assert "staat niet" in tc("LEGO Speed Champions BMW M3 (E30) 77263", "358")[1]
    assert tc("LEGO 102810 something", "10281")[0] == "suspect"            # no partial number match
    assert tc("LEGO Icons 10311 Orchidee", "10311") == ("ok", "titel bevat setnummer")
    assert tc(None, "1")[0] is None
    assert parsers.clean_title("LEGO Icons 10311 Orchidee, kunstplanten | bol.com", "10311") == "Icons Orchidee"


def test_link_check():
    lc = models.link_check
    assert lc({"title": "LEGO 10311 Orchidee"}, {}, "10311")[0] == "ok"
    assert lc({"title": "LED kit for LEGO 10311"}, {}, "10311")[0] == "suspect"
    assert lc({"url": "https://www.bol.com/nl/nl/p/vitrine-voor-lego-10311/93/"}, {}, "10311")[0] == "suspect"
    assert lc({"url": "https://www.amazon.nl/dp/B0ABCDEFGH"}, {}, "10311")[0] is None
    assert "te laag" in lc({"title": "LEGO 10311", "history": [[1, 9.99]]}, {"rrp": 49.99}, "10311")[1]
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
    assert by[3]["status"] == "error" and "ongeldig setnummer" in txt(3)
    assert by[4]["status"] == "error" and "aantal is 0" in txt(4)
    assert by[5]["status"] == "error" and "negatief" in txt(5)
    assert by[6]["status"] == "warning" and "31/02/2022" in txt(6) and "extra exemplaar" in txt(6)
    assert by[6]["condition"] == "Gebouwd" and "added" not in by[6]
    assert by[7]["status"] == "warning" and "toekomst" in txt(7) and "1800" in txt(7) and "3× de adviesprijs" in txt(7)
    assert by[8]["status"] == "warning" and "erg hoog" in txt(8) and "bijgewerkt" in txt(8)
    assert a["summary"]["error"] == 3 and a["summary"]["merged"] == 1 and a["summary"]["update"] == 1
    assert a["columns"]["Setnummer"] == "Setnummer" and a["ignored_columns"] == []
    rows = csv_import.importable_rows(a)
    res = csv_import.apply_import(store, rows)
    assert store["collection"]["10281"]["qty"] == 2 and store["collection"]["10281"]["paid"] == 44.99
    assert "10311" not in store["collection"] and "42143" not in store["collection"]
    assert res["added"] == 2 and res["updated"] == 1


def test_analyze_fatal_cases():
    assert "setnummers" in csv_import.analyze_csv("foo;bar\n1;2\n")["fatal"]
    assert csv_import.analyze_csv("   ")["fatal"] == "Leeg bestand."
    assert "te groot" in csv_import.analyze_csv("Number\n" + "1" * 2_100_000)["fatal"]
    a = csv_import.analyze_csv("Number,Foo,Name\n10281,x,Bonsai\n")
    assert a["ignored_columns"] == ["Foo"]


def test_paid_average_ignores_missing_prices():
    store = models.new_store()
    csv_import.apply_import(store, [{"set_number": "1", "qty": 1, "paid": 40.0}, {"set_number": "1", "qty": 1}])
    assert store["collection"]["1"] == {"qty": 2, "paid": 40.0}


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
    assert "adviesprijs" in models.is_suspicious_price(4.99, {"rrp": 49.99}, {})
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
    assert a["by_year"]["2022"]["value"] == 80 and a["by_condition"] == {"Sealed": 1, "Onbekend": 2}
    assert a["top_gainers"][0]["pct"] == 50.0 and a["top_losers"][0]["set_number"] == "2"
    assert all(m["pct"] >= 0 for m in a["top_gainers"])
    assert a["avg_paid_per_piece"] == 0.1


def test_backup_validation_rejects_bad_offers():
    base = {"sets": {"10281": {}}, "offers": {"10281": {"bol": {"url": "javascript:alert(1)", "history": []}}}}
    with pytest.raises(ValueError, match="URL"):
        models.validate_backup(base)
    base["offers"]["10281"]["bol"] = {"url": "https://x", "history": [["a", 1]]}
    with pytest.raises(ValueError, match="historiek"):
        models.validate_backup(base)
    with pytest.raises(ValueError, match="onbekende set"):
        models.validate_backup({"sets": {}, "offers": {"1": {}}})


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
