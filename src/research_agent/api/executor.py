"""In-process job execution with an explicit concurrency policy (Sprint 03, OD7).

Policy:
- at most ``max_concurrent`` jobs execute at the same time (``asyncio.Semaphore``);
- at most ``max_queued`` further accepted jobs wait for a slot (they stay ``QUEUED``; the job
  deadline starts only when the runner claims the job);
- admission is decided *synchronously* by ``try_reserve()`` before a job is created, so the
  limit never depends on whether repository calls yield to the event loop;
- beyond the limits new jobs are rejected (API: 503 ``CAPACITY_EXHAUSTED``).

This is not a queue/worker system: nothing is persisted, and everything is lost on restart.

Each job runs in its own task created with a *fresh* ``contextvars.Context`` so execution logs
carry ``job_id`` (bound by the runner) and never the ``http_request_id`` of the request that
created the job. ``JobNotClaimableError`` (e.g. the job was cancelled while waiting) is expected
control flow, logged at info level; it never becomes a job failure.
"""

from __future__ import annotations

import asyncio
import contextvars

from research_agent.jobs.errors import JobNotClaimableError
from research_agent.jobs.runner import JobRunner
from research_agent.logging import get_logger

log = get_logger(__name__)


class JobExecutor:
    def __init__(self, runner: JobRunner, *, max_concurrent: int, max_queued: int) -> None:
        if max_concurrent < 1 or max_queued < 0:
            raise ValueError("max_concurrent must be >= 1 and max_queued >= 0")
        self._runner = runner
        self._slots = asyncio.Semaphore(max_concurrent)
        self._capacity = max_concurrent + max_queued
        self._reserved = 0
        self._waiting: set[str] = set()
        self._running: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def queued(self) -> int:
        return len(self._waiting) + self._reserved

    def try_reserve(self) -> bool:
        """Reserve capacity for one job about to be created. Synchronous: no await between
        the check and the increment."""
        if len(self._running) + len(self._waiting) + self._reserved >= self._capacity:
            return False
        self._reserved += 1
        return True

    def release(self) -> None:
        """Give back a reservation that did not lead to a new job (replay, error)."""
        if self._reserved <= 0:
            raise RuntimeError("release() without reservation")
        self._reserved -= 1

    def spawn(self, job_id: str) -> None:
        """Start executing ``job_id`` using a previously reserved slot."""
        if self._reserved <= 0:
            raise RuntimeError("spawn() without reservation")
        self._reserved -= 1
        self._waiting.add(job_id)
        task = asyncio.create_task(
            self._execute(job_id), name=f"research-job-{job_id}", context=contextvars.Context()
        )
        self._tasks[job_id] = task
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task[None]) -> None:
        for job_id, known in list(self._tasks.items()):
            if known is task:
                del self._tasks[job_id]

    async def wait(self, job_id: str) -> None:
        """Wait until the execution task of ``job_id`` has finished (if it exists)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.wait({task})

    async def shutdown(self) -> None:
        """Cancel all job tasks. Running jobs are recorded FAILED/RUNNER_INTERRUPTED by the
        Sprint 02 runner; jobs still waiting for a slot stay QUEUED (in-memory, lost anyway)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("executor.shutdown", cancelled=len(tasks))

    async def _execute(self, job_id: str) -> None:
        try:
            async with self._slots:
                self._waiting.discard(job_id)
                self._running.add(job_id)
                try:
                    await self._runner.run(job_id)
                except JobNotClaimableError as exc:
                    log.info("job.not_started", job_id=job_id, status=exc.status)
                except Exception as exc:
                    log.error("job.task_error", job_id=job_id, error_type=type(exc).__name__)
                finally:
                    self._running.discard(job_id)
        finally:
            self._waiting.discard(job_id)
