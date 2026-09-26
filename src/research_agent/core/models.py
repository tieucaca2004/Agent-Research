"""Provider-independent domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_results: int = Field(default=10, ge=1, le=50)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    """ISO 639-1 code, e.g. ``vi``."""
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    """ISO 3166-1 alpha-2 code, e.g. ``VN``."""
    timeout_s: float = Field(default=15.0, gt=0, le=120)


class SearchResult(BaseModel):
    """One normalized search hit. Application code depends on this, never on a
    provider's raw format."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    url: str
    """Normalized URL (see ``core.urls.normalize_url``)."""
    original_url: str
    """URL exactly as returned by the provider (provenance)."""
    snippet: str | None = None
    source: str
    """Name of the search provider that returned the hit."""
    published_at: datetime | None = None
    rank: int = Field(ge=1)
    """1-based position in the provider's result list."""
    query: str
    """The query string that produced the hit (provenance)."""
    metadata: dict[str, str] = Field(default_factory=dict)


ProviderRunStatus = Literal["SUCCESS", "EMPTY", "PARTIAL", "FAILED", "SKIPPED", "NOT_CALLED"]
"""Aggregated outcome of one provider across all queries of a run.

SUCCESS    no call failed and at least one call returned results
EMPTY      no call failed and no call returned results
PARTIAL    some calls failed and some succeeded
FAILED     every executed call failed (see ``error_categories``)
SKIPPED    every planned call was skipped by the circuit breaker
NOT_CALLED never needed (e.g. fallback provider while the primary succeeded)
Calls skipped by the circuit breaker are counted in ``skipped`` only.
"""


class ProviderExecutionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    status: ProviderRunStatus
    calls: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    empty: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    result_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    error_categories: list[str] = Field(default_factory=list)
    """Sorted, unique error categories (e.g. ``TIMEOUT``, ``AUTHENTICATION_ERROR``)."""


class DedupedSearchResult(BaseModel):
    """One unique normalized URL with the provenance of every hit that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: SearchResult
    """Canonical hit: lowest (query order, provider priority, provider rank)."""
    providers: list[str]
    """Providers that returned this URL, in provider-priority order."""
    queries: list[str]
    """Queries that returned this URL, in query order."""
    occurrences: int = Field(ge=1)


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    strategy: Literal["fallback", "fanout"]
    queries: list[str]
    results: list[DedupedSearchResult]
    provider_statuses: list[ProviderExecutionStatus]
    total_hits: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
