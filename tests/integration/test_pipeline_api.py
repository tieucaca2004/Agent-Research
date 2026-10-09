"""Sprint 07 API compatibility: pipeline off = Sprint 03 contract; pipeline on = additive source
summaries without page text (D9, D10)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from research_agent.api import ApiSettings, create_app
from research_agent.pipeline.config import PipelineConfigurationError, PipelineSettings
from research_agent.pipeline.search import SearchService
from research_agent.pipeline.sources import Pipeline
from tests.conftest import build_settings
from tests.crawler_support import LocalServer
from tests.fakes import RecordingSleep, ScriptedSearchProvider
from tests.integration.test_api import create, data, error, run_to_end
from tests.pipeline_support import article, delayed, hanging, make_pipeline, start_server, url

MARKER = "UNIQUE-PAGE-TEXT-MARKER"


@asynccontextmanager
async def api(
    *urls: str,
    pipeline: Pipeline | None = None,
    pipeline_settings: PipelineSettings | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    service = SearchService(
        [ScriptedSearchProvider("perplexity", [list(urls)])], max_retries=0, sleep=RecordingSleep()
    )
    app = create_app(
        ApiSettings(_env_file=None),
        search_service=service,
        search_settings=build_settings(),
        pipeline=pipeline,
        pipeline_settings=pipeline_settings,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


@pytest.fixture
async def server() -> AsyncIterator[LocalServer]:
    srv = await start_server()
    yield srv
    await srv.stop()


async def test_pipeline_off_keeps_the_sprint_03_contract() -> None:
    async with api("https://a.example/", "https://b.example/") as (client, app):
        created = await create(client)
        assert set(created.json()["data"]) == {"job_id", "status", "created_at"}
        job_id = created.json()["data"]["job_id"]
        await asyncio.wait_for(app.state.service.executor.wait(job_id), 5)
        job = data(await client.get(f"/research/{job_id}"))
        assert job["stages"] == ["PLANNING", "SEARCHING"]
        assert job["result_summary"] == {"coverage": "COMPLETE", "results": 2}
        assert {k: job["progress"][k] for k in ("urls_total", "documents", "groups")} == {
            "urls_total": None,
            "documents": None,
            "groups": None,
        }
        results = data(await client.get(f"/research/{job_id}/results"))
        assert results["sources"] is None and results["dedup"] is None
        assert [r["url"] for r in results["results"]] == [
            "https://a.example/",
            "https://b.example/",
        ]


async def test_pipeline_on_returns_source_summaries_without_page_text(server: LocalServer) -> None:
    body = f"<p>{MARKER} Phở bò Nha Trang giá 45.000đ, đủ dài để là nội dung thật của trang.</p>"
    server.route("h0.test", "/p", delayed(0.0, article("A", body)))
    server.route("h1.test", "/p", delayed(0.0, article("A", body)))
    pipeline = make_pipeline(server)
    try:
        async with api(
            url(server, "h0.test"),
            url(server, "h1.test"),
            url(server, "h2.test"),
            pipeline=pipeline,
        ) as (client, app):
            job_id = await run_to_end(client, app)
            job_response = await client.get(f"/research/{job_id}")
            job = data(job_response)
            assert job["stages"] == ["PLANNING", "SEARCHING", "CRAWLING", "NORMALIZING"]
            assert job["status"] == "COMPLETED"
            assert job["result_summary"] == {"coverage": "COMPLETE", "results": 3}
            assert job["progress"]["urls_total"] == 3 and job["progress"]["documents"] == 3
            response = await client.get(f"/research/{job_id}/results")
            results = data(response)
    finally:
        await pipeline.crawler.aclose()
    assert MARKER not in response.text and MARKER not in job_response.text  # D9: no page text
    sources = results["sources"]
    assert [s["position"] for s in sources] == [0, 1, 2]
    assert [s["fetch_status"] for s in sources] == ["OK", "OK", "HTTP_ERROR"]
    assert [s["document_status"] for s in sources] == ["SUCCESS", "SUCCESS", "NOT_FETCHED"]
    assert set(sources[0]["groups"]) == {"L2", "L3"} and sources[2]["groups"] == {}
    assert "text" not in sources[0] and sources[0]["text_chars"] > 0
    groups = {g["level"]: (g["representative"], g["members"]) for g in results["dedup"]["groups"]}
    assert groups == {"L2": (0, [0, 1]), "L3": (0, [0, 1])}
    assert [w["code"] for w in results["warnings"]] == ["FETCH_FAILURES"]


async def test_new_stages_are_not_finished_for_results(server: LocalServer) -> None:
    server.route("h0.test", "/p", hanging(10))
    pipeline = make_pipeline(server)
    try:
        async with api(url(server, "h0.test"), pipeline=pipeline) as (client, _app):
            job_id = (await create(client)).json()["data"]["job_id"]
            async with asyncio.timeout(5):
                while data(await client.get(f"/research/{job_id}"))["status"] != "CRAWLING":  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            pending = await client.get(f"/research/{job_id}/results")
            assert pending.status_code == 409
            assert error(pending)["details"] == {"status": "CRAWLING"}
            cancelled = await client.post(f"/research/{job_id}/cancel")
            assert cancelled.status_code in (200, 202)
    finally:
        await pipeline.crawler.aclose()


async def test_startup_refuses_a_configuration_over_the_text_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACT_MAX_TEXT_CHARS", "2000000")  # 30 x 2 M > 30 M
    with pytest.raises(PipelineConfigurationError):
        async with api(
            "https://a.example/", pipeline_settings=PipelineSettings(_env_file=None, enabled=True)
        ):
            pass


async def test_enabled_setting_builds_and_closes_a_shared_pipeline() -> None:
    async with api(
        "https://a.example/", pipeline_settings=PipelineSettings(_env_file=None, enabled=True)
    ) as (client, app):
        runner: Any = app.state.service._runner
        assert runner._pipeline is not None and runner._stages[-1].value == "NORMALIZING"
        assert (await client.get("/health")).status_code == 200
