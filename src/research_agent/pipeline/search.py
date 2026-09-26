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

Timeouts: besides the HTTP client's per-operation timeout inside each adapter, every provider
try is bounded by ``options.timeout_s`` of wall-clock time at service level
(``ProviderTimeoutError``, category ``TIMEOUT``, retryable).

Observability: each run has a ``request_id`` bound to every log line; queries are logged
only as ``query_hash`` (SHA-256 prefix) + length, never verbatim.

``SearchRun.to_response()`` builds the ``SearchResponse`` with deterministic ordering:
hits sorted by (query order, provider priority, provider rank); the first hit per normalized
URL is canonical and every contributing provider/query is kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import httpx
import structlog

from research_agent.config import Settings
from research_agent.core.errors import (
    AllSearchProvidersFailedError,
    NoSearchProviderConfiguredError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)
from research_agent.core.models import (
    DedupedSearchResult,
    ProviderExecutionStatus,
    ProviderRunStatus,
    SearchOptions,
    SearchResponse,
    SearchResult,
)
from research_agent.logging import get_logger
from research_agent.providers.search.base import SearchProvider
from research_agent.providers.search.registry import build_search_providers

log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
MAX_BACKOFF_S = 30.0

AttemptStatus = Literal["OK", "EMPTY", "FAILED", "SKIPPED_CIRCUIT_OPEN"]
Strategy = Literal["fallback", "fanout"]


def query_hash(query: str) -> str:
    """Stable, non-reversible identifier for logging a query without its text."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


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
    request_id: str = ""
    strategy: Strategy = "fallback"
    queries: list[str] = field(default_factory=list)
    """Cleaned queries in execution order."""
    providers: list[str] = field(default_factory=list)
    """Provider names in priority order."""

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

    def _order_key(self, index: int, hit: SearchResult) -> tuple[int, int, int, int]:
        q = self.queries.index(hit.query) if hit.query in self.queries else len(self.queries)
        p = (
            self.providers.index(hit.source)
            if hit.source in self.providers
            else len(self.providers)
        )
        return (q, p, hit.rank, index)

    def to_response(self) -> SearchResponse:
        ordered = [
            hit
            for _, hit in sorted(
                enumerate(self.hits), key=lambda pair: self._order_key(pair[0], pair[1])
            )
        ]
        groups: dict[str, list[SearchResult]] = {}
        for hit in ordered:
            groups.setdefault(hit.url, []).append(hit)
        results = [
            DedupedSearchResult(
                result=group[0],
                providers=_unique([h.source for h in group]),
                queries=_unique([h.query for h in group]),
                occurrences=len(group),
            )
            for group in groups.values()
        ]
        return SearchResponse(
            request_id=self.request_id,
            strategy=self.strategy,
            queries=list(self.queries),
            results=results,
            provider_statuses=[self._provider_status(name) for name in self.providers],
            total_hits=len(self.hits),
            duplicate_count=len(self.hits) - len(results),
        )

    def _provider_status(self, provider: str) -> ProviderExecutionStatus:
        attempts = [a for a in self.attempts if a.provider == provider]
        skipped = sum(1 for a in attempts if a.status == "SKIPPED_CIRCUIT_OPEN")
        succeeded = sum(1 for a in attempts if a.status == "OK")
        empty = sum(1 for a in attempts if a.status == "EMPTY")
        failed = sum(1 for a in attempts if a.status == "FAILED")
        calls = succeeded + empty + failed
        status: ProviderRunStatus
        if not attempts:
            status = "NOT_CALLED"
        elif calls == 0:
            status = "SKIPPED"
        elif failed == calls:
            status = "FAILED"
        elif failed:
            status = "PARTIAL"
        elif succeeded:
            status = "SUCCESS"
        else:
            status = "EMPTY"
        categories = sorted(
            {str(a.error.get("category")) for a in attempts if a.error and a.error.get("category")}
        )
        return ProviderExecutionStatus(
            provider=provider,
            status=status,
            calls=calls,
            succeeded=succeeded,
            empty=empty,
            failed=failed,
            skipped=skipped,
            result_count=sum(a.result_count for a in attempts),
            duration_ms=sum(a.duration_ms for a in attempts),
            error_categories=categories,
        )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


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
        strategy: Strategy = "fallback",
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

    async def search(
        self,
        queries: Sequence[str],
        options: SearchOptions,
        *,
        request_id: str | None = None,
    ) -> SearchRun:
        request_id = request_id or uuid.uuid4().hex
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            return await self._search(queries, options, request_id)

    async def _search(
        self, queries: Sequence[str], options: SearchOptions, request_id: str
    ) -> SearchRun:
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

        run = SearchRun(
            request_id=request_id,
            strategy=self._strategy,
            queries=cleaned,
            providers=self.provider_names,
        )
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
            log.warning("search.circuit_open", provider=provider.name, query_hash=query_hash(query))
            return [], SearchAttempt(provider.name, query, "SKIPPED_CIRCUIT_OPEN", 0, 0, 0)

        started = time.monotonic()
        tries = 0
        while True:
            tries += 1
            try:
                hits = await self._bounded_search(provider, query, options)
            except ProviderError as exc:
                if exc.retryable and tries <= self._max_retries:
                    delay = self._backoff(tries, exc)
                    log.warning(
                        "search.retry",
                        provider=provider.name,
                        query_hash=query_hash(query),
                        error_code=exc.code,
                        error_category=exc.category,
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
                    query_hash=query_hash(query),
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
                query_hash=query_hash(query),
                query_len=len(query),
                status=status,
                results=len(hits),
                duration_ms=duration_ms,
                tries=tries,
            )
            return hits, SearchAttempt(provider.name, query, status, len(hits), duration_ms, tries)

    @staticmethod
    async def _bounded_search(
        provider: SearchProvider, query: str, options: SearchOptions
    ) -> list[SearchResult]:
        try:
            async with asyncio.timeout(options.timeout_s):
                return await provider.search(query, options)
        except TimeoutError:
            raise ProviderTimeoutError(
                provider.name, f"service-level timeout after {options.timeout_s}s"
            ) from None

    def _backoff(self, tries: int, exc: ProviderError) -> float:
        if isinstance(exc, ProviderRateLimitedError) and exc.retry_after_s is not None:
            return min(exc.retry_after_s, MAX_BACKOFF_S)
        return min(self._base_backoff_s * (2.0 ** (tries - 1)), MAX_BACKOFF_S)


def build_search_service(settings: Settings, client: httpx.AsyncClient) -> SearchService:
    """Wire a ``SearchService`` from configuration.

    Raises ``NoSearchProviderConfiguredError`` (code ``REQUIRES_CONFIGURATION``) when no
    provider in ``SEARCH_PROVIDERS`` has its credentials set.
    """
    log.info("search.configuration", **settings.safe_summary())
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
