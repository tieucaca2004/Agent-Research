"""Noise removal, main-content selection, boilerplate removal and rendering (design 8-9, 17, 23).

Rendering is Markdown-oriented plain text: headings ``#``, lists ``-`` / ``1.``, data tables as
``| a | b |`` rows (header separator after a ``<th>`` row), ``<pre>`` fenced and verbatim,
``<blockquote>`` lines prefixed ``> ``. Blocks are separated by a blank line.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from research_agent.extraction.models import ContentSource
from research_agent.extraction.text import clean_inline, clean_pre
from research_agent.extraction.tree import HEADINGS, Element

ALWAYS_DROP = frozenset(
    {
        "script", "style", "template", "svg", "canvas", "object", "embed", "iframe", "frame",
        "frameset", "audio", "video", "picture", "map", "applet", "input", "select", "option",
        "optgroup", "textarea", "button", "datalist", "head", "title", "meta", "link", "base",
        "param", "source", "track", "img",
    }
)  # fmt: skip
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
        "dialog", "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "legend", "li",
        "listing", "main", "menu", "nav", "ol", "p", "plaintext", "pre", "search", "section",
        "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "xmp", "#root",
    }
)  # fmt: skip
PRE_TAGS = frozenset({"pre", "listing", "xmp", "plaintext"})
LIST_TAGS = frozenset({"ul", "ol", "menu", "dir"})
PAGE_LEVEL_TAGS = frozenset({"header", "footer", "aside"})
PAGE_LEVEL_ROLES = frozenset({"banner", "contentinfo", "complementary", "navigation", "search"})
LINK_DENSE_CANDIDATES = frozenset({"div", "ul", "ol", "section", "td"})
# A table whose cells contain any of these is a layout table (rendered as ordinary blocks).
_LAYOUT_MARKERS = frozenset(
    {"main", "article", "section", "nav", "header", "footer", "aside", "form"} | HEADINGS
)
_TICK = 512
_BACKTICKS = re.compile(r"`+")


def roles(element: Element) -> frozenset[str]:
    value = element.attr("role")
    return frozenset(value.lower().split()) if value else frozenset()


def noise_reason(element: Element) -> str | None:
    """Why an element is never content, or ``None``."""
    tag = element.tag
    if tag == "noscript":
        return "noscript"
    if tag in ALWAYS_DROP:
        return "noise"
    hidden = element.attr("hidden")
    if hidden is not None and hidden.strip().lower() != "until-found":
        return "hidden"
    style = element.attr("style")
    if style:
        compact = "".join(style.lower().split())
        if "display:none" in compact or "visibility:hidden" in compact:
            return "hidden"
    if tag == "dialog" and not element.has_attr("open"):
        return "hidden"
    return None  # aria-hidden and CSS classes are deliberately not "hidden" (design P8)


def text_length(element: Element) -> int:
    total = 0
    for child in element.children:
        total += len(child) if isinstance(child, str) else text_length(child)
    return total


def remove_noise(element: Element, removed: dict[str, int]) -> None:
    kept: list[Element | str] = []
    for child in element.children:
        if isinstance(child, Element):
            reason = noise_reason(child)
            if reason is not None:
                removed[reason] = removed.get(reason, 0) + text_length(child)
                continue
            remove_noise(child, removed)
        kept.append(child)
    element.children = kept


@dataclass
class Measures:
    text: dict[int, int] = field(default_factory=dict)
    link_text: dict[int, int] = field(default_factory=dict)
    links: dict[int, int] = field(default_factory=dict)
    has_block: set[int] = field(default_factory=set)
    """Elements with at least one block-level descendant."""
    has_landmark: set[int] = field(default_factory=set)
    """Elements containing a ``<main>`` or ``<article>`` descendant."""
    layout_marker: set[int] = field(default_factory=set)
    """Elements containing a layout-table marker descendant."""

    def measure(self, element: Element, in_link: bool = False) -> tuple[int, int, int]:
        text = link_text = links = 0
        block = landmark = layout = False
        child_in_link = in_link or element.tag == "a"
        for child in element.children:
            if isinstance(child, str):
                text += len(child)
                if child_in_link:
                    link_text += len(child)
                continue
            child_text, child_link_text, child_links = self.measure(child, child_in_link)
            text += child_text
            link_text += child_link_text
            links += child_links
            key = id(child)
            block = block or child.tag in BLOCK_TAGS or key in self.has_block
            landmark = landmark or child.tag in ("main", "article") or key in self.has_landmark
            layout = layout or child.tag in _LAYOUT_MARKERS or key in self.layout_marker
        if element.tag == "a":
            links += 1
        key = id(element)
        self.text[key] = text
        self.link_text[key] = link_text
        self.links[key] = links
        if block:
            self.has_block.add(key)
        if landmark:
            self.has_landmark.add(key)
        if layout:
            self.layout_marker.add(key)
        return text, link_text, links


def iter_elements(element: Element) -> list[Element]:
    """Elements below ``element`` in document order (iterative; bounded by the tree limits)."""
    out: list[Element] = []
    stack: list[Element] = [element]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(child for child in reversed(node.children) if isinstance(child, Element))
    return out[1:]


def gather_text(element: Element) -> str:
    """All descendant text, ``<br>`` as a space (for titles, anchors, table cells)."""
    parts: list[str] = []
    stack: list[Element | str] = [element]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            parts.append(node)
        elif node.tag == "br":
            parts.append(" ")
        else:
            stack.extend(reversed(node.children))
    return "".join(parts)


@dataclass
class Selection:
    element: Element
    source: ContentSource
    low_confidence: bool
    in_section: bool
    """The selection is (or is inside) an ``<article>``/``<main>``."""


def _ancestors(element: Element) -> list[Element]:
    chain: list[Element] = []
    node: Element | None = element
    while node is not None:
        chain.append(node)
        node = node.parent
    return chain


def select_main(root: Element, measures: Measures, min_main_chars: int) -> Selection:
    elements = iter_elements(root)
    body = next((e for e in elements if e.tag == "body"), root)
    body_text = measures.text.get(id(body), 0)

    def big_enough(element: Element) -> bool:
        size = measures.text.get(id(element), 0)
        return size > 0 and (size >= min_main_chars or size >= 0.25 * body_text)

    def low(element: Element) -> bool:
        return measures.text.get(id(element), 0) < 0.25 * body_text

    main = next((e for e in elements if e.tag == "main" or "main" in roles(e)), None)
    if main is not None and big_enough(main):
        source: ContentSource = "MAIN" if main.tag == "main" else "ROLE_MAIN"
        return Selection(main, source, low(main), True)

    articles = [
        e
        for e in elements
        if e.tag == "article" and not any(a.tag == "article" for a in _ancestors(e)[1:])
    ]
    if len(articles) == 1 and big_enough(articles[0]):
        return Selection(articles[0], "ARTICLE", low(articles[0]), True)
    if len(articles) >= 2:
        common = _ancestors(articles[0])
        for article in articles[1:]:
            ids = {id(node) for node in _ancestors(article)}
            common = [node for node in common if id(node) in ids]
        ancestor = common[0]
        in_section = any(n.tag in ("main", "article") for n in _ancestors(ancestor))
        return Selection(ancestor, "ARTICLES_COMMON_ANCESTOR", low(ancestor), in_section)
    return Selection(body, "BODY", True, False)


@dataclass
class BoilerplateResult:
    removed_boilerplate: int = 0
    removed_link_dense: int = 0
    guarded: bool = False


def remove_boilerplate(
    selection: Selection, root: Element, measures: Measures, *, prune_link_dense: bool
) -> BoilerplateResult:
    """Drop ``nav`` anywhere and header/footer/aside/landmarks outside article/main.

    Swallow guard: an element is kept when it holds ≥ 50 % of the body text, contains a
    ``<main>``/``<article>`` or contains the first ``<h1>`` (unclosed-tag protection).
    """
    elements = iter_elements(root)
    body = next((e for e in elements if e.tag == "body"), root)
    body_text = measures.text.get(id(body), 0)
    first_h1 = next((e for e in elements if e.tag == "h1"), None)
    h1_chain = {id(node) for node in _ancestors(first_h1)} if first_h1 is not None else set()
    result = BoilerplateResult()

    def guarded(element: Element) -> bool:
        key = id(element)
        return (
            (body_text > 0 and measures.text.get(key, 0) >= 0.5 * body_text)
            or key in measures.has_landmark
            or key in h1_chain
        )

    def link_dense(element: Element) -> bool:
        key = id(element)
        size = measures.text.get(key, 0)
        return (
            measures.links.get(key, 0) >= 3
            and size > 0
            and measures.link_text.get(key, 0) >= 0.5 * size
        )

    def walk(element: Element, in_section: bool) -> None:
        kept: list[Element | str] = []
        for child in element.children:
            if isinstance(child, str):
                kept.append(child)
                continue
            child_roles = roles(child)
            is_boilerplate = child.tag == "nav" or (
                not in_section
                and (child.tag in PAGE_LEVEL_TAGS or bool(child_roles & PAGE_LEVEL_ROLES))
            )
            if is_boilerplate or (
                prune_link_dense and child.tag in LINK_DENSE_CANDIDATES and link_dense(child)
            ):
                if not guarded(child):
                    size = measures.text.get(id(child), 0)
                    if is_boilerplate:
                        result.removed_boilerplate += size
                    else:
                        result.removed_link_dense += size
                    continue
                if is_boilerplate:
                    result.guarded = True
            walk(
                child,
                in_section or child.tag in ("main", "article") or "main" in child_roles,
            )
            kept.append(child)
        element.children = kept

    walk(selection.element, selection.in_section)
    return result


class _Break:
    """Line-break marker in an inline run (``<br>``)."""


_BR = _Break()


class Renderer:
    def __init__(
        self, measures: Measures, *, should_stop: Callable[[], bool], char_limit: int
    ) -> None:
        self._measures = measures
        self._should_stop = should_stop
        self._char_limit = char_limit
        self._visits = 0
        self._chars = 0
        self.timed_out = False
        self.limited = False

    @property
    def stopped(self) -> bool:
        return self.timed_out or self.limited

    def render(self, element: Element) -> list[str]:
        return self._container(element.children)

    def _tick(self) -> None:
        self._visits += 1
        if self._visits % _TICK == 0 and not self.timed_out and self._should_stop():
            self.timed_out = True

    @staticmethod
    def _emit(out: list[str], block: str) -> None:
        if block:
            out.append(block)

    def _count(self, produced: int) -> None:
        """Source text is produced exactly once (inline flush or ``<pre>``): count it there."""
        self._chars += produced
        if self._chars > self._char_limit:
            self.limited = True

    def _is_block(self, element: Element) -> bool:
        return element.tag in BLOCK_TAGS or id(element) in self._measures.has_block

    # -- inline -------------------------------------------------------------------------

    def _inline(self, element: Element, run: list[str | _Break]) -> None:
        if element.tag == "br":
            run.append(_BR)
            return
        for child in element.children:
            if isinstance(child, str):
                run.append(child)
            else:
                self._tick()
                self._inline(child, run)

    def _flush(self, run: list[str | _Break], out: list[str]) -> None:
        if not run:
            return
        lines: list[str] = []
        current: list[str] = []
        for piece in run:
            if isinstance(piece, _Break):
                lines.append(clean_inline("".join(current)))
                current = []
            else:
                current.append(piece)
        lines.append(clean_inline("".join(current)))
        run.clear()
        compact: list[str] = []
        for line in lines:
            if line or (compact and compact[-1]):
                compact.append(line)
        block = "\n".join(compact).strip("\n")
        self._count(len(block) + 2)
        self._emit(out, block)

    def _inline_text(self, element: Element) -> str:
        run: list[str | _Break] = []
        self._inline(element, run)
        return clean_inline("".join(" " if isinstance(p, _Break) else p for p in run))

    # -- blocks -------------------------------------------------------------------------

    def _container(self, children: list[Element | str]) -> list[str]:
        out: list[str] = []
        run: list[str | _Break] = []
        for child in children:
            if self.stopped:
                break
            if isinstance(child, str):
                run.append(child)
                continue
            self._tick()
            if self._is_block(child):
                self._flush(run, out)
                for block in self._block(child):
                    self._emit(out, block)
            else:
                self._inline(child, run)
        self._flush(run, out)
        return out

    def _block(self, element: Element) -> list[str]:
        tag = element.tag
        if tag in HEADINGS:
            if id(element) in self._measures.has_block:
                text = " ".join(" ".join(self._container(element.children)).split("\n"))
            else:
                text = self._inline_text(element)
            return ["#" * int(tag[1]) + " " + text] if text else []
        if tag in LIST_TAGS:
            return [self._list(element)]
        if tag in PRE_TAGS:
            return [self._pre(element)]
        if tag == "table":
            return self._table(element)
        if tag == "blockquote":
            inner = "\n\n".join(self._container(element.children))
            if not inner:
                return []
            return ["\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))]
        if tag == "dl":
            return [self._dl(element)]
        if tag == "hr":
            return []
        return self._container(element.children)

    def _list(self, element: Element) -> str:
        ordered = element.tag == "ol"
        number = 1
        if ordered:
            start = (element.attr("start") or "").strip()
            if start.lstrip("-").isdigit() and len(start) < 10:
                number = int(start)
        lines: list[str] = []
        for child in element.children:
            if self.stopped:
                break
            if isinstance(child, str):
                text = clean_inline(child)
                if text:
                    lines.append(text)
                continue
            self._tick()
            if child.tag != "li":  # stray content inside a list: indent it under the list
                for block in self._container([child]):
                    lines.extend("  " + line if line else "" for line in block.split("\n"))
                continue
            marker = f"{number}. " if ordered else "- "
            number += 1
            blocks = self._container(child.children)
            if not blocks:
                continue
            first, *rest = "\n".join(blocks).split("\n")
            indent = " " * len(marker)
            lines.append(marker + first)
            lines.extend(indent + line if line else "" for line in rest)
        return "\n".join(lines)

    def _pre(self, element: Element) -> str:
        parts: list[str] = []
        stack: list[Element | str] = [element]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            elif node.tag == "br":
                parts.append("\n")
            else:
                stack.extend(reversed(node.children))
        text = clean_pre("".join(parts))
        text = text.removeprefix("\n").rstrip("\n")  # HTML ignores a newline right after <pre>
        if not text.strip():
            return ""
        self._count(len(text) + 2)
        longest = max((len(m) for m in _BACKTICKS.findall(text)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}\n{text}\n{fence}"

    def _rows(self, table: Element) -> tuple[list[tuple[Element, bool]], list[Element]]:
        rows: list[tuple[Element, bool]] = []
        captions: list[Element] = []
        for child in table.children:
            if isinstance(child, str):
                continue
            if child.tag == "tr":
                rows.append((child, False))
            elif child.tag in ("thead", "tbody", "tfoot"):
                rows.extend(
                    (row, child.tag == "thead")
                    for row in child.children
                    if isinstance(row, Element) and row.tag == "tr"
                )
            elif child.tag == "caption":
                captions.append(child)
        return rows, captions

    def _table(self, table: Element) -> list[str]:
        rows, captions = self._rows(table)
        cells_by_row = [
            ([c for c in row.children if isinstance(c, Element) and c.tag in ("td", "th")], head)
            for row, head in rows
        ]
        layout = any(
            id(cell) in self._measures.layout_marker for cells, _ in cells_by_row for cell in cells
        )
        if layout:  # layout table: render cell content as ordinary blocks
            blocks: list[str] = []
            for caption in captions:
                blocks.extend(self._container(caption.children))
            for cells, _ in cells_by_row:
                for cell in cells:
                    blocks.extend(self._container(cell.children))
            return blocks
        lines: list[str] = []
        header_done = False
        for index, (cells, head) in enumerate(cells_by_row):
            if self.stopped:
                break
            self._tick()
            texts = [
                " ".join(" ".join(self._container(cell.children)).split("\n"))
                .replace("|", "\\|")
                .strip()
                for cell in cells
            ]
            if not any(texts):
                continue
            lines.append("| " + " | ".join(texts) + " |")
            is_header = head or all(cell.tag == "th" for cell in cells)
            if not header_done and index == 0 and is_header:
                lines.append("|" + " --- |" * len(texts))
                header_done = True
        caption_text = " ".join(clean_inline(gather_text(c)) for c in captions).strip()
        out = [caption_text] if caption_text else []
        if lines:
            out.append("\n".join(lines))
        return out

    def _dl(self, element: Element) -> str:
        lines: list[str] = []
        for child in element.children:
            if not isinstance(child, Element):
                continue
            text = " ".join(" ".join(self._container(child.children)).split("\n")).strip()
            if not text:
                continue
            lines.append(f": {text}" if child.tag == "dd" else text)
        return "\n".join(lines)
