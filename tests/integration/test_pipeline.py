"""Sprint 07 end-to-end: S01 search → S04 crawler → S05 extraction → S06 dedup through the runner.

Real crawler, extractor and dedup against a local 127.0.0.1 server (``*.test`` hosts); search is
scripted. Covers the order contract (OD-13), provenance, failures, stage errors, cancellation
(B2), race windows for the new write sites, deadlines and the runtime text budget."""

from __future__ import annotations

import asyncio
import dataclasses
import random
import threading
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import pytest

from research_agent.core.models import SearchOptions, SearchResult
from research_agent.crawler import CrawlContext, FetchResult, FetchStatus, FetchTarget
from research_agent.dedup import DedupLevel, DocumentSet, GroupWarning
from research_agent.extraction import ExtractedDocument
from research_agent.extraction.models import ExtractionStatus
from research_agent.jobs import InMemoryJobRepository, JobState, ResearchJob, ResearchJobRequest
from research_agent.pipeline.config import PipelineSettings
from research_agent.pipeline.sources import Pipeline
from tests.crawler_support import LocalServer, Req, page, send
from tests.fakes import ScriptedSearchProvider, make_result
from tests.pipeline_support import (
    article,
    dedup_signature,
    delayed,
    hanging,
    make_pipeline,
    make_runner,
    provider,
    run_job,
    start_server,
    state_path,
    url,
)

S = JobState


@pytest.fixture
async def server() -> AsyncIterator[LocalServer]:
    srv = await start_server()
    yield srv
    await srv.stop()


def pages(server: LocalServer, spec: dict[str, Any]) -> None:
    for host, handler in spec.items():
        server.route(host, "/p", handler)


async def run_pipeline(
    server: LocalServer, urls: list[str], **kwargs: Any
) -> tuple[ResearchJob, InMemoryJobRepository, Pipeline]:
    pipeline = make_pipeline(server, **kwargs.pop("pipeline", {}))
    repo, runner = make_runner(pipeline, provider(*urls), **kwargs)
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    return job, repo, pipeline


def page_requests(server: LocalServer) -> list[str]:
    return [f"{r.host}{r.path}" for r in server.requests if r.path != "/robots.txt"]


# -- happy path, provenance, content lifetime -------------------------------------------------


async def test_pipeline_job_runs_all_stages_with_provenance(server: LocalServer) -> None:
    pages(
        server,
        {
            "h0.test": delayed(0.05, article("A")),
            "h1.test": delayed(0.0, article("A")),
            "h2.test": delayed(0.0, article("B")),
        },
    )
    urls = [url(server, h) for h in ("h0.test", "h1.test", "h2.test")]
    job, repo, pipeline = await run_pipeline(server, urls)
    assert job.status is S.COMPLETED and job.error is None and job.warnings == []
    assert await state_path(repo, job.id) == [
        "QUEUED",
        "PLANNING",
        "SEARCHING",
        "CRAWLING",
        "NORMALIZING",
        "COMPLETED",
    ]
    result = job.result
    assert result is not None and result.response is not None and result.dedup is not None
    hits = result.response.results
    assert [s.position for s in result.sources] == [0, 1, 2]
    assert [s.url for s in result.sources] == [h.result.url for h in hits]
    for p, (source, doc) in enumerate(zip(result.sources, result.documents, strict=True)):
        assert source.state == "EXTRACTED" and source.fetch is not None
        assert doc.document_id == source.document_id
        assert doc.provenance.crawl_id == source.fetch.crawl_id
        assert doc.provenance.source is hits[p].result  # same object end to end
        ref = result.dedup.result.refs[p]
        assert ref.search_source is hits[p].result and ref.trust == "UNTRUSTED"
    assert result.dedup.positions == [0, 1, 2]
    assert dedup_signature(job) == [
        ("L2", result.dedup.result.groups[DedupLevel.L2][0].key, 0, [0, 1]),
        ("L3", result.dedup.result.groups[DedupLevel.L3][0].key, 0, [0, 1]),
    ]
    assert [o.stage for o in result.stage_outcomes] == ["CRAWLING", "NORMALIZING"]
    assert all(o.status == "COMPLETED" for o in result.stage_outcomes)
    progress = job.progress
    assert progress is not None
    assert (progress.urls_total, progress.urls_done, progress.documents, progress.groups) == (
        3,
        3,
        3,
        2,
    )
    # bodies are never stored: no raw HTML anywhere in the job, every body released
    assert "<article>" not in job.model_dump_json()
    assert pipeline.bodies.held == 0 and pipeline.admission.in_flight == 0


# -- OD-13 order --------------------------------------------------------------------------------


async def test_reverse_completion_keeps_position_slots(server: LocalServer) -> None:
    hosts = [f"h{i}.test" for i in range(6)]
    pages(
        server,
        {
            h: delayed(0.05 * (5 - i), article("T" if i % 2 == 0 else "U"))
            for i, h in enumerate(hosts)
        },
    )
    job, _, _ = await run_pipeline(
        server, [url(server, h) for h in hosts], pipeline={"crawler": {"per_host_concurrency": 1}}
    )
    assert job.status is S.COMPLETED
    result = job.result
    assert result is not None and result.dedup is not None
    finished = sorted(result.sources, key=lambda s: s.fetch.fetched_at if s.fetch else datetime.max)
    assert [s.position for s in result.sources] == list(range(6))
    assert [s.url for s in result.sources] == [url(server, h) for h in hosts]
    assert result.dedup.positions == list(range(6))
    for level, key, representative, members in dedup_signature(job):
        assert representative == min(members), (level, key)
    l3 = [(r, m) for lvl, _, r, m in dedup_signature(job) if lvl == "L3"]
    assert l3 == [(0, [0, 2, 4]), (1, [1, 3, 5])]
    assert finished  # completion order differs from position order by construction


async def test_seeded_random_completion_order_gives_identical_results(server: LocalServer) -> None:
    hosts = [f"h{i}.test" for i in range(8)]
    pipeline = make_pipeline(server, crawler={"concurrency": 8})
    signatures = set()
    try:
        for seed in range(20):
            rng = random.Random(seed)  # noqa: S311 - reproducible test schedule, not crypto
            pages(
                server,
                {
                    h: delayed(rng.uniform(0, 0.03), article("XYZ"[i % 3]))
                    for i, h in enumerate(hosts)
                },
            )
            _, runner = make_runner(pipeline, provider(*[url(server, h) for h in hosts]))
            job = await run_job(runner)
            assert job.status is S.COMPLETED, seed
            assert job.result is not None
            sources = tuple(
                (s.position, s.url, s.state, s.document_status) for s in job.result.sources
            )
            signatures.add(
                (sources, tuple((a, b, c, tuple(d)) for a, b, c, d in dedup_signature(job)))
            )
    finally:
        await pipeline.crawler.aclose()
    assert len(signatures) == 1
    ((_, groups),) = signatures
    assert all(rep == members[0] == min(members) for _, _, rep, members in groups)


async def test_same_url_from_several_queries_and_providers_is_fetched_once(
    server: LocalServer,
) -> None:
    hosts = ["h0.test", "h1.test", "h2.test"]
    pages(server, {h: delayed(0.0, article(h)) for h in hosts})
    u0, u1, u2 = (url(server, h) for h in hosts)
    pipeline = make_pipeline(server)
    _, runner = make_runner(
        pipeline,
        ScriptedSearchProvider("perplexity", [[u0, u1]]),
        ScriptedSearchProvider("google", [[u1, u2]]),
        queries=["q one", "q two"],
        strategy="fanout",
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.COMPLETED and job.result is not None
    sources = job.result.sources
    assert [s.url for s in sources] == [u0, u1, u2]
    assert sources[1].providers == ["perplexity", "google"]
    assert sources[1].queries == ["q one", "q two"]
    assert sorted(page_requests(server)) == sorted(f"{h}/p" for h in hosts)  # once each


async def test_redirects_to_one_final_url_group_under_the_better_ranked_source(
    server: LocalServer,
) -> None:
    final = url(server, "h9.test", "/final")
    server.route("h9.test", "/final", delayed(0.0, article("F")))

    def slow_redirect(seconds: float) -> Any:
        async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(seconds)
            await send(writer, 302, {"Location": final}, b"")

        return handler

    server.route("h0.test", "/r", slow_redirect(0.2))  # better-ranked, finishes last
    server.route("h1.test", "/r", slow_redirect(0.0))
    job, _, _ = await run_pipeline(
        server, [url(server, "h0.test", "/r"), url(server, "h1.test", "/r")]
    )
    assert job.status is S.COMPLETED and job.result is not None
    first, second = job.result.sources
    assert first.fetch is not None and second.fetch is not None
    assert first.fetch.final_url == second.fetch.final_url == final
    assert [h.url for h in first.fetch.redirect_chain] == [url(server, "h0.test", "/r")]
    l1 = [(rep, members) for lvl, _, rep, members in dedup_signature(job) if lvl == "L1"]
    assert l1 == [(0, [0, 1])]


async def test_document_in_different_groups_at_different_levels(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("same text")))
    server.route("h1.test", "/p", delayed(0.0, article("same text")))
    server.route("h0.test", "/p?utm_source=x", delayed(0.0, article("other text")))
    server.route(
        "h2.test",
        "/r",
        page(b"", status=302, headers={"Location": url(server, "h0.test", "/p?utm_source=x")}),
    )
    job, _, _ = await run_pipeline(
        server,
        [url(server, "h0.test"), url(server, "h1.test"), url(server, "h2.test", "/r")],
    )
    assert job.status is S.COMPLETED
    groups = {lvl: (rep, members) for lvl, _, rep, members in dedup_signature(job)}
    assert groups["L1"] == (0, [0, 2])  # same source identity (utm_source dropped by S01)
    assert groups["L3"] == (0, [0, 1])  # same verified text
    assert not any({1, 2} <= set(m) for _, _, _, m in dedup_signature(job))  # no transitive merge
    assert job.result is not None and job.result.dedup is not None
    l1 = job.result.dedup.result.groups[DedupLevel.L1][0]
    assert GroupWarning.SOURCE_CONTENT_DIFFERS in l1.warnings


# -- failures and final status ----------------------------------------------------------------


async def test_per_url_failures_are_warnings_and_keep_their_slots(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("ok 0")))
    # h1: no route → 404
    server.route(
        "h2.test", "/robots.txt", page(b"User-agent: *\nDisallow: /\n", content_type="text/plain")
    )
    server.route("h3.test", "/p", hanging(10))
    server.route("h4.test", "/p", delayed(0.0, article("ok 4")))
    urls = [url(server, f"h{i}.test") for i in range(5)]
    job, _, _ = await run_pipeline(server, urls, pipeline={"crawler": {"timeout_s": 0.5}})
    assert job.status is S.COMPLETED
    assert [w.code for w in job.warnings] == ["FETCH_FAILURES"]
    result = job.result
    assert result is not None and result.crawl_stats is not None
    statuses = [s.fetch.status if s.fetch else None for s in result.sources]
    assert statuses == [
        FetchStatus.OK,
        FetchStatus.HTTP_ERROR,
        FetchStatus.ROBOTS_BLOCKED,
        FetchStatus.FETCH_TIMEOUT,
        FetchStatus.OK,
    ]
    assert [s.document_status for s in result.sources] == [
        ExtractionStatus.SUCCESS,
        ExtractionStatus.NOT_FETCHED,
        ExtractionStatus.NOT_FETCHED,
        ExtractionStatus.NOT_FETCHED,
        ExtractionStatus.SUCCESS,
    ]
    stats = result.crawl_stats
    assert (stats.fetch_ok, stats.fetch_failed, stats.fetch_timeout) == (2, 3, 1)
    assert stats.fetch_timeout_other == 1 and stats.fetch_timeout_deadline_capped == 0


async def test_no_usable_document_fails_with_no_documents(server: LocalServer) -> None:
    job, _, _ = await run_pipeline(server, [url(server, "h0.test"), url(server, "h1.test")])  # 404s
    assert job.status is S.FAILED
    assert job.error is not None and (job.error.code, job.error.step) == (
        "NO_DOCUMENTS",
        S.CRAWLING,
    )
    assert job.error.category == "FETCH_ERROR"
    assert [w.code for w in job.warnings] == ["FETCH_FAILURES"]
    assert job.result is not None and len(job.result.sources) == 2 and job.result.dedup is not None


async def test_zero_search_results_complete_without_sources(server: LocalServer) -> None:
    job, repo, _ = await run_pipeline(server, [])
    assert job.status is S.COMPLETED and job.result is not None
    assert job.result.sources == [] and job.result.documents == []
    assert await state_path(repo, job.id) == [
        "QUEUED",
        "PLANNING",
        "SEARCHING",
        "CRAWLING",
        "NORMALIZING",
        "COMPLETED",
    ]


async def test_selection_cut_at_max_urls(server: LocalServer) -> None:
    hosts = [f"h{i}.test" for i in range(5)]
    pages(server, {h: delayed(0.0, article(h)) for h in hosts})
    job, _, _ = await run_pipeline(
        server, [url(server, h) for h in hosts], pipeline={"settings": {"max_urls": 3}}
    )
    assert job.result is not None
    assert [s.url for s in job.result.sources] == [url(server, h) for h in hosts[:3]]
    assert job.result.not_selected == 2 and job.result.crawl_stats is not None
    assert job.result.crawl_stats.not_selected == 2
    assert sorted(page_requests(server)) == sorted(f"{h}/p" for h in hosts[:3])


# -- join checks and stage errors (D7) --------------------------------------------------------


class WrappedCrawler:
    def __init__(self, inner: Any, transform: Callable[[FetchTarget, FetchResult], Any]) -> None:
        self.inner = inner
        self.transform = transform

    async def fetch(self, target: FetchTarget, context: CrawlContext | None = None) -> FetchResult:
        result = await self.inner.fetch(target, context)
        out = self.transform(target, result)
        if asyncio.iscoroutine(out):
            out = await out
        return out  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        await self.inner.aclose()


class WrappedExtractor:
    def __init__(self, inner: Any, transform: Callable[[ExtractedDocument], Any]) -> None:
        self.inner = inner
        self.transform = transform

    async def aextract(
        self, fetch: FetchResult, context: CrawlContext | None = None
    ) -> ExtractedDocument:
        document = await self.inner.aextract(fetch, context)
        out = self.transform(document)
        if asyncio.iscoroutine(out):
            out = await out
        return out  # type: ignore[no-any-return]


async def run_with(
    server: LocalServer,
    urls: list[str],
    *,
    crawler: Callable[[FetchTarget, FetchResult], Any] | None = None,
    extractor: Callable[[ExtractedDocument], Any] | None = None,
    deduplicate: Any = None,
    repo: InMemoryJobRepository | None = None,
    **kwargs: Any,
) -> tuple[ResearchJob, InMemoryJobRepository]:
    pipeline = make_pipeline(server, **kwargs.pop("pipeline", {}))
    if crawler is not None:
        pipeline.crawler = WrappedCrawler(pipeline.crawler, crawler)  # type: ignore[assignment]
    if extractor is not None:
        pipeline.extractor = WrappedExtractor(pipeline.extractor, extractor)  # type: ignore[assignment]
    if deduplicate is not None:
        pipeline.deduplicate = deduplicate
    repository, runner = make_runner(pipeline, provider(*urls), repo=repo, **kwargs)
    try:
        return await run_job(runner), repository
    finally:
        await pipeline.crawler.aclose()


def two_pages(server: LocalServer) -> list[str]:
    pages(
        server, {"h0.test": delayed(0.0, article("zero")), "h1.test": delayed(0.1, article("one"))}
    )
    return [url(server, "h0.test"), url(server, "h1.test")]


async def test_source_mismatch_is_a_crawl_stage_error(server: LocalServer) -> None:
    other = make_result("https://elsewhere.example/", source="perplexity", query="q")

    def swap(target: FetchTarget, result: FetchResult) -> FetchResult:
        return result.model_copy(update={"source": other}) if "h1.test" in target.url else result

    job, _ = await run_with(server, two_pages(server), crawler=swap)
    assert job.status is S.FAILED and job.error is not None
    assert (job.error.code, job.error.step, job.error.category) == (
        "CRAWL_FAILED",
        S.CRAWLING,
        "INTERNAL_ERROR",
    )
    assert job.result is not None
    assert job.result.sources[0].state == "EXTRACTED"  # captured before the failure: kept
    assert job.result.dedup is None


async def test_requested_url_mismatch_is_a_crawl_stage_error(server: LocalServer) -> None:
    def swap(target: FetchTarget, result: FetchResult) -> FetchResult:
        return result.model_copy(update={"requested_url": "http://h7.test/"})

    job, _ = await run_with(server, two_pages(server), crawler=swap)
    assert job.error is not None and job.error.code == "CRAWL_FAILED"


async def test_crawl_id_mismatch_is_an_extraction_stage_error(server: LocalServer) -> None:
    def swap(document: ExtractedDocument) -> ExtractedDocument:
        provenance = document.provenance.model_copy(update={"crawl_id": "someone-else"})
        return document.model_copy(update={"provenance": provenance})

    job, _ = await run_with(server, two_pages(server), extractor=swap)
    assert job.error is not None and job.error.code == "EXTRACTION_FAILED"
    assert job.status is S.FAILED


async def test_fetch_exception_fails_the_stage_and_keeps_captured(server: LocalServer) -> None:
    def boom(target: FetchTarget, result: FetchResult) -> FetchResult:
        if "h1.test" in target.url:
            raise RuntimeError("crawler bug")
        return result

    job, _ = await run_with(server, two_pages(server), crawler=boom)
    assert job.status is S.FAILED and job.error is not None
    assert job.error.code == "CRAWL_FAILED" and "crawler bug" not in job.error.message
    assert job.result is not None and job.result.sources[0].state == "EXTRACTED"
    assert job.result.sources[1].state == "INTERRUPTED"
    assert job.result.sources[1].reason == "STAGE_FAILED"


async def test_extractor_exception_fails_the_stage(server: LocalServer) -> None:
    def boom(document: ExtractedDocument) -> ExtractedDocument:
        raise RuntimeError("extractor bug")

    job, _ = await run_with(server, two_pages(server), extractor=boom)
    assert job.error is not None and job.error.code == "EXTRACTION_FAILED"
    assert job.result is not None and job.result.sources[0].state == "FETCHED"


async def test_dedup_exception_is_dedup_failed_with_documents_kept(server: LocalServer) -> None:
    def boom(documents: Any) -> DocumentSet:
        raise RuntimeError("dedup bug")

    job, repo = await run_with(server, two_pages(server), deduplicate=boom)
    assert job.status is S.FAILED and job.error is not None
    assert (job.error.code, job.error.step) == ("DEDUP_FAILED", S.NORMALIZING)
    assert job.result is not None and len(job.result.documents) == 2 and job.result.dedup is None
    assert job.result.stage_outcomes[-1].status == "FAILED"
    assert (await state_path(repo, job.id))[-2:] == ["NORMALIZING", "FAILED"]


async def test_runtime_text_budget_is_a_stage_error_not_truncation(server: LocalServer) -> None:
    long_text = "<p>" + "Phở bò Nha Trang. " * 120 + "</p>"
    pages(
        server,
        {
            "h0.test": delayed(0.0, article("a", long_text)),
            "h1.test": delayed(0.0, article("b", long_text)),
        },
    )
    pipeline = make_pipeline(server)
    # bypass the static check on purpose: the runtime counter is the second line of defence
    pipeline = dataclasses.replace(
        pipeline,
        settings=PipelineSettings(_env_file=None, enabled=True, max_total_text_chars=3_000),
    )
    _, runner = make_runner(pipeline, provider(url(server, "h0.test"), url(server, "h1.test")))
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.FAILED and job.error is not None
    assert job.error.code == "PIPELINE_BUDGET_EXCEEDED"
    assert job.result is not None
    assert all(len(d.text) > 1_000 for d in job.result.documents)  # nothing truncated


# -- cancellation (B2) --------------------------------------------------------------------------


async def start_job(runner: Any) -> tuple[str, asyncio.Task[ResearchJob]]:
    job, _ = await runner.submit(ResearchJobRequest(query="phở nha trang"))
    return job.id, asyncio.create_task(runner.run(job.id))


async def wait_for(predicate: Callable[[], bool], limit_s: float = 5.0) -> None:
    async with asyncio.timeout(limit_s):
        while not predicate():  # noqa: ASYNC110 - polling test state
            await asyncio.sleep(0.01)


async def test_cancel_during_fetch_keeps_captured_and_marks_the_rest(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    server.route("h2.test", "/p", hanging(10))
    pipeline = make_pipeline(server, crawler={"concurrency": 2})
    repo, runner = make_runner(pipeline, provider(*[url(server, f"h{i}.test") for i in range(3)]))
    try:
        job_id, task = await start_job(runner)

        def first_extracted() -> bool:
            stored = repo._jobs[job_id].result
            return (
                stored is not None
                and bool(stored.sources)
                and stored.sources[0].state == "EXTRACTED"
            )

        await wait_for(lambda: "h1.test/p" in page_requests(server) and first_extracted())
        await runner.cancel(job_id)
        job = await task
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.CANCELLED and job.error is None
    assert job.result is not None
    states = [s.state for s in job.result.sources]
    assert states[0] == "EXTRACTED" and states[1] == "INTERRUPTED"
    assert states[2] in ("INTERRUPTED", "NOT_RUN")
    assert all(s.reason == "CANCELLED" for s in job.result.sources[1:])
    assert pipeline.admission.in_flight == 0 and pipeline.bodies.held == 0


async def test_cancel_while_waiting_for_admission(server: LocalServer) -> None:
    server.route("h0.test", "/p", hanging(10))
    pipeline = make_pipeline(server, crawler={"concurrency": 1})
    _, runner = make_runner(pipeline, provider(*[url(server, f"h{i}.test") for i in range(4)]))
    try:
        job_id, task = await start_job(runner)
        await wait_for(lambda: "h0.test/p" in page_requests(server))
        await runner.cancel(job_id)
        job = await task
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.CANCELLED and job.result is not None
    assert [s.state for s in job.result.sources] == ["INTERRUPTED", "NOT_RUN", "NOT_RUN", "NOT_RUN"]
    assert page_requests(server) == ["h0.test/p"]  # nothing admitted after the cancel


async def test_cancel_during_extraction_stops_it(server: LocalServer) -> None:
    pages(server, {"h0.test": delayed(0.0, article("zero"))})
    entered = asyncio.Event()

    async def block(document: ExtractedDocument) -> ExtractedDocument:
        entered.set()
        await asyncio.sleep(30)
        return document

    pipeline = make_pipeline(server)
    pipeline.extractor = WrappedExtractor(pipeline.extractor, block)  # type: ignore[assignment]
    _, runner = make_runner(pipeline, provider(url(server, "h0.test")))
    try:
        job_id, task = await start_job(runner)
        await asyncio.wait_for(entered.wait(), 5)
        await runner.cancel(job_id)
        job = await task
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.CANCELLED and job.result is not None
    assert job.result.sources[0].state == "FETCHED" and job.result.sources[0].fetch is not None
    assert pipeline.bodies.held == 0


async def test_b2_cancel_recorded_before_a_crawl_stage_error_wins(server: LocalServer) -> None:
    pages(server, {"h0.test": delayed(0.0, article("zero"))})
    repo = InMemoryJobRepository()

    async def cancel_then_fail(target: FetchTarget, result: FetchResult) -> FetchResult:
        jobs = list(repo._jobs)
        await repo.request_cancel(jobs[0], now=datetime.now().astimezone())
        raise RuntimeError("crawler bug")

    job, _ = await run_with(server, [url(server, "h0.test")], crawler=cancel_then_fail, repo=repo)
    assert job.status is S.CANCELLED and job.error is None
    assert [w.code for w in job.warnings] == ["CRAWL_FAILED"]  # the stage error stays visible
    assert job.result is not None and job.result.sources[0].state == "INTERRUPTED"


async def test_b2_cancel_recorded_before_a_dedup_error_wins(server: LocalServer) -> None:
    pages(server, {"h0.test": delayed(0.0, article("zero"))})
    repo = InMemoryJobRepository()
    proceed = threading.Event()

    def fail_after_cancel(documents: Any) -> DocumentSet:
        proceed.wait(5)
        raise RuntimeError("dedup bug")

    pipeline = make_pipeline(server)
    pipeline.deduplicate = fail_after_cancel
    _, runner = make_runner(pipeline, provider(url(server, "h0.test")), repo=repo)
    try:
        job_id, task = await start_job(runner)
        await wait_for(lambda: repo._jobs[job_id].status is S.NORMALIZING)
        await repo.request_cancel(job_id, now=datetime.now().astimezone())
        proceed.set()
        job = await task
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.CANCELLED and job.error is None
    assert [w.code for w in job.warnings] == ["DEDUP_FAILED"]
    assert job.result is not None and len(job.result.documents) == 1


async def test_b2_does_not_change_searching_semantics(server: LocalServer) -> None:
    """A SEARCHING stage error after a recorded cancel stays FAILED/INTERNAL_ERROR (Sprint 02)."""
    repo = InMemoryJobRepository()

    class CancelThenRaise(ScriptedSearchProvider):
        async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
            await repo.request_cancel(next(iter(repo._jobs)), now=datetime.now().astimezone())
            raise RuntimeError("provider bug")

    pipeline = make_pipeline(server)
    _, runner = make_runner(pipeline, CancelThenRaise("perplexity", [[]]), repo=repo)
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.FAILED and job.error is not None
    assert (job.error.code, job.error.step) == ("INTERNAL_ERROR", S.SEARCHING)


class CancelBeforeSave(InMemoryJobRepository):
    """Records a user cancel right before the first matching ``save`` (real version conflict)."""

    def __init__(self, when: Callable[[ResearchJob, str | None], bool]) -> None:
        super().__init__()
        self.when = when
        self.fired = False

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        if not self.fired and self.when(job, event):
            self.fired = True
            await super().request_cancel(job.id, now=job.updated_at)
        return await super().save(job, expected_version=expected_version, event=event, data=data)


NEW_WRITE_SITES: dict[str, tuple[Callable[[ResearchJob, str | None], bool], bool]] = {
    "CRAWLING transition": (lambda j, e: j.status is S.CRAWLING and e is None, True),
    "source captured": (lambda j, e: e == "job.crawl.source_captured", True),
    "NORMALIZING transition": (lambda j, e: j.status is S.NORMALIZING and e is None, True),
    "COMPLETED write": (lambda j, e: j.status is S.COMPLETED, True),
    "FAILED NO_DOCUMENTS write": (lambda j, e: j.status is S.FAILED, False),
}


@pytest.mark.parametrize("site", list(NEW_WRITE_SITES))
async def test_cancel_landing_on_each_new_write_site_ends_cancelled(
    server: LocalServer, site: str
) -> None:
    when, ok = NEW_WRITE_SITES[site]
    if ok:
        pages(server, {"h0.test": delayed(0.0, article("zero"))})
    repo = CancelBeforeSave(when)
    job, _ = await run_with(server, [url(server, "h0.test")], repo=repo)
    assert repo.fired, site
    assert job.status is S.CANCELLED and job.error is None, site
    assert job.result is not None and job.result.response is not None  # search results kept


async def test_job_finalized_elsewhere_stops_before_any_fetch(server: LocalServer) -> None:
    pages(server, {"h0.test": delayed(0.0, article("zero"))})

    class OtherWriter(InMemoryJobRepository):
        fired = False

        async def save(
            self,
            job: ResearchJob,
            *,
            expected_version: int,
            event: str | None = None,
            data: dict[str, object] | None = None,
        ) -> ResearchJob:
            stored = await super().save(
                job, expected_version=expected_version, event=event, data=data
            )
            if not self.fired and stored.status is S.CRAWLING:
                self.fired = True
                terminal = stored.model_copy(
                    update={"status": S.FAILED, "completed_at": stored.updated_at}
                )
                return await super().save(
                    terminal, expected_version=stored.version, event="other.writer"
                )
            return stored

    repo = OtherWriter()
    job, _ = await run_with(server, [url(server, "h0.test")], repo=repo)
    assert job.status is S.FAILED and job.error is None  # the other writer's terminal state
    assert page_requests(server) == []  # no fetch admitted after the terminal state was seen
    stored = await repo.get(job.id)
    events = [e.type for e in await repo.list_events(job.id)]
    assert events[-1] == "other.writer" and stored.version == job.version  # nothing written after


async def test_runner_shutdown_during_crawling_is_runner_interrupted(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    pipeline = make_pipeline(server)
    repo, runner = make_runner(pipeline, provider(url(server, "h0.test"), url(server, "h1.test")))
    try:
        job_id, task = await start_job(runner)

        def one_extracted() -> bool:
            stored = repo._jobs[job_id].result
            return stored is not None and any(s.state == "EXTRACTED" for s in stored.sources)

        await wait_for(one_extracted)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await pipeline.crawler.aclose()
    job = await repo.get(job_id)
    assert job.status is S.FAILED and job.error is not None
    assert job.error.code == "RUNNER_INTERRUPTED"
    assert job.result is not None and job.result.sources[0].state == "EXTRACTED"
    assert pipeline.admission.in_flight == 0 and pipeline.bodies.held == 0


# -- deadlines -----------------------------------------------------------------------------------


async def test_crawl_stage_timeout_still_normalizes_and_ends_partial(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    job, repo, _ = await run_pipeline(
        server,
        [url(server, "h0.test"), url(server, "h1.test")],
        pipeline={"settings": {"crawl_stage_timeout_s": 0.5}},
    )
    assert job.status is S.PARTIAL
    assert [w.code for w in job.warnings] == ["CRAWL_STAGE_TIMEOUT", "FETCH_FAILURES"]
    assert "NORMALIZING" in await state_path(repo, job.id)
    assert job.result is not None and job.result.dedup is not None
    assert job.result.crawl_stats is not None
    assert job.result.crawl_stats.fetch_timeout_deadline_capped == 1
    assert job.result.stage_outcomes[0].status == "PARTIAL"


async def test_job_deadline_in_crawling_skips_normalizing(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    job, repo, _ = await run_pipeline(
        server, [url(server, "h0.test"), url(server, "h1.test")], job_timeout_s=1.0
    )
    assert job.status is S.PARTIAL
    assert [w.code for w in job.warnings] == ["JOB_DEADLINE_EXCEEDED"]
    # the frozen state machine allows PARTIAL only from the last stage: NORMALIZING is entered
    # without running (outcome NOT_RUN, no dedup result)
    assert (await state_path(repo, job.id))[-3:] == ["CRAWLING", "NORMALIZING", "PARTIAL"]
    assert job.result is not None and job.result.dedup is None
    assert [(o.stage, o.status) for o in job.result.stage_outcomes] == [
        ("CRAWLING", "INTERRUPTED"),
        ("NORMALIZING", "NOT_RUN"),
    ]
    assert job.result.sources[0].state == "EXTRACTED"


async def test_job_deadline_without_documents_fails(server: LocalServer) -> None:
    server.route("h0.test", "/p", hanging(10))
    job, _, _ = await run_pipeline(server, [url(server, "h0.test")], job_timeout_s=1.0)
    assert job.status is S.FAILED and job.error is not None
    assert (job.error.code, job.error.step) == ("JOB_DEADLINE_EXCEEDED", S.CRAWLING)


async def test_search_stage_timeout_with_coverage_continues_the_pipeline(
    server: LocalServer,
) -> None:
    pages(server, {"h0.test": delayed(0.0, article("zero"))})
    first = url(server, "h0.test")

    class SlowSecond(ScriptedSearchProvider):
        async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
            if query == "q two":
                await asyncio.sleep(10)
            return [make_result(first, source=self.name, query=query)]

    pipeline = make_pipeline(server)
    repo, runner = make_runner(
        pipeline, SlowSecond("perplexity", [[]]), queries=["q one", "q two"], stage_timeout_s=0.5
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.PARTIAL
    assert [w.code for w in job.warnings] == ["SEARCH_STAGE_TIMEOUT"]
    assert "CRAWLING" in await state_path(repo, job.id)
    assert job.result is not None and len(job.result.documents) == 1
    assert [o.status for o in job.result.outcomes] == ["COVERED", "INTERRUPTED"]


# -- pipeline off ----------------------------------------------------------------------------------


def comparable(job: ResearchJob) -> dict[str, Any]:
    data = job.model_dump(mode="json", exclude={"id", "worker_id"})
    for key in ("created_at", "updated_at", "started_at", "completed_at", "deadline_at"):
        data.pop(key, None)
    if data.get("plan"):
        data["plan"].pop("created_at", None)
    if data.get("result") and data["result"].get("outcomes"):
        for outcome in data["result"]["outcomes"]:
            outcome.pop("duration_ms", None)
            outcome.pop("request_id", None)
            for attempt in outcome.get("attempts", []):
                attempt.pop("duration_ms", None)
    if data.get("result") and data["result"].get("response"):
        data["result"]["response"].pop("request_id", None)
        for status in data["result"]["response"]["provider_statuses"]:
            status.pop("duration_ms", None)
    return data


async def test_disabled_pipeline_behaves_exactly_like_sprint_02(server: LocalServer) -> None:
    urls = ["https://a.example/", "https://b.example/"]
    plain_repo, plain = make_runner(None, provider(*urls))
    disabled = make_pipeline(server)
    disabled = dataclasses.replace(
        disabled, settings=PipelineSettings(_env_file=None, enabled=False)
    )
    off_repo, off = make_runner(disabled, provider(*urls))
    try:
        a = await run_job(plain)
        b = await run_job(off)
    finally:
        await disabled.crawler.aclose()
    assert comparable(a) == comparable(b)
    assert a.stages == b.stages == (S.PLANNING, S.SEARCHING)
    assert [e.type for e in await plain_repo.list_events(a.id)] == [
        e.type for e in await off_repo.list_events(b.id)
    ]
    assert page_requests(server) == []


async def test_job_deadline_in_searching_with_coverage_is_partial_without_crawling(
    server: LocalServer,
) -> None:
    """Row 4a: the Sprint 02 deadline rule (PARTIAL if ≥ 1 query covered); the later stages are
    entered without running (the frozen state machine allows PARTIAL only from the last stage)."""
    first = url(server, "h0.test")

    class SlowSecond(ScriptedSearchProvider):
        async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
            if query == "q two":
                await asyncio.sleep(10)
            return [make_result(first, source=self.name, query=query)]

    pipeline = make_pipeline(server)
    repo, runner = make_runner(
        pipeline, SlowSecond("perplexity", [[]]), queries=["q one", "q two"], job_timeout_s=0.5
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.PARTIAL
    assert [w.code for w in job.warnings] == ["JOB_DEADLINE_EXCEEDED"]
    assert (await state_path(repo, job.id))[2:] == [
        "SEARCHING",
        "CRAWLING",
        "NORMALIZING",
        "PARTIAL",
    ]
    assert job.result is not None and job.result.sources == []
    assert [(o.stage, o.status) for o in job.result.stage_outcomes] == [
        ("CRAWLING", "NOT_RUN"),
        ("NORMALIZING", "NOT_RUN"),
    ]
    assert page_requests(server) == []


async def test_cancel_landing_on_a_skip_transition_ends_cancelled(server: LocalServer) -> None:
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    repo = CancelBeforeSave(lambda j, e: j.status is S.NORMALIZING and e is None)
    job, _ = await run_with(
        server, [url(server, "h0.test"), url(server, "h1.test")], repo=repo, job_timeout_s=1.0
    )
    assert repo.fired
    assert job.status is S.CANCELLED and job.error is None
    assert job.result is not None and job.result.sources[0].state == "EXTRACTED"


async def test_position_map_skips_slots_without_documents(server: LocalServer) -> None:
    """A NOT_RUN position between finished ones: S06 groups still name the right sources."""
    server.route("h0.test", "/a", hanging(10))  # holds the h0 slot until the stage deadline
    server.route("h0.test", "/b", delayed(0.0, article("never fetched")))  # waits → NOT_RUN
    server.route("h1.test", "/p", delayed(0.0, article("shared")))
    server.route("h2.test", "/p", delayed(0.0, article("shared")))
    urls = [
        url(server, "h0.test", "/a"),
        url(server, "h0.test", "/b"),
        url(server, "h1.test"),
        url(server, "h2.test"),
    ]
    job, _, _ = await run_pipeline(
        server, urls, pipeline={"settings": {"crawl_stage_timeout_s": 0.6}}
    )
    assert job.result is not None and job.result.dedup is not None
    assert [s.state for s in job.result.sources] == [
        "EXTRACTED",
        "NOT_RUN",
        "EXTRACTED",
        "EXTRACTED",
    ]
    assert job.result.dedup.positions == [0, 2, 3]
    l3 = [(rep, members) for lvl, _, rep, members in dedup_signature(job) if lvl == "L3"]
    assert l3 == [(2, [2, 3])]
    assert job.status is S.PARTIAL


async def test_job_finalized_elsewhere_during_crawling_stops_further_fetches(
    server: LocalServer,
) -> None:
    """Every fetch is preceded by a job re-read: a terminal state written by another writer after
    the first capture means no later source is fetched and nothing is written afterwards."""
    for i in range(3):
        server.route(f"h{i}.test", "/p", delayed(0.0, article(f"page {i}")))

    class FinalizeAfterFirstCapture(InMemoryJobRepository):
        fired = False

        async def save(
            self,
            job: ResearchJob,
            *,
            expected_version: int,
            event: str | None = None,
            data: dict[str, object] | None = None,
        ) -> ResearchJob:
            stored = await super().save(
                job, expected_version=expected_version, event=event, data=data
            )
            if not self.fired and event == "job.crawl.source_captured":
                self.fired = True
                terminal = stored.model_copy(
                    update={"status": S.FAILED, "completed_at": stored.updated_at}
                )
                await super().save(terminal, expected_version=stored.version, event="other.writer")
            return stored

    repo = FinalizeAfterFirstCapture()
    job, _ = await run_with(
        server,
        [url(server, f"h{i}.test") for i in range(3)],
        repo=repo,
        pipeline={"crawler": {"concurrency": 1}},
    )
    assert repo.fired and job.status is S.FAILED and job.error is None  # the other writer's state
    assert page_requests(server) == ["h0.test/p"]
    events = [e.type for e in await repo.list_events(job.id)]
    assert events[-1] == "other.writer"


async def test_cancel_flag_seen_before_the_next_fetch(server: LocalServer) -> None:
    for i in range(3):
        server.route(f"h{i}.test", "/p", delayed(0.0, article(f"page {i}")))

    class CancelAfterFirstCapture(InMemoryJobRepository):
        fired = False

        async def save(
            self,
            job: ResearchJob,
            *,
            expected_version: int,
            event: str | None = None,
            data: dict[str, object] | None = None,
        ) -> ResearchJob:
            stored = await super().save(
                job, expected_version=expected_version, event=event, data=data
            )
            if not self.fired and event == "job.crawl.source_captured":
                self.fired = True
                await super().request_cancel(job.id, now=job.updated_at)
            return stored

    repo = CancelAfterFirstCapture()
    job, _ = await run_with(
        server,
        [url(server, f"h{i}.test") for i in range(3)],
        repo=repo,
        pipeline={"crawler": {"concurrency": 1}},
    )
    assert job.status is S.CANCELLED
    assert page_requests(server) == ["h0.test/p"]


async def test_cancel_recorded_as_crawling_starts_admits_nothing(server: LocalServer) -> None:
    """A cancel recorded right after the CRAWLING transition: the stage is stopped before any
    target takes a shared admission slot."""
    pages(server, {"h0.test": delayed(0.0, article("zero"))})

    class CancelAfterTransition(InMemoryJobRepository):
        fired = False

        async def save(
            self,
            job: ResearchJob,
            *,
            expected_version: int,
            event: str | None = None,
            data: dict[str, object] | None = None,
        ) -> ResearchJob:
            stored = await super().save(
                job, expected_version=expected_version, event=event, data=data
            )
            if not self.fired and stored.status is S.CRAWLING and event is None:
                self.fired = True
                await super().request_cancel(job.id, now=job.updated_at)
            return stored

    repo = CancelAfterTransition()
    pipeline = make_pipeline(server)
    _, runner = make_runner(pipeline, provider(url(server, "h0.test")), repo=repo)
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert repo.fired and job.status is S.CANCELLED
    assert pipeline.admission.granted_total == 0 and page_requests(server) == []


async def test_job_deadline_in_crawling_is_final_even_if_the_clock_disagrees(
    server: LocalServer,
) -> None:
    """The CRAWLING stage limit was the job deadline: NORMALIZING must not start, even when the
    runner's clock (here frozen) does not yet show the deadline as passed."""
    server.route("h0.test", "/p", delayed(0.0, article("zero")))
    server.route("h1.test", "/p", hanging(10))
    frozen = datetime(2026, 10, 9, 12, 0).astimezone()
    pipeline = make_pipeline(server)
    _, runner = make_runner(
        pipeline,
        provider(url(server, "h0.test"), url(server, "h1.test")),
        job_timeout_s=1.0,
        clock=lambda: frozen,
    )
    try:
        job = await run_job(runner)
    finally:
        await pipeline.crawler.aclose()
    assert job.status is S.PARTIAL
    assert [w.code for w in job.warnings] == ["JOB_DEADLINE_EXCEEDED"]
    assert job.result is not None and job.result.dedup is None
    assert job.result.stage_outcomes[-1].status == "NOT_RUN"
