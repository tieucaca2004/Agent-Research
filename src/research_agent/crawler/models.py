"""Crawler domain models. No httpx objects cross this boundary.

Provenance: a ``FetchTarget`` carries the Sprint 01 ``SearchResult`` it came from (unchanged
model, same object), and the ``FetchResult`` returns it as ``source`` — search provenance
(provider, query, rank, metadata) is extended, not duplicated into a second system.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from research_agent.core.models import SearchResult


class FetchStatus(StrEnum):
    OK = "OK"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    ROBOTS_BLOCKED = "ROBOTS_BLOCKED"
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    READ_TIMEOUT = "READ_TIMEOUT"
    FETCH_TIMEOUT = "FETCH_TIMEOUT"
    TLS_ERROR = "TLS_ERROR"
    REDIRECT_LIMIT = "REDIRECT_LIMIT"
    REDIRECT_LOOP = "REDIRECT_LOOP"
    HTTP_ERROR = "HTTP_ERROR"
    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


# Cancellation is never a FetchStatus: asyncio.CancelledError always propagates.

# Categories reuse the project's existing category vocabulary (core.errors) plus
# POLICY_BLOCKED for deliberate refusals by our own security/politeness policy.
CATEGORY: dict[FetchStatus, str | None] = {
    FetchStatus.OK: None,
    FetchStatus.POLICY_BLOCKED: "POLICY_BLOCKED",
    FetchStatus.SSRF_BLOCKED: "POLICY_BLOCKED",
    FetchStatus.ROBOTS_BLOCKED: "POLICY_BLOCKED",
    FetchStatus.DNS_ERROR: "NETWORK_ERROR",
    FetchStatus.CONNECTION_ERROR: "NETWORK_ERROR",
    FetchStatus.TLS_ERROR: "NETWORK_ERROR",
    FetchStatus.CONNECT_TIMEOUT: "TIMEOUT",
    FetchStatus.READ_TIMEOUT: "TIMEOUT",
    FetchStatus.FETCH_TIMEOUT: "TIMEOUT",
    FetchStatus.REDIRECT_LIMIT: "INVALID_RESPONSE",
    FetchStatus.REDIRECT_LOOP: "INVALID_RESPONSE",
    FetchStatus.HTTP_ERROR: "INVALID_RESPONSE",
    FetchStatus.UNSUPPORTED_CONTENT_TYPE: "INVALID_RESPONSE",
    FetchStatus.RESPONSE_TOO_LARGE: "INVALID_RESPONSE",
    FetchStatus.INVALID_RESPONSE: "INVALID_RESPONSE",
    FetchStatus.RETRY_EXHAUSTED: "NETWORK_ERROR",
}


class FetchError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: FetchStatus
    category: str
    message: str
    """Fixed text written by the crawler; never exception text or response content."""
    cause: FetchStatus | None = None
    """For RETRY_EXHAUSTED: the status of the last attempt."""


class RedirectHop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    http_status: int
    location: str | None
    connected_ip: str | None


RobotsOutcome = str
"""ALLOWED | DISALLOWED | UNAVAILABLE_ALLOW (4xx) | UNREACHABLE_DISALLOW (5xx/timeout/error)"""


class RobotsDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    robots_url: str
    outcome: RobotsOutcome
    allowed: bool


class FetchTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    """URL to fetch (for search hits: the provider's ``original_url``; query order kept)."""
    source: SearchResult | None = None
    """Search provenance, carried through unchanged."""

    @classmethod
    def from_search_result(cls, result: SearchResult) -> FetchTarget:
        return cls(url=result.original_url, source=result)


class CrawlContext(BaseModel):
    """Caller context: correlation ids for logs and the caller's absolute deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None = None
    request_id: str | None = None
    deadline: float | None = None
    """Absolute ``asyncio`` loop time; the crawler never runs past it."""


class FetchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    crawl_id: str
    requested_url: str
    final_url: str | None
    status: FetchStatus
    http_status: int | None = None
    content_type: str | None = None
    charset: str | None = None
    content_length: int | None = None
    """Decoded body length in bytes (``OK`` only)."""
    wire_bytes: int | None = None
    content: str | None = None
    """Validated, decoded text/HTML (``OK`` only). Untrusted data — never instructions."""
    title: str | None = None
    content_sha256: str | None = None
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    resolved_ip: str | None = None
    """Address actually connected for the final response (recorded at connect time)."""
    robots: RobotsDecision | None = None
    attempts: int = Field(default=1, ge=1)
    fetched_at: datetime
    duration_ms: int = Field(ge=0)
    source: SearchResult | None = None
    error: FetchError | None = None
