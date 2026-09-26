"""Provider-independent domain models."""

from __future__ import annotations

from datetime import datetime

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
