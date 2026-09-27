"""Bounded HTML element tree on top of the stdlib ``html.parser`` tokenizer (design 7-8, 16).

Simplified, text-oriented tree construction: void elements are never pushed, an end tag closes
the nearest open element of that name (unmatched end tags are ignored), ``<p>``/``<li>``/``<dt>``/
``<dd>``/``<tr>``/``<td>``/``<th>``/``<option>`` and headings are implicitly closed as in HTML,
``<head>`` is implicitly closed by body content, and everything is closed at EOF.

Limits: at most ``max_elements`` elements and ``max_depth`` nesting. Beyond them no element is
created; text is attached to the deepest kept element, except text inside elements that are
dropped anyway (``<script>`` …), which is discarded. Only whitelisted attributes are kept, the
first occurrence of a duplicated attribute wins (HTML rule), and values are capped.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from html.parser import HTMLParser

CHUNK_CHARS = 65_536

VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "keygen", "link", "meta",
        "param", "source", "track", "wbr",
    }
)  # fmt: skip
KEPT_ATTRIBUTES = frozenset(
    {
        "id", "class", "role", "hidden", "style", "href", "lang", "name", "property", "content",
        "http-equiv", "charset", "rel", "type", "datetime", "open", "start",
    }
)  # fmt: skip
_MAX_ATTR_CHARS = 2_048
_MAX_HREF_CHARS = 8_192

# Text inside these is never content; when they are not materialised (limits) their text is
# discarded instead of leaking into the parent.
TEXT_DISCARDING_TAGS = frozenset(
    {"script", "style", "template", "svg", "noscript", "textarea", "select", "iframe", "title"}
)
_HEAD_TAGS = frozenset({"base", "link", "meta", "noscript", "script", "style", "template", "title"})
_P_CLOSERS = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "ul", "li",
        "dd", "dt", "listing", "xmp", "search",
    }
)  # fmt: skip
_HEAD = frozenset({"head"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SCOPE = frozenset({"html", "table", "td", "th", "caption", "template", "button", "object"})
# tag → (tags it implicitly closes, tags that stop the search)
_IMPLIED_CLOSE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu"}) | _SCOPE),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"}) | _SCOPE),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"}) | _SCOPE),
    "tr": (frozenset({"tr", "td", "th"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot", "tr", "td", "th"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot", "tr", "td", "th"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot", "tr", "td", "th"}), frozenset({"table"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist"}) | _SCOPE),
    "a": (frozenset({"a"}), _SCOPE),
}
_TABLE_PART_SCOPE = frozenset({"table", "html", "template"})
_END_TAG_SCOPE: dict[str, frozenset[str]] = {
    "table": frozenset({"html", "template"}),
    **{
        part: _TABLE_PART_SCOPE for part in ("tbody", "thead", "tfoot", "tr", "td", "th", "caption")
    },
}
_OVERFLOW_SEARCH = 64


class ExtractionCancelled(Exception):
    """Raised inside ``Extractor.extract`` when the caller's cancel event is set."""


class Element:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str] | None, parent: Element | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Element | str] = []
        self.parent = parent

    def attr(self, name: str) -> str | None:
        return None if self.attrs is None else self.attrs.get(name)

    def has_attr(self, name: str) -> bool:
        return self.attrs is not None and name in self.attrs


def oversized_tag_pattern(max_tag_chars: int) -> re.Pattern[str]:
    """Markup constructs of at least ``max_tag_chars`` characters (design probe P5).

    Linear: ``[^<>]`` cannot cross another ``<``, so candidate scans never overlap.
    """
    return re.compile(r"<[A-Za-z/!?][^<>]{" + str(max_tag_chars - 1) + r",}>?")


def remove_oversized_tags(content: str, pattern: re.Pattern[str]) -> tuple[str, int]:
    """Drop giant tags (e.g. 700 k attributes: 29.6 s / +410 MB unguarded). Text is not a tag
    unless it starts with ``<`` + letter and runs ``max_tag_chars`` without ``<`` or ``>``."""
    return pattern.subn("", content)


class TreeBuilder(HTMLParser):
    def __init__(self, *, max_elements: int, max_depth: int) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", None, None)
        self._current = self.root
        self._depth = 0
        self._max_elements = max_elements
        self._max_depth = max_depth
        self.elements = 0
        self.structure_limited = False
        # Tags of elements not materialised because of the limits (interned strings: 8 bytes
        # per entry even for 1 M unclosed tags).
        self._overflow: list[str] = []
        self._discard_depth = 0
        # Open elements per tag name: implied-close and end-tag searches only walk the stack
        # when a matching element is actually open (keeps the common case O(1)).
        self._open: dict[str, int] = {}
        # Consecutive text events (html.parser can emit one per "<" in a "<<<<" run) are joined
        # into one string per element: a 5 MiB run is one child, not millions.
        self._text: list[str] = []

    # -- helpers -------------------------------------------------------------------------

    def _pop_to(self, element: Element) -> None:
        """Close ``element`` and everything opened inside it."""
        node = self._current
        while True:
            self._open[node.tag] -= 1
            self._depth -= 1
            if node is element or node.parent is None:
                break
            node = node.parent
        self._current = element.parent if element.parent is not None else self.root

    def _find_open(self, tags: frozenset[str], stop: frozenset[str]) -> Element | None:
        """Outermost open element in ``tags`` below the nearest ``stop`` element."""
        if not any(self._open.get(tag) for tag in tags):
            return None
        found: Element | None = None
        node: Element | None = self._current
        while node is not None and node is not self.root:
            if node.tag in tags:
                found = node
            elif node.tag in stop:
                break
            node = node.parent
        return found

    def _implied_closes(self, tag: str) -> None:
        if tag not in _HEAD_TAGS:
            head = self._find_open(_HEAD, frozenset())
            if head is not None:
                self._pop_to(head)
        if tag in _P_CLOSERS:
            open_p = self._find_open(frozenset({"p"}), _SCOPE)
            if open_p is not None:
                self._pop_to(open_p)
        if tag in HEADINGS and self._current.tag in HEADINGS:
            self._pop_to(self._current)
        rule = _IMPLIED_CLOSE.get(tag)
        if rule is not None:
            target = self._find_open(*rule)
            if target is not None:
                self._pop_to(target)

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str] | None:
        kept: dict[str, str] | None = None
        for name, value in attrs:
            if name not in KEPT_ATTRIBUTES:
                continue
            if kept is None:
                kept = {}
            if name in kept:
                continue  # first occurrence wins
            limit = _MAX_HREF_CHARS if name == "href" else _MAX_ATTR_CHARS
            kept[name] = (value or "")[:limit]
        return kept

    # -- HTMLParser callbacks ---------------------------------------------------------

    def flush_text(self) -> None:
        if self._text:
            self._current.children.append("".join(self._text))
            self._text.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.flush_text()
        tag = sys.intern(tag)
        if self._overflow or self._discard_depth:
            self._start_overflow(tag)
            return
        if tag != "br":
            self._implied_closes(tag)
        if self.elements >= self._max_elements or (
            tag not in VOID_TAGS and self._depth >= self._max_depth
        ):
            self.structure_limited = True
            self._start_overflow(tag)
            return
        element = Element(tag, self._attributes(attrs), self._current)
        self.elements += 1
        self._current.children.append(element)
        if tag not in VOID_TAGS:
            self._current = element
            self._depth += 1
            self._open[tag] = self._open.get(tag, 0) + 1

    def _start_overflow(self, tag: str) -> None:
        if tag in VOID_TAGS:
            if tag == "br" and not self._discard_depth:
                self._current.children.append(" ")  # line break lost with the structure
            return
        self._overflow.append(tag)
        if tag in TEXT_DISCARDING_TAGS:
            self._discard_depth += 1

    def handle_endtag(self, tag: str) -> None:
        self.flush_text()
        if self._overflow:
            lowest = max(-1, len(self._overflow) - 1 - _OVERFLOW_SEARCH)
            for index in range(len(self._overflow) - 1, lowest, -1):
                if self._overflow[index] == tag:
                    for closed in self._overflow[index:]:
                        if closed in TEXT_DISCARDING_TAGS:
                            self._discard_depth -= 1
                    del self._overflow[index:]
                    return
            return
        if tag == "br":  # </br> is treated as <br> by browsers
            self.handle_starttag("br", [])
            return
        if not self._open.get(tag):
            return
        stop = _END_TAG_SCOPE.get(tag, _SCOPE)
        node: Element | None = self._current
        while node is not None and node is not self.root:
            if node.tag == tag:
                self._pop_to(node)
                return
            if node.tag in stop:
                return
            node = node.parent

    def handle_data(self, data: str) -> None:
        if self._discard_depth:
            return
        if self._open.get("head") and self._current.tag == "head" and data.strip():
            self.flush_text()
            self._pop_to(self._current)
        self._text.append(data)


def build_tree(
    content: str, *, max_elements: int, max_depth: int, should_stop: Callable[[], bool]
) -> tuple[TreeBuilder, bool]:
    """Parse ``content`` in chunks; ``should_stop`` is checked before every chunk.

    Returns the builder and whether parsing stopped early (time budget).
    """
    builder = TreeBuilder(max_elements=max_elements, max_depth=max_depth)
    stopped = False
    for start in range(0, len(content), CHUNK_CHARS):
        if should_stop():
            stopped = True
            break
        builder.feed(content[start : start + CHUNK_CHARS])
    if not stopped:
        builder.close()
    builder.flush_text()
    return builder, stopped
