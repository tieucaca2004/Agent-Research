"""Google Programmable Search Engine — Custom Search JSON API adapter (fallback provider).

API contract (verified 2026-09-26 against the official discovery document
``customsearch.v1.json`` shipped in ``google-api-python-client`` v2.200.0, revision 20240821;
developers.google.com was not reachable from the build environment). See docs/providers.md.

    GET https://customsearch.googleapis.com/customsearch/v1
    Query parameters (used here):
        key   API key (required unless OAuth)
        cx    Programmable Search Engine ID
        q     query
        num   1..10
        gl    two-letter country code (boosts results from that country)
        hl    interface language
    ``lr`` is not used: its documented value list has no Vietnamese (``lang_vi``).

    Response 200 (``Search`` schema, fields used here):
        { "items": [ {"title": str, "link": str, "snippet": str, "displayLink": str,
                      "pagemap": object} ],        # "items" absent when no results
          "searchInformation": {"totalResults": str, ...} }

AVAILABILITY WARNING: Google states the Custom Search JSON API is closed to new customers
and will be discontinued on 2027-01-01. Only pre-existing credentials can work.

Live behaviour is UNVERIFIED until the live test runs with real credentials.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

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

GOOGLE_CSE_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"
GOOGLE_MAX_RESULTS = 10
_PUBLISHED_META_KEYS = ("article:published_time", "og:published_time", "datepublished")


class _GoogleItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    link: str
    snippet: str | None = None
    displayLink: str | None = None  # provider field name
    pagemap: dict[str, Any] | None = None


class _GoogleResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_GoogleItem] = []


def _published_from_pagemap(pagemap: dict[str, Any] | None) -> str | None:
    if not pagemap:
        return None
    metatags = pagemap.get("metatags")
    if not isinstance(metatags, list):
        return None
    for tags in metatags:
        if not isinstance(tags, dict):
            continue
        for key in _PUBLISHED_META_KEYS:
            value = tags.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


class GoogleSearchProvider(SearchProvider):
    name = "google"
    required_settings = ("google_api_key", "google_cse_id")

    def __init__(
        self,
        api_key: str,
        cse_id: str,
        client: httpx.AsyncClient,
        endpoint: str = GOOGLE_CSE_ENDPOINT,
    ) -> None:
        missing = [n for n, v in (("GOOGLE_API_KEY", api_key), ("GOOGLE_CSE_ID", cse_id)) if not v]
        if missing:
            raise ProviderConfigurationError(self.name, missing)
        self._api_key = api_key
        self._cse_id = cse_id
        self._client = client
        self._endpoint = endpoint

    def __repr__(self) -> str:  # never expose the key
        return f"GoogleSearchProvider(endpoint={self._endpoint!r})"

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.AsyncClient) -> SearchProvider:
        missing = cls.missing_settings(settings)
        if missing or settings.google_api_key is None or settings.google_cse_id is None:
            raise ProviderConfigurationError(cls.name, missing)
        return cls(settings.google_api_key.get_secret_value(), settings.google_cse_id, client)

    def build_request(self, query: str, options: SearchOptions) -> httpx.Request:
        params: dict[str, str | int] = {
            "key": self._api_key,
            "cx": self._cse_id,
            "q": query,
            "num": min(options.max_results, GOOGLE_MAX_RESULTS),
        }
        if options.country:
            params["gl"] = options.country.lower()
        if options.language:
            params["hl"] = options.language
        return self._client.build_request(
            "GET", self._endpoint, params=params, timeout=options.timeout_s
        )

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        payload = await send_json_request(
            self.name, self._client, self.build_request(query, options), options.timeout_s
        )
        if not isinstance(payload, dict):
            raise ProviderResponseError(self.name, "response is not a JSON object")
        try:
            parsed = _GoogleResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderResponseError(
                self.name, f"response does not match contract ({exc.error_count()} errors)"
            ) from None

        results: list[SearchResult] = []
        skipped = 0
        for index, item in enumerate(parsed.items, start=1):
            try:
                url = normalize_url(item.link)
            except InvalidURLError:
                skipped += 1
                continue
            metadata = {"display_link": item.displayLink} if item.displayLink else {}
            results.append(
                SearchResult(
                    title=item.title.strip(),
                    url=url,
                    original_url=item.link,
                    snippet=(item.snippet or None),
                    source=self.name,
                    published_at=parse_date(_published_from_pagemap(item.pagemap)),
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
