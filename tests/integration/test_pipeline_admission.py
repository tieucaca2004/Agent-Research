"""Sprint 07 admission control with the real S04 crawler (design §9.8, A1-A12; OD-A = A1).

A1/A12 (completion order) and A6/A7 (cancel / exceptions) are in ``test_pipeline.py``; A11 (leaks
under 1000 seeded schedules) is in ``tests/unit/test_pipeline_admission.py``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from research_agent.crawler import FetchStatus
from research_agent.extraction import ExtractedDocument
from research_agent.jobs import JobState
from tests.crawler_support import LocalServer, Req, send
from tests.integration.test_pipeline import WrappedExtractor
from tests.pipeline_support import (
    article,
    delayed,
    hanging,
    make_pipeline,
    make_runner,
    provider,
    run_job,
    start_server,
    url,
)

S = JobState


@pytest.fixture
async def server() -> AsyncIterator[LocalServer]:
    srv = await start_server()
    yield srv
    await srv.stop()


async def test_a2_long_queue_has_no_queue_caused_timeouts(server: LocalServer) -> None:
    """P1 regression: 30 targets, 5 slots, pages of 0.6 s, a 1.0 s S04 budget."""
    hosts = [f"h{i}.test" for i in range(30)]
    for h in hosts:
        server.route(h, "/p", delayed(0.6, article(h)))
    pipeline = make_pipeline(server, crawler={"concurrency": 5, "timeout_s": 1.0})
    _, runner = make_runner(pipeline, provider(*[url(server, h) for h in hosts]))
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.COMPLETED and job.result is not None
    stats = job.result.crawl_stats
    assert stats is not None
    assert (stats.fetch_ok, stats.fetch_timeout, stats.admitted) == (30, 0, 30)
    assert pipeline.admission.max_in_flight == 5
    assert server.max_total_in_flight <= 5
    assert stats.admission_wait_ms_max >= 500  # the waiting happened in admission, not in S04


async def test_a3_per_host_contention_is_queued_outside_the_budget(server: LocalServer) -> None:
    """P5 regression: 6 URLs on one host, per-host 1, pages of 0.6 s, 1.0 s budget."""
    for i in range(6):
        server.route("h0.test", f"/p{i}", delayed(0.6, article(f"page {i}")))
    pipeline = make_pipeline(server, crawler={"timeout_s": 1.0, "per_host_concurrency": 1})
    _, runner = make_runner(
        pipeline, provider(*[url(server, "h0.test", f"/p{i}") for i in range(6)])
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.COMPLETED and job.result is not None
    assert job.result.crawl_stats is not None and job.result.crawl_stats.fetch_timeout == 0
    assert server.max_in_flight["h0.test"] == 1
    assert pipeline.admission.max_in_flight_by_host["h0.test"] == 1


async def test_a4_two_jobs_share_admission_fairly(server: LocalServer) -> None:
    hosts = [f"h{i}.test" for i in range(20)]
    for h in hosts:
        server.route(h, "/p", delayed(0.3, article(h)))
    pipeline = make_pipeline(
        server, crawler={"concurrency": 5, "timeout_s": 1.0}, max_concurrent_jobs=2
    )
    assert pipeline.admission.per_job_limit == 3
    _, first = make_runner(pipeline, provider(*[url(server, h) for h in hosts[:10]]))
    _, second = make_runner(pipeline, provider(*[url(server, h) for h in hosts[10:]]))
    try:
        jobs = await asyncio.gather(run_job(first), run_job(second))
    finally:
        await pipeline.crawler.aclose()
    for job in jobs:
        assert job.status is S.COMPLETED and job.result is not None
        assert job.result.crawl_stats is not None
        assert job.result.crawl_stats.fetch_ok == 10 and job.result.crawl_stats.fetch_timeout == 0
    assert pipeline.admission.max_in_flight == 5
    assert max(pipeline.admission.max_in_flight_by_job.values()) == 3
    assert len(pipeline.admission.max_in_flight_by_job) == 2  # both jobs held slots


async def test_a5_stage_deadline_while_waiting_marks_not_run(server: LocalServer) -> None:
    for i in range(4):
        server.route(f"h{i}.test", "/p", hanging(10))
    pipeline = make_pipeline(
        server,
        crawler={"concurrency": 1, "timeout_s": 5.0},
        settings={"crawl_stage_timeout_s": 0.5},
    )
    _, runner = make_runner(pipeline, provider(*[url(server, f"h{i}.test") for i in range(4)]))
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.result is not None and job.result.crawl_stats is not None
    states = [s.state for s in job.result.sources]
    assert states == ["EXTRACTED", "NOT_RUN", "NOT_RUN", "NOT_RUN"]
    first = job.result.sources[0].fetch
    assert first is not None and first.status is FetchStatus.FETCH_TIMEOUT
    stats = job.result.crawl_stats
    assert stats.fetch_timeout_deadline_capped == 1 and stats.not_run == 3 and stats.deadline_hit
    assert all(s.reason == "STAGE_TIMEOUT" for s in job.result.sources[1:])
    assert job.status is S.FAILED and job.error is not None and job.error.code == "NO_DOCUMENTS"
    assert [w.code for w in job.warnings] == ["CRAWL_STAGE_TIMEOUT", "FETCH_FAILURES"]


async def test_a8_cross_host_redirect_residual_is_counted(server: LocalServer) -> None:
    """P7 through the pipeline: admission cannot see redirect targets (OD-A = A1, accepted)."""
    server.route("h0.test", "/slow", delayed(0.95, article("slow")))
    server.route("h1.test", "/r", _redirect_after(0.05, url(server, "h0.test", "/fast")))
    server.route("h0.test", "/fast", delayed(0.1, article("fast")))
    pipeline = make_pipeline(server, crawler={"timeout_s": 1.0, "concurrency": 5})
    _, runner = make_runner(
        pipeline, provider(url(server, "h0.test", "/slow"), url(server, "h1.test", "/r"))
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.result is not None and job.result.crawl_stats is not None
    redirected = job.result.sources[1].fetch
    assert redirected is not None and redirected.status is FetchStatus.FETCH_TIMEOUT
    assert job.result.crawl_stats.fetch_timeout_cross_host_redirect == 1  # measured, not hidden
    assert job.result.sources[0].fetch is not None
    assert job.result.sources[0].fetch.status is FetchStatus.OK


def _redirect_after(seconds: float, location: str):  # type: ignore[no-untyped-def]
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(seconds)
        await send(writer, 302, {"Location": location}, b"")

    return handler


async def test_a9_url_rejected_by_policy_needs_no_host_slot(server: LocalServer) -> None:
    pipeline = make_pipeline(server, crawler={"concurrency": 1})
    _, runner = make_runner(pipeline, provider("http://h0.test:1/blocked"))
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.result is not None
    record = job.result.sources[0].fetch
    assert record is not None and record.status is FetchStatus.POLICY_BLOCKED
    assert [r.path for r in server.requests] == []  # no network activity
    assert job.status is S.FAILED and job.error is not None and job.error.code == "NO_DOCUMENTS"


async def test_a10_slow_extraction_applies_backpressure(server: LocalServer) -> None:
    hosts = [f"h{i}.test" for i in range(10)]
    for h in hosts:
        server.route(h, "/p", delayed(0.0, article(h)))

    async def slow(document: ExtractedDocument) -> ExtractedDocument:
        await asyncio.sleep(0.05)
        return document

    pipeline = make_pipeline(server, crawler={"concurrency": 2}, settings={"extraction_backlog": 1})
    pipeline.extractor = WrappedExtractor(pipeline.extractor, slow)  # type: ignore[assignment]
    capacity = pipeline.bodies.capacity
    assert capacity == 2 + 1 + 2
    _, runner = make_runner(pipeline, provider(*[url(server, h) for h in hosts]))
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.COMPLETED
    # bodies stay held until their extraction ends: slow extraction fills the budget
    assert pipeline.bodies.max_held == capacity and pipeline.bodies.held == 0
    assert pipeline.admission.in_flight == 0
