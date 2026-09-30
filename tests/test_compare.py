"""Regression tests for comparison-page DOM offer extraction."""
from unittest.mock import Mock

import pytest

from lego_pkg import compare


@pytest.mark.parametrize("source,url", [
    ("shoparize", "https://www.shoparize.com/be/q?q=lego+10368"),
    ("channable", "https://shopping.channable.com/?search=lego+10368"),
    ("producthero", "https://shopping.producthero.com/nl/product/123"),
])
@pytest.mark.parametrize("shared_prices", ["", "€ 20", "€ 20 € 30 € 40 € 50"])
def test_sibling_links_have_linear_extraction_work(monkeypatch, source, url, shared_prices):
    count = 2000
    page = '<div>' + '<a href="https://noise.example/item">LEGO 10368</a>' * count + shared_prices + '</div>'
    page += '<div><a href="https://shop.example/item" data-shop="Shop">LEGO 10368</a><b>€ 75</b></div>'
    price_re = Mock(wraps=compare.PRICE_RE)
    out_link = Mock(wraps=compare._out)
    monkeypatch.setattr(compare, "PRICE_RE", price_re)
    monkeypatch.setattr(compare, "_out", out_link)

    result = compare.parse(source, page, "10368", url, {})

    assert [(shop["name"], shop["price"]) for shop in result.shops] == [("Shop", 75)]
    # Each text is scanned at most once; link classification is bounded by nodes
    # plus candidates. Covers both empty and repeatedly rejected shared rows.
    assert price_re.finditer.call_count <= count + 3
    assert out_link.call_count <= 2 * count + 10


@pytest.mark.parametrize("excluded", [
    '<s>€ 5</s>', '<del>€ 5</del>', '<strike>€ 5</strike>',
    '<script>€ 5</script>', '<style>€ 5</style>',
    '<div class="old"><span>€ 5</span></div>',
    '<div data-test="shipping"><span>€ 5</span></div>',
])
def test_price_summary_preserves_exclusions_and_duplicates(excluded):
    page = '<div><a href="https://shop.example/">Shop</a>' + excluded + '<b>€ 75 € 75 € 79 € 80</b></div>'
    offers = compare._dom_offers(compare._dom(page), "https://www.shoparize.com/", False)
    assert [(offer["name"], offer["price"]) for offer in offers] == [("Shop", 75)]


def test_summary_is_bounded_and_iterative():
    # Distinct prices throughout a deep tree must not be copied in full at each level.
    root = compare._dom(''.join(f'<div>€ {i}' for i in range(1, 1501)) + '</div>' * 1500)
    cache = {}
    prices, links = compare._offer_summary(root, "www.shoparize.com", cache)
    assert len(prices) == 4 and links == 0
    assert len(cache) == 1501
    assert all(len(summary[0]) <= 4 for summary in cache.values())


def test_summary_keeps_child_prices_independent_of_old_parent():
    root = compare._dom('<div class="old"><span>€ 5</span></div>')
    cache = {}
    assert compare._offer_summary(root, "www.shoparize.com", cache)[0] == set()
    child = root.children[0].children[0]
    assert compare._offer_summary(child, "www.shoparize.com", cache)[0] == {5}


def test_card_extraction_can_summarize_ancestors_outside_card():
    root = compare._dom('<section><div><a href="https://shop.example/">Shop</a></div><b>€ 75</b></section>')
    card = root.children[0].children[0]
    offers = compare._dom_offers(card, "https://www.shoparize.com/", False)
    assert [(offer["name"], offer["price"]) for offer in offers] == [("Shop", 75)]
