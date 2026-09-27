"""ResearchService: application layer between HTTP routes and the Sprint 02 job components.

Its one essential rule: execution is scheduled only for a *newly created* job — an idempotent
replay returns the existing job and never runs it again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from research_agent.api import errors
from research_agent.api.executor import JobExecutor
from research_agent.core.errors import NoSearchProviderConfiguredError
from research_agent.jobs.errors import IdempotencyConflictError, JobNotFoundError
from research_agent.jobs.models import JobState, ResearchJob, ResearchJobRequest
from research_agent.jobs.repository import JobRepository
from research_agent.jobs.runner import JobRunner

CAPACITY_RETRY_AFTER_S = 5


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ResearchService:
    def __init__(
        self,
        repository: JobRepository,
        runner: JobRunner | None,
        executor: JobExecutor | None,
        *,
        search_error: NoSearchProviderConfiguredError | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if (runner is None or executor is None) and search_error is None:
            raise ValueError("runner and executor are required when search is configured")
        self._repo = repository
        self._runner = runner
        self._executor = executor
        self._search_error = search_error
        self._clock = clock

    @property
    def executor(self) -> JobExecutor | None:
        return self._executor

    async def submit(
        self, request: ResearchJobRequest, *, http_request_id: str
    ) -> tuple[ResearchJob, bool]:
        if self._runner is None or self._executor is None:
            missing = sorted(
                {
                    m
                    for d in (self._search_error.details if self._search_error else ())
                    for m in d.missing
                }
            )
            raise errors.requires_configuration(missing)
        if not self._executor.try_reserve():
            raise errors.capacity_exhausted(CAPACITY_RETRY_AFTER_S)
        try:
            job, created = await self._runner.submit(request)
        except IdempotencyConflictError:
            self._executor.release()
            raise errors.idempotency_conflict() from None
        except BaseException:
            self._executor.release()
            raise
        if not created:
            self._executor.release()
            return job, False
        self._executor.spawn(job.id)
        await self._repo.append_event(
            job.id,
            "api.job_submitted",
            now=self._clock(),
            data={"http_request_id": http_request_id},
        )
        return job, True

    async def get(self, job_id: str) -> ResearchJob:
        try:
            return await self._repo.get(job_id)
        except JobNotFoundError:
            raise errors.job_not_found() from None

    async def cancel(self, job_id: str) -> ResearchJob:
        """Request cancellation; returns the job as stored afterwards.

        The HTTP status is derived from the *resulting* state, so a cancel racing a completion
        is reported by whichever the repository applied first.
        """
        if self._runner is None:
            job = await self.get(job_id)  # no job can exist without a runner; 404 path
            return job
        try:
            return await self._runner.cancel(job_id)
        except JobNotFoundError:
            raise errors.job_not_found() from None


def cancel_http_status(job: ResearchJob) -> int:
    if job.status is JobState.CANCELLED:
        return 200
    if job.is_terminal:
        raise errors.job_already_finished(job.status.value)
    return 202
