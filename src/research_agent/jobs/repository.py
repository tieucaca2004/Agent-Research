"""Job persistence boundary.

``JobRepository`` is the contract; Sprint 02 ships ``InMemoryJobRepository`` (single process,
one ``asyncio.Lock``). A PostgreSQL implementation behind the same protocol is Sprint 07.
Process-restart recovery (lease expiry → WORKER_LOST) is designed in
docs/sprint-02-research-job.md but deliberately NOT implemented here (decision D5).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from research_agent.jobs.errors import (
    IdempotencyConflictError,
    JobNotClaimableError,
    JobNotFoundError,
    JobVersionConflictError,
)
from research_agent.jobs.models import (
    SPRINT_02_STAGES,
    JobEvent,
    JobProgress,
    JobState,
    ResearchJob,
    ResearchJobRequest,
)
from research_agent.jobs.state_machine import check_transition


class JobRepository(Protocol):
    async def create(
        self,
        request: ResearchJobRequest,
        *,
        now: datetime,
        stages: tuple[JobState, ...] = SPRINT_02_STAGES,
    ) -> tuple[ResearchJob, bool]:
        """Create a QUEUED job. Returns ``(job, created)``; ``created`` is False when an
        identical request with the same idempotency key already exists."""

    async def get(self, job_id: str) -> ResearchJob: ...

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        """Store ``job`` if the stored version equals ``expected_version``; status changes are
        validated by the state machine and recorded as ``job.state_changed`` events."""

    async def claim(
        self, job_id: str, *, worker_id: str, now: datetime, timeout_s: float
    ) -> ResearchJob:
        """Atomically move QUEUED → first stage (compare-and-set). Raises
        ``JobNotClaimableError`` for any other status."""

    async def request_cancel(self, job_id: str, *, now: datetime) -> ResearchJob: ...

    async def append_event(
        self, job_id: str, event: str, *, now: datetime, data: dict[str, object] | None = None
    ) -> None: ...

    async def list_events(self, job_id: str) -> list[JobEvent]: ...


def _fingerprint(request: ResearchJobRequest) -> tuple[str, str | None, str | None]:
    return (request.query, request.language, request.country)


class InMemoryJobRepository:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._jobs: dict[str, ResearchJob] = {}
        self._events: dict[str, list[JobEvent]] = {}
        self._idempotency: dict[str, str] = {}

    async def create(
        self,
        request: ResearchJobRequest,
        *,
        now: datetime,
        stages: tuple[JobState, ...] = SPRINT_02_STAGES,
    ) -> tuple[ResearchJob, bool]:
        async with self._lock:
            key = request.idempotency_key
            if key is not None and key in self._idempotency:
                existing = self._jobs[self._idempotency[key]]
                if _fingerprint(existing.request) != _fingerprint(request):
                    raise IdempotencyConflictError(hashlib.sha256(key.encode()).hexdigest()[:12])
                self._add_event(existing.id, "job.idempotent_hit", now, {})
                return existing, False
            job = ResearchJob(
                id=uuid.uuid4().hex,
                version=0,
                request=request,
                status=JobState.QUEUED,
                stages=stages,
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.id] = job
            self._events[job.id] = []
            if key is not None:
                self._idempotency[key] = job.id
            self._add_event(job.id, "job.created", now, {"stages": [s.value for s in stages]})
            return job, True

    async def get(self, job_id: str) -> ResearchJob:
        async with self._lock:
            return self._get(job_id)

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        async with self._lock:
            return self._save(job, expected_version, event, data)

    async def claim(
        self, job_id: str, *, worker_id: str, now: datetime, timeout_s: float
    ) -> ResearchJob:
        async with self._lock:
            current = self._get(job_id)
            if current.status is not JobState.QUEUED:
                raise JobNotClaimableError(job_id, current.status.value)
            first = current.stages[0]
            claimed = current.model_copy(
                update={
                    "status": first,
                    "worker_id": worker_id,
                    "started_at": now,
                    "deadline_at": now + timedelta(seconds=timeout_s),
                    "updated_at": now,
                    "progress": JobProgress(
                        stage=first, stage_index=0, total_stages=len(current.stages)
                    ),
                }
            )
            return self._save(claimed, current.version, "job.claimed", {"worker_id": worker_id})

    async def request_cancel(self, job_id: str, *, now: datetime) -> ResearchJob:
        async with self._lock:
            current = self._get(job_id)
            if current.is_terminal:
                return current
            if current.status is JobState.QUEUED:
                cancelled = current.model_copy(
                    update={
                        "status": JobState.CANCELLED,
                        "cancel_requested": True,
                        "completed_at": now,
                        "updated_at": now,
                    }
                )
                return self._save(cancelled, current.version, "job.cancel_requested", {})
            if current.cancel_requested:
                return current
            flagged = current.model_copy(update={"cancel_requested": True, "updated_at": now})
            return self._save(flagged, current.version, "job.cancel_requested", {})

    async def append_event(
        self, job_id: str, event: str, *, now: datetime, data: dict[str, object] | None = None
    ) -> None:
        async with self._lock:
            self._get(job_id)
            self._add_event(job_id, event, now, data or {})

    async def list_events(self, job_id: str) -> list[JobEvent]:
        async with self._lock:
            self._get(job_id)
            return list(self._events[job_id])

    # -- internals (caller holds the lock) --------------------------------------------

    def _get(self, job_id: str) -> ResearchJob:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(job_id) from None

    def _save(
        self,
        job: ResearchJob,
        expected_version: int,
        event: str | None,
        data: dict[str, object] | None,
    ) -> ResearchJob:
        current = self._get(job.id)
        if current.version != expected_version:
            raise JobVersionConflictError(job.id, expected_version, current.version)
        if job.stages != current.stages or job.request != current.request:
            raise ValueError("job stages and request are immutable")
        if job.status is not current.status:
            check_transition(job.id, current.stages, current.status, job.status)
        stored = job.model_copy(update={"version": current.version + 1})
        self._jobs[job.id] = stored
        at = stored.updated_at
        if job.status is not current.status:
            self._add_event(
                job.id,
                "job.state_changed",
                at,
                {"from": current.status.value, "to": job.status.value},
            )
        if event is not None:
            self._add_event(job.id, event, at, data or {})
        return stored

    def _add_event(self, job_id: str, event: str, at: datetime, data: dict[str, object]) -> None:
        events = self._events[job_id]
        events.append(JobEvent(job_id=job_id, seq=len(events) + 1, type=event, at=at, data=data))
