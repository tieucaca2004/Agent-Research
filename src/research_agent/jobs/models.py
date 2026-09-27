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


class JobResult(BaseModel):
    """Search-stage result. Captured incrementally: each finished query is stored at once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: list[QueryOutcome] = Field(default_factory=list)
    response: SearchResponse | None = None
    """Merged Sprint 01 ``SearchResponse`` over all covered queries (None if none covered)."""
    coverage: Coverage = "NONE"


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
