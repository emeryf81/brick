"""DOM behavior and work bounds for untrusted comparison/search pages."""
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
