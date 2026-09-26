import pytest

from research_agent.core.errors import (
    AllSearchProvidersFailedError,
    NoSearchProviderConfiguredError,
    ProviderAuthError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from research_agent.core.models import SearchOptions
from research_agent.pipeline.search import SearchService
from tests.fakes import RecordingSleep, ScriptedSearchProvider

OPTS = SearchOptions()


def down(name: str) -> ProviderUnavailableError:
    return ProviderUnavailableError(name, "server error (HTTP 503)", status_code=503)


def service(*providers: ScriptedSearchProvider, **kwargs: object) -> SearchService:
    kwargs.setdefault("sleep", RecordingSleep())
    return SearchService(list(providers), **kwargs)  # type: ignore[arg-type]


def test_requires_at_least_one_provider() -> None:
    with pytest.raises(NoSearchProviderConfiguredError):
        SearchService([])


def test_rejects_duplicate_provider_names() -> None:
    a = ScriptedSearchProvider("x", [[]])
    b = ScriptedSearchProvider("x", [[]])
    with pytest.raises(ValueError, match="duplicate"):
        SearchService([a, b])


async def test_rejects_empty_queries() -> None:
    svc = service(ScriptedSearchProvider("p", [[]]))
    with pytest.raises(ValueError):
        await svc.search(["", "   "], OPTS)


async def test_primary_success_does_not_call_fallback() -> None:
    primary = ScriptedSearchProvider("perplexity", [["https://a.example/"]])
    fallback = ScriptedSearchProvider("google", [["https://b.example/"]])
    run = await service(primary, fallback).search(["q"], OPTS)
    assert [r.url for r in run.results] == ["https://a.example/"]
    assert fallback.calls == []
    assert [(a.provider, a.status) for a in run.attempts] == [("perplexity", "OK")]


async def test_fallback_on_primary_failure() -> None:
    primary = ScriptedSearchProvider("perplexity", [ProviderAuthError("perplexity", "401")])
    fallback = ScriptedSearchProvider("google", [["https://b.example/"]])
    run = await service(primary, fallback).search(["q"], OPTS)
    assert [r.source for r in run.results] == ["google"]
    assert [(a.provider, a.status) for a in run.attempts] == [
        ("perplexity", "FAILED"),
        ("google", "OK"),
    ]
    assert run.attempts[0].error is not None
    assert run.attempts[0].error["code"] == "PROVIDER_AUTH_FAILED"


async def test_fallback_on_empty_primary() -> None:
    primary = ScriptedSearchProvider("perplexity", [[]])
    fallback = ScriptedSearchProvider("google", [["https://b.example/"]])
    run = await service(primary, fallback).search(["q"], OPTS)
    assert [a.status for a in run.attempts] == ["EMPTY", "OK"]
    assert len(run.results) == 1


async def test_all_empty_is_a_valid_empty_run() -> None:
    run = await service(
        ScriptedSearchProvider("a", [[]]), ScriptedSearchProvider("b", [[]])
    ).search(["q"], OPTS)
    assert run.results == []
    assert [a.status for a in run.attempts] == ["EMPTY", "EMPTY"]


async def test_all_providers_failing_raises_with_every_error() -> None:
    svc = service(
        ScriptedSearchProvider("perplexity", [down("perplexity")]),
        ScriptedSearchProvider("google", [ProviderResponseError("google", "malformed")]),
        max_retries=0,
    )
    with pytest.raises(AllSearchProvidersFailedError) as info:
        await svc.search(["q1", "q2"], OPTS)
    codes = {(e.provider, e.code) for e in info.value.errors}
    assert codes == {
        ("perplexity", "PROVIDER_UNAVAILABLE"),
        ("google", "PROVIDER_MALFORMED_RESPONSE"),
    }


async def test_partial_failure_across_queries_does_not_fail_run() -> None:
    provider = ScriptedSearchProvider(
        "p", [lambda q: ["https://ok.example/"] if q == "good" else []]
    )
    failing = ScriptedSearchProvider("f", [down("f")])
    run = await service(failing, provider, max_retries=0).search(["good", "bad"], OPTS)
    assert len(run.results) == 1


async def test_retryable_error_is_retried_with_backoff() -> None:
    sleep = RecordingSleep()
    provider = ScriptedSearchProvider("p", [down("p"), down("p"), ["https://a.example/"]])
    run = await service(provider, max_retries=2, base_backoff_s=0.5, sleep=sleep).search(
        ["q"], OPTS
    )
    assert len(provider.calls) == 3
    assert sleep.delays == [0.5, 1.0]
    assert run.attempts[0].status == "OK"
    assert run.attempts[0].tries == 3


async def test_retry_exhaustion_marks_failed_then_falls_back() -> None:
    primary = ScriptedSearchProvider("p", [down("p")])
    fallback = ScriptedSearchProvider("g", [["https://g.example/"]])
    run = await service(primary, fallback, max_retries=1).search(["q"], OPTS)
    assert len(primary.calls) == 2
    assert run.attempts[0].status == "FAILED"
    assert run.attempts[0].tries == 2
    assert run.results[0].source == "g"


async def test_non_retryable_error_is_not_retried() -> None:
    sleep = RecordingSleep()
    provider = ScriptedSearchProvider("p", [ProviderAuthError("p", "401"), ["https://a/"]])
    with pytest.raises(AllSearchProvidersFailedError):
        await service(provider, max_retries=3, sleep=sleep).search(["q"], OPTS)
    assert len(provider.calls) == 1
    assert sleep.delays == []


async def test_retry_after_is_honoured_and_capped() -> None:
    sleep = RecordingSleep()
    provider = ScriptedSearchProvider(
        "p",
        [
            ProviderRateLimitedError("p", "429", retry_after_s=4),
            ProviderRateLimitedError("p", "429", retry_after_s=9999),
            ["https://a.example/"],
        ],
    )
    await service(provider, max_retries=2, sleep=sleep).search(["q"], OPTS)
    assert sleep.delays == [4, 30.0]


async def test_circuit_breaker_stops_calling_failing_provider() -> None:
    primary = ScriptedSearchProvider("p", [down("p")])
    fallback = ScriptedSearchProvider("g", [["https://g.example/"]])
    svc = service(primary, fallback, max_retries=0, circuit_breaker_threshold=2, concurrency=1)
    run = await svc.search(["q1", "q2", "q3", "q4"], OPTS)
    assert len(primary.calls) == 2
    assert [a.status for a in run.attempts if a.provider == "p"] == [
        "FAILED",
        "FAILED",
        "SKIPPED_CIRCUIT_OPEN",
        "SKIPPED_CIRCUIT_OPEN",
    ]
    assert len(fallback.calls) == 4


async def test_fanout_queries_all_providers_and_dedups() -> None:
    a = ScriptedSearchProvider("a", [["https://x.example/?utm_source=a", "https://y.example/"]])
    b = ScriptedSearchProvider("b", [["https://X.example/", "https://z.example/"]])
    run = await service(a, b, strategy="fanout").search(["q"], OPTS)
    assert len(run.hits) == 4
    assert [r.url for r in run.results] == [
        "https://x.example/",
        "https://y.example/",
        "https://z.example/",
    ]
    assert run.results[0].source == "a"  # first occurrence wins deterministically
    assert run.duplicate_count == 1


async def test_duplicate_urls_across_queries_keep_full_provenance() -> None:
    p = ScriptedSearchProvider("p", [["https://a.example/", "https://b.example/"]])
    run = await service(p).search(["q1", "q2"], OPTS)
    assert len(run.results) == 2
    assert run.duplicate_count == 2
    assert {(h.query, h.url) for h in run.hits} == {
        ("q1", "https://a.example/"),
        ("q1", "https://b.example/"),
        ("q2", "https://a.example/"),
        ("q2", "https://b.example/"),
    }
    assert all(r.query == "q1" for r in run.results)


async def test_queries_are_whitespace_normalized_and_deduped() -> None:
    p = ScriptedSearchProvider("p", [["https://a.example/"]])
    await service(p).search(["  món   Nhật ", "món Nhật", ""], OPTS)
    assert p.calls == ["món Nhật"]


async def test_concurrency_limit_is_respected() -> None:
    p = ScriptedSearchProvider("p", [["https://a.example/"]], delay_s=0.01)
    await service(p, concurrency=2).search([f"q{i}" for i in range(8)], OPTS)
    assert p.max_in_flight == 2


async def test_new_provider_plugs_in_without_service_changes() -> None:
    class BraveLikeProvider(ScriptedSearchProvider):
        pass

    new = BraveLikeProvider("brave", [["https://new.example/"]])
    run = await service(new).search(["q"], OPTS)
    assert run.results[0].source == "brave"
