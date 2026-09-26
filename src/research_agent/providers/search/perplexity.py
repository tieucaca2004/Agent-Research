"""Perplexity Search API adapter.

API contract (verified 2026-09-26 against the official ``perplexityai`` Python SDK v0.43.6,
published 2026-09-25, "generated from our OpenAPI spec"; unchanged vs v0.42.0.
docs.perplexity.ai was not reachable from the build environment). See docs/providers.md.

    POST https://api.perplexity.ai/search
    Authorization: Bearer <PERPLEXITY_API_KEY>
    Content-Type: application/json

    Request body (fields used here):
        query                   str (required; SDK also allows list[str] — not used)
        max_results             int  (web search supports up to 20)
        country                 str | null
        search_language_filter  list[str] | null
    Other documented optional fields (not used in V1): max_tokens, max_tokens_per_page,
        search_context_size, search_domain_filter, search_recency_filter, search_mode,
        search_type, search_after/before_date_filter, last_updated_after/before_filter,
        display_server_time.

    Response 200:
        { "id": str,
          "results": [ {"title": str, "url": str, "snippet": str,
                        "date": str | null, "last_updated": str | null} ],
          "server_time": str | null }

Live behaviour is UNVERIFIED until the live test runs with a real key.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ValidationError

from research_agent.config import Settings
from research_agent.core.errors import (
    InvalidURLError,
    ProviderConfigurationError,
    ProviderResponseError,
)
from research_agent.core.models import SearchOptions, SearchResult
from research_agent.core.urls import normalize_url
from research_agent.logging import get_logger
from research_agent.providers.search.base import SearchProvider, parse_date, send_json_request

log = get_logger(__name__)

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
PERPLEXITY_MAX_RESULTS = 20


class _PerplexityResult(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    date: str | None = None
    last_updated: str | None = None


class _PerplexityResponse(BaseModel):
    id: str
    results: list[_PerplexityResult]
    server_time: str | None = None


class PerplexitySearchProvider(SearchProvider):
    name = "perplexity"
    required_settings = ("perplexity_api_key",)

    def __init__(
        self, api_key: str, client: httpx.AsyncClient, base_url: str = PERPLEXITY_BASE_URL
    ) -> None:
        if not api_key:
            raise ProviderConfigurationError(self.name, ["PERPLEXITY_API_KEY"])
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:  # never expose the key
        return f"PerplexitySearchProvider(base_url={self._base_url!r})"

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.AsyncClient) -> SearchProvider:
        missing = cls.missing_settings(settings)
        if missing or settings.perplexity_api_key is None:
            raise ProviderConfigurationError(cls.name, missing or ["PERPLEXITY_API_KEY"])
        return cls(settings.perplexity_api_key.get_secret_value(), client)

    def build_request(self, query: str, options: SearchOptions) -> httpx.Request:
        body: dict[str, object] = {
            "query": query,
            "max_results": min(options.max_results, PERPLEXITY_MAX_RESULTS),
        }
        if options.country:
            body["country"] = options.country
        if options.language:
            body["search_language_filter"] = [options.language]
        return self._client.build_request(
            "POST",
            f"{self._base_url}/search",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=options.timeout_s,
        )

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        payload = await send_json_request(
            self.name, self._client, self.build_request(query, options), options.timeout_s
        )
        try:
            parsed = _PerplexityResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderResponseError(
                self.name, f"response does not match contract ({exc.error_count()} errors)"
            ) from None

        results: list[SearchResult] = []
        skipped = 0
        for index, item in enumerate(parsed.results, start=1):
            try:
                url = normalize_url(item.url)
            except InvalidURLError:
                skipped += 1
                continue
            metadata = {"provider_request_id": parsed.id}
            if item.last_updated:
                metadata["last_updated"] = item.last_updated
            results.append(
                SearchResult(
                    title=item.title.strip(),
                    url=url,
                    original_url=item.url,
                    snippet=(item.snippet or None),
                    source=self.name,
                    published_at=parse_date(item.date),
                    rank=index,
                    query=query,
                    metadata=metadata,
                )
            )
        if skipped:
            log.warning(
                "search.results_skipped", provider=self.name, reason="invalid_url", count=skipped
            )
        return results
