"""DOM behavior and work bounds for untrusted comparison/search pages."""
"""Regression tests for comparison-page DOM offer extraction."""
from unittest.mock import Mock

import pytest

from lego_pkg import compare


@pytest.mark.parametrize("closing_tag", ["missing", "img", "root", "span"])
def test_unmatched_closing_tags_do_linear_work(monkeypatch, closing_tag):
    parent_reads = 0

    class CountingNode(compare._Node):
        def __getattribute__(self, name):
            nonlocal parent_reads
            if name == "parent":
                parent_reads += 1
            return super().__getattribute__(name)

    monkeypatch.setattr(compare, "_Node", CountingNode)
    depth = 2000
    builder = compare._Builder()
    # span is implicitly closed by section; img never enters the open chain.
    builder.feed("<section><span></section><img>" + "<div>" * depth)
    deepest = builder.cur
    parent_reads = 0
    builder.feed(f"</{closing_tag}>" * depth)
    assert builder.cur is deepest
    builder.feed("text" + "</div>" * depth)
    assert builder.cur is builder.root
    assert builder.root.all_text() == "text"
    assert len(list(builder.root.iter())) == depth + 4
    # Count work instead of timing it: unmatched tags must not revisit ancestors.
    assert parent_reads <= 2 * depth


def test_closing_tags_preserve_nearest_match_and_implicit_closes():
    builder = compare._Builder()
    builder.feed('<div id="outer"><div id="inner"><span>one</div>')
    assert builder.cur.attrs == {"id": "outer"}
    builder.feed("</span><p>two</p></div></div><aside>three</aside>")
    outer, aside = builder.root.children
    inner, paragraph = outer.children
    assert inner.attrs == {"id": "inner"}
    assert inner.all_text() == "one"
    assert paragraph.tag == "p" and paragraph.all_text() == "two"
    assert paragraph.parent is outer
    assert aside.tag == "aside" and aside.all_text() == "three"
    assert builder.cur is builder.root


def test_void_self_closing_and_root_named_elements():
    root = compare._dom("<root><div><img></img><br/><custom/>text</div></root><p>end</p>")
    element, paragraph = root.children
    div = element.children[0]
    assert element.tag == "root" and element.parent is root
    assert [child.tag for child in div.children[:-1]] == ["img", "br", "custom"]
    assert div.children[-1] == "text"
    assert paragraph.tag == "p" and paragraph.parent is root


def test_comparison_search_survives_unmatched_closes():
    page = "<div>" * 2000 + "</missing>" * 2000
    page += '<a href="/product/1234">LEGO 60454</a>' + "</div>" * 2000
    result = compare.parse("kieskeurig", page, "60454", "https://www.kieskeurig.be/search", {})
    assert result.kind == "follow"
    assert result.url == "https://www.kieskeurig.be/product/1234"
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
