"""SearchProvider interface and shared HTTP error mapping.

To add a provider:
1. subclass ``SearchProvider`` in a new module, implement ``from_settings`` and ``search``;
2. add it to ``registry.PROVIDERS``.
``SearchService`` never needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import httpx

from research_agent.config import Settings
from research_agent.core.errors import (
    ProviderAuthError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from research_agent.core.models import SearchOptions, SearchResult


class SearchProvider(ABC):
    name: ClassVar[str]
    required_settings: ClassVar[tuple[str, ...]]
    """Settings attribute names that must be non-empty; reported upper-cased as env vars."""

    @classmethod
    def missing_settings(cls, settings: Settings) -> list[str]:
        return [
            attr.upper() for attr in cls.required_settings if getattr(settings, attr, None) is None
        ]

    @classmethod
    @abstractmethod
    def from_settings(cls, settings: Settings, client: httpx.AsyncClient) -> SearchProvider:
        """Build the provider. Must raise ``ProviderConfigurationError`` when misconfigured."""

    @abstractmethod
    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        """Return normalized results. Raises ``ProviderError`` subclasses on failure."""


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def send_json_request(
    provider: str, client: httpx.AsyncClient, request: httpx.Request, timeout_s: float
) -> Any:
    """Send ``request`` and return decoded JSON, mapping failures to typed provider errors.

    Error messages are built here and never include the request URL/headers, because they
    may carry credentials.
    """
    try:
        response = await client.send(request, auth=None, follow_redirects=False)
    except httpx.TimeoutException:
        raise ProviderUnavailableError(provider, f"timeout after {timeout_s}s") from None
    except httpx.TransportError as exc:
        raise ProviderUnavailableError(provider, f"transport error: {type(exc).__name__}") from None

    status = response.status_code
    if status in (401, 403):
        raise ProviderAuthError(
            provider, f"credentials rejected (HTTP {status})", status_code=status
        )
    if status == 429:
        raise ProviderRateLimitedError(
            provider,
            "rate limited (HTTP 429)",
            retry_after_s=parse_retry_after(response.headers.get("retry-after")),
        )
    if status >= 500:
        raise ProviderUnavailableError(
            provider, f"server error (HTTP {status})", status_code=status
        )
    if status >= 400:
        raise ProviderRequestError(
            provider, f"request rejected (HTTP {status})", status_code=status
        )
    if not 200 <= status < 300:
        raise ProviderResponseError(
            provider, f"unexpected HTTP status {status}", status_code=status
        )

    try:
        return response.json()
    except ValueError:
        raise ProviderResponseError(provider, "response body is not valid JSON") from None


def parse_date(value: str | None) -> datetime | None:
    """Parse ISO-8601 date or datetime leniently; unparseable → ``None`` (never guessed)."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return None
        parsed = datetime(d.year, d.month, d.day)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
