"""Sprint 02: JobRunner lifecycle over the unchanged Sprint 01 SearchService.

Search providers are test doubles (or respx-mocked real adapters); nothing here calls the
internet or claims live-provider behaviour.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import datetime

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr

from research_agent.core.errors import (
    ProviderAuthError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from research_agent.core.models import SearchOptions, SearchResult
from research_agent.jobs import (
    InMemoryJobRepository,
    JobNotClaimableError,
    JobRunner,
    JobState,
    ResearchJob,
    ResearchJobRequest,
    ResearchPlan,
)
from research_agent.logging import configure_logging
from research_agent.pipeline.search import SearchRun, SearchService, build_search_service
from tests.conftest import FAKE_CSE_ID, FAKE_GOOGLE_KEY, FAKE_PPLX_KEY, build_settings
from tests.fakes import RecordingSleep, ScriptedSearchProvider

S = JobState
QUERY = "món Nhật Nha Trang"


class ControlledProvider(ScriptedSearchProvider):
    """Scripted provider that hangs (until cancelled) for selected queries."""

    def __init__(self, name: str, steps: Sequence[object], *, hang_on: Sequence[str] = ()) -> None:
        super().__init__(name, steps)  # type: ignore[arg-type]
        self.hang_on = set(hang_on)
        self.cancelled = 0
        self.hanging = asyncio.Event()

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if query in self.hang_on:
            self.calls.append(query)
            self.in_flight += 1
            self.hanging.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.in_flight -= 1
        return await super().search(query, options)


class ListPlanner:
    """Test planner producing several queries (the production FixedPlanner yields one)."""

    name = "test-list-v1"

    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: datetime
    ) -> ResearchPlan:
        return ResearchPlan(
            planner=self.name, queries=self.queries, search_options=base_options, created_at=now
        )


class BrokenPlanner:
    name = "broken"

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: datetime
    ) -> ResearchPlan:
        raise KeyError("boom")


def make(
    *providers: ScriptedSearchProvider,
    queries: list[str] | None = None,
    job_timeout_s: float = 30,
    stage_timeout_s: float = 30,
    max_retries: int = 0,
    provider_timeout_s: float = 10,
    sleep: object = None,
    planner: object = None,
) -> tuple[InMemoryJobRepository, JobRunner]:
    repo = InMemoryJobRepository()
    service = SearchService(
        list(providers),
        max_retries=max_retries,
        sleep=sleep or RecordingSleep(),  # type: ignore[arg-type]
    )
    runner = JobRunner(
        repo,
        service,
        base_options=SearchOptions(timeout_s=provider_timeout_s),
        planner=planner or (ListPlanner(queries) if queries else None),  # type: ignore[arg-type]
        job_timeout_s=job_timeout_s,
        search_stage_timeout_s=stage_timeout_s,
    )
    return repo, runner


async def submit_and_run(runner: JobRunner, query: str = QUERY) -> ResearchJob:
    job, _ = await runner.submit(ResearchJobRequest(query=query))
    return await runner.run(job.id)


async def state_path(repo: InMemoryJobRepository, job_id: str) -> list[str]:
    events = await repo.list_events(job_id)
    return ["QUEUED"] + [str(e.data["to"]) for e in events if e.type == "job.state_changed"]


def statuses(job: ResearchJob) -> list[str]:
    assert job.result is not None
    return [o.status for o in job.result.outcomes]


# --- lifecycle ---------------------------------------------------------------------


async def test_successful_job_lifecycle() -> None:
    p = ScriptedSearchProvider("perplexity", [["https://a.example/", "https://b.example/"]])
    g = ScriptedSearchProvider("google", [["https://c.example/"]])
    repo, runner = make(p, g)
    job, created = await runner.submit(ResearchJobRequest(query=QUERY))
    assert created and job.status is S.QUEUED

    done = await runner.run(job.id)

    assert done.status is S.COMPLETED
    assert await state_path(repo, job.id) == ["QUEUED", "PLANNING", "SEARCHING", "COMPLETED"]
    assert done.plan is not None and done.plan.planner == "fixed-v1"
    assert done.plan.queries == [QUERY]  # fixed planner: queries = [query]
    assert done.result is not None and done.result.coverage == "COMPLETE"
    assert done.result.response is not None
    assert [r.result.url for r in done.result.response.results] == [
        "https://a.example/",
        "https://b.example/",
    ]
    # Sprint 01 default behaviour preserved: fallback provider not needed → NOT_CALLED
    assert {s.provider: s.status for s in done.result.response.provider_statuses} == {
        "perplexity": "SUCCESS",
        "google": "NOT_CALLED",
    }
    assert g.calls == []
    assert done.error is None and done.warnings == []
    assert done.started_at and done.completed_at and done.deadline_at
    assert done.progress is not None and done.progress.queries_done == 1
    types = [e.type for e in await repo.list_events(job.id)]
    assert types[0] == "job.created" and types[-1] == "job.completed"
    assert "job.plan_created" in types and "job.search.query_finished" in types


async def test_provider_failure_with_fallback_completes_with_preserved_error() -> None:
    p = ScriptedSearchProvider("perplexity", [ProviderAuthError("perplexity", "401")])
    g = ScriptedSearchProvider("google", [["https://g.example/"]])
    _, runner = make(p, g)
    done = await submit_and_run(runner)
    assert done.status is S.COMPLETED
    [warning] = done.warnings
    assert warning.code == "PROVIDER_FAILURES"
    assert [(e.provider, e.code, e.category) for e in warning.provider_errors] == [
        ("perplexity", "PROVIDER_AUTH_FAILED", "AUTHENTICATION_ERROR")
    ]


async def test_all_providers_fail_job_failed_with_provider_errors() -> None:
    p = ScriptedSearchProvider("perplexity", [ProviderUnavailableError("perplexity", "503")])
    g = ScriptedSearchProvider("google", [ProviderResponseError("google", "bad json")])
    _, runner = make(p, g)
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.category, done.error.step) == (
        "ALL_SEARCH_PROVIDERS_FAILED",
        "PROVIDER_ERROR",
        S.SEARCHING,
    )
    assert {(e.provider, e.code, e.category) for e in done.error.provider_errors} == {
        ("perplexity", "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR"),
        ("google", "PROVIDER_MALFORMED_RESPONSE", "INVALID_RESPONSE"),
    }
    assert statuses(done) == ["FAILED"]
    assert done.result is not None and done.result.coverage == "NONE"


class FailOnQueryTwo(ScriptedSearchProvider):
    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if query == "q two":
            self.calls.append(query)
            raise ProviderAuthError(self.name, "401")
        return await super().search(query, options)


async def test_partial_when_one_query_fails() -> None:
    _, runner = make(FailOnQueryTwo("only", [["https://q1.example/"]]), queries=["q one", "q two"])
    done = await submit_and_run(runner)
    assert done.status is S.PARTIAL
    assert statuses(done) == ["COVERED", "FAILED"]
    assert done.result is not None and done.result.coverage == "PARTIAL"
    assert done.result.response is not None
    assert [r.result.url for r in done.result.response.results] == ["https://q1.example/"]
    assert done.warnings[0].code == "PROVIDER_FAILURES"
    assert [(e.provider, e.code, e.category) for e in done.warnings[0].provider_errors] == [
        ("only", "PROVIDER_AUTH_FAILED", "AUTHENTICATION_ERROR")
    ]


async def test_planner_failure_is_internal_error_at_planning() -> None:
    p = ScriptedSearchProvider("p", [["https://a/"]])
    repo, runner = make(p, planner=BrokenPlanner())
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.step) == ("INTERNAL_ERROR", S.PLANNING)
    assert "boom" not in done.error.message  # type name only
    assert p.calls == []
    assert await state_path(repo, done.id) == ["QUEUED", "PLANNING", "FAILED"]


# --- timeouts (layers kept distinct) ----------------------------------------------------


async def test_provider_attempt_timeout_is_a_provider_error_not_a_job_timeout() -> None:
    slow = ControlledProvider("perplexity", [["https://never/"]], hang_on=[QUERY])
    g = ScriptedSearchProvider("google", [["https://g.example/"]])
    _, runner = make(slow, g, provider_timeout_s=0.05)
    done = await submit_and_run(runner)
    assert done.status is S.COMPLETED  # Sprint 01 fallback handled it
    assert [(e.provider, e.category) for e in done.warnings[0].provider_errors] == [
        ("perplexity", "TIMEOUT")
    ]

    slow2 = ControlledProvider("perplexity", [["https://never/"]], hang_on=[QUERY])
    _, runner = make(slow2, provider_timeout_s=0.05)
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert done.error.code == "ALL_SEARCH_PROVIDERS_FAILED"  # not SEARCH_STAGE_TIMEOUT
    assert [e.category for e in done.error.provider_errors] == ["TIMEOUT"]


async def test_stage_timeout_keeps_captured_results_partial() -> None:
    p = ControlledProvider("p", [["https://q1.example/"]], hang_on=["q two"])
    _, runner = make(p, queries=["q one", "q two", "q three"], stage_timeout_s=0.3)
    t = time.monotonic()
    done = await submit_and_run(runner)
    assert time.monotonic() - t < 2
    assert done.status is S.PARTIAL
    assert statuses(done) == ["COVERED", "INTERRUPTED", "NOT_RUN"]
    assert [w.code for w in done.warnings] == ["SEARCH_STAGE_TIMEOUT"]
    assert done.warnings[0].category == "TIMEOUT"
    assert "q three" not in p.calls
    assert p.cancelled == 1 and p.in_flight == 0


async def test_stage_timeout_with_nothing_captured_fails() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    _, runner = make(p, stage_timeout_s=0.2)
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.category) == ("SEARCH_STAGE_TIMEOUT", "TIMEOUT")
    assert statuses(done) == ["INTERRUPTED"]


async def test_p5_regression_query1_kept_when_deadline_hits_query2() -> None:
    """Mandatory regression for probe P5: query 1 completes, query 2 is cut by the job
    deadline → query 1 result remains and the final result is PARTIAL."""
    primary = ControlledProvider("perplexity", [["https://q1.example/menu"]], hang_on=["q two"])
    fallback = ScriptedSearchProvider("google", [["https://fallback.example/"]])
    repo, runner = make(primary, fallback, queries=["q one", "q two", "q three"], job_timeout_s=0.3)
    t = time.monotonic()
    done = await submit_and_run(runner)
    elapsed = time.monotonic() - t
    await asyncio.sleep(0.1)  # nothing may start after the deadline

    assert elapsed < 2
    assert done.status is S.PARTIAL
    assert done.result is not None and done.result.coverage == "PARTIAL"
    assert statuses(done) == ["COVERED", "INTERRUPTED", "NOT_RUN"]
    assert done.result.response is not None
    assert [r.result.url for r in done.result.response.results] == ["https://q1.example/menu"]
    assert [w.code for w in done.warnings] == ["JOB_DEADLINE_EXCEEDED"]
    assert fallback.calls == []  # no fallback after the deadline
    assert primary.calls == ["q one", "q two"]  # no new query after the deadline
    assert primary.in_flight == 0 and primary.cancelled == 1
    stored = await repo.get(done.id)
    assert stored.result == done.result


async def test_deadline_before_any_result_fails() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    _, runner = make(p, job_timeout_s=0.2)
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.category) == ("JOB_DEADLINE_EXCEEDED", "TIMEOUT")


async def test_job_deadline_wins_over_longer_stage_timeout_and_provider_timeout() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    _, runner = make(p, job_timeout_s=0.2, stage_timeout_s=10, provider_timeout_s=10)
    done = await submit_and_run(runner)
    assert done.error is not None and done.error.code == "JOB_DEADLINE_EXCEEDED"


# --- retry interaction -----------------------------------------------------------------


async def test_provider_retries_happen_inside_search_not_at_job_level() -> None:
    sleep = RecordingSleep()
    p = ScriptedSearchProvider(
        "p",
        [
            ProviderUnavailableError("p", "503"),
            ProviderUnavailableError("p", "503"),
            ["https://ok/"],
        ],
    )
    _, runner = make(p, max_retries=2, sleep=sleep)
    done = await submit_and_run(runner)
    assert done.status is S.COMPLETED
    assert len(p.calls) == 3
    assert sleep.delays == [0.5, 1.0]
    assert done.result is not None
    assert done.result.outcomes[0].attempts[0].tries == 3


async def test_retry_backoff_is_cut_by_job_deadline() -> None:
    p = ScriptedSearchProvider("p", [ProviderUnavailableError("p", "503")])
    repo = InMemoryJobRepository()
    service = SearchService([p], max_retries=3, base_backoff_s=5)  # real asyncio.sleep backoff
    runner = JobRunner(repo, service, base_options=SearchOptions(), job_timeout_s=0.3)
    t = time.monotonic()
    done = await submit_and_run(runner)
    assert time.monotonic() - t < 2
    assert len(p.calls) == 1  # the retry after the 5 s backoff never happened
    assert done.status is S.FAILED
    assert done.error is not None and done.error.code == "JOB_DEADLINE_EXCEEDED"


async def test_no_job_level_retry() -> None:
    p = ScriptedSearchProvider("p", [ProviderAuthError("p", "401")])
    _, runner = make(p)
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    with pytest.raises(JobNotClaimableError):
        await runner.run(done.id)
    assert len(p.calls) == 1


async def test_duplicate_concurrent_start_runs_once() -> None:
    p = ControlledProvider("p", [["https://a/"]])
    _, runner = make(p)
    job, _ = await runner.submit(ResearchJobRequest(query=QUERY))
    results = await asyncio.gather(runner.run(job.id), runner.run(job.id), return_exceptions=True)
    finished = [r for r in results if isinstance(r, ResearchJob)]
    rejected = [r for r in results if isinstance(r, JobNotClaimableError)]
    assert len(finished) == 1 and len(rejected) == 1
    assert finished[0].status is S.COMPLETED
    assert p.calls == [QUERY]


# --- cancellation ----------------------------------------------------------------------


async def test_cancel_during_search_keeps_captured_results() -> None:
    p = ControlledProvider("perplexity", [["https://q1.example/"]], hang_on=["q two"])
    g = ScriptedSearchProvider("google", [["https://g/"]])
    repo, runner = make(p, g, queries=["q one", "q two", "q three"])
    job, _ = await runner.submit(ResearchJobRequest(query=QUERY))
    run_task = asyncio.create_task(runner.run(job.id))
    await asyncio.wait_for(p.hanging.wait(), timeout=2)

    await runner.cancel(job.id)
    done = await run_task  # the runner's own task is NOT cancelled; it concludes normally
    await asyncio.sleep(0.05)

    assert done.status is S.CANCELLED
    assert done.error is None  # cancellation is a status, not a provider failure
    assert done.warnings == []
    assert statuses(done) == ["COVERED", "INTERRUPTED", "NOT_RUN"]
    assert done.result is not None and done.result.coverage == "PARTIAL"
    assert done.result.response is not None
    assert [r.result.url for r in done.result.response.results] == ["https://q1.example/"]
    # propagation: the in-flight provider call was cancelled, nothing ran afterwards
    assert p.cancelled == 1 and p.in_flight == 0
    assert "q three" not in p.calls and g.calls == []
    assert await state_path(repo, job.id) == ["QUEUED", "PLANNING", "SEARCHING", "CANCELLED"]


async def test_cancel_queued_job_then_run_is_rejected() -> None:
    p = ScriptedSearchProvider("p", [["https://a/"]])
    _, runner = make(p)
    job, _ = await runner.submit(ResearchJobRequest(query=QUERY))
    cancelled = await runner.cancel(job.id)
    assert cancelled.status is S.CANCELLED
    with pytest.raises(JobNotClaimableError):
        await runner.run(job.id)
    assert p.calls == []


async def test_cancelling_the_runner_task_is_not_swallowed() -> None:
    p = ControlledProvider("p", [["https://a/"]], hang_on=[QUERY])
    repo, runner = make(p)
    job, _ = await runner.submit(ResearchJobRequest(query=QUERY))
    task = asyncio.create_task(runner.run(job.id))
    await asyncio.wait_for(p.hanging.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await repo.get(job.id)
    assert stored.status is S.FAILED
    assert stored.error is not None
    assert (stored.error.code, stored.error.category) == ("RUNNER_INTERRUPTED", "INTERNAL_ERROR")
    assert p.cancelled == 1 and p.in_flight == 0


async def test_cancel_takes_precedence_over_deadline() -> None:
    p = ControlledProvider("p", [["https://a/"]], hang_on=[QUERY])
    _, runner = make(p, job_timeout_s=0.3)
    job, _ = await runner.submit(ResearchJobRequest(query=QUERY))
    run_task = asyncio.create_task(runner.run(job.id))
    await asyncio.wait_for(p.hanging.wait(), timeout=2)
    await runner._repo.request_cancel(job.id, now=runner._clock())  # flag only, no task cancel
    done = await run_task  # deadline fires afterwards
    assert done.status is S.CANCELLED


# --- determinism, correlation, error safety ------------------------------------------------


async def test_final_status_is_deterministic() -> None:
    outcomes = set()
    for _ in range(5):
        p = ControlledProvider("p", [["https://q1/"]], hang_on=["q two"])
        _, runner = make(p, queries=["q one", "q two"], stage_timeout_s=0.1)
        done = await submit_and_run(runner)
        outcomes.add((done.status, tuple(statuses(done)), tuple(w.code for w in done.warnings)))
    assert outcomes == {(S.PARTIAL, ("COVERED", "INTERRUPTED"), ("SEARCH_STAGE_TIMEOUT",))}


async def test_unexpected_search_exception_is_internal_error_with_results_kept() -> None:
    class Exploding(ScriptedSearchProvider):
        async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
            if query == "q two":
                raise ZeroDivisionError("secret detail")
            return await super().search(query, options)

    _, runner = make(Exploding("p", [["https://q1/"]]), queries=["q one", "q two"])
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.step) == ("INTERNAL_ERROR", S.SEARCHING)
    assert "secret detail" not in done.error.message
    assert done.result is not None and statuses(done)[0] == "COVERED"


async def test_job_and_search_correlation_in_logs(capsys: pytest.CaptureFixture[str]) -> None:
    secret_query = "Nguyễn Văn A 0901234567 sushi"
    p = ScriptedSearchProvider("perplexity", [ProviderAuthError("perplexity", "401")])
    g = ScriptedSearchProvider("google", [["https://g.example/"]])
    _, runner = make(p, g)
    configure_logging("INFO", "json")
    try:
        job, _ = await runner.submit(ResearchJobRequest(query=secret_query))
        done = await runner.run(job.id)
        out = capsys.readouterr().out
    finally:
        structlog.reset_defaults()
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert records
    assert all(r.get("job_id") == done.id for r in records)
    search_lines = [r for r in records if str(r["event"]).startswith("search.")]
    assert search_lines
    assert all(r["request_id"] == f"{done.id}:search:0" for r in search_lines)
    assert secret_query not in out and "0901234567" not in out
    events = [r["event"] for r in records]
    assert events[0] == "job.created" and events[-1] == "job.completed"
    assert done.result is not None
    assert done.result.outcomes[0].request_id == f"{done.id}:search:0"


# --- real Sprint 01 adapters (respx-mocked HTTP) ----------------------------------------


@respx.mock
async def test_job_with_real_adapters_fallback_default() -> None:
    respx.post("https://api.perplexity.ai/search").mock(return_value=httpx.Response(503))
    respx.get("https://customsearch.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(
            200, json={"items": [{"title": "t", "link": "https://g.example/menu", "snippet": "s"}]}
        )
    )
    settings = build_settings(
        perplexity_api_key=SecretStr(FAKE_PPLX_KEY),
        google_api_key=SecretStr(FAKE_GOOGLE_KEY),
        google_cse_id=FAKE_CSE_ID,
        search_max_retries=0,
    )
    async with httpx.AsyncClient() as client:
        runner = JobRunner(
            InMemoryJobRepository(),
            build_search_service(settings, client),
            base_options=SearchOptions(),
        )
        done = await submit_and_run(runner)
    assert done.status is S.COMPLETED
    assert done.result is not None and done.result.response is not None
    assert [r.result.source for r in done.result.response.results] == ["google"]
    assert [(e.provider, e.category) for e in done.warnings[0].provider_errors] == [
        ("perplexity", "PROVIDER_ERROR")
    ]
    dumped = done.model_dump_json()
    assert FAKE_PPLX_KEY not in dumped and FAKE_GOOGLE_KEY not in dumped


async def test_raw_timeout_error_from_search_is_not_mislabelled_as_stage_timeout() -> None:
    """Only the runner's own time limits may produce SEARCH_STAGE_TIMEOUT /
    JOB_DEADLINE_EXCEEDED; a TimeoutError raised by the search layer is an internal error."""

    class TimeoutRaisingSearch(SearchService):
        async def search(
            self, queries: Sequence[str], options: SearchOptions, *, request_id: str | None = None
        ) -> SearchRun:
            raise TimeoutError("raised by search layer, not by the runner's time limits")

    runner = JobRunner(
        InMemoryJobRepository(),
        TimeoutRaisingSearch([ScriptedSearchProvider("p", [["https://a/"]])]),
        base_options=SearchOptions(),
    )
    done = await submit_and_run(runner)
    assert done.status is S.FAILED
    assert done.error is not None
    assert (done.error.code, done.error.step) == ("INTERNAL_ERROR", S.SEARCHING)
