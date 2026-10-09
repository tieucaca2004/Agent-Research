"""Pipeline integration records (Sprint 07): provenance per selected source, stage outcomes,
crawl statistics. No page content is stored here: ``FetchRecord`` is a ``FetchResult`` without
``content``/``title``; extracted documents live in ``JobResult.documents``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.crawler.models import (
    FetchError,
    FetchResult,
    FetchStatus,
    RedirectHop,
    RobotsDecision,
)
from research_agent.dedup.models import DocumentSet
from research_agent.extraction.models import ExtractionStatus, ExtractionWarning

SourceState = Literal["NOT_RUN", "INTERRUPTED", "FETCHED", "EXTRACTED"]
"""NOT_RUN     never fetched (stage stopped first, or no time left when its turn came)
INTERRUPTED  fetch in flight when the stage stopped (no FetchResult exists)
FETCHED      fetched, extraction not finished when the stage stopped (no document)
EXTRACTED    fetched and extracted (a document exists, possibly NOT_FETCHED)"""

StopReason = Literal["STAGE_TIMEOUT", "JOB_DEADLINE", "CANCELLED", "STAGE_FAILED", "FINALIZED"]

StageName = Literal["CRAWLING", "NORMALIZING"]
StageStatus = Literal["COMPLETED", "PARTIAL", "FAILED", "INTERRUPTED", "NOT_RUN"]


class FetchRecord(BaseModel):
    """Every ``FetchResult`` field except the body (``content``), ``title`` and ``source``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crawl_id: str
    requested_url: str
    final_url: str | None
    status: FetchStatus
    http_status: int | None = None
    content_type: str | None = None
    charset: str | None = None
    content_length: int | None = None
    wire_bytes: int | None = None
    content_sha256: str | None = None
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    resolved_ip: str | None = None
    robots: RobotsDecision | None = None
    attempts: int = 1
    fetched_at: datetime
    duration_ms: int = 0
    error: FetchError | None = None

    @classmethod
    def from_result(cls, result: FetchResult) -> FetchRecord:
        return cls(
            crawl_id=result.crawl_id,
            requested_url=result.requested_url,
            final_url=result.final_url,
            status=result.status,
            http_status=result.http_status,
            content_type=result.content_type,
            charset=result.charset,
            content_length=result.content_length,
            wire_bytes=result.wire_bytes,
            content_sha256=result.content_sha256,
            redirect_chain=list(result.redirect_chain),
            resolved_ip=result.resolved_ip,
            robots=result.robots,
            attempts=result.attempts,
            fetched_at=result.fetched_at,
            duration_ms=result.duration_ms,
            error=result.error,
        )


class SourceRecord(BaseModel):
    """One selected search result (position = index in ``JobResult.response.results``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=0)
    url: str
    """Normalized search URL (``DedupedSearchResult.result.url``)."""
    original_url: str
    """URL actually requested (the canonical hit's ``original_url``)."""
    providers: list[str]
    queries: list[str]
    state: SourceState
    reason: StopReason | None = None
    """Why a source is NOT_RUN / INTERRUPTED / FETCHED-only."""
    fetch: FetchRecord | None = None
    document_id: str | None = None
    document_status: ExtractionStatus | None = None
    document_warnings: list[ExtractionWarning] = Field(default_factory=list)
    text_chars: int = 0


class DedupRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    positions: list[int]
    """``positions[i]`` = source position of S06 input ``i`` (strictly increasing)."""
    result: DocumentSet


class StageOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: StageName
    status: StageStatus
    code: str | None = None


class CrawlStats(BaseModel):
    """Counters of one CRAWLING stage (A1 measurement, OD-A)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected: int = 0
    not_selected: int = 0
    admitted: int = 0
    fetched: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    fetch_status_counts: dict[str, int] = Field(default_factory=dict)
    fetch_timeout: int = 0
    fetch_timeout_cross_host_redirect: int = 0
    """FETCH_TIMEOUT whose redirect chain left the initial host: candidates for the S04-F1
    residual (queue wait on a busy redirect target, P7). Not provable per case."""
    fetch_timeout_deadline_capped: int = 0
    """FETCH_TIMEOUT of a fetch whose S04 budget was capped by the stage deadline."""
    fetch_timeout_other: int = 0
    extracted: int = 0
    extraction_status_counts: dict[str, int] = Field(default_factory=dict)
    not_run: int = 0
    interrupted: int = 0
    fetched_not_extracted: int = 0
    text_chars: int = 0
    admission_wait_ms_total: int = 0
    admission_wait_ms_max: int = 0
    deadline_hit: bool = False
