"""OI-1 regression: a user cancel must never be lost, even when repository calls yield.

``InMemoryJobRepository`` never yields between the runner's read and its versioned save, which hid
the race. These doubles are valid ``JobRepository`` implementations that behave like an async DB:

- ``YieldingRepository``: every call yields (``await asyncio.sleep(0)``) — randomized stress;
- ``CancelBeforeSaveRepository``: records a real ``request_cancel`` immediately before one chosen
  ``save`` — deterministically forces the version conflict at every runner write site.

Invariant: cancel > deadline > stage timeout > outcome; a cancelled job ends CANCELLED, never
FAILED/INTERNAL_ERROR, never stuck non-terminal; optimistic versioning is still enforced.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from collections.abc import Callable
from datetime import datetime

import pytest

from research_agent.core.errors import ProviderUnavailableError
from research_agent.core.models import SearchOptions
from research_agent.jobs import (
    TERMINAL_STATES,
    InMemoryJobRepository,
    JobNotClaimableError,
    JobRunner,
    JobState,
    JobVersionConflictError,
    ResearchJob,
    ResearchJobRequest,
)
from research_agent.pipeline.search import SearchService
from tests.fakes import RecordingSleep, ScriptedSearchProvider

S = JobState
QUERY = "cancel race query"


class YieldingRepository(InMemoryJobRepository):
    """Yields to the event loop on every call, like an async database driver."""

    async def get(self, job_id: str) -> ResearchJob:
        await asyncio.sleep(0)
        return await super().get(job_id)

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        await asyncio.sleep(0)
        return await super().save(job, expected_version=expected_version, event=event, data=data)

    async def request_cancel(self, job_id: str, *, now: datetime) -> ResearchJob:
        await asyncio.sleep(0)
        return await super().request_cancel(job_id, now=now)


SavePredicate = Callable[[ResearchJob, str | None], bool]


class CancelBeforeSaveRepository(YieldingRepository):
    """Records a user cancel right before the first ``save`` matching ``when`` → that save hits a
    real ``JobVersionConflictError`` (the stored version moved underneath it)."""

    def __init__(self, when: SavePredicate) -> None:
        super().__init__()
        self._when = when
        self.fired = False

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        if not self.fired and self._when(job, event):
            self.fired = True
            await super().request_cancel(job.id, now=job.updated_at)
        return await super().save(job, expected_version=expected_version, event=event, data=data)


def runner(
    repo: InMemoryJobRepository,
    provider: ScriptedSearchProvider,
    search_stage_timeout_s: float = 300.0,
) -> JobRunner:
    service = SearchService([provider], max_retries=0, sleep=RecordingSleep())
    return JobRunner(
        repo, service, base_options=SearchOptions(), search_stage_timeout_s=search_stage_timeout_s
    )


def ok() -> ScriptedSearchProvider:
    return ScriptedSearchProvider("a", [["https://a.example/"]])


async def start(r: JobRunner) -> str:
    job, _ = await r.submit(ResearchJobRequest(query=QUERY))
    return job.id


# every versioned write the runner performs during a Sprint 02 job
WRITE_SITES: dict[str, tuple[SavePredicate, Callable[[], ScriptedSearchProvider], float]] = {
    "plan saved": (lambda j, e: e == "job.plan_created", ok, 300.0),
    "SEARCHING transition": (lambda j, e: j.status is S.SEARCHING and e is None, ok, 300.0),
    "query progress": (lambda j, e: e == "job.search.query_finished", ok, 300.0),
    "COMPLETED write": (lambda j, e: j.status is S.COMPLETED, ok, 300.0),
    "FAILED (all providers) write": (
        lambda j, e: j.status is S.FAILED,
        lambda: ScriptedSearchProvider(
            "a", [ProviderUnavailableError("a", "503", status_code=503)]
        ),
        300.0,
    ),
    "FAILED (stage timeout) write": (
        lambda j, e: j.status is S.FAILED,
        lambda: ScriptedSearchProvider("a", [["https://slow.example/"]], delay_s=5),
        0.05,
    ),
}


@pytest.mark.parametrize("site", list(WRITE_SITES))
async def test_cancel_landing_on_each_write_site_ends_cancelled(site: str) -> None:
    when, provider, stage_timeout_s = WRITE_SITES[site]
    repo = CancelBeforeSaveRepository(when)
    r = runner(repo, provider(), stage_timeout_s)
    job_id = await start(r)

    final = await r.run(job_id)  # must not raise JobVersionConflictError

    assert repo.fired, f"cancel was never injected at {site}"
    assert final.status is S.CANCELLED, (site, final.status, final.error)
    assert final.error is None  # never FAILED/INTERNAL_ERROR
    stored = await repo.get(job_id)
    assert stored.status is S.CANCELLED and stored.cancel_requested
    events = [e.type for e in await repo.list_events(job_id)]
    assert events[-1] == "job.cancelled" and events.count("job.cancelled") == 1


async def test_cancel_during_finalization_keeps_captured_results() -> None:
    repo = CancelBeforeSaveRepository(lambda j, e: j.status is S.COMPLETED)
    final = await runner(repo, ok()).run(await start(runner(repo, ok())))
    assert final.status is S.CANCELLED
    assert final.result is not None and final.result.outcomes[0].status == "COVERED"
    assert final.result.response is not None and len(final.result.response.results) == 1


async def test_without_cancel_yielding_repository_completes_normally() -> None:
    repo = YieldingRepository()
    r = runner(repo, ok())
    final = await r.run(await start(r))
    assert final.status is S.COMPLETED and final.error is None


async def test_stage_timeout_still_works_with_yielding_repository() -> None:
    repo = YieldingRepository()
    slow = ScriptedSearchProvider("a", [["https://slow.example/"]], delay_s=5)
    r = runner(repo, slow, 0.05)
    final = await r.run(await start(r))
    assert final.status is S.FAILED and final.error is not None
    assert final.error.code == "SEARCH_STAGE_TIMEOUT"


async def test_shutdown_is_still_distinct_from_user_cancel() -> None:
    repo = YieldingRepository()
    slow = ScriptedSearchProvider("a", [["https://slow.example/"]], delay_s=5)
    r = runner(repo, slow)
    job_id = await start(r)
    task = asyncio.create_task(r.run(job_id))
    while slow.in_flight == 0:  # noqa: ASYNC110 - polling a test double's counter
        await asyncio.sleep(0.001)
    task.cancel()  # the runner's own task, not a user cancel
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await repo.get(job_id)
    assert stored.status is S.FAILED and stored.error is not None
    assert stored.error.code == "RUNNER_INTERRUPTED" and not stored.cancel_requested


async def test_optimistic_versioning_still_rejects_an_unknown_concurrent_writer() -> None:
    """A writer that is NOT a cancel keeps moving the version: the runner must not overwrite it
    blindly — after bounded retries the conflict surfaces."""

    class HostileWriter(YieldingRepository):
        async def save(
            self,
            job: ResearchJob,
            *,
            expected_version: int,
            event: str | None = None,
            data: dict[str, object] | None = None,
        ) -> ResearchJob:
            if event == "job.plan_created":
                current = await super().get(job.id)
                await super().save(
                    current.model_copy(update={"updated_at": current.updated_at}),
                    expected_version=current.version,
                )
            return await super().save(
                job, expected_version=expected_version, event=event, data=data
            )

    repo = HostileWriter()
    r = runner(repo, ok())
    with pytest.raises(JobVersionConflictError):
        await r.run(await start(r))


async def test_randomized_cancel_races_with_yielding_repository() -> None:
    """1000 seeded races: cancel at a random scheduling point of a real run."""
    outcomes: Counter[str] = Counter()
    for seed in range(1000):
        rnd = random.Random(seed)  # noqa: S311 - reproducible test schedule, not crypto
        repo = YieldingRepository()
        provider = ScriptedSearchProvider(
            "a", [["https://a.example/"]], delay_s=rnd.choice([0.0, 0.0, 0.001])
        )
        r = runner(repo, provider)
        job_id = await start(r)
        task = asyncio.create_task(r.run(job_id))
        for _ in range(rnd.randint(0, 25)):
            await asyncio.sleep(0)
        await r.cancel(job_id)
        try:
            final = await task
        except JobNotClaimableError:  # cancelled while still QUEUED: documented guard
            final = await repo.get(job_id)
            assert final.status is S.CANCELLED
            outcomes["CANCELLED_BEFORE_CLAIM"] += 1
            continue
        stored = await repo.get(job_id)
        assert stored.status in TERMINAL_STATES, (seed, stored.status)
        assert stored.status in (S.CANCELLED, S.COMPLETED), (seed, stored.status, stored.error)
        if stored.status is S.COMPLETED:
            # only valid if the cancel arrived after completion: a cancel on a terminal job is a
            # no-op, so no cancel may have been recorded while the job was still running
            events = [e.type for e in await repo.list_events(job_id)]
            assert "job.cancel_requested" not in events and not stored.cancel_requested, seed
        outcomes[final.status.value] += 1
    assert sum(outcomes.values()) == 1000
    assert outcomes["CANCELLED"] > 0 and set(outcomes) <= {
        "CANCELLED",
        "COMPLETED",
        "CANCELLED_BEFORE_CLAIM",
    }
