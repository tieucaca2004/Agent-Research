"""Sprint 01 delta: SearchResponse, per-provider status, service-level timeout,
provenance-preserving dedup, deterministic ordering, request ID and query hashing."""

import json

import pytest
import structlog
from pydantic import ValidationError

from research_agent.core.errors import (
    AllSearchProvidersFailedError,
    ProviderAuthError,
    ProviderUnavailableError,
)
from research_agent.core.models import (
    DedupedSearchResult,
    ProviderExecutionStatus,
    SearchOptions,
    SearchResponse,
    SearchResult,
)
from research_agent.logging import configure_logging
from research_agent.pipeline.search import SearchAttempt, SearchRun, SearchService, query_hash
from tests.fakes import RecordingSleep, ScriptedSearchProvider, make_result

OPTS = SearchOptions()


def service(*providers: ScriptedSearchProvider, **kwargs: object) -> SearchService:
    kwargs.setdefault("sleep", RecordingSleep())
    return SearchService(list(providers), **kwargs)  # type: ignore[arg-type]


def statuses(response: SearchResponse) -> dict[str, ProviderExecutionStatus]:
    return {s.provider: s for s in response.provider_statuses}


# --- per-provider status -------------------------------------------------------------


async def test_fallback_default_marks_unused_fallback_not_called() -> None:
    primary = ScriptedSearchProvider("perplexity", [["https://a.example/"]])
    fallback = ScriptedSearchProvider("google", [["https://b.example/"]])
    response = (await service(primary, fallback).search(["q"], OPTS)).to_response()

    assert response.strategy == "fallback"
    st = statuses(response)
    assert st["perplexity"].status == "SUCCESS"
    assert st["perplexity"].calls == 1
    assert st["google"].status == "NOT_CALLED"
    assert st["google"].calls == 0
    assert [s.provider for s in response.provider_statuses] == ["perplexity", "google"]


async def test_fanout_success_plus_timeout_isolated() -> None:
    """Perplexity = SUCCESS, Google = TIMEOUT → Perplexity results + Google status TIMEOUT."""
    perplexity = ScriptedSearchProvider("perplexity", [["https://a.example/"]])
    google = ScriptedSearchProvider("google", [["https://never.example/"]], delay_s=5)
    svc = service(perplexity, google, strategy="fanout", max_retries=0)

    response = (await svc.search(["q"], SearchOptions(timeout_s=0.05))).to_response()

    assert [r.result.url for r in response.results] == ["https://a.example/"]
    st = statuses(response)
    assert st["perplexity"].status == "SUCCESS"
    assert st["google"].status == "FAILED"
    assert st["google"].error_categories == ["TIMEOUT"]


async def test_status_partial_empty_and_skipped() -> None:
    flaky = ScriptedSearchProvider(
        "flaky", [["https://a.example/"], ProviderAuthError("flaky", "401")]
    )
    empty = ScriptedSearchProvider("empty", [[]])
    svc = service(flaky, empty, strategy="fanout", concurrency=1)
    st = statuses((await svc.search(["q1", "q2"], OPTS)).to_response())
    assert st["flaky"].status == "PARTIAL"
    assert (st["flaky"].succeeded, st["flaky"].failed) == (1, 1)
    assert st["flaky"].error_categories == ["AUTHENTICATION_ERROR"]
    assert st["empty"].status == "EMPTY"

    broken = ScriptedSearchProvider("broken", [ProviderAuthError("broken", "401")])
    backup = ScriptedSearchProvider("backup", [["https://b.example/"]])
    svc = service(broken, backup, circuit_breaker_threshold=1, concurrency=1)
    st = statuses((await svc.search(["q1", "q2", "q3"], OPTS)).to_response())
    assert st["broken"].status == "FAILED"
    assert (st["broken"].calls, st["broken"].skipped) == (1, 2)


async def test_all_skipped_provider_reports_skipped() -> None:
    run = await service(ScriptedSearchProvider("p", [["https://a.example/"]])).search(["q"], OPTS)
    run.attempts.clear()
    run.attempts.append(SearchAttempt("p", "q", "SKIPPED_CIRCUIT_OPEN", 0, 0, 0))
    assert statuses(run.to_response())["p"].status == "SKIPPED"


# --- service-level timeout -----------------------------------------------------------


async def test_service_timeout_bounds_hanging_provider_and_is_retried() -> None:
    sleep = RecordingSleep()
    slow = ScriptedSearchProvider("slow", [["https://x.example/"]], delay_s=5)
    svc = service(slow, max_retries=1, sleep=sleep)
    with pytest.raises(AllSearchProvidersFailedError) as info:
        await svc.search(["q"], SearchOptions(timeout_s=0.05))
    [error] = info.value.errors
    assert isinstance(error, ProviderUnavailableError)  # backward compatible type
    assert error.code == "PROVIDER_UNAVAILABLE"
    assert error.category == "TIMEOUT"
    assert len(slow.calls) == 2  # retried once
    assert len(sleep.delays) == 1


# --- dedup + provenance + deterministic ordering --------------------------------------


async def test_same_url_from_two_providers_keeps_both_in_provenance() -> None:
    a = ScriptedSearchProvider("perplexity", [["https://x.example/p#frag", "https://y.example/"]])
    b = ScriptedSearchProvider(
        "google", [["https://X.example/p?utm_source=g", "https://z.example/"]]
    )
    response = (await service(a, b, strategy="fanout").search(["q"], OPTS)).to_response()

    assert [r.result.url for r in response.results] == [
        "https://x.example/p",
        "https://y.example/",
        "https://z.example/",
    ]
    first = response.results[0]
    assert first.providers == ["perplexity", "google"]
    assert first.occurrences == 2
    assert first.result.source == "perplexity"  # canonical = higher-priority provider
    assert first.result.original_url == "https://x.example/p#frag"
    assert response.total_hits == 4
    assert response.duplicate_count == 1


async def test_same_url_across_queries_records_all_queries() -> None:
    p = ScriptedSearchProvider("p", [["https://a.example/"]])
    response = (await service(p).search(["q1", "q2"], OPTS)).to_response()
    [only] = response.results
    assert only.queries == ["q1", "q2"]
    assert only.result.query == "q1"


async def test_ordering_is_deterministic_regardless_of_completion_order() -> None:
    # google (lower priority) finishes first; perplexity is slower.
    slow_primary = ScriptedSearchProvider(
        "perplexity", [["https://p1.example/", "https://shared.example/"]], delay_s=0.03
    )
    fast_secondary = ScriptedSearchProvider(
        "google", [["https://shared.example/", "https://g1.example/"]]
    )
    expected = [
        "https://p1.example/",
        "https://shared.example/",
        "https://g1.example/",
    ]
    for _ in range(3):
        svc = service(slow_primary, fast_secondary, strategy="fanout")
        response = (await svc.search(["q"], OPTS)).to_response()
        assert [r.result.url for r in response.results] == expected
        assert response.results[1].providers == ["perplexity", "google"]


def test_to_response_sorts_out_of_order_hits() -> None:
    run = SearchRun(
        request_id="r",
        queries=["q1", "q2"],
        providers=["perplexity", "google"],
        hits=[
            make_result("https://c.example/", source="google", query="q2", rank=1),
            make_result("https://b.example/", source="google", query="q1", rank=1),
            make_result("https://a.example/", source="perplexity", query="q1", rank=2),
            make_result("https://z.example/", source="perplexity", query="q1", rank=1),
        ],
    )
    urls = [r.result.url for r in run.to_response().results]
    assert urls == [
        "https://z.example/",
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
    ]


async def test_legacy_run_api_unchanged() -> None:
    p = ScriptedSearchProvider("p", [["https://a.example/", "https://b.example/"]])
    run = await service(p).search(["q1", "q2"], OPTS)
    assert [r.url for r in run.results] == ["https://a.example/", "https://b.example/"]
    assert run.duplicate_count == 2
    assert len(run.hits) == 4


# --- request ID + query hashing -------------------------------------------------------


async def test_request_id_propagates_to_response_and_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_query = "Nguyễn Văn A 0901234567 sushi"
    configure_logging("INFO", "json")
    try:
        p = ScriptedSearchProvider("p", [ProviderAuthError("p", "401")])
        g = ScriptedSearchProvider("g", [["https://a.example/"]])
        run = await service(p, g).search([secret_query], OPTS, request_id="req-abc-123")
        output = capsys.readouterr().out
    finally:
        structlog.reset_defaults()

    assert run.to_response().request_id == "req-abc-123"
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert records, "expected structured log output"
    assert all(r.get("request_id") == "req-abc-123" for r in records)
    assert secret_query not in output
    assert "0901234567" not in output
    provider_events = [
        r for r in records if "provider" in r and r["event"] != "search.run_completed"
    ]
    assert provider_events
    assert all(r["query_hash"] == query_hash(secret_query) for r in provider_events)
    failed = [r for r in records if r["event"] == "search.provider_failed"]
    assert failed[0]["error"]["category"] == "AUTHENTICATION_ERROR"


async def test_request_id_generated_when_missing() -> None:
    run = await service(ScriptedSearchProvider("p", [["https://a/"]])).search(["q"], OPTS)
    assert len(run.request_id) == 32
    int(run.request_id, 16)


def test_query_hash_is_stable_and_not_reversible() -> None:
    assert query_hash("món Nhật") == query_hash("món Nhật")
    assert query_hash("món Nhật") != query_hash("món Hàn")
    assert len(query_hash("x")) == 16
    assert "Nhật" not in query_hash("món Nhật")


# --- models / schema -----------------------------------------------------------------


def test_search_result_valid_with_provider_metadata() -> None:
    result = make_result("https://a.example/x", source="perplexity", query="q")
    enriched = result.model_copy(update={"metadata": {"provider_request_id": "r1"}})
    assert enriched.metadata == {"provider_request_id": "r1"}
    assert json.loads(enriched.model_dump_json())["source"] == "perplexity"


@pytest.mark.parametrize(
    "overrides",
    [
        {"rank": 0},
        {"rank": -1},
        {"url": None},
        {"title": None},
        {"unexpected_field": "x"},
        {"metadata": {"k": 1}},
    ],
)
def test_search_result_rejects_invalid(overrides: dict[str, object]) -> None:
    base: dict[str, object] = {
        "title": "t",
        "url": "https://a.example/",
        "original_url": "https://a.example/",
        "source": "p",
        "rank": 1,
        "query": "q",
    }
    with pytest.raises(ValidationError):
        SearchResult.model_validate({**base, **overrides})


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_results": 0},
        {"max_results": 51},
        {"language": "vie"},
        {"language": "VI"},
        {"country": "vn"},
        {"timeout_s": 0},
        {"timeout_s": 121},
        {"extra": 1},
    ],
)
def test_search_options_rejects_invalid(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchOptions.model_validate(overrides)


def test_response_models_forbid_unknown_fields_and_bad_status() -> None:
    with pytest.raises(ValidationError):
        ProviderExecutionStatus(
            provider="p",
            status="TIMEOUT",
            calls=1,
            succeeded=0,
            empty=0,
            failed=1,
            skipped=0,
            result_count=0,
            duration_ms=1,
        )
    with pytest.raises(ValidationError):
        DedupedSearchResult(
            result=make_result("https://a/", source="p", query="q"),
            providers=["p"],
            queries=["q"],
            occurrences=0,
        )
    with pytest.raises(ValidationError):
        SearchResponse.model_validate(
            {
                "request_id": "r",
                "strategy": "random",
                "queries": [],
                "results": [],
                "provider_statuses": [],
                "total_hits": 0,
                "duplicate_count": 0,
            }
        )
