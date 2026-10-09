"""Research Job domain models (Sprint 02).

All models are immutable; changes produce new instances (``model_copy``) that the repository
stores with an incremented ``version``.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_agent.core.models import SearchOptions, SearchResponse
from research_agent.extraction.models import ExtractedDocument
from research_agent.pipeline.models import CrawlStats, DedupRecord, SourceRecord, StageOutcome


class JobState(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    SEARCHING = "SEARCHING"
    CRAWLING = "CRAWLING"
    EXTRACTING = "EXTRACTING"
    NORMALIZING = "NORMALIZING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


STAGE_ORDER: tuple[JobState, ...] = (
    JobState.PLANNING,
    JobState.SEARCHING,
    JobState.CRAWLING,
    JobState.EXTRACTING,
    JobState.NORMALIZING,
    JobState.VERIFYING,
)
"""Canonical order of pipeline stages. A job's ``stages`` is an ordered subset of this."""

TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED}
)

SPRINT_02_STAGES: tuple[JobState, ...] = (JobState.PLANNING, JobState.SEARCHING)
"""Stages executed by Sprint 02 jobs. Later sprints append CRAWLING … VERIFYING."""

PIPELINE_STAGES: tuple[JobState, ...] = (
    JobState.PLANNING,
    JobState.SEARCHING,
    JobState.CRAWLING,
    JobState.NORMALIZING,
)
"""Sprint 07 pipeline jobs (feature flag, default off): CRAWLING = S04 fetch + S05 content
extraction, NORMALIZING = S06 grouping. EXTRACTING (AI extraction) is not configured."""


class ResearchJobRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=3, max_length=500)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = unicodedata.normalize("NFC", value)
        if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in text):
            raise ValueError("query contains control characters")
        return " ".join(text.split())


class ResearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    planner: str
    """Planner name and version, e.g. ``fixed-v1``."""
    queries: list[str] = Field(min_length=1, max_length=8)
    search_options: SearchOptions
    created_at: datetime

    @field_validator("queries")
    @classmethod
    def _queries_non_empty_unique(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(q.split()) for q in value]
        if any(not q for q in cleaned):
            raise ValueError("plan queries must be non-empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("plan queries must be unique")
        return cleaned


class ProviderErrorRecord(BaseModel):
    """Error information preserved from the search layer (Sprint 01 codes/categories)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    code: str
    category: str
    retryable: bool = False


class AttemptRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    status: str
    tries: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    result_count: int = Field(ge=0)
    error: ProviderErrorRecord | None = None


QueryStatus = Literal["COVERED", "FAILED", "INTERRUPTED", "NOT_RUN"]
"""COVERED     at least one provider attempt ended OK or EMPTY
FAILED      every provider attempt failed (``AllSearchProvidersFailedError``)
INTERRUPTED execution was stopped by cancellation, stage timeout or job deadline
NOT_RUN     never started (stopped earlier)"""


class QueryOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    query_hash: str
    request_id: str
    status: QueryStatus
    result_count: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    errors: list[ProviderErrorRecord] = Field(default_factory=list)


JobErrorCode = Literal[
    "ALL_SEARCH_PROVIDERS_FAILED",
    "PROVIDER_FAILURES",
    "SEARCH_STAGE_TIMEOUT",
    "JOB_DEADLINE_EXCEEDED",
    "CANCELLED",
    "RUNNER_INTERRUPTED",
    "INTERNAL_ERROR",
    # Sprint 07 pipeline (additive)
    "CRAWL_STAGE_TIMEOUT",
    "FETCH_FAILURES",
    "EXTRACTION_DEGRADED",
    "NO_DOCUMENTS",
    "CRAWL_FAILED",
    "EXTRACTION_FAILED",
    "DEDUP_FAILED",
    "DEDUP_CONTRACT_ERRORS",
    "PIPELINE_BUDGET_EXCEEDED",
]


class JobError(BaseModel):
    """Why a job failed (``error``) or why it is degraded (``warnings``).

    code                        category          meaning
    ALL_SEARCH_PROVIDERS_FAILED PROVIDER_ERROR    no planned query was covered
    PROVIDER_FAILURES           per provider      some provider calls failed (details kept)
    SEARCH_STAGE_TIMEOUT        TIMEOUT           SEARCHING exceeded its stage timeout
    JOB_DEADLINE_EXCEEDED       TIMEOUT           the job exceeded its overall deadline
    CANCELLED                   CANCELLED         cancellation was requested
    RUNNER_INTERRUPTED          INTERNAL_ERROR    the runner task itself was cancelled
    INTERNAL_ERROR              INTERNAL_ERROR    unexpected exception (type name only)

    Sprint 07 pipeline jobs:
    CRAWL_STAGE_TIMEOUT         TIMEOUT           CRAWLING exceeded its stage timeout (warning)
    FETCH_FAILURES              FETCH_ERROR       some sources were not fetched (warning)
    EXTRACTION_DEGRADED         EXTRACTION_ERROR  some documents are not SUCCESS (warning)
    NO_DOCUMENTS                FETCH_ERROR       sources selected, no usable document
    CRAWL_FAILED                INTERNAL_ERROR    unexpected exception / join mismatch, fetch side
    EXTRACTION_FAILED           INTERNAL_ERROR    unexpected exception / join mismatch, extraction
    DEDUP_FAILED                INTERNAL_ERROR    S06 raised
    DEDUP_CONTRACT_ERRORS       INTERNAL_ERROR    S06 reported per-document errors (warning)
    PIPELINE_BUDGET_EXCEEDED    INTERNAL_ERROR    extracted text exceeded the job budget
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: JobErrorCode
    category: str
    step: JobState
    message: str
    retryable: bool = False
    provider_errors: list[ProviderErrorRecord] = Field(default_factory=list)


Coverage = Literal["COMPLETE", "PARTIAL", "NONE"]


class JobProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: JobState
    stage_index: int = Field(ge=0)
    total_stages: int = Field(ge=1)
    queries_total: int = Field(default=0, ge=0)
    queries_done: int = Field(default=0, ge=0)
    queries_failed: int = Field(default=0, ge=0)
    results_unique: int = Field(default=0, ge=0)
    # Sprint 07 pipeline jobs (additive; 0 for Sprint 02 jobs)
    urls_total: int = Field(default=0, ge=0)
    urls_done: int = Field(default=0, ge=0)
    fetch_failed: int = Field(default=0, ge=0)
    documents: int = Field(default=0, ge=0)
    groups: int = Field(default=0, ge=0)


class JobResult(BaseModel):
    """Search-stage result. Captured incrementally: each finished query is stored at once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: list[QueryOutcome] = Field(default_factory=list)
    response: SearchResponse | None = None
    """Merged Sprint 01 ``SearchResponse`` over all covered queries (None if none covered)."""
    coverage: Coverage = "NONE"
    # Sprint 07 pipeline jobs (additive; empty for Sprint 02 jobs)
    sources: list[SourceRecord] = Field(default_factory=list)
    """Selected sources in canonical order (position = index in ``response.results``)."""
    not_selected: int = Field(default=0, ge=0)
    documents: list[ExtractedDocument] = Field(default_factory=list)
    """Extracted documents in ascending source position (= S06 input order)."""
    dedup: DedupRecord | None = None
    crawl_stats: CrawlStats | None = None
    stage_outcomes: list[StageOutcome] = Field(default_factory=list)


class ResearchJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: int = Field(ge=0)
    request: ResearchJobRequest
    status: JobState
    stages: tuple[JobState, ...]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline_at: datetime | None = None
    worker_id: str | None = None
    cancel_requested: bool = False
    plan: ResearchPlan | None = None
    progress: JobProgress | None = None
    result: JobResult | None = None
    error: JobError | None = None
    warnings: list[JobError] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_stages(self) -> ResearchJob:
        if not self.stages:
            raise ValueError("a job needs at least one stage")
        positions = []
        for stage in self.stages:
            if stage not in STAGE_ORDER:
                raise ValueError(f"{stage} is not a pipeline stage")
            positions.append(STAGE_ORDER.index(stage))
        if positions != sorted(set(positions)):
            raise ValueError("stages must be unique and in canonical order")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


class JobEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    seq: int = Field(ge=1)
    type: str
    at: datetime
    data: dict[str, object] = Field(default_factory=dict)
