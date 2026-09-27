"""JobRunner: executes one Research Job in-process (Sprint 02 pipeline: PLANNING → SEARCHING).

Orchestration (decision D3-B): the runner calls the unchanged Sprint 01 ``SearchService`` once
per planned query and captures each finished query immediately, so cancellation or a
deadline never discards results that were already obtained.

Time limits — one mechanism per layer:
- HTTP timeout / provider attempt timeout / provider retry+backoff: Sprint 01, untouched;
- SEARCHING stage timeout: ``asyncio.timeout`` around the stage (``search_stage_timeout_s``);
- job deadline: ``deadline_at = started_at + job_timeout_s`` set at claim; the stage runs
  under ``min(stage timeout, time left until deadline)``; boundaries re-check the deadline.
Neither is ever turned into a provider failure; no new query or fallback starts after them.

Cancellation: ``cancel(job_id)`` flags the job and cancels the in-flight search task. The
runner tells a job cancel apart from its own task being cancelled via
``Task.cancelling()``; the latter is recorded (RUNNER_INTERRUPTED) and re-raised — a
``CancelledError`` is never swallowed.

There is no job-level retry: a job runs at most once (claim is compare-and-set). Re-running a
research means creating a new job.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from research_agent.core.errors import AllSearchProvidersFailedError
from research_agent.core.models import SearchOptions
from research_agent.jobs.errors import JobVersionConflictError
from research_agent.jobs.models import (
    SPRINT_02_STAGES,
    AttemptRecord,
    Coverage,
    JobError,
    JobErrorCode,
    JobProgress,
    JobResult,
    JobState,
    ProviderErrorRecord,
    QueryOutcome,
    ResearchJob,
    ResearchJobRequest,
    ResearchPlan,
)
from research_agent.jobs.planner import FixedPlanner, ResearchPlanner
from research_agent.jobs.repository import JobRepository
from research_agent.logging import get_logger
from research_agent.pipeline.search import SearchAttempt, SearchRun, SearchService, query_hash

log = get_logger(__name__)

Clock = Callable[[], datetime]

_CONFLICT_RETRIES = 3
"""Re-read/re-apply attempts after a version conflict (the only concurrent writer is
``request_cancel``, which writes at most once per job)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _JobCancelled(Exception):
    """Internal signal: cancellation was requested for this job."""


@dataclass
class _SearchCapture:
    outcomes: list[QueryOutcome]
    runs: list[SearchRun]
    stop: JobErrorCode | None = None
    interrupted_index: int | None = None


class JobRunner:
    def __init__(
        self,
        repository: JobRepository,
        search_service: SearchService,
        *,
        base_options: SearchOptions,
        planner: ResearchPlanner | None = None,
        job_timeout_s: float = 600.0,
        search_stage_timeout_s: float = 300.0,
        worker_id: str | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        if job_timeout_s <= 0 or search_stage_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self._repo = repository
        self._search = search_service
        self._base_options = base_options
        self._planner: ResearchPlanner = planner or FixedPlanner()
        self._job_timeout_s = job_timeout_s
        self._stage_timeout_s = search_stage_timeout_s
        self._worker_id = worker_id or f"runner-{uuid.uuid4().hex[:8]}"
        self._clock = clock
        self._inflight: dict[str, asyncio.Task[SearchRun]] = {}

    # -- public API ---------------------------------------------------------------------

    async def submit(self, request: ResearchJobRequest) -> tuple[ResearchJob, bool]:
        job, created = await self._repo.create(request, now=self._clock())
        with structlog.contextvars.bound_contextvars(job_id=job.id):
            log.info(
                "job.created" if created else "job.idempotent_hit",
                query_hash=query_hash(request.query),
                stages=[s.value for s in job.stages],
            )
        return job, created

    async def cancel(self, job_id: str) -> ResearchJob:
        with structlog.contextvars.bound_contextvars(job_id=job_id):
            job = await self._repo.request_cancel(job_id, now=self._clock())
            task = self._inflight.get(job_id)
            if task is not None and not task.done():
                task.cancel()
            log.info("job.cancel_requested", status=job.status.value)
            return job

    async def run(self, job_id: str) -> ResearchJob:
        """Execute a QUEUED job to a terminal state and return it.

        Raises ``JobNotClaimableError`` if the job is not QUEUED (already started, finished or
        cancelled) — this is the duplicate-execution guard.
        """
        with structlog.contextvars.bound_contextvars(job_id=job_id):
            current = await self._repo.get(job_id)
            if current.stages != SPRINT_02_STAGES:
                raise ValueError(
                    f"runner supports stages {[s.value for s in SPRINT_02_STAGES]}, "
                    f"job has {[s.value for s in current.stages]}"
                )
            job = await self._repo.claim(
                job_id, worker_id=self._worker_id, now=self._clock(), timeout_s=self._job_timeout_s
            )
            log.info("job.claimed", worker_id=self._worker_id, deadline_at=str(job.deadline_at))
            try:
                return await self._execute(job)
            except asyncio.CancelledError:
                await self._record_runner_interrupted(job_id)
                raise

    # -- pipeline -----------------------------------------------------------------------

    async def _execute(self, job: ResearchJob) -> ResearchJob:
        job = await self._planning(job)
        if job.is_terminal:
            return job
        return await self._searching(job)

    async def _planning(self, job: ResearchJob) -> ResearchJob:
        log.info("job.stage_started", stage=JobState.PLANNING.value)
        started = time.monotonic()
        try:
            plan = self._planner.plan(
                job.request, base_options=self._base_options, now=self._clock()
            )
        except Exception as exc:
            return await self._finish(
                job,
                JobState.FAILED,
                error=self._error(
                    "INTERNAL_ERROR", JobState.PLANNING, f"planner raised {type(exc).__name__}"
                ),
            )
        try:
            job = await self._save_update(
                job,
                {"plan": plan, "updated_at": self._clock()},
                event="job.plan_created",
                data={"planner": plan.planner, "queries": len(plan.queries)},
            )
        except _JobCancelled:
            return await self._finish_cancelled(job.id)
        log.info(
            "job.stage_finished",
            stage=JobState.PLANNING.value,
            planner=plan.planner,
            queries=len(plan.queries),
            query_hashes=[query_hash(q) for q in plan.queries],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return await self._boundary(job, JobState.PLANNING)

    async def _boundary(self, job: ResearchJob, step: JobState) -> ResearchJob:
        """Stage boundary: honour cancellation, then the job deadline (precedence order)."""
        latest = await self._repo.get(job.id)
        if latest.cancel_requested:
            return await self._finish(latest, JobState.CANCELLED)
        if self._seconds_left(latest) <= 0:
            return await self._finish(
                latest,
                JobState.FAILED,
                error=self._error("JOB_DEADLINE_EXCEEDED", step, "job deadline exceeded"),
            )
        return latest

    async def _searching(self, job: ResearchJob) -> ResearchJob:
        plan = job.plan
        if plan is None:  # _planning always stores a plan before this stage
            raise RuntimeError("SEARCHING entered without a plan")
        try:
            job = await self._save_update(
                job,
                {
                    "status": JobState.SEARCHING,
                    "updated_at": self._clock(),
                    "progress": JobProgress(
                        stage=JobState.SEARCHING,
                        stage_index=1,
                        total_stages=len(job.stages),
                        queries_total=len(plan.queries),
                    ),
                },
            )
        except _JobCancelled:
            return await self._finish_cancelled(job.id)
        if job.is_terminal:  # finalized concurrently: never search for a finished job
            return job
        log.info("job.stage_started", stage=JobState.SEARCHING.value, queries=len(plan.queries))
        started = time.monotonic()
        capture = _SearchCapture(outcomes=[], runs=[])
        try:
            await self._run_queries(job, plan, capture)
        except Exception as exc:
            job = await self._repo.get(job.id)
            return await self._finish(
                job,
                JobState.FAILED,
                result=self._result(job, plan, capture),
                error=self._error(
                    "INTERNAL_ERROR",
                    JobState.SEARCHING,
                    f"search stage raised {type(exc).__name__}",
                ),
            )
        log.info(
            "job.stage_finished",
            stage=JobState.SEARCHING.value,
            duration_ms=int((time.monotonic() - started) * 1000),
            stopped_by=capture.stop,
        )
        job = await self._repo.get(job.id)
        return await self._conclude(job, plan, capture)

    async def _run_queries(
        self, job: ResearchJob, plan: ResearchPlan, capture: _SearchCapture
    ) -> None:
        seconds_left = self._seconds_left(job)
        limit_code: JobErrorCode
        if seconds_left <= self._stage_timeout_s:  # the job deadline is the tighter limit
            limit, limit_code = seconds_left, "JOB_DEADLINE_EXCEEDED"
        else:
            limit, limit_code = self._stage_timeout_s, "SEARCH_STAGE_TIMEOUT"
        if limit <= 0:
            capture.stop = limit_code
            return
        try:
            async with asyncio.timeout(limit) as scope:
                for index, query in enumerate(plan.queries):
                    if (await self._repo.get(job.id)).cancel_requested:
                        raise _JobCancelled
                    capture.interrupted_index = index
                    outcome, run = await self._run_one(job, plan, index, query)
                    # Captured before any further await: can never be lost afterwards.
                    capture.outcomes.append(outcome)
                    if run is not None:
                        capture.runs.append(run)
                    capture.interrupted_index = None
                    await self._save_progress(job.id, plan, capture, outcome)
        except TimeoutError:
            if not scope.expired():
                raise  # not our time limit: surfaces as INTERNAL_ERROR
            capture.stop = limit_code
        except _JobCancelled:
            capture.stop = "CANCELLED"

    async def _run_one(
        self, job: ResearchJob, plan: ResearchPlan, index: int, query: str
    ) -> tuple[QueryOutcome, SearchRun | None]:
        request_id = f"{job.id}:search:{index}"
        started = time.monotonic()
        task = asyncio.create_task(
            self._search.search([query], plan.search_options, request_id=request_id)
        )
        self._inflight[job.id] = task
        try:
            if (await self._repo.get(job.id)).cancel_requested:
                task.cancel()
            run = await task
        except AllSearchProvidersFailedError as exc:
            outcome = QueryOutcome(
                index=index,
                query_hash=query_hash(query),
                request_id=request_id,
                status="FAILED",
                duration_ms=int((time.monotonic() - started) * 1000),
                errors=[
                    ProviderErrorRecord(
                        provider=e.provider, code=e.code, category=e.category, retryable=e.retryable
                    )
                    for e in exc.errors
                ],
            )
            self._log_query(outcome)
            return outcome, None
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and current is not None and current.cancelling() == 0:
                raise _JobCancelled from None  # cancelled via cancel(job_id), not our own task
            raise
        finally:
            self._inflight.pop(job.id, None)
            if not task.done():
                task.cancel()
        outcome = QueryOutcome(
            index=index,
            query_hash=query_hash(query),
            request_id=request_id,
            status="COVERED",
            result_count=len(run.results),
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=[_attempt_record(a) for a in run.attempts],
        )
        self._log_query(outcome)
        return outcome, run

    async def _save_progress(
        self, job_id: str, plan: ResearchPlan, capture: _SearchCapture, outcome: QueryOutcome
    ) -> None:
        job = await self._repo.get(job_id)
        await self._save_update(
            job,
            {
                "updated_at": self._clock(),
                "result": self._result(job, plan, capture),
                "progress": self._progress(job, plan, capture),
            },
            event="job.search.query_finished",
            data={
                "index": outcome.index,
                "query_hash": outcome.query_hash,
                "request_id": outcome.request_id,
                "status": outcome.status,
                "results": outcome.result_count,
            },
        )

    # -- outcome ------------------------------------------------------------------------

    async def _conclude(
        self, job: ResearchJob, plan: ResearchPlan, capture: _SearchCapture
    ) -> ResearchJob:
        if job.cancel_requested:
            capture.stop = "CANCELLED"  # precedence: cancel > deadline > stage timeout > outcome
        result = self._result(job, plan, capture)
        covered = sum(1 for o in result.outcomes if o.status == "COVERED")
        provider_errors = _provider_errors(result.outcomes)
        step = JobState.SEARCHING

        if capture.stop == "CANCELLED":
            return await self._finish(job, JobState.CANCELLED, result=result)
        if capture.stop in ("JOB_DEADLINE_EXCEEDED", "SEARCH_STAGE_TIMEOUT"):
            timeout_error = self._error(
                capture.stop,
                step,
                "job deadline exceeded"
                if capture.stop == "JOB_DEADLINE_EXCEEDED"
                else "search stage timeout",
                provider_errors=provider_errors,
            )
            if covered:
                return await self._finish(
                    job, JobState.PARTIAL, result=result, warnings=[timeout_error]
                )
            return await self._finish(job, JobState.FAILED, result=result, error=timeout_error)
        if covered == 0:
            return await self._finish(
                job,
                JobState.FAILED,
                result=result,
                error=self._error(
                    "ALL_SEARCH_PROVIDERS_FAILED",
                    step,
                    "no planned query was covered by any provider",
                    provider_errors=provider_errors,
                    category="PROVIDER_ERROR",
                ),
            )
        warnings = []
        if provider_errors:
            warnings.append(
                self._error(
                    "PROVIDER_FAILURES",
                    step,
                    "some provider calls failed",
                    provider_errors=provider_errors,
                    category="PROVIDER_ERROR",
                )
            )
        status = JobState.COMPLETED if covered == len(plan.queries) else JobState.PARTIAL
        return await self._finish(job, status, result=result, warnings=warnings)

    def _result(self, job: ResearchJob, plan: ResearchPlan, capture: _SearchCapture) -> JobResult:
        outcomes = list(capture.outcomes)
        if capture.stop is not None:
            done = {o.index for o in outcomes}
            for index, query in enumerate(plan.queries):
                if index in done:
                    continue
                status = "INTERRUPTED" if index == capture.interrupted_index else "NOT_RUN"
                outcomes.append(
                    QueryOutcome(
                        index=index,
                        query_hash=query_hash(query),
                        request_id=f"{job.id}:search:{index}",
                        status=status,
                    )
                )
        outcomes.sort(key=lambda o: o.index)
        covered = sum(1 for o in outcomes if o.status == "COVERED")
        coverage: Coverage = (
            "COMPLETE" if covered == len(plan.queries) else "PARTIAL" if covered else "NONE"
        )
        response = None
        if capture.runs:
            merged = SearchRun(
                hits=[h for r in capture.runs for h in r.hits],
                attempts=[a for r in capture.runs for a in r.attempts],
                request_id=job.id,
                strategy=capture.runs[0].strategy,
                queries=[q for r in capture.runs for q in r.queries],
                providers=self._search.provider_names,
            )
            response = merged.to_response()
        return JobResult(outcomes=outcomes, response=response, coverage=coverage)

    def _progress(
        self, job: ResearchJob, plan: ResearchPlan, capture: _SearchCapture
    ) -> JobProgress:
        results = self._result(job, plan, capture).response
        return JobProgress(
            stage=JobState.SEARCHING,
            stage_index=1,
            total_stages=len(job.stages),
            queries_total=len(plan.queries),
            queries_done=len(capture.outcomes),
            queries_failed=sum(1 for o in capture.outcomes if o.status == "FAILED"),
            results_unique=len(results.results) if results else 0,
        )

    async def _finish(
        self,
        job: ResearchJob,
        status: JobState,
        *,
        result: JobResult | None = None,
        error: JobError | None = None,
        warnings: list[JobError] | None = None,
    ) -> ResearchJob:
        if job.is_terminal:
            return job
        now = self._clock()
        update: dict[str, object] = {
            "status": status,
            "updated_at": now,
            "completed_at": now,
            "error": error,
            "warnings": warnings or [],
        }
        if result is not None:
            update["result"] = result
            plan = job.plan
            if plan is not None:
                update["progress"] = JobProgress(
                    stage=job.status if job.status in job.stages else job.stages[0],
                    stage_index=job.stages.index(job.status) if job.status in job.stages else 0,
                    total_stages=len(job.stages),
                    queries_total=len(plan.queries),
                    queries_done=sum(
                        1 for o in result.outcomes if o.status in ("COVERED", "FAILED")
                    ),
                    queries_failed=sum(1 for o in result.outcomes if o.status == "FAILED"),
                    results_unique=len(result.response.results) if result.response else 0,
                )
        event = {
            JobState.COMPLETED: "job.completed",
            JobState.PARTIAL: "job.completed",
            JobState.CANCELLED: "job.cancelled",
            JobState.FAILED: "job.failed",
        }[status]
        data: dict[str, object] = {"status": status.value}
        if error is not None:
            data.update({"code": error.code, "category": error.category, "step": error.step.value})
        try:
            saved = await self._save_update(job, update, event=event, data=data)
        except _JobCancelled:  # cancel recorded while finishing: cancel wins (precedence)
            return await self._finish(
                await self._repo.get(job.id), JobState.CANCELLED, result=result
            )
        if saved.status is not status:  # already finalized concurrently: report what is stored
            return saved
        log_method = log.warning if status in (JobState.FAILED, JobState.CANCELLED) else log.info
        log_method(
            event,
            status=status.value,
            error_code=error.code if error else None,
            error_category=error.category if error else None,
            warnings=[w.code for w in saved.warnings],
            coverage=saved.result.coverage if saved.result else None,
            results=len(saved.result.response.results)
            if saved.result and saved.result.response
            else 0,
        )
        return saved

    async def _record_runner_interrupted(self, job_id: str) -> None:
        job = await self._repo.get(job_id)
        if job.is_terminal:
            return
        await self._finish(
            job,
            JobState.FAILED,
            result=job.result,
            error=self._error(
                "RUNNER_INTERRUPTED",
                job.status,
                "runner task was cancelled",
                category="INTERNAL_ERROR",
            ),
        )

    # -- helpers ------------------------------------------------------------------------

    async def _save_update(
        self,
        job: ResearchJob,
        update: dict[str, object],
        *,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        """Store ``update`` on ``job`` with optimistic versioning, safe against a concurrent
        ``cancel()`` (OI-1).

        Every write still requires the version it was based on. On ``JobVersionConflictError``
        the job is re-read: a terminal job is returned as-is; a recorded cancel raises
        ``_JobCancelled`` (unless this write is the CANCELLED write itself) so the caller
        finalizes CANCELLED instead of losing the cancel; otherwise the same update is
        re-applied on the fresh version.
        """
        attempts = 0
        while True:
            try:
                return await self._repo.save(
                    job.model_copy(update=update),
                    expected_version=job.version,
                    event=event,
                    data=data,
                )
            except JobVersionConflictError:
                job = await self._repo.get(job.id)
                if job.is_terminal:
                    return job
                if job.cancel_requested and update.get("status") is not JobState.CANCELLED:
                    raise _JobCancelled from None
                attempts += 1
                if attempts > _CONFLICT_RETRIES:
                    raise  # an unknown concurrent writer keeps winning: surface it

    async def _finish_cancelled(self, job_id: str) -> ResearchJob:
        return await self._finish(await self._repo.get(job_id), JobState.CANCELLED)

    def _seconds_left(self, job: ResearchJob) -> float:
        if job.deadline_at is None:
            return self._job_timeout_s
        return (job.deadline_at - self._clock()).total_seconds()

    @staticmethod
    def _error(
        code: JobErrorCode,
        step: JobState,
        message: str,
        *,
        provider_errors: list[ProviderErrorRecord] | None = None,
        category: str | None = None,
    ) -> JobError:
        default_category = {
            "SEARCH_STAGE_TIMEOUT": "TIMEOUT",
            "JOB_DEADLINE_EXCEEDED": "TIMEOUT",
            "CANCELLED": "CANCELLED",
        }.get(code, "INTERNAL_ERROR")
        return JobError(
            code=code,
            category=category or default_category,
            step=step,
            message=message,
            retryable=False,
            provider_errors=provider_errors or [],
        )

    @staticmethod
    def _log_query(outcome: QueryOutcome) -> None:
        log.info(
            "job.search.query_finished",
            index=outcome.index,
            query_hash=outcome.query_hash,
            request_id=outcome.request_id,
            status=outcome.status,
            results=outcome.result_count,
            duration_ms=outcome.duration_ms,
            error_categories=sorted({e.category for e in outcome.errors}),
        )


def _attempt_record(attempt: SearchAttempt) -> AttemptRecord:
    error = None
    if attempt.error is not None:
        error = ProviderErrorRecord(
            provider=str(attempt.error.get("provider", attempt.provider)),
            code=str(attempt.error.get("code", "")),
            category=str(attempt.error.get("category", "")),
            retryable=bool(attempt.error.get("retryable", False)),
        )
    return AttemptRecord(
        provider=attempt.provider,
        status=attempt.status,
        tries=attempt.tries,
        duration_ms=attempt.duration_ms,
        result_count=attempt.result_count,
        error=error,
    )


def _provider_errors(outcomes: list[QueryOutcome]) -> list[ProviderErrorRecord]:
    seen: dict[tuple[str, str, str], ProviderErrorRecord] = {}
    for outcome in outcomes:
        records = list(outcome.errors) + [a.error for a in outcome.attempts if a.error is not None]
        for record in records:
            seen.setdefault((record.provider, record.code, record.category), record)
    return list(seen.values())
