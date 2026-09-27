"""Sprint 05: bounded tree construction and the oversized-tag guard (design 7-8, 16, P3-P6)."""

from __future__ import annotations

import time

from research_agent.extraction.tree import (
    Element,
    TreeBuilder,
    build_tree,
    oversized_tag_pattern,
    remove_oversized_tags,
)


def parse(html: str, *, max_elements: int = 1_000, max_depth: int = 64) -> TreeBuilder:
    builder, stopped = build_tree(
        html, max_elements=max_elements, max_depth=max_depth, should_stop=lambda: False
    )
    assert not stopped
    return builder


def shape(element: Element) -> list[object]:
    out: list[object] = []
    for child in element.children:
        if isinstance(child, str):
            if child.strip():
                out.append(child.strip())
        else:
            out.append((child.tag, shape(child)))
    return out


def test_simple_nesting() -> None:
    root = parse("<html><body><p>a <b>b</b></p></body></html>").root
    assert shape(root) == [("html", [("body", [("p", ["a", ("b", ["b"])])])])]


def test_implied_closes_lists_paragraphs_tables() -> None:
    assert shape(parse("<ul><li>a<li>b<ul><li>c</ul><li>d</ul>").root) == [
        ("ul", [("li", ["a"]), ("li", ["b", ("ul", [("li", ["c"])])]), ("li", ["d"])])
    ]
    assert shape(parse("<p>a<div>b</div>c").root) == [("p", ["a"]), ("div", ["b"]), "c"]
    assert shape(parse("<table><tr><td>1<td>2<tr><td>3</table>after").root) == [
        ("table", [("tr", [("td", ["1"]), ("td", ["2"])]), ("tr", [("td", ["3"])])]),
        "after",
    ]


def test_broken_nesting_and_unmatched_end_tags_keep_all_text() -> None:
    root = parse("<div><span>x</div>tail</span></p></section><b>one<i>two</b>three").root
    text = " ".join(str(x) for x in _texts(root))
    for word in ("x", "tail", "one", "two", "three"):
        assert word in text


def _texts(element: Element) -> list[str]:
    out: list[str] = []
    for child in element.children:
        if isinstance(child, str):
            out.append(child)
        else:
            out.extend(_texts(child))
    return out


def test_head_is_closed_by_body_content() -> None:
    root = parse("<html><head><title>T</title><p>para<div>x</div>").root
    html = root.children[0]
    assert isinstance(html, Element)
    tags = [c.tag for c in html.children if isinstance(c, Element)]
    assert tags == ["head", "p", "div"]


def test_first_duplicate_attribute_wins_and_only_whitelisted_kept() -> None:
    root = parse('<a href="/first" href="javascript:x" onclick="evil()" data-x="1">t</a>').root
    anchor = root.children[0]
    assert isinstance(anchor, Element)
    assert anchor.attrs == {"href": "/first"}


def test_attribute_values_are_capped() -> None:
    root = parse(f'<div title="x" class="{"c" * 5000}">t</div>').root
    div = root.children[0]
    assert isinstance(div, Element)
    assert div.attrs is not None and len(div.attrs["class"]) == 2048


def test_br_is_a_void_element() -> None:
    root = parse("a<br>b</br>c").root
    assert [c.tag if isinstance(c, Element) else c for c in root.children] == [
        "a",
        "br",
        "b",
        "br",
        "c",
    ]


def test_element_limit_flattens_but_keeps_text() -> None:
    builder = parse("<b>x</b>" * 20, max_elements=10)
    assert builder.elements == 10
    assert builder.structure_limited
    assert "".join(_texts(builder.root)) == "x" * 20


def test_depth_limit_keeps_text_and_discards_script_text() -> None:
    html = "<div>" * 100 + "deep<script>SECRET()</script>" + "</div>" * 100 + "after"
    builder = parse(html, max_depth=10)
    assert builder.structure_limited
    texts = "".join(_texts(builder.root))
    assert "deep" in texts and "after" in texts
    assert "SECRET" not in texts


def test_consecutive_text_events_are_joined() -> None:
    builder = parse("<p>" + "<" * 10_000 + "</p>")
    paragraph = builder.root.children[0]
    assert isinstance(paragraph, Element)
    assert paragraph.children == ["<" * 10_000]


def test_unclosed_constructs_do_not_crash() -> None:
    for html in (
        "<p>a</p><!--hidden",
        "<p>a</p><script>var x",
        "<p>a</p><title>unterminated",
        "<p>x</p><a href='unterminated",
        "<![CDATA[zz",
        "&#xFFFFFFFF; &#0; &bogus; &am",
    ):
        parse(html)


def test_should_stop_is_checked_between_chunks() -> None:
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    builder, stopped = build_tree(
        "<p>" + "x" * 300_000, max_elements=100, max_depth=10, should_stop=stop
    )
    assert stopped
    assert len("".join(_texts(builder.root))) < 300_000


# -- oversized-tag guard (probe P5) ------------------------------------------------------


def test_guard_removes_attribute_heavy_tag_quickly() -> None:
    html = (
        "<p>before</p><div " + " ".join(f"a{i}=1" for i in range(600_000)) + ">x</div><p>after</p>"
    )
    started = time.perf_counter()
    cleaned, removed = remove_oversized_tags(html, oversized_tag_pattern(32_768))
    builder = parse(cleaned)
    assert time.perf_counter() - started < 2.0
    assert removed == 1
    assert "".join(_texts(builder.root)) == "beforexafter"


def test_guard_threshold_is_exact() -> None:
    pattern = oversized_tag_pattern(1_024)
    under = "<div title='" + "x" * (1_024 - 15) + "'>"
    assert len(under) < 1_024
    assert remove_oversized_tags(under + "t", pattern) == (under + "t", 0)
    over = "<div title='" + "x" * 1_024 + "'>"
    assert remove_oversized_tags(over + "t", pattern) == ("t", 1)


def test_guard_leaves_normal_large_html_untouched() -> None:
    html = "<p>" + "text " * 200_000 + "</p>" + "<a href='/x'>l</a>" * 10_000
    assert remove_oversized_tags(html, oversized_tag_pattern(32_768)) == (html, 0)


def test_guard_also_covers_unterminated_giant_constructs() -> None:
    pattern = oversized_tag_pattern(1_024)
    for construct in ("<!DOCTYPE ", "</div", "<?pi ", "<abc"):
        cleaned, removed = remove_oversized_tags("ok" + construct + "y" * 2_000, pattern)
        assert (cleaned, removed) == ("ok", 1)
