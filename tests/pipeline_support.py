"""Sprint 07 test helpers: a local-server pipeline (real S04 crawler, S05 extractor, S06 dedup),
pipeline job runners and the pipeline benchmark runner.

Only 127.0.0.1 is contacted: ``*.test`` hosts resolve to loopback through the S04 test overrides.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from research_agent.core.models import SearchOptions
from research_agent.crawler import Crawler, CrawlerSettings, UnsafeTestOverrides
from research_agent.extraction import ExtractionSettings
from research_agent.jobs import InMemoryJobRepository, JobRunner, ResearchJob, ResearchJobRequest
from research_agent.jobs.models import ResearchPlan
from research_agent.pipeline.config import PipelineSettings
from research_agent.pipeline.search import SearchService
from research_agent.pipeline.sources import Pipeline
from tests.crawler_support import LocalServer, Req, fake_resolver, page, redirect, send
from tests.fakes import RecordingSleep, ScriptedSearchProvider

HOSTS = (*(f"h{i}.test" for i in range(70)), "a.test", "b.test", "c.test")

Handler = Callable[[Req, asyncio.StreamWriter], Awaitable[None]]


def article(label: str, body: str | None = None) -> str:
    text = body or (
        f"<p>{label}: Phở bò Nha Trang giá 45.000đ một tô, mở cửa từ 6 giờ sáng.</p>"
        "<p>Second paragraph with more words so the page is real content.</p>"
    )
    return f"<html><head><title>{label}</title></head><body><article>{text}</article></body></html>"


def delayed(
    seconds: float, body: str, *, content_type: str = "text/html; charset=utf-8"
) -> Handler:
    data = body.encode("utf-8")

    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        if seconds:
            await asyncio.sleep(seconds)
        await send(writer, 200, {"Content-Type": content_type}, data)

    return handler


def hanging(seconds: float = 30.0) -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(seconds)

    return handler


async def start_server() -> LocalServer:
    server = LocalServer()
    await server.start()
    server.route("*", "/robots.txt", page(b"", status=404))
    return server


def url(server: LocalServer, host: str, path: str = "/p") -> str:
    return f"http://{host}:{server.port}{path}"


def crawler_settings(**values: Any) -> CrawlerSettings:
    base: dict[str, Any] = {
        "per_host_min_interval_s": 0.0,
        "retry_backoff_s": 0.0,
        "connect_timeout_s": 2.0,
        "read_timeout_s": 2.0,
        "timeout_s": 5.0,
        "retries": 0,
    }
    base.update(values)
    return CrawlerSettings(_env_file=None, **base)


def make_pipeline(
    server: LocalServer,
    *,
    crawler: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    max_concurrent_jobs: int = 1,
    extraction: ExtractionSettings | None = None,
) -> Pipeline:
    cfg = crawler_settings(**(crawler or {}))
    instance = Crawler(
        cfg,
        resolver=fake_resolver,
        test_overrides=UnsafeTestOverrides(
            loopback_hosts=frozenset(HOSTS), extra_ports=frozenset({server.port})
        ),
    )
    return Pipeline.build(
        PipelineSettings(_env_file=None, enabled=True, **(settings or {})),
        instance,
        crawler_settings=cfg,
        extraction_settings=extraction or ExtractionSettings(_env_file=None),
        max_concurrent_jobs=max_concurrent_jobs,
        extra_ports=frozenset({server.port}),
    )


class ListPlanner:
    name = "test-list-v1"

    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: Any
    ) -> ResearchPlan:
        return ResearchPlan(
            planner=self.name, queries=self.queries, search_options=base_options, created_at=now
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def make_runner(
    pipeline: Pipeline | None,
    *providers: ScriptedSearchProvider,
    queries: list[str] | None = None,
    repo: InMemoryJobRepository | None = None,
    job_timeout_s: float = 60,
    stage_timeout_s: float = 30,
    strategy: Literal["fallback", "fanout"] = "fallback",
    clock: Callable[[], datetime] | None = None,
) -> tuple[InMemoryJobRepository, JobRunner]:
    repository = repo or InMemoryJobRepository()
    service = SearchService(
        list(providers),
        max_retries=0,
        strategy=strategy,
        sleep=RecordingSleep(),
    )
    runner = JobRunner(
        repository,
        service,
        base_options=SearchOptions(timeout_s=10),
        planner=ListPlanner(queries) if queries else None,
        job_timeout_s=job_timeout_s,
        search_stage_timeout_s=stage_timeout_s,
        pipeline=pipeline,
        clock=clock or _utcnow,
    )
    return repository, runner


def provider(*urls: str, name: str = "perplexity") -> ScriptedSearchProvider:
    return ScriptedSearchProvider(name, [list(urls)])


async def run_job(runner: JobRunner, query: str = "phở nha trang") -> ResearchJob:
    job, _ = await runner.submit(ResearchJobRequest(query=query))
    return await runner.run(job.id)


async def state_path(repo: InMemoryJobRepository, job_id: str) -> list[str]:
    events = await repo.list_events(job_id)
    return ["QUEUED"] + [str(e.data["to"]) for e in events if e.type == "job.state_changed"]


def dedup_signature(job: ResearchJob) -> list[tuple[str, str, int, list[int]]]:
    """Groups as (level, key, representative source position, member source positions)."""
    assert job.result is not None and job.result.dedup is not None
    positions = job.result.dedup.positions
    return [
        (level.value, g.key, positions[g.representative], [positions[m] for m in g.members])
        for level, groups in job.result.dedup.result.groups.items()
        for g in groups
    ]


def redirect_to(location: str) -> Handler:
    return redirect(location)


# -- benchmark (subprocess per scenario; peak RSS growth via VmHWM reset) -----------------

BENCHMARKS = ("crawl_30_small", "crawl_30_large_unicode", "normalize_30m", "two_jobs_30_small")
SMALL_BODY = article(
    "bench", "<p>" + ("Phở bò Nha Trang giá 45.000đ. " * 3_300) + "</p>"
)  # ≈ 100 KB
_UNIT = "\U0001f600 Ph\u1edf b\u00f2 \U0001f468\u200d\U0001f469\u200d\U0001f467 "
LARGE_BODY = _UNIT * ((5 * 1024 * 1024 - 64) // len(_UNIT.encode("utf-8")))
"""Just under the S04 5 MiB cap, mostly 4-byte UTF-8 (S05 truncates it at 1 M chars)."""


def _proc_kib(key: str) -> int:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith(key):
                return int(line.split()[1])
    return 0


async def _bench(name: str) -> dict[str, Any]:
    server = await start_server()
    jobs = 2 if name == "two_jobs_30_small" else 1
    large = name == "crawl_30_large_unicode"
    body = LARGE_BODY if large else SMALL_BODY
    content_type = "text/plain; charset=utf-8" if large else "text/html; charset=utf-8"
    for i in range(30 * jobs):
        server.route(HOSTS[i], "/p", delayed(0.0, f"{i} {body}", content_type=content_type))
    pipeline = make_pipeline(server, max_concurrent_jobs=jobs)
    runners = []
    for j in range(jobs):
        urls = [url(server, HOSTS[j * 30 + i]) for i in range(30)]
        runners.append(
            make_runner(pipeline, ScriptedSearchProvider("perplexity", [urls]), job_timeout_s=600)[
                1
            ]
        )
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as refs:  # noqa: ASYNC230
            refs.write("5")
    except OSError:
        pass
    before = _proc_kib("VmRSS")
    started = time.perf_counter()
    done = await asyncio.gather(*(run_job(r) for r in runners))
    duration = time.perf_counter() - started
    peak = _proc_kib("VmHWM") - before
    await pipeline.crawler.aclose()
    await server.stop()
    stats = [d.result.crawl_stats for d in done if d.result and d.result.crawl_stats]
    return {
        "scenario": name,
        "jobs": jobs,
        "statuses": [d.status.value for d in done],
        "duration_s": round(duration, 3),
        "peak_rss_growth_mb": round(peak / 1024, 1),
        "fetch_ok": sum(s.fetch_ok for s in stats),
        "fetch_timeout": sum(s.fetch_timeout for s in stats),
        "extracted": sum(s.extracted for s in stats),
        "extraction": [s.extraction_status_counts for s in stats],
        "bodies_max_held": pipeline.bodies.max_held,
        "admission_max_in_flight": pipeline.admission.max_in_flight,
        "text_chars": sum(s.text_chars for s in stats),
    }


async def _bench_normalize() -> dict[str, Any]:
    """(c) NORMALIZING at the static text maximum: 30 SUCCESS documents x 1 000 000 chars."""
    from research_agent.dedup import group_documents
    from tests.dedup_support import make_doc

    body = "Đoạn văn về phở bò Nha Trang, giá 45.000đ một tô. " * 20_000
    docs = [
        make_doc(f"{i % 10}" + body[: 1_000_000 - 1], url=f"https://n{i}.example/")
        for i in range(30)
    ]
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as refs:  # noqa: ASYNC230
            refs.write("5")
    except OSError:
        pass
    before = _proc_kib("VmRSS")
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        result = await asyncio.to_thread(group_documents, docs)
        best = min(best, time.perf_counter() - started)
        del result
    return {
        "scenario": "normalize_30m",
        "input_chars": sum(len(d.text) for d in docs),
        "duration_s": round(best, 4),
        "peak_rss_growth_mb": round((_proc_kib("VmHWM") - before) / 1024, 1),
    }


def run_benchmark(name: str) -> None:
    coro = _bench_normalize() if name == "normalize_30m" else _bench(name)
    json.dump(asyncio.run(coro), sys.stdout)


def _main(argv: Sequence[str]) -> None:
    run_benchmark(argv[1])


if __name__ == "__main__":
    _main(sys.argv)
