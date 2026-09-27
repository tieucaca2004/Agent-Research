"""Extraction domain models (Sprint 05).

Everything in an ``ExtractedDocument`` comes from a web page and is **untrusted data**: it is
evidence to be quoted, never instructions to follow (``trust`` is always ``"UNTRUSTED"``).

Provenance is not re-invented: ``DocumentProvenance`` copies the Sprint 04 ``FetchResult``
fields and carries the Sprint 01 ``SearchResult`` (``source``) unchanged.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.core.models import SearchResult
from research_agent.crawler.models import FetchStatus, RedirectHop, RobotsDecision


class ExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    """Text produced within every limit (may still carry warnings)."""
    PARTIAL = "PARTIAL"
    """Text produced but incomplete (input/text truncated or time budget exceeded)."""
    EMPTY = "EMPTY"
    """Fetched and supported, but no readable text (e.g. HTTP 200 with a JS-only shell)."""
    NOT_FETCHED = "NOT_FETCHED"
    """The FetchResult is not ``OK`` (e.g. HTTP 404, SSRF block); see ``fetch_status``."""
    UNSUPPORTED = "UNSUPPORTED"
    """Content type outside the extraction scope."""
    FAILED = "FAILED"
    """Invalid input or an internal extractor error (fixed message, no page text)."""


class ExtractionWarning(StrEnum):
    LOW_CONFIDENCE_MAIN_CONTENT = "LOW_CONFIDENCE_MAIN_CONTENT"
    THIN_CONTENT = "THIN_CONTENT"
    JS_REQUIRED_SUSPECTED = "JS_REQUIRED_SUSPECTED"
    ONLY_BOILERPLATE = "ONLY_BOILERPLATE"
    BOILERPLATE_GUARD = "BOILERPLATE_GUARD"
    STRUCTURE_LIMIT = "STRUCTURE_LIMIT"
    OVERSIZED_TAG_REMOVED = "OVERSIZED_TAG_REMOVED"
    INPUT_TRUNCATED = "INPUT_TRUNCATED"
    TEXT_TRUNCATED = "TEXT_TRUNCATED"
    TIME_BUDGET_EXCEEDED = "TIME_BUDGET_EXCEEDED"
    TITLE_TRUNCATED = "TITLE_TRUNCATED"
    LINKS_TRUNCATED = "LINKS_TRUNCATED"
    METADATA_TRUNCATED = "METADATA_TRUNCATED"
    JSONLD_DROPPED = "JSONLD_DROPPED"
    CHARSET_SUSPECT = "CHARSET_SUSPECT"
    """Decoded text looks mis-decoded (NULs, or C1 controls with a Latin-1 charset).
    S05 only reports it; decoding belongs to the crawler (S04 findings SF-1…SF-3)."""
    DECODING_ERRORS = "DECODING_ERRORS"
    """Many U+FFFD replacement characters in the decoded text."""


ContentSource = Literal[
    "MAIN", "ROLE_MAIN", "ARTICLE", "ARTICLES_COMMON_ANCESTOR", "BODY", "PLAIN_TEXT", "NONE"
]
TitleSource = Literal["TITLE_TAG", "OG_TITLE", "H1", "NONE"]
LanguageSource = Literal["HTML_LANG", "CONTENT_LANGUAGE", "OG_LOCALE", "NONE"]


class ExtractionError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ExtractionStatus
    category: str
    """Project category vocabulary (``core.errors``): INVALID_INPUT, INTERNAL_ERROR, …"""
    message: str
    """Fixed text written by the extractor; never page content or exception text."""


class ExtractedLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    """Absolute http(s) URL as written in the page (resolved, fragment removed). Never fetched."""
    normalized_url: str
    """Identity from the project URL normalizer (``core.urls.normalize_url``); used for dedup."""
    text: str
    in_main_content: bool


class ClaimedMetadata(BaseModel):
    """Values the page *claims* about itself. Unverified — verification is Sprint 07's job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_type: str | None = None
    og_site_name: str | None = None
    og_locale: str | None = None
    og_image: str | None = None
    canonical_url: str | None = None
    author: str | None = None
    published_time: str | None = None
    published_time_parsed: datetime | None = None
    """Set only when ``published_time`` is ISO 8601 (``datetime.fromisoformat``)."""
    modified_time: str | None = None
    modified_time_parsed: datetime | None = None
    declared_charset: str | None = None
    """Charset named by ``<meta>`` in the page (the crawler's decoding choice is in provenance)."""
    json_ld: list[str] = Field(default_factory=list)
    """Raw ``application/ld+json`` blocks that parse as JSON; not interpreted."""


class ExtractionStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_chars: int = 0
    elements: int = 0
    oversized_tags_removed: int = 0
    removed_chars: dict[str, int] = Field(default_factory=dict)
    """Text characters removed per reason: noise, noscript, hidden, boilerplate, link_dense."""
    links_found: int = 0
    links_dropped: dict[str, int] = Field(default_factory=dict)
    jsonld_dropped: int = 0
    duration_ms: int = 0


class DocumentProvenance(BaseModel):
    """Field-for-field copy of the crawl provenance; ``source`` is the search provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crawl_id: str
    requested_url: str
    final_url: str | None
    http_status: int | None
    content_type: str | None
    charset: str | None
    fetched_at: datetime
    redirect_chain: list[RedirectHop]
    robots: RobotsDecision | None
    resolved_ip: str | None
    content_sha256: str | None
    """SHA-256 of the fetched (decompressed) body bytes, computed by the crawler."""
    source: SearchResult | None


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["source_document"] = "source_document"
    trust: Literal["UNTRUSTED"] = "UNTRUSTED"
    """Always UNTRUSTED: later LLM stages must treat every field as quoted evidence."""
    document_id: str
    extractor_version: str
    status: ExtractionStatus
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    error: ExtractionError | None = None
    fetch_status: FetchStatus
    content_source: ContentSource = "NONE"
    title: str | None = None
    title_source: TitleSource = "NONE"
    text: str = ""
    """Readable, Markdown-oriented text (headings ``#``, lists ``-``/``1.``, tables ``|``,
    fenced ``<pre>``, ``>`` quotes). Evidence offsets refer to this string."""
    char_count: int = 0
    word_count: int = 0
    """Unicode ``\\w+`` runs (CJK text without spaces counts one per run)."""
    text_sha256: str | None = None
    """SHA-256 of ``text`` encoded as UTF-8 (normalized-text hash for exact duplicates)."""
    claimed_language: str | None = None
    """Language the page declares (``lang`` / content-language / og:locale). Not detected."""
    language_source: LanguageSource = "NONE"
    links: list[ExtractedLink] = Field(default_factory=list)
    claimed_metadata: ClaimedMetadata = Field(default_factory=ClaimedMetadata)
    stats: ExtractionStats = Field(default_factory=ExtractionStats)
    provenance: DocumentProvenance
    extracted_at: datetime

    @property
    def requested_url(self) -> str:
        return self.provenance.requested_url

    @property
    def final_url(self) -> str | None:
        return self.provenance.final_url

    @property
    def content_type(self) -> str | None:
        return self.provenance.content_type

    @property
    def charset(self) -> str | None:
        return self.provenance.charset

    @property
    def content_sha256(self) -> str | None:
        """Raw-body hash (crawler); ``text_sha256`` is the extracted-text hash."""
        return self.provenance.content_sha256
