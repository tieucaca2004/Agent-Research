"""Content extraction: Sprint 04 ``FetchResult`` → ``ExtractedDocument`` (design sections 5-21).

Pure CPU, offline: this module performs no network, file or subprocess I/O and never re-fetches.
All page content is untrusted data; nothing in it is interpreted as an instruction.
The crawler's decoded ``FetchResult.content`` is read, never modified or re-decoded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from research_agent.core.errors import InvalidURLError
from research_agent.core.urls import normalize_url
from research_agent.crawler.models import CrawlContext, FetchResult, FetchStatus
from research_agent.extraction.config import ExtractionSettings
from research_agent.extraction.content import (
    Measures,
    Renderer,
    gather_text,
    iter_elements,
    remove_boilerplate,
    remove_noise,
    select_main,
)
from research_agent.extraction.models import (
    ClaimedMetadata,
    ContentSource,
    DocumentProvenance,
    ExtractedDocument,
    ExtractedLink,
    ExtractionError,
    ExtractionStats,
    ExtractionStatus,
    ExtractionWarning,
    LanguageSource,
    TitleSource,
)
from research_agent.extraction.text import clean_inline, clean_plain_text, clean_value
from research_agent.extraction.tree import (
    Element,
    ExtractionCancelled,
    build_tree,
    oversized_tag_pattern,
    remove_oversized_tags,
)
from research_agent.logging import get_logger

log = get_logger(__name__)

EXTRACTOR_VERSION = "s05.1"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
SUPPORTED_TYPES = HTML_TYPES | {"text/plain"}

MAX_URL_CHARS = 2_048
MAX_ANCHOR_CHARS = 200
MAX_META_ELEMENTS = 200
LINK_EXAMINE_FACTOR = 20
THIN_CONTENT_CHARS = 200

_W = ExtractionWarning
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_URL_NOISE = re.compile(r"[\t\n\r]")
_WORD = re.compile(r"\w+")
_C1 = re.compile("[\x80-\x9f]")
_LATIN1 = frozenset({"iso8859-1", "iso-8859-1", "latin-1", "latin1", "l1"})
_KNOWN_SCHEMES = frozenset({"javascript", "data", "mailto", "tel", "ftp", "file", "blob", "about"})
_OG_FIELDS = {
    "og:title": "og_title",
    "og:description": "og_description",
    "og:type": "og_type",
    "og:site_name": "og_site_name",
    "og:locale": "og_locale",
    "og:image": "og_image",
    "description": "description",
    "author": "author",
    "article:author": "author",
    "article:published_time": "published_time",
    "article:modified_time": "modified_time",
    "og:updated_time": "modified_time",
}


class _Budget:
    """Cooperative deadline + cancellation, checked between parser chunks and render steps."""

    def __init__(
        self, deadline: float, cancel: threading.Event | None, clock: Callable[[], float]
    ) -> None:
        self._deadline = deadline
        self._cancel = cancel
        self._clock = clock
        self.expired = False

    def cancel_only(self) -> bool:
        """For bounded work after the deadline already expired: honours cancellation only."""
        if self._cancel is not None and self._cancel.is_set():
            raise ExtractionCancelled
        return False

    def should_stop(self) -> bool:
        if self._cancel is not None and self._cancel.is_set():
            raise ExtractionCancelled
        if not self.expired and self._clock() >= self._deadline:
            self.expired = True
        return self.expired


@dataclass
class _Head:
    """Facts collected from the whole tree before any removal."""

    title: str | None = None
    html_lang: str | None = None
    base_href: str | None = None
    canonical_href: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    content_language: str | None = None
    declared_charset: str | None = None
    json_ld: list[str] = field(default_factory=list)


def _collect_head(root: Element) -> _Head:
    head = _Head()
    meta_seen = 0
    stack: list[tuple[Element, bool]] = [(root, False)]
    while stack:
        element, in_foreign = stack.pop()
        tag = element.tag
        foreign = in_foreign or tag in ("svg", "math")
        if tag == "title" and not foreign and head.title is None:
            head.title = gather_text(element)
        elif tag == "html" and head.html_lang is None:
            head.html_lang = element.attr("lang")
        elif tag == "base" and head.base_href is None and element.attr("href"):
            head.base_href = element.attr("href")
        elif tag == "link" and head.canonical_href is None:
            rel = (element.attr("rel") or "").lower().split()
            if "canonical" in rel and element.attr("href"):
                head.canonical_href = element.attr("href")
        elif tag == "meta" and meta_seen < MAX_META_ELEMENTS:
            meta_seen += 1
            _collect_meta(element, head)
        elif tag == "script":
            kind = (element.attr("type") or "").split(";")[0].strip().lower()
            if kind == "application/ld+json":
                head.json_ld.append("".join(c for c in element.children if isinstance(c, str)))
        stack.extend(
            (child, foreign) for child in reversed(element.children) if isinstance(child, Element)
        )
    return head


def _collect_meta(element: Element, head: _Head) -> None:
    if element.attr("charset") and head.declared_charset is None:
        head.declared_charset = element.attr("charset")
    content = element.attr("content")
    if content is None:
        return
    equiv = (element.attr("http-equiv") or "").strip().lower()
    if equiv == "content-language" and head.content_language is None:
        head.content_language = content
    elif equiv == "content-type" and head.declared_charset is None:
        match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", content, re.IGNORECASE)
        if match:
            head.declared_charset = match.group(1)
    key = (element.attr("property") or element.attr("name") or "").strip().lower()
    if key and key not in head.meta:  # first occurrence wins
        head.meta[key] = content


def _http_url(value: str, base: str) -> tuple[str | None, str | None]:
    """Absolute http(s) URL without credentials/fragment, or (None, drop reason)."""
    href = _URL_NOISE.sub("", value.strip())
    if not href:
        return None, "empty"
    if href.startswith("#"):
        return None, "fragment"
    scheme = _SCHEME.match(href)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        name = scheme.group(1).lower()
        return None, name if name in _KNOWN_SCHEMES else "other_scheme"
    try:
        parts = urlsplit(urljoin(base, href))
        if parts.scheme.lower() not in ("http", "https"):
            return None, "other_scheme"
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None, "credentials"
        if not parts.hostname:
            return None, "invalid"
        url = urlunsplit(parts._replace(fragment=""))
    except ValueError:
        return None, "invalid"
    if len(url) > MAX_URL_CHARS:
        return None, "too_long"
    return url, None


def _resolve_link(href: str, base: str) -> tuple[str | None, str | None, str | None]:
    """(url, normalized_url, drop reason)."""
    url, reason = _http_url(href, base)
    if url is None:
        return None, None, reason
    try:
        return url, normalize_url(url), None
    except InvalidURLError:
        return None, None, "invalid"


def _iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _language(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(",")[0].strip().lower().replace("_", "-")
    return candidate if _LANGUAGE.fullmatch(candidate) else None


def _charset_warnings(content: str, charset: str | None) -> list[ExtractionWarning]:
    """Signals only: S05 never re-decodes (S04 findings SF-1…SF-3 are not repaired here)."""
    warnings: list[ExtractionWarning] = []
    suspect = "\x00" in content or (
        (charset or "").lower() in _LATIN1 and _C1.search(content) is not None
    )
    if suspect:
        warnings.append(_W.CHARSET_SUSPECT)
    replacements = content.count("�")
    if replacements > 100 or (content and replacements > 0.005 * len(content)):
        warnings.append(_W.DECODING_ERRORS)
    return warnings


@dataclass
class _Draft:
    """Mutable result under construction; frozen into ``ExtractedDocument`` at the end."""

    warnings: list[ExtractionWarning] = field(default_factory=list)
    partial: bool = False
    content_source: ContentSource = "NONE"
    title: str | None = None
    title_source: TitleSource = "NONE"
    text: str = ""
    language: str | None = None
    language_source: LanguageSource = "NONE"
    links: list[ExtractedLink] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)

    def warn(self, warning: ExtractionWarning) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)


class Extractor:
    def __init__(
        self,
        settings: ExtractionSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or ExtractionSettings()
        self._clock = clock
        self._oversized = oversized_tag_pattern(self._settings.max_tag_chars)
        self._semaphore = asyncio.Semaphore(self._settings.concurrency)
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Extractions currently running (threads included)."""
        return self._in_flight

    # -- async entry point ------------------------------------------------------------

    async def aextract(
        self, fetch: FetchResult, context: CrawlContext | None = None
    ) -> ExtractedDocument:
        """Run ``extract`` in a worker thread, at most ``concurrency`` at a time.

        The caller's deadline (loop time) bounds the time budget. On cancellation the worker is
        told to stop at its next chunk, the slot is held until it has stopped, and
        ``CancelledError`` propagates.
        """
        async with self._semaphore:
            loop = asyncio.get_running_loop()
            deadline = None
            if context is not None and context.deadline is not None:
                deadline = self._clock() + (context.deadline - loop.time())
            cancel = threading.Event()
            task = asyncio.ensure_future(
                asyncio.to_thread(self.extract, fetch, deadline=deadline, cancel=cancel)
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel.set()
                await asyncio.wait({task})
                raise

    # -- sync entry point -------------------------------------------------------------

    def extract(
        self,
        fetch: FetchResult,
        *,
        deadline: float | None = None,
        cancel: threading.Event | None = None,
    ) -> ExtractedDocument:
        """Extract one document. Never raises for page content (errors → ``FAILED``);
        raises ``ExtractionCancelled`` only when ``cancel`` is set."""
        with self._lock:
            self._in_flight += 1
        try:
            return self._extract(fetch, deadline, cancel)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _extract(
        self, fetch: FetchResult, deadline: float | None, cancel: threading.Event | None
    ) -> ExtractedDocument:
        started = self._clock()
        limit = started + self._settings.timeout_s
        budget = _Budget(limit if deadline is None else min(limit, deadline), cancel, self._clock)
        draft = _Draft()
        status: ExtractionStatus
        error: ExtractionError | None = None
        if fetch.status is not FetchStatus.OK:
            status = ExtractionStatus.NOT_FETCHED
            error = ExtractionError(
                code=status, category="INVALID_INPUT", message="source was not fetched"
            )
        elif fetch.content_type not in SUPPORTED_TYPES:
            status = ExtractionStatus.UNSUPPORTED
            error = ExtractionError(
                code=status, category="INVALID_INPUT", message="content type not supported"
            )
        elif fetch.content is None:
            status = ExtractionStatus.FAILED
            error = ExtractionError(
                code=status, category="INVALID_INPUT", message="fetch result has no content"
            )
        else:
            try:
                self._run(fetch, fetch.content, draft, budget)
                status = self._status(draft)
            except ExtractionCancelled:
                raise
            except Exception:  # hostile input must never crash the pipeline
                status = ExtractionStatus.FAILED
                error = ExtractionError(
                    code=status, category="INTERNAL_ERROR", message="extraction failed"
                )
                draft = _Draft(warnings=draft.warnings)
        document = self._document(fetch, draft, status, error, started)
        log.info(
            "extract.document",
            crawl_id=fetch.crawl_id,
            document_id=document.document_id,
            host=urlsplit(fetch.final_url or fetch.requested_url).hostname,
            status=document.status.value,
            content_source=document.content_source,
            warnings=[w.value for w in document.warnings],
            chars=document.char_count,
            duration_ms=document.stats.duration_ms,
        )
        return document

    @staticmethod
    def _status(draft: _Draft) -> ExtractionStatus:
        if draft.partial:
            return ExtractionStatus.PARTIAL
        if not draft.text:
            return ExtractionStatus.EMPTY
        return ExtractionStatus.SUCCESS

    # -- pipeline -----------------------------------------------------------------------

    def _run(self, fetch: FetchResult, content: str, draft: _Draft, budget: _Budget) -> None:
        s = self._settings
        draft.stats["input_chars"] = len(content)
        for warning in _charset_warnings(content, fetch.charset):
            draft.warn(warning)
        if len(content) > s.max_input_chars:
            content = content[: s.max_input_chars]
            draft.warn(_W.INPUT_TRUNCATED)
            draft.partial = True
        if fetch.content_type == "text/plain":
            draft.content_source = "PLAIN_TEXT"
            self._set_text([clean_plain_text(content)], draft, plain=True)
            return
        self._run_html(fetch, content, draft, budget)

    def _run_html(self, fetch: FetchResult, content: str, draft: _Draft, budget: _Budget) -> None:
        s = self._settings
        content, oversized = remove_oversized_tags(content, self._oversized)
        draft.stats["oversized_tags_removed"] = oversized
        if oversized:
            draft.warn(_W.OVERSIZED_TAG_REMOVED)

        builder, stopped = build_tree(
            content,
            max_elements=s.max_elements,
            max_depth=s.max_depth,
            should_stop=budget.should_stop,
        )
        root = builder.root
        draft.stats["elements"] = builder.elements
        if builder.structure_limited:
            draft.warn(_W.STRUCTURE_LIMIT)
        if stopped:
            self._time_out(draft)

        head = _collect_head(root)
        base = self._base_url(fetch, head.base_href)
        self._metadata(head, base, draft)
        self._language(head, draft)

        remove_noise(root, draft.removed)
        measures = Measures()
        measures.measure(root)
        selection = select_main(root, measures, s.min_main_chars)
        draft.content_source = selection.source
        if selection.low_confidence:
            draft.warn(_W.LOW_CONFIDENCE_MAIN_CONTENT)

        self._links(root, selection.element, base, draft)
        self._title(head, root, selection.element, draft)

        boilerplate = remove_boilerplate(
            selection, root, measures, prune_link_dense=selection.source == "BODY"
        )
        if boilerplate.removed_boilerplate:
            draft.removed["boilerplate"] = boilerplate.removed_boilerplate
        if boilerplate.removed_link_dense:
            draft.removed["link_dense"] = boilerplate.removed_link_dense
        if boilerplate.guarded:
            draft.warn(_W.BOILERPLATE_GUARD)

        # After a parse time-out the (bounded) partial tree is still rendered: that text is the
        # PARTIAL result. Otherwise rendering itself stops at the deadline.
        renderer = Renderer(
            measures,
            should_stop=budget.cancel_only if stopped else budget.should_stop,
            char_limit=s.max_text_chars,
        )
        blocks = renderer.render(selection.element)
        if renderer.timed_out:
            self._time_out(draft)
        self._set_text(blocks, draft, plain=False)

        if not draft.text:
            if draft.removed.get("noscript"):
                draft.warn(_W.JS_REQUIRED_SUSPECTED)
            if boilerplate.removed_boilerplate or boilerplate.removed_link_dense:
                draft.warn(_W.ONLY_BOILERPLATE)

    @staticmethod
    def _time_out(draft: _Draft) -> None:
        draft.warn(_W.TIME_BUDGET_EXCEEDED)
        draft.partial = True

    def _set_text(self, blocks: list[str], draft: _Draft, *, plain: bool) -> None:
        limit = self._settings.max_text_chars
        kept: list[str] = []
        total = 0
        truncated = False
        for block in blocks:
            if not block:
                continue
            extra = len(block) + (2 if kept else 0)
            if total + extra > limit:
                truncated = True
                if not kept:
                    kept.append(self._cut(block, limit, plain))
                break
            kept.append(block)
            total += extra
        if truncated:
            draft.warn(_W.TEXT_TRUNCATED)
            draft.partial = True
        draft.text = "\n\n".join(kept)
        if draft.text and len(draft.text) < THIN_CONTENT_CHARS:
            draft.warn(_W.THIN_CONTENT)

    @staticmethod
    def _cut(block: str, limit: int, plain: bool) -> str:
        """Cut one oversized block at a paragraph/line boundary when possible."""
        head = block[:limit]
        for separator in ("\n\n", "\n") if plain else ("\n",):
            index = head.rfind(separator)
            if index > limit // 2:
                return head[:index].rstrip()
        return head.rstrip()

    @staticmethod
    def _base_url(fetch: FetchResult, base_href: str | None) -> str:
        document_url = fetch.final_url or fetch.requested_url
        if base_href:
            base, _ = _http_url(base_href, document_url)
            if base is not None:
                return base
        return document_url

    def _metadata(self, head: _Head, base: str, draft: _Draft) -> None:
        limit = self._settings.max_metadata_chars
        values: dict[str, Any] = {}
        for key, name in _OG_FIELDS.items():
            raw = head.meta.get(key)
            if raw is None or name in values:
                continue
            value, truncated = clean_value(raw, limit)
            if truncated:
                draft.warn(_W.METADATA_TRUNCATED)
            if value is not None:
                values[name] = value
        values["published_time_parsed"] = _iso_datetime(values.get("published_time"))
        values["modified_time_parsed"] = _iso_datetime(values.get("modified_time"))
        if head.canonical_href:
            canonical, _ = _http_url(head.canonical_href, base)
            values["canonical_url"] = canonical
        if head.declared_charset:
            values["declared_charset"], _ = clean_value(head.declared_charset, 64)
        values["json_ld"] = self._json_ld(head.json_ld, draft)
        draft.metadata = values

    def _json_ld(self, blocks: list[str], draft: _Draft) -> list[str]:
        s = self._settings
        kept: list[str] = []
        dropped = 0
        for raw in blocks:
            text = raw.strip()
            if len(kept) >= s.max_jsonld_blocks or len(text) > s.max_jsonld_chars or not text:
                dropped += 1
                continue
            try:
                json.loads(text)
            except (ValueError, RecursionError):
                dropped += 1
                continue
            kept.append(text)  # raw, uninterpreted
        if dropped:
            draft.stats["jsonld_dropped"] = dropped
            draft.warn(_W.JSONLD_DROPPED)
        return kept

    @staticmethod
    def _language(head: _Head, draft: _Draft) -> None:
        sources: tuple[tuple[LanguageSource, str | None], ...] = (
            ("HTML_LANG", head.html_lang),
            ("CONTENT_LANGUAGE", head.content_language),
            ("OG_LOCALE", head.meta.get("og:locale")),
        )
        for source, value in sources:
            language = _language(value)
            if language is not None:
                draft.language, draft.language_source = language, source
                return

    def _title(self, head: _Head, root: Element, main: Element, draft: _Draft) -> None:
        limit = self._settings.max_title_chars
        candidates: list[tuple[TitleSource, str | None]] = [
            ("TITLE_TAG", head.title),
            ("OG_TITLE", head.meta.get("og:title")),
        ]
        for scope in (main, root):
            h1 = next((e for e in iter_elements(scope) if e.tag == "h1"), None)
            if h1 is not None:
                candidates.append(("H1", gather_text(h1)))
                break
        for source, raw in candidates:
            if raw is None:
                continue
            value, truncated = clean_value(raw, limit)
            if value is None:
                continue
            if truncated:
                draft.warn(_W.TITLE_TRUNCATED)
            draft.title, draft.title_source = value, source
            return

    def _links(self, root: Element, main: Element, base: str, draft: _Draft) -> None:
        """http(s) links, main-content first, deduplicated by normalized URL, capped.

        Candidates are examined in that order only until ``max_links`` links are kept or
        ``LINK_EXAMINE_FACTOR * max_links`` candidates were examined; if any remain unexamined,
        ``LINKS_TRUNCATED`` is set. Identical ``href`` values are resolved once (bounded work on
        link-heavy or duplicate-heavy pages).
        """
        candidates: list[tuple[Element, bool]] = []
        stack: list[tuple[Element, bool]] = [(root, root is main)]
        while stack:
            element, in_main = stack.pop()
            if element.tag == "a" and element.attr("href") is not None:
                candidates.append((element, in_main))
            stack.extend(
                (child, in_main or child is main)
                for child in reversed(element.children)
                if isinstance(child, Element)
            )
        candidates.sort(key=lambda item: not item[1])  # main-content links first (stable)
        links: list[ExtractedLink] = []
        seen: set[str] = set()
        dropped: dict[str, int] = {}
        resolved: dict[str, tuple[str | None, str | None, str | None]] = {}
        max_links = self._settings.max_links
        for index, (element, in_main) in enumerate(candidates):
            if len(links) >= max_links or index >= LINK_EXAMINE_FACTOR * max(max_links, 1):
                draft.warn(_W.LINKS_TRUNCATED)
                dropped["not_examined"] = len(candidates) - index
                break
            href = element.attr("href") or ""
            if href not in resolved:
                resolved[href] = _resolve_link(href, base)
            url, normalized, reason = resolved[href]
            if url is None or normalized is None:
                key = reason or "invalid"
                dropped[key] = dropped.get(key, 0) + 1
                continue
            if normalized in seen:
                dropped["duplicate"] = dropped.get("duplicate", 0) + 1
                continue
            seen.add(normalized)
            text = clean_inline(gather_text(element))[:MAX_ANCHOR_CHARS].rstrip()
            links.append(
                ExtractedLink(
                    url=url, normalized_url=normalized, text=text, in_main_content=in_main
                )
            )
        draft.links = links
        draft.stats["links_found"] = len(candidates)
        draft.stats["links_dropped"] = dropped

    # -- result -------------------------------------------------------------------------

    def _document(
        self,
        fetch: FetchResult,
        draft: _Draft,
        status: ExtractionStatus,
        error: ExtractionError | None,
        started: float,
    ) -> ExtractedDocument:
        text = draft.text
        stats = ExtractionStats(
            **{k: v for k, v in draft.stats.items() if k in ExtractionStats.model_fields},
            removed_chars=dict(draft.removed),
            duration_ms=max(0, int((self._clock() - started) * 1000)),
        )
        return ExtractedDocument(
            document_id=uuid.uuid4().hex,
            extractor_version=EXTRACTOR_VERSION,
            status=status,
            warnings=list(draft.warnings),
            error=error,
            fetch_status=fetch.status,
            content_source=draft.content_source,
            title=draft.title,
            title_source=draft.title_source,
            text=text,
            char_count=len(text),
            word_count=sum(1 for _ in _WORD.finditer(text)),
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            claimed_language=draft.language,
            language_source=draft.language_source,
            links=list(draft.links),
            claimed_metadata=ClaimedMetadata(**draft.metadata),
            stats=stats,
            provenance=DocumentProvenance(
                crawl_id=fetch.crawl_id,
                requested_url=fetch.requested_url,
                final_url=fetch.final_url,
                http_status=fetch.http_status,
                content_type=fetch.content_type,
                charset=fetch.charset,
                fetched_at=fetch.fetched_at,
                redirect_chain=list(fetch.redirect_chain),
                robots=fetch.robots,
                resolved_ip=fetch.resolved_ip,
                content_sha256=fetch.content_sha256,
                source=fetch.source,
            ),
            extracted_at=datetime.now(UTC),
        )
