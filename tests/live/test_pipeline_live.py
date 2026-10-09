"""Real-web pipeline job: scripted search hits → S04 → S05 → S06 through the runner (opt-in).

Run: ``RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s tests/live/test_pipeline_live.py``
(behind TLS inspection also set ``CRAWL_CA_BUNDLE``; verification stays on). Without
RUN_LIVE_NETWORK the test is skipped as REQUIRES_NETWORK — never a PASS. Search is scripted (no
search API key needed); only egress-allowed hosts; nothing is sent to an LLM.

F-1 rule: a PASS requires every source to be fetched ``OK`` and extracted ``SUCCESS`` before any
pipeline assertion runs; a failed fetch fails the test."""

from __future__ import annotations

import json
import os

import pytest

from research_agent.core.models import SearchOptions
from research_agent.crawler import Crawler, CrawlerSettings, FetchStatus
from research_agent.extraction.models import ExtractionStatus
from research_agent.jobs import InMemoryJobRepository, JobRunner, JobState, ResearchJobRequest
from research_agent.pipeline.config import PipelineSettings
from research_agent.pipeline.search import SearchService
from research_agent.pipeline.sources import Pipeline
from tests.fakes import RecordingSleep, ScriptedSearchProvider

pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_NETWORK") != "1",
        reason="REQUIRES_NETWORK: set RUN_LIVE_NETWORK=1 to fetch real websites",
    ),
]

URLS = [
    "https://pypi.org/project/httpx/",
    "https://pypi.org/project/httpx/?utm_source=probe",  # same S01 normalized URL: one source
    "https://pypi.org/project/httpx/0.28.1/",
    "https://pypi.org/help/",
]


async def test_real_web_pipeline_job() -> None:
    crawler_settings = CrawlerSettings()
    crawler = Crawler(crawler_settings)
    pipeline = Pipeline.build(
        PipelineSettings(_env_file=None, enabled=True), crawler, crawler_settings=crawler_settings
    )
    repo = InMemoryJobRepository()
    service = SearchService(
        [ScriptedSearchProvider("perplexity", [URLS])], max_retries=0, sleep=RecordingSleep()
    )
    runner = JobRunner(repo, service, base_options=SearchOptions(), pipeline=pipeline)
    try:
        job, _ = await runner.submit(ResearchJobRequest(query="httpx python client"))
        job = await runner.run(job.id)
    finally:
        await crawler.aclose()
    result = job.result
    assert result is not None
    for source in result.sources:
        print(
            json.dumps(
                {
                    "position": source.position,
                    "url": source.url,
                    "state": source.state,
                    "fetch": source.fetch.status.value if source.fetch else None,
                    "document": source.document_status.value if source.document_status else None,
                    "chars": source.text_chars,
                }
            )
        )
    print(
        json.dumps(
            {
                "status": job.status.value,
                "warnings": [w.code for w in job.warnings],
                "stats": result.crawl_stats.model_dump() if result.crawl_stats else None,
            }
        )
    )
    # 1. evidence first: every source fetched and extracted
    assert len(result.sources) == 3  # the utm variant was merged by S01 before fetching
    for source in result.sources:
        assert source.fetch is not None and source.fetch.status is FetchStatus.OK, source.position
        assert source.document_status is ExtractionStatus.SUCCESS, source.position
    # 2. pipeline behaviour
    assert job.status is JobState.COMPLETED and job.error is None
    assert result.dedup is not None and result.dedup.positions == [0, 1, 2]
    for _level, groups in result.dedup.result.groups.items():
        for group in groups:
            members = [result.dedup.positions[m] for m in group.members]
            assert result.dedup.positions[group.representative] == min(members)
    assert result.crawl_stats is not None and result.crawl_stats.fetch_timeout == 0
