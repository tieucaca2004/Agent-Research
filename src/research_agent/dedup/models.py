"""Sprint 06 output models: a non-destructive document set with exact-duplicate groups.

Nothing here copies document text or merges documents: ``DocumentSet.documents`` holds the input
``ExtractedDocument`` objects unchanged, and every group lists all of its members by position.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.core.models import SearchResult
from research_agent.crawler.models import FetchStatus
from research_agent.extraction.models import (
    ExtractedDocument,
    ExtractionStatus,
    ExtractionWarning,
)


class DedupLevel(StrEnum):
    L1 = "L1"
    """Same source identity (normalized final URL)."""
    L2 = "L2"
    """Same raw ``content_sha256`` (S04 decompressed body bytes)."""
    L3 = "L3"
    """Same verified ``text_sha256`` (S05 extracted text), ``SUCCESS`` documents only."""


class GroupWarning(StrEnum):
    SOURCE_CONTENT_DIFFERS = "SOURCE_CONTENT_DIFFERS"
    """L1: members from the same source carry different raw content."""
    MIXED_FETCH_STATUS = "MIXED_FETCH_STATUS"
    """L1: members of the same source have different fetch statuses."""
    RAW_DUPLICATE_TEXT_DIFFERS = "RAW_DUPLICATE_TEXT_DIFFERS"
    """L2: identical bytes produced different extracted texts (e.g. different charsets)."""
    CROSS_SOURCE_DUPLICATE = "CROSS_SOURCE_DUPLICATE"
    """L2/L3: identical content from more than one source identity — not independent evidence."""


class ExclusionReason(StrEnum):
    INVALID_SOURCE_URL = "INVALID_SOURCE_URL"
    NO_CONTENT_HASH = "NO_CONTENT_HASH"
    INVALID_CONTENT_HASH = "INVALID_CONTENT_HASH"
    NOT_SUCCESS = "NOT_SUCCESS"
    EMPTY_TEXT = "EMPTY_TEXT"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    MISSING_TEXT_HASH = "MISSING_TEXT_HASH"
    TEXT_HASH_MISMATCH = "TEXT_HASH_MISMATCH"
    TEXT_HASH_UNVERIFIABLE = "TEXT_HASH_UNVERIFIABLE"


ERROR_REASONS: frozenset[ExclusionReason] = frozenset(
    {
        ExclusionReason.INVALID_SOURCE_URL,
        ExclusionReason.INVALID_CONTENT_HASH,
        ExclusionReason.TEXT_TOO_LONG,
        ExclusionReason.MISSING_TEXT_HASH,
        ExclusionReason.TEXT_HASH_MISMATCH,
        ExclusionReason.TEXT_HASH_UNVERIFIABLE,
    }
)
"""Exclusions that indicate a contract violation (also reported as errors). The others —
``NO_CONTENT_HASH``, ``NOT_SUCCESS``, ``EMPTY_TEXT`` — are normal ineligibility."""


class DocumentRef(BaseModel):
    """Provenance reference to one input document (no text copy)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=0)
    """Index in the input sequence: the S06-local document key."""
    document_id: str
    crawl_id: str
    source_identity: str | None
    """``normalize_url(final_url or requested_url)``; ``None`` when the URL is invalid."""
    host: str | None
    requested_url: str
    final_url: str | None
    status: ExtractionStatus
    fetch_status: FetchStatus
    warnings: list[ExtractionWarning]
    content_sha256: str | None
    text_sha256: str | None
    kind: Literal["source_document"]
    trust: Literal["UNTRUSTED"]
    search_source: SearchResult | None


class DuplicateGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    """``L1:url:<normalized>``, ``L2:sha256:<hex>`` or ``L3:sha256:<hex>``."""
    level: DedupLevel
    representative: int
    """Lowest member position in the caller's input sequence."""
    members: list[int] = Field(min_length=2)
    """All member positions, ascending."""
    source_identities: list[str]
    """Distinct source identities, in member order."""
    hosts: list[str]
    """Distinct hosts, in member order (for independence counting downstream)."""
    warnings: list[GroupWarning]


class Exclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int
    level: DedupLevel
    reason: ExclusionReason


class DocumentError(BaseModel):
    """A contract violation for one document; never contains page text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int
    level: DedupLevel
    code: ExclusionReason


class DedupStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: int
    groups: dict[DedupLevel, int]
    grouped_documents: dict[DedupLevel, int]
    exclusions: int
    errors: int


class DocumentSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    s06_version: str
    documents: list[ExtractedDocument]
    """The input documents: same objects, same order, unchanged."""
    refs: list[DocumentRef]
    groups: dict[DedupLevel, list[DuplicateGroup]]
    """Always contains L1, L2 and L3 (possibly empty); groups ordered by representative."""
    exclusions: list[Exclusion]
    errors: list[DocumentError]
    stats: DedupStats
