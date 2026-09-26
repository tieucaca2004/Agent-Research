"""SearchService: runs a set of queries against the configured providers.

Depends only on the ``SearchProvider`` interface — adding a provider never changes this module.

Strategies:
- ``fallback`` (default): per query, providers are tried in priority order; the next one is
  used only if the previous one failed or returned zero results.
- ``fanout``: every provider is queried and results are merged.

Reliability:
- retryable errors (timeout, transport, 5xx, 429) are retried up to ``max_retries`` times
  with exponential backoff (``Retry-After`` honoured, capped);
- a per-run circuit breaker stops calling a provider after ``circuit_breaker_threshold``
  consecutive failed calls;
- one provider failing does not fail the run; only if *every* attempt failed and nothing
  was found is ``AllSearchProvidersFailedError`` raised.

Every provider call is recorded as a ``SearchAttempt`` (provenance + observability).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import httpx

from research_agent.config import Settings
from research_agent.core.errors import (
    AllSearchProvidersFailedError,
    NoSearchProviderConfiguredError,
    ProviderError,
    ProviderRateLimitedError,
)
from research_agent.core.models import SearchOptions, SearchResult
from research_agent.logging import get_logger
from research_agent.providers.search.base import SearchProvider
from research_agent.providers.search.registry import build_search_providers

log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
MAX_BACKOFF_S = 30.0

AttemptStatus = Literal["OK", "EMPTY", "FAILED", "SKIPPED_CIRCUIT_OPEN"]


@dataclass(frozen=True)
class SearchAttempt:
    provider: str
    query: str
    status: AttemptStatus
    result_count: int
    duration_ms: int
    tries: int
    error: dict[str, object] | None = None
    exception: ProviderError | None = field(default=None, repr=False, compare=False)


@dataclass
class SearchRun:
    hits: list[SearchResult] = field(default_factory=list)
    """Every normalized hit, including duplicates (full provenance)."""
    attempts: list[SearchAttempt] = field(default_factory=list)

    @property
    def results(self) -> list[SearchResult]:
        """Hits unique by normalized URL; first occurrence (query order, then rank) wins."""
        seen: set[str] = set()
        unique: list[SearchResult] = []
        for hit in self.hits:
            if hit.url not in seen:
                seen.add(hit.url)
                unique.append(hit)
        return unique

    @property
    def duplicate_count(self) -> int:
        return len(self.hits) - len(self.results)

    @property
    def errors(self) -> list[dict[str, object]]:
        return [a.error for a in self.attempts if a.error is not None]


class _CircuitBreaker:
    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._failures: dict[str, int] = {}

    def is_open(self, provider: str) -> bool:
        return self._failures.get(provider, 0) >= self._threshold

    def record(self, provider: str, *, success: bool) -> None:
        self._failures[provider] = 0 if success else self._failures.get(provider, 0) + 1


class SearchService:
    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        strategy: Literal["fallback", "fanout"] = "fallback",
        max_retries: int = 1,
        concurrency: int = 4,
        circuit_breaker_threshold: int = 3,
        base_backoff_s: float = 0.5,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not providers:
            raise NoSearchProviderConfiguredError([])
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate providers: {names}")
        self._providers = list(providers)
        self._strategy = strategy
        self._max_retries = max_retries
        self._concurrency = concurrency
        self._breaker_threshold = circuit_breaker_threshold
        self._base_backoff_s = base_backoff_s
        self._sleep = sleep

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    async def search(self, queries: Sequence[str], options: SearchOptions) -> SearchRun:
        cleaned: list[str] = []
        for q in queries:
            q = " ".join(q.split())
            if q and q not in cleaned:
                cleaned.append(q)
        if not cleaned:
            raise ValueError("at least one non-empty query is required")

        breaker = _CircuitBreaker(self._breaker_threshold)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def run_query(query: str) -> tuple[list[SearchResult], list[SearchAttempt]]:
            async with semaphore:
                if self._strategy == "fanout":
                    return await self._fanout(query, options, breaker)
                return await self._fallback(query, options, breaker)

        per_query = await asyncio.gather(*(run_query(q) for q in cleaned))

        run = SearchRun()
        for hits, attempts in per_query:  # preserves query order → deterministic dedup
            run.hits.extend(hits)
            run.attempts.extend(attempts)

        if not run.hits and all(
            a.status in ("FAILED", "SKIPPED_CIRCUIT_OPEN") for a in run.attempts
        ):
            raise AllSearchProvidersFailedError(
                [a.exception for a in run.attempts if a.exception is not None]
            )
        log.info(
            "search.run_completed",
            queries=len(cleaned),
            hits=len(run.hits),
            unique=len(run.results),
            duplicates=run.duplicate_count,
            failed_attempts=sum(1 for a in run.attempts if a.status == "FAILED"),
        )
        return run

    async def _fallback(
        self, query: str, options: SearchOptions, breaker: _CircuitBreaker
    ) -> tuple[list[SearchResult], list[SearchAttempt]]:
        attempts: list[SearchAttempt] = []
        for provider in self._providers:
            hits, attempt = await self._call(provider, query, options, breaker)
            attempts.append(attempt)
            if hits:
                return hits, attempts
        return [], attempts

    async def _fanout(
        self, query: str, options: SearchOptions, breaker: _CircuitBreaker
    ) -> tuple[list[SearchResult], list[SearchAttempt]]:
        outcomes = await asyncio.gather(
            *(self._call(p, query, options, breaker) for p in self._providers)
        )
        hits = [h for provider_hits, _ in outcomes for h in provider_hits]
        return hits, [attempt for _, attempt in outcomes]

    async def _call(
        self,
        provider: SearchProvider,
        query: str,
        options: SearchOptions,
        breaker: _CircuitBreaker,
    ) -> tuple[list[SearchResult], SearchAttempt]:
        if breaker.is_open(provider.name):
            log.warning("search.circuit_open", provider=provider.name, query=query)
            return [], SearchAttempt(provider.name, query, "SKIPPED_CIRCUIT_OPEN", 0, 0, 0)

        started = time.monotonic()
        tries = 0
        while True:
            tries += 1
            try:
                hits = await provider.search(query, options)
            except ProviderError as exc:
                if exc.retryable and tries <= self._max_retries:
                    delay = self._backoff(tries, exc)
                    log.warning(
                        "search.retry",
                        provider=provider.name,
                        query=query,
                        error_code=exc.code,
                        attempt=tries,
                        delay_s=delay,
                    )
                    await self._sleep(delay)
                    continue
                breaker.record(provider.name, success=False)
                duration_ms = int((time.monotonic() - started) * 1000)
                log.warning(
                    "search.provider_failed",
                    provider=provider.name,
                    query=query,
                    duration_ms=duration_ms,
                    error=exc.to_dict(),
                )
                return [], SearchAttempt(
                    provider.name, query, "FAILED", 0, duration_ms, tries, exc.to_dict(), exc
                )
            breaker.record(provider.name, success=True)
            duration_ms = int((time.monotonic() - started) * 1000)
            status: AttemptStatus = "OK" if hits else "EMPTY"
            log.info(
                "search.provider_call",
                provider=provider.name,
                query=query,
                status=status,
                results=len(hits),
                duration_ms=duration_ms,
                tries=tries,
            )
            return hits, SearchAttempt(provider.name, query, status, len(hits), duration_ms, tries)

    def _backoff(self, tries: int, exc: ProviderError) -> float:
        if isinstance(exc, ProviderRateLimitedError) and exc.retry_after_s is not None:
            return min(exc.retry_after_s, MAX_BACKOFF_S)
        return min(self._base_backoff_s * (2.0 ** (tries - 1)), MAX_BACKOFF_S)


def build_search_service(settings: Settings, client: httpx.AsyncClient) -> SearchService:
    """Wire a ``SearchService`` from configuration.

    Raises ``NoSearchProviderConfiguredError`` (code ``REQUIRES_CONFIGURATION``) when no
    provider in ``SEARCH_PROVIDERS`` has its credentials set.
    """
    return SearchService(
        build_search_providers(settings, client),
        strategy=settings.search_strategy,
        max_retries=settings.search_max_retries,
        concurrency=settings.search_concurrency,
        circuit_breaker_threshold=settings.search_circuit_breaker_threshold,
    )


def default_search_options(settings: Settings, **overrides: object) -> SearchOptions:
    return SearchOptions.model_validate(
        {
            "max_results": settings.search_max_results,
            "timeout_s": settings.search_timeout_s,
            **overrides,
        }
    )
