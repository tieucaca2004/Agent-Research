"""Sprint 03: Research API over the unchanged Sprint 01/02 components.

ASGI in-process (httpx.ASGITransport + real lifespan); providers are test doubles, no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI

from research_agent.api import ApiSettings, create_app
from research_agent.api.service import ResearchService
from research_agent.config import Settings
from research_agent.core.errors import ProviderAuthError
from research_agent.core.models import SearchOptions, SearchResult
from research_agent.jobs import JobState, ResearchJob
from research_agent.logging import configure_logging
from research_agent.pipeline.search import SearchService
from tests.conftest import build_settings
from tests.fakes import RecordingSleep, ScriptedSearchProvider
from tests.integration.test_job_runner import ControlledProvider, ListPlanner

QUERY = "món Nhật Nha Trang"


@asynccontextmanager
async def api(
    *providers: ScriptedSearchProvider,
    planner: Any = None,
    search_settings: Settings | None = None,
    search_service: SearchService | bool | None = True,
    **settings: Any,
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    service = (
        SearchService(list(providers), max_retries=0, sleep=RecordingSleep())
        if search_service is True
        else search_service or None
    )
    app = create_app(
        ApiSettings(_env_file=None, **settings),
        search_service=service,
        search_settings=search_settings or build_settings(),
        planner=planner,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


def svc(app: FastAPI) -> ResearchService:
    service: ResearchService = app.state.service
    return service


async def wait_done(app: FastAPI, job_id: str) -> None:
    executor = svc(app).executor
    assert executor is not None
    await asyncio.wait_for(executor.wait(job_id), timeout=5)


async def create(client: httpx.AsyncClient, query: str = QUERY, **headers: str) -> httpx.Response:
    return await client.post("/research", json={"query": query}, headers=headers)


async def run_to_end(client: httpx.AsyncClient, app: FastAPI, query: str = QUERY) -> str:
    response = await create(client, query)
    assert response.status_code == 202, response.text
    job_id: str = response.json()["data"]["job_id"]
    await wait_done(app, job_id)
    return job_id


def data(response: httpx.Response) -> Any:
    return response.json()["data"]


def error(response: httpx.Response) -> Any:
    return response.json()["error"]


# --- 1. lifecycle ----------------------------------------------------------------------


async def test_api_lifecycle_create_poll_results() -> None:
    p = ScriptedSearchProvider("perplexity", [["https://a.example/", "https://b.example/"]])
    async with api(p) as (client, app):
        created = await create(client)
        assert created.status_code == 202
        body = created.json()
        assert body["error"] is None
        assert body["meta"]["idempotent_replay"] is False
        assert body["meta"]["request_id"] == created.headers["x-request-id"]
        job_id = body["data"]["job_id"]
        assert body["data"]["status"] == "QUEUED"
        assert set(body["data"]) == {"job_id", "status", "created_at"}

        await wait_done(app, job_id)
        job = data(await client.get(f"/research/{job_id}"))
        assert job["status"] == "COMPLETED"
        assert job["stage"] is None
        assert job["stages"] == ["PLANNING", "SEARCHING"]
        assert job["query"] == QUERY
        assert job["result_summary"] == {"coverage": "COMPLETE", "results": 2}
        assert job["finished_at"] and job["started_at"] and job["deadline_at"]
        assert "version" not in job and "worker_id" not in job and "plan" not in job

        results = await client.get(f"/research/{job_id}/results")
        assert results.status_code == 200
        r = data(results)
        assert (r["status"], r["coverage"]) == ("COMPLETED", "COMPLETE")
        assert [i["url"] for i in r["results"]] == ["https://a.example/", "https://b.example/"]
        assert r["results"][0]["provider"] == "perplexity"
        assert r["query_outcomes"] == [{"index": 0, "status": "COVERED", "result_count": 2}]
        assert r["error"] is None


async def test_running_job_state_and_results_not_ready() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(p) as (client, _app):
        job_id = data(await create(client))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        job = data(await client.get(f"/research/{job_id}"))
        assert (job["status"], job["stage"]) == ("SEARCHING", "SEARCHING")
        assert job["result_summary"] is None
        pending = await client.get(f"/research/{job_id}/results")
        assert pending.status_code == 409
        assert error(pending)["code"] == "CONFLICT"
        assert error(pending)["reason"] == "JOB_NOT_FINISHED"
        assert error(pending)["details"] == {"status": "SEARCHING"}


async def test_request_does_not_own_job_lifetime() -> None:
    """The POST finishes immediately; the job keeps running afterwards in the executor."""
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(p) as (client, app):
        response = await create(client)
        assert response.status_code == 202  # request fully completed here
        job_id = data(response)["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        assert data(await client.get(f"/research/{job_id}"))["status"] == "SEARCHING"
        assert svc(app).executor is not None and svc(app).executor.running == 1  # type: ignore[union-attr]


# --- 2/3. idempotency ------------------------------------------------------------------


async def test_idempotent_replay_returns_same_job_without_new_execution() -> None:
    p = ScriptedSearchProvider("p", [["https://a/"]])
    async with api(p) as (client, app):
        first = await client.post(
            "/research", json={"query": QUERY}, headers={"Idempotency-Key": "key-1"}
        )
        assert first.status_code == 202
        job_id = data(first)["job_id"]
        await wait_done(app, job_id)
        replay = await client.post(
            "/research", json={"query": f"  {QUERY}   "}, headers={"Idempotency-Key": "key-1"}
        )
        assert replay.status_code == 200
        assert data(replay)["job_id"] == job_id
        assert data(replay)["status"] == "COMPLETED"  # current state of the original job
        assert replay.json()["meta"]["idempotent_replay"] is True
        assert p.calls == [QUERY]  # executed once


async def test_conflicting_idempotency_payload_is_409_and_creates_nothing() -> None:
    p = ScriptedSearchProvider("p", [["https://a/"]])
    async with api(p) as (client, app):
        first = await client.post(
            "/research", json={"query": QUERY}, headers={"Idempotency-Key": "k"}
        )
        conflict = await client.post(
            "/research", json={"query": "different research"}, headers={"Idempotency-Key": "k"}
        )
        assert conflict.status_code == 409
        assert error(conflict)["code"] == "CONFLICT"
        assert error(conflict)["reason"] == "IDEMPOTENCY_KEY_REUSED"
        assert "different research" not in conflict.text
        await wait_done(app, data(first)["job_id"])
        health = data(await client.get("/health"))
        assert health["jobs"] == {"running": 0, "queued": 0}  # reservation released


async def test_concurrent_same_key_creates_exactly_one_job() -> None:
    p = ScriptedSearchProvider("p", [["https://a/"]])
    async with api(p, max_queued_jobs=20) as (client, app):
        responses = await asyncio.gather(
            *(
                client.post("/research", json={"query": QUERY}, headers={"Idempotency-Key": "same"})
                for _ in range(6)
            )
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 200, 200, 200, 200, 202]
        assert len({data(r)["job_id"] for r in responses}) == 1
        await wait_done(app, data(responses[0])["job_id"])
        assert p.calls == [QUERY]


@pytest.mark.parametrize("key", ["", "has space", "x" * 129, "ümlaut"])
async def test_invalid_idempotency_key_rejected(key: str) -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, _):
        response = await client.post(
            "/research", json={"query": QUERY}, headers={b"Idempotency-Key": key.encode("utf-8")}
        )
        assert response.status_code == 422
        assert error(response)["code"] == "VALIDATION_ERROR"


# --- 4/5/6. cancellation ----------------------------------------------------------------


async def test_cancel_running_job_then_idempotent_and_propagated() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(p) as (client, app):
        job_id = data(await create(client))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        cancel = await client.post(f"/research/{job_id}/cancel")
        assert cancel.status_code in (200, 202)
        await wait_done(app, job_id)
        job = data(await client.get(f"/research/{job_id}"))
        assert job["status"] == "CANCELLED"
        assert job["error"] is None
        assert p.cancelled == 1 and p.in_flight == 0  # runner cancellation reached the provider
        again = await client.post(f"/research/{job_id}/cancel")
        assert again.status_code == 200
        assert data(again)["status"] == "CANCELLED"


async def test_cancel_while_running_reports_202_with_flag() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(p) as (client, app):
        job_id = data(await create(client))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        # cancel through the repository flag path only, so the job is observed mid-cancel
        await svc(app)._repo.request_cancel(job_id, now=datetime.now(UTC))
        response = await client.post(f"/research/{job_id}/cancel")
        assert response.status_code == 202
        assert data(response)["cancel_requested"] is True
        assert data(response)["status"] == "SEARCHING"


async def test_cancel_finished_job_is_409_and_unknown_is_404() -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, app):
        job_id = await run_to_end(client, app)
        finished = await client.post(f"/research/{job_id}/cancel")
        assert finished.status_code == 409
        assert error(finished)["reason"] == "JOB_ALREADY_FINISHED"
        assert error(finished)["details"] == {"status": "COMPLETED"}
        unknown = await client.post(f"/research/{'0' * 32}/cancel")
        assert unknown.status_code == 404 and error(unknown)["reason"] == "JOB_NOT_FOUND"
        malformed = await client.get("/research/../etc")
        assert malformed.status_code == 404


async def test_cancel_before_runner_claim_stays_cancelled_without_search(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Required (probe A4): job created → cancelled before the runner claims it → stays
    CANCELLED, never FAILED, and no search is executed. JobNotClaimableError is expected flow."""
    blocker = ControlledProvider("p", [["https://x/"]], hang_on=["blocking query"])
    configure_logging("INFO", "json")
    try:
        async with api(blocker, max_concurrent_jobs=1) as (client, app):
            a = data(await create(client, "blocking query"))["job_id"]
            await asyncio.wait_for(blocker.hanging.wait(), 2)
            b = data(await create(client, "waiting query"))["job_id"]
            assert data(await client.get(f"/research/{b}"))["status"] == "QUEUED"

            cancel_b = await client.post(f"/research/{b}/cancel")
            assert cancel_b.status_code == 200
            assert data(cancel_b)["status"] == "CANCELLED"

            await client.post(f"/research/{a}/cancel")  # frees the slot; B's task now runs
            await wait_done(app, a)
            await wait_done(app, b)
            job_b = data(await client.get(f"/research/{b}"))
            assert job_b["status"] == "CANCELLED"
            assert job_b["error"] is None
            assert job_b["started_at"] is None  # never claimed
            assert "waiting query" not in blocker.calls  # no search execution
            events = [e.type for e in await svc(app)._repo.list_events(b)]
            assert "job.claimed" not in events and "job.failed" not in events
        out = capsys.readouterr().out
    finally:
        structlog.reset_defaults()
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    not_started = [r for r in records if r["event"] == "job.not_started"]
    assert [r["job_id"] for r in not_started] == [b]
    assert not [r for r in records if r["event"] == "job.task_error"]


async def test_cancel_vs_completion_race_is_always_consistent() -> None:
    seen: set[tuple[int, str]] = set()
    for k in range(0, 40):
        p = ScriptedSearchProvider("p", [["https://a/"]])
        async with api(p) as (client, app):
            job_id = data(await create(client))["job_id"]
            for _ in range(k):
                await asyncio.sleep(0)
            cancel = await client.post(f"/research/{job_id}/cancel")
            await wait_done(app, job_id)
            final = data(await client.get(f"/research/{job_id}"))["status"]
            pair = (cancel.status_code, final)
            assert pair in {(200, "CANCELLED"), (202, "CANCELLED"), (409, "COMPLETED")}, pair
            seen.add(pair)
    assert (409, "COMPLETED") in seen  # both race orders were exercised
    assert any(status == "CANCELLED" for _, status in seen)


# --- 7/8/9. results and timeout mapping ---------------------------------------------------


class FailOnQueryTwo(ScriptedSearchProvider):
    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if query == "q two":
            self.calls.append(query)
            raise ProviderAuthError(self.name, "401")
        return await super().search(query, options)


class ExplodeOnQueryTwo(ScriptedSearchProvider):
    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if query == "q two":
            raise ZeroDivisionError("internal detail")
        return await super().search(query, options)


async def test_partial_job_results() -> None:
    p = FailOnQueryTwo("only", [["https://q1.example/"]])
    async with api(p, planner=ListPlanner(["q one", "q two"])) as (client, app):
        job_id = await run_to_end(client, app)
        r = data(await client.get(f"/research/{job_id}/results"))
        assert (r["status"], r["coverage"]) == ("PARTIAL", "PARTIAL")
        assert [i["url"] for i in r["results"]] == ["https://q1.example/"]
        assert [o["status"] for o in r["query_outcomes"]] == ["COVERED", "FAILED"]
        assert r["warnings"][0]["code"] == "PROVIDER_FAILURES"
        assert r["warnings"][0]["provider_errors"] == [
            {"provider": "only", "code": "PROVIDER_AUTH_FAILED", "category": "AUTHENTICATION_ERROR"}
        ]


async def test_failed_job_with_partial_result_is_retrievable() -> None:
    p = ExplodeOnQueryTwo("p", [["https://q1.example/"]])
    async with api(p, planner=ListPlanner(["q one", "q two"])) as (client, app):
        job_id = await run_to_end(client, app)
        response = await client.get(f"/research/{job_id}/results")
        assert response.status_code == 200
        r = data(response)
        assert (r["status"], r["coverage"]) == ("FAILED", "PARTIAL")
        assert [i["url"] for i in r["results"]] == ["https://q1.example/"]
        assert (r["error"]["code"], r["error"]["step"]) == ("INTERNAL_ERROR", "SEARCHING")
        assert "internal detail" not in response.text


async def test_failed_job_without_result_uses_error_contract() -> None:
    p = ScriptedSearchProvider("perplexity", [ProviderAuthError("perplexity", "401")])
    async with api(p) as (client, app):
        job_id = await run_to_end(client, app)
        response = await client.get(f"/research/{job_id}/results")
        assert response.status_code == 409
        body = response.json()
        assert body["data"] is None
        assert (body["error"]["code"], body["error"]["reason"]) == ("CONFLICT", "JOB_FAILED")
        job_error = body["error"]["details"]["job_error"]
        assert (job_error["code"], job_error["category"]) == (
            "ALL_SEARCH_PROVIDERS_FAILED",
            "PROVIDER_ERROR",
        )
        assert job_error["provider_errors"] == [
            {
                "provider": "perplexity",
                "code": "PROVIDER_AUTH_FAILED",
                "category": "AUTHENTICATION_ERROR",
            }
        ]


async def test_cancelled_job_results_keep_captured_items() -> None:
    p = ControlledProvider("p", [["https://q1.example/"]], hang_on=["q two"])
    async with api(p, planner=ListPlanner(["q one", "q two"])) as (client, app):
        job_id = data(await create(client))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        await client.post(f"/research/{job_id}/cancel")
        await wait_done(app, job_id)
        r = data(await client.get(f"/research/{job_id}/results"))
        assert (r["status"], r["coverage"]) == ("CANCELLED", "PARTIAL")
        assert [i["url"] for i in r["results"]] == ["https://q1.example/"]
        assert [o["status"] for o in r["query_outcomes"]] == ["COVERED", "INTERRUPTED"]


async def test_timeout_mapping_job_deadline_partial_and_failed() -> None:
    p = ControlledProvider("p", [["https://q1.example/"]], hang_on=["q two"])
    async with api(p, planner=ListPlanner(["q one", "q two"]), job_timeout_s=0.3) as (client, app):
        job_id = await run_to_end(client, app)
        r = data(await client.get(f"/research/{job_id}/results"))
        assert (r["status"], r["coverage"]) == ("PARTIAL", "PARTIAL")
        assert [(w["code"], w["category"]) for w in r["warnings"]] == [
            ("JOB_DEADLINE_EXCEEDED", "TIMEOUT")
        ]

    hang = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(hang, job_search_timeout_s=0.2) as (client, app):
        job_id = await run_to_end(client, app)
        job = data(await client.get(f"/research/{job_id}"))
        assert job["status"] == "FAILED"
        assert (job["error"]["code"], job["error"]["category"]) == (
            "SEARCH_STAGE_TIMEOUT",
            "TIMEOUT",
        )
        response = await client.get(f"/research/{job_id}/results")
        assert response.status_code == 409
        assert error(response)["details"]["job_error"]["code"] == "SEARCH_STAGE_TIMEOUT"


async def test_provider_timeout_is_a_provider_warning_not_a_job_timeout() -> None:
    slow = ControlledProvider("perplexity", [["https://never/"]], hang_on=[QUERY])
    fallback = ScriptedSearchProvider("google", [["https://g/"]])
    async with api(slow, fallback, search_settings=build_settings(search_timeout_s=0.05)) as (
        client,
        app,
    ):
        job_id = await run_to_end(client, app)
        r = data(await client.get(f"/research/{job_id}/results"))
        assert r["status"] == "COMPLETED"
        assert [(e["provider"], e["category"]) for e in r["warnings"][0]["provider_errors"]] == [
            ("perplexity", "TIMEOUT")
        ]


# --- 10. request / job ID separation ----------------------------------------------------


async def test_http_request_id_job_id_and_search_request_id_are_separate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_query = "Nguyễn Văn A 0901234567 sushi"
    configure_logging("INFO", "json")
    try:
        async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, app):
            response = await client.post(
                "/research", json={"query": secret_query}, headers={"X-Request-ID": "client-req.1"}
            )
            job_id = data(response)["job_id"]
            await wait_done(app, job_id)
            bad = await client.get(f"/research/{job_id}", headers={"X-Request-ID": "x" * 65})
            events = await svc(app)._repo.list_events(job_id)
        out = capsys.readouterr().out
    finally:
        structlog.reset_defaults()

    assert response.headers["x-request-id"] == "client-req.1"
    assert response.json()["meta"]["request_id"] == "client-req.1"
    assert len(bad.headers["x-request-id"]) == 32 and bad.headers["x-request-id"] != "x" * 65
    submitted = [e for e in events if e.type == "api.job_submitted"]
    assert [e.data for e in submitted] == [{"http_request_id": "client-req.1"}]

    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    http_lines = [r for r in records if r["event"] == "http.request"]
    assert http_lines and all("http_request_id" in r for r in http_lines)
    assert all(r.get("request_id") != "client-req.1" for r in records)  # key never reused
    execution = [
        r for r in records if r["event"] in ("job.claimed", "job.stage_started", "job.completed")
    ]
    assert execution and all(
        r["job_id"] == job_id and "http_request_id" not in r for r in execution
    )
    search = [r for r in records if str(r["event"]).startswith("search.")]
    assert search and all(r["request_id"] == f"{job_id}:search:0" for r in search)
    assert secret_query not in out and "0901234567" not in out


# --- 11. bounded concurrency ------------------------------------------------------------


async def test_bounded_concurrency_and_capacity_rejection() -> None:
    p = ControlledProvider("p", [["https://ok/"]], hang_on=["job a query"])
    async with api(p, max_concurrent_jobs=1, max_queued_jobs=1) as (client, app):
        a = data(await create(client, "job a query"))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        b = data(await create(client, "job b query"))["job_id"]
        rejected = await create(client, "job c query")
        assert rejected.status_code == 503
        assert error(rejected)["code"] == "CAPACITY_EXHAUSTED"
        assert rejected.headers["retry-after"] == "5"
        assert data(await client.get("/health"))["jobs"] == {"running": 1, "queued": 1}
        assert data(await client.get(f"/research/{b}"))["status"] == "QUEUED"
        await asyncio.sleep(0.05)  # give B every chance to start while A still holds the slot
        assert data(await client.get(f"/research/{b}"))["status"] == "QUEUED"
        assert p.calls == ["job a query"]  # B has not searched: the slot limit is enforced

        await client.post(f"/research/{a}/cancel")
        await wait_done(app, a)
        await wait_done(app, b)
        assert data(await client.get(f"/research/{b}"))["status"] == "COMPLETED"
        assert p.calls == ["job a query", "job b query"]  # B ran only after A released the slot
        assert "job c query" not in p.calls


async def test_default_concurrency_policy_is_conservative() -> None:
    settings = ApiSettings(_env_file=None)
    assert (settings.max_concurrent_jobs, settings.max_queued_jobs) == (2, 20)
    assert settings.api_host == "127.0.0.1"


# --- validation, errors, security --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"query": "ab"},
        {"query": "secretvalue\x00x"},
        {"query": QUERY, "language": "vie"},
        {"query": QUERY, "unexpected": "secretvalue"},
        {},
    ],
)
async def test_validation_errors_do_not_echo_input(body: dict[str, Any]) -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, _):
        response = await client.post("/research", json=body)
        assert response.status_code == 422
        assert error(response)["code"] == "VALIDATION_ERROR"
        assert "secretvalue" not in response.text
        assert error(response)["details"]["errors"]


async def test_body_size_limit_declared_and_streamed() -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]]), api_max_body_bytes=1024) as (
        client,
        _,
    ):
        big = json.dumps({"query": "x" * 5000}).encode()
        declared = await client.post(
            "/research", content=big, headers={"content-type": "application/json"}
        )
        assert declared.status_code == 413
        assert error(declared)["reason"] == "PAYLOAD_TOO_LARGE"

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(big), 256):
                yield big[i : i + 256]

        streamed = await client.post(
            "/research", content=chunks(), headers={"content-type": "application/json"}
        )
        assert streamed.status_code == 413
        assert "x-request-id" in streamed.headers


async def test_unexpected_error_is_sanitized_500() -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, app):

        async def boom(job_id: str) -> ResearchJob:
            raise ZeroDivisionError("secret internal detail /home/user/path")

        svc(app).get = boom  # type: ignore[method-assign]
        response = await client.get(f"/research/{'a' * 32}")
        assert response.status_code == 500
        assert error(response) == {"code": "INTERNAL_ERROR", "message": "internal server error"}
        assert "secret" not in response.text and "/home" not in response.text
        assert response.json()["meta"]["request_id"] == response.headers["x-request-id"]


async def test_unknown_route_and_method_use_envelope() -> None:
    async with api(ScriptedSearchProvider("p", [["https://a/"]])) as (client, _):
        missing = await client.get("/nope")
        assert missing.status_code == 404 and error(missing)["code"] == "NOT_FOUND"
        wrong = await client.delete("/research")
        assert wrong.status_code == 405 and error(wrong)["reason"] == "METHOD_NOT_ALLOWED"


async def test_no_provider_configured_503_and_health() -> None:
    async with api(search_service=None, search_settings=build_settings()) as (client, _):
        response = await create(client)
        assert response.status_code == 503
        assert error(response)["code"] == "REQUIRES_CONFIGURATION"
        assert error(response)["details"]["missing"] == [
            "GOOGLE_API_KEY",
            "GOOGLE_CSE_ID",
            "PERPLEXITY_API_KEY",
        ]
        health = data(await client.get("/health"))
        assert {p["name"]: p["status"] for p in health["providers"]} == {
            "perplexity": "REQUIRES_CONFIGURATION",
            "google": "REQUIRES_CONFIGURATION",
        }
        assert (await client.get(f"/research/{'0' * 32}")).status_code == 404


async def test_shutdown_interrupts_running_jobs() -> None:
    p = ControlledProvider("p", [["https://x/"]], hang_on=[QUERY])
    async with api(p) as (client, app):
        job_id = data(await create(client))["job_id"]
        await asyncio.wait_for(p.hanging.wait(), 2)
        repo = svc(app)._repo
    stored = await repo.get(job_id)  # lifespan exited → executor.shutdown()
    assert stored.status is JobState.FAILED
    assert stored.error is not None and stored.error.code == "RUNNER_INTERRUPTED"
    assert p.cancelled == 1
