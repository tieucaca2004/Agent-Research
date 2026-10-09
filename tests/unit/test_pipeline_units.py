"""Sprint 07: selection (OD-13 canonical prefix), budgets (OD-14), records without page content."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.core.models import DedupedSearchResult, SearchResponse
from research_agent.crawler import Crawler, CrawlerSettings, FetchStatus
from research_agent.crawler.models import RedirectHop
from research_agent.extraction import ExtractionSettings
from research_agent.jobs.models import JobProgress, JobResult, JobState
from research_agent.pipeline.config import (
    MAX_TOTAL_TEXT_CHARS,
    MAX_URLS_PER_JOB,
    PipelineConfigurationError,
    PipelineSettings,
    check_text_budget,
)
from research_agent.pipeline.discovery import select_sources
from research_agent.pipeline.models import FetchRecord
from research_agent.pipeline.sources import Pipeline, SourceCapture, _timeout_kind
from tests.extraction_support import fetch_result
from tests.fakes import make_result


def response(n: int) -> SearchResponse:
    return SearchResponse(
        request_id="r",
        strategy="fallback",
        queries=["q"],
        results=[
            DedupedSearchResult(
                result=make_result(
                    f"https://h{i}.example/", source="perplexity", query="q", rank=i + 1
                ),
                providers=["perplexity"],
                queries=["q"],
                occurrences=1,
            )
            for i in range(n)
        ],
        provider_statuses=[],
        total_hits=n,
        duplicate_count=0,
    )


def settings(**values: object) -> PipelineSettings:
    return PipelineSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_selection_is_the_canonical_prefix() -> None:
    resp = response(45)
    selection = select_sources(resp, 30)
    assert list(selection.sources) == resp.results[:30]
    assert all(a is b for a, b in zip(selection.sources, resp.results, strict=False))
    assert selection.not_selected == 15
    assert select_sources(response(3), 30).not_selected == 0
    empty = select_sources(None, 30)
    assert empty.sources == () and empty.not_selected == 0
    with pytest.raises(ValueError):
        select_sources(resp, 0)


def test_settings_defaults_and_approved_bounds() -> None:
    cfg = settings()
    assert cfg.enabled is False  # D10
    assert (cfg.max_urls, cfg.max_total_text_chars) == (30, 30_000_000)  # D2
    assert (MAX_URLS_PER_JOB, MAX_TOTAL_TEXT_CHARS) == (30, 30_000_000)
    with pytest.raises(ValidationError):
        settings(max_urls=31)
    with pytest.raises(ValidationError):
        settings(max_total_text_chars=30_000_001)


def test_enabled_flag_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_ENABLED", "true")
    assert PipelineSettings(_env_file=None).enabled is True


def test_static_text_budget() -> None:
    check_text_budget(settings(), 1_000_000)  # 30 x 1 M = 30 M: allowed
    with pytest.raises(PipelineConfigurationError):
        check_text_budget(settings(), 2_000_000)  # 30 x 2 M > 30 M
    check_text_budget(settings(max_urls=15), 2_000_000)


async def test_pipeline_build_refuses_a_configuration_over_budget() -> None:
    crawler_cfg = CrawlerSettings(_env_file=None)
    crawler = Crawler(crawler_cfg)
    try:
        with pytest.raises(PipelineConfigurationError):
            Pipeline.build(
                settings(enabled=True),
                crawler,
                crawler_settings=crawler_cfg,
                extraction_settings=ExtractionSettings(_env_file=None, max_text_chars=2_000_000),
            )
        built = Pipeline.build(
            settings(enabled=True),
            crawler,
            crawler_settings=crawler_cfg,
            extraction_settings=ExtractionSettings(_env_file=None),
            max_concurrent_jobs=2,
        )
        assert built.admission.global_limit == crawler_cfg.concurrency == 5
        assert built.admission.per_host_limit == crawler_cfg.per_host_concurrency == 1
        assert built.admission.per_job_limit == 3  # ceil(5 / 2)
        assert built.bodies.capacity == 5 + 4 + 2  # fetching + backlog + extracting
        assert built.fetch_timeout_s == crawler_cfg.timeout_s
    finally:
        await crawler.aclose()


def test_fetch_record_never_holds_the_body() -> None:
    result = fetch_result("<html><body><p>secret page text</p></body></html>")
    record = FetchRecord.from_result(result)
    assert "content" not in FetchRecord.model_fields and "title" not in FetchRecord.model_fields
    assert "secret page text" not in record.model_dump_json()
    for name in FetchRecord.model_fields:
        assert getattr(record, name) == getattr(result, name)


def test_source_capture_records_and_position_map() -> None:
    selection = select_sources(response(4), 30)
    capture = SourceCapture(selection)
    assert [r.state for r in capture.records()] == ["NOT_RUN"] * 4
    assert capture.documents_in_order() == ([], [])
    stats = capture.stats()
    assert (stats.selected, stats.not_run, stats.fetched) == (4, 4, 0)


def _record(requested: str, final: str | None, hops: list[str]) -> FetchRecord:
    return FetchRecord(
        crawl_id="c",
        requested_url=requested,
        final_url=final,
        status=FetchStatus.FETCH_TIMEOUT,
        fetched_at=datetime(2026, 10, 9, tzinfo=UTC),
        redirect_chain=[
            RedirectHop(url=h, http_status=302, location=None, connected_ip=None) for h in hops
        ],
    )


def test_fetch_timeout_classification_from_evidence() -> None:
    cross = _record("http://a.test/r", "http://b.test/p", ["http://a.test/r"])
    same = _record("http://a.test/r", "http://a.test/p", ["http://a.test/r"])
    assert _timeout_kind(cross, capped=False) == "cross_host_redirect"
    assert _timeout_kind(same, capped=True) == "deadline_capped"
    assert _timeout_kind(same, capped=False) == "other"
    # S04 records no final URL on a timeout: the hop's Location carries the target host
    timed_out = FetchRecord(
        crawl_id="c",
        requested_url="http://a.test/r",
        final_url=None,
        status=FetchStatus.FETCH_TIMEOUT,
        fetched_at=datetime(2026, 10, 9, tzinfo=UTC),
        redirect_chain=[
            RedirectHop(
                url="http://a.test/r",
                http_status=302,
                location="http://b.test/p",
                connected_ip=None,
            )
        ],
    )
    assert _timeout_kind(timed_out, capped=False) == "cross_host_redirect"
    relative = timed_out.model_copy(
        update={
            "redirect_chain": [
                RedirectHop(
                    url="http://a.test/r", http_status=302, location="/p", connected_ip=None
                )
            ]
        }
    )
    assert _timeout_kind(relative, capped=False) == "other"


def test_job_models_are_extended_additively() -> None:
    result = JobResult()
    assert (result.sources, result.documents, result.dedup, result.crawl_stats) == (
        [],
        [],
        None,
        None,
    )
    assert result.stage_outcomes == [] and result.not_selected == 0
    progress = JobProgress(stage=JobState.SEARCHING, stage_index=1, total_stages=2)
    assert (progress.urls_total, progress.documents, progress.groups) == (0, 0, 0)
