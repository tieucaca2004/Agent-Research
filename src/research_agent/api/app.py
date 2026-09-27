"""FastAPI application: local/internal Research API boundary (Sprint 03).

NOT a public production API: authentication and rate limiting are deferred (OD6); the server
binds to 127.0.0.1 by default. Jobs live in memory and are lost on restart.

Endpoints (envelope ``{data, error, meta{request_id}}``, ARCHITECTURE §14):
    POST /research                     create + schedule (202) | idempotent replay (200)
    GET  /research/{job_id}            status / polling
    GET  /research/{job_id}/results    results (see ``get_results`` for status rules)
    POST /research/{job_id}/cancel     200 CANCELLED | 202 cancel requested | 409 finished
    GET  /health                       provider configuration + executor load
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from research_agent.api import errors
from research_agent.api.config import ApiSettings
from research_agent.api.executor import JobExecutor
from research_agent.api.middleware import RequestContextMiddleware
from research_agent.api.schemas import (
    CreateResearchBody,
    JobCreatedView,
    envelope,
    has_results,
    job_view,
    results_view,
)
from research_agent.api.service import ResearchService, cancel_http_status
from research_agent.config import Settings
from research_agent.core.errors import NoSearchProviderConfiguredError
from research_agent.jobs.models import TERMINAL_STATES, JobState, ResearchJobRequest
from research_agent.jobs.planner import ResearchPlanner
from research_agent.jobs.repository import InMemoryJobRepository
from research_agent.jobs.runner import JobRunner
from research_agent.logging import get_logger
from research_agent.pipeline.search import (
    SearchService,
    build_search_service,
    default_search_options,
)
from research_agent.providers.search import provider_statuses

log = get_logger(__name__)

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id(request: Request) -> str:
    return str(request.scope.get("state", {}).get("http_request_id", ""))


def _json(
    request: Request, status: int, data: Any, *, meta: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        envelope(data, request_id=_request_id(request), meta=meta), status_code=status
    )


def _validation_details(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    # Only location, message and type — never the rejected input value.
    return [
        {
            "loc": [str(p) for p in e.get("loc", ())],
            "msg": str(e.get("msg", "")),
            "type": e.get("type"),
        }
        for e in exc.errors()
    ]


def create_app(
    settings: ApiSettings | None = None,
    *,
    search_service: SearchService | None = None,
    search_settings: Settings | None = None,
    planner: ResearchPlanner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the app. ``search_service``/``planner`` are injectable for tests; by default the
    Sprint 01 service is built from environment configuration during startup."""
    api_settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = search_settings or Settings()
        client: httpx.AsyncClient | None = None
        service_search = search_service
        search_error: NoSearchProviderConfiguredError | None = None
        if service_search is None:
            client = httpx.AsyncClient()
            try:
                service_search = build_search_service(cfg, client)
            except NoSearchProviderConfiguredError as exc:
                search_error = exc
                log.warning("api.search_unavailable", code=exc.code)
        repository = InMemoryJobRepository()
        runner: JobRunner | None = None
        executor: JobExecutor | None = None
        if service_search is not None:
            runner_kwargs: dict[str, Any] = {}
            if clock is not None:
                runner_kwargs["clock"] = clock
            runner = JobRunner(
                repository,
                service_search,
                base_options=default_search_options(cfg),
                planner=planner,
                job_timeout_s=api_settings.job_timeout_s,
                search_stage_timeout_s=api_settings.job_search_timeout_s,
                **runner_kwargs,
            )
            executor = JobExecutor(
                runner,
                max_concurrent=api_settings.max_concurrent_jobs,
                max_queued=api_settings.max_queued_jobs,
            )
        app.state.service = ResearchService(repository, runner, executor, search_error=search_error)
        app.state.providers = (
            [
                {"name": n, "status": "CONFIGURED", "missing": []}
                for n in search_service.provider_names
            ]
            if search_service is not None
            else [
                {"name": s.name, "status": s.status, "missing": list(s.missing)}
                for s in provider_statuses(cfg)
            ]
        )
        log.info(
            "api.started",
            host=api_settings.api_host,
            max_concurrent_jobs=api_settings.max_concurrent_jobs,
            max_queued_jobs=api_settings.max_queued_jobs,
            search_available=service_search is not None,
        )
        try:
            yield
        finally:
            if executor is not None:
                await executor.shutdown()
            if client is not None:
                await client.aclose()

    app = FastAPI(
        title="Research Agent API (local/internal)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestContextMiddleware, max_body_bytes=api_settings.api_max_body_bytes)

    @app.exception_handler(errors.ApiError)
    async def _api_error(request: Request, exc: errors.ApiError) -> JSONResponse:
        return JSONResponse(
            envelope(None, request_id=_request_id(request), error=exc.body()),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        error = {
            "code": "VALIDATION_ERROR",
            "message": "request validation failed",
            "details": {"errors": _validation_details(exc)},
        }
        return JSONResponse(
            envelope(None, request_id=_request_id(request), error=error), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            error = {"code": "NOT_FOUND", "message": "resource not found"}
        elif exc.status_code == 405:
            error = {
                "code": "VALIDATION_ERROR",
                "message": "method not allowed",
                "reason": "METHOD_NOT_ALLOWED",
            }
        else:
            error = {"code": "VALIDATION_ERROR", "message": "request rejected"}
        return JSONResponse(
            envelope(None, request_id=_request_id(request), error=error),
            status_code=exc.status_code,
        )

    def service(request: Request) -> ResearchService:
        svc: ResearchService = request.app.state.service
        return svc

    def checked_job_id(job_id: str) -> str:
        if not _JOB_ID.fullmatch(job_id):
            raise errors.job_not_found()
        return job_id

    @app.post("/research")
    async def create_research(
        request: Request,
        body: CreateResearchBody,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise errors.ApiError(
                422,
                "VALIDATION_ERROR",
                "request validation failed",
                details={
                    "errors": [
                        {
                            "loc": ["header", "Idempotency-Key"],
                            "msg": "invalid format",
                            "type": "value_error",
                        }
                    ]
                },
            )
        try:
            domain_request = ResearchJobRequest(
                query=body.query,
                language=body.language,
                country=body.country,
                idempotency_key=idempotency_key,
            )
        except ValidationError as exc:
            raise errors.ApiError(
                422,
                "VALIDATION_ERROR",
                "request validation failed",
                details={
                    "errors": [{**e, "loc": ["body", *e["loc"]]} for e in _validation_details(exc)]
                },
            ) from None
        job, created = await service(request).submit(
            domain_request, http_request_id=_request_id(request)
        )
        data = JobCreatedView(job_id=job.id, status=job.status.value, created_at=job.created_at)
        return _json(
            request, 202 if created else 200, data, meta={"idempotent_replay": not created}
        )

    @app.get("/research/{job_id}")
    async def get_research(request: Request, job_id: str) -> JSONResponse:
        job = await service(request).get(checked_job_id(job_id))
        return _json(request, 200, job_view(job))

    @app.get("/research/{job_id}/results")
    async def get_results(request: Request, job_id: str) -> JSONResponse:
        """Results rules (OD4 as decided):
        - job not terminal                      → 409 JOB_NOT_FINISHED
        - COMPLETED / PARTIAL / CANCELLED       → 200 with captured results (may be empty)
        - FAILED with captured results          → 200, status FAILED, coverage, results + error
        - FAILED without any result             → 409 JOB_FAILED with the job error in details
        """
        job = await service(request).get(checked_job_id(job_id))
        if job.status not in TERMINAL_STATES:
            raise errors.job_not_finished(job.status.value)
        if job.status is JobState.FAILED and not has_results(job):
            view = results_view(job)
            raise errors.job_failed_without_results(
                view.error.model_dump(mode="json") if view.error else None
            )
        return _json(request, 200, results_view(job))

    @app.post("/research/{job_id}/cancel")
    async def cancel_research(request: Request, job_id: str) -> JSONResponse:
        job = await service(request).cancel(checked_job_id(job_id))
        return _json(request, cancel_http_status(job), job_view(job))

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        executor = service(request).executor
        data = {
            "status": "ok",
            "providers": request.app.state.providers,
            "jobs": {
                "running": executor.running if executor else 0,
                "queued": executor.queued if executor else 0,
            },
        }
        return _json(request, 200, data)

    return app
