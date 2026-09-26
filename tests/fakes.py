"""Test-only doubles. Never registered in production configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import httpx

from research_agent.config import Settings
from research_agent.core.errors import ProviderError
from research_agent.core.models import SearchOptions, SearchResult
from research_agent.core.urls import normalize_url
from research_agent.providers.search.base import SearchProvider

Step = list[str] | ProviderError | Callable[[str], list[str]]


def make_result(url: str, *, source: str, query: str, rank: int = 1) -> SearchResult:
    return SearchResult(
        title=f"title {url}",
        url=normalize_url(url),
        original_url=url,
        snippet=None,
        source=source,
        rank=rank,
        query=query,
    )


class ScriptedSearchProvider(SearchProvider):
    """Returns scripted outcomes in order; the last step repeats."""

    required_settings = ()

    def __init__(self, name: str, steps: Sequence[Step], delay_s: float = 0.0) -> None:
        self.name = name  # type: ignore[misc]
        self._steps = list(steps)
        self._delay_s = delay_s
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.AsyncClient) -> SearchProvider:
        raise NotImplementedError

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        self.calls.append(query)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            step = self._steps[min(len(self.calls) - 1, len(self._steps) - 1)]
            if isinstance(step, ProviderError):
                raise step
            urls = step(query) if callable(step) else step
            return [
                make_result(u, source=self.name, query=query, rank=i)
                for i, u in enumerate(urls, start=1)
            ]
        finally:
            self.in_flight -= 1


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
