"""OI-1 hardening: result retention (M4), conflict retry (M5), the terminal guard (M6) and
OI1-DEF-01 (no runner write or new provider call after another writer finalized the job, D1-D8).

Interleavings are forced deterministically: ``ScheduledRepository`` runs a scripted action
(another writer's versioned write, a user cancel, or a terminal write) immediately before a chosen
runner ``save`` attempt, so the runner's save hits a real ``JobVersionConflictError``. No sleeps are
used for synchronisation; the only real-time element is the SEARCHING stage timeout in the M4
timeout cases, applied to a provider call that never returns.

The non-cancel writer used here is a valid ``JobRepository`` client, but no such writer exists in
the shipped system today (the only concurrent writer is ``request_cancel``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest
from structlog.testing import capture_logs

from research_agent.core.models import SearchOptions, SearchResult
from research_agent.jobs import (
    InMemoryJobRepository,
    JobRunner,
    JobState,
    JobVersionConflictError,
    ResearchJob,
    ResearchJobRequest,
)
from research_agent.jobs.models import JobError, ResearchPlan
from research_agent.pipeline.search import SearchService
from tests.fakes import RecordingSleep, ScriptedSearchProvider

S = JobState
QUERIES = ["q one", "q two", "q three"]
SavePredicate = Callable[[ResearchJob, str | None], bool]
Action = Callable[["ScheduledRepository", ResearchJob], Awaitable[None]]


class ScheduledRepository(InMemoryJobRepository):
    """Runs the next scripted action before each runner ``save`` attempt matching ``when``.

    Actions write through the base class directly, so only the runner's own attempts are recorded
    in ``attempts`` (attempted job, event, expected_version).
    """

    def __init__(self, when: SavePredicate | None = None, actions: list[Action] | None = None):
        super().__init__()
        self._when = when
        self._actions = list(actions or [])
        self.attempts: list[tuple[ResearchJob, str | None, int]] = []

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        if self._actions and self._when is not None and self._when(job, event):
            await self._actions.pop(0)(self, job)
        self.attempts.append((job, event, expected_version))
        return await super().save(job, expected_version=expected_version, event=event, data=data)

    @property
    def pending_actions(self) -> int:
        return len(self._actions)


async def other_writer_bump(repo: ScheduledRepository, job: ResearchJob) -> None:
    """A non-cancel writer stores the job unchanged: only the version moves."""
    current = await InMemoryJobRepository.get(repo, job.id)
    await InMemoryJobRepository.save(
        repo, current, expected_version=current.version, event="test.other_writer"
    )


async def user_cancel(repo: ScheduledRepository, job: ResearchJob) -> None:
    await InMemoryJobRepository.request_cancel(repo, job.id, now=job.updated_at)


async def other_writer_finalizes_failed(repo: ScheduledRepository, job: ResearchJob) -> None:
    current = await InMemoryJobRepository.get(repo, job.id)
    failed = current.model_copy(
        update={
            "status": S.FAILED,
            "completed_at": current.updated_at,
            "error": JobError(
                code="INTERNAL_ERROR",
                category="INTERNAL_ERROR",
                step=current.status,
                message="finalized by another writer",
                retryable=False,
            ),
        }
    )
    await InMemoryJobRepository.save(
        repo, failed, expected_version=current.version, event="test.other_writer"
    )


class ThreeQueries:
    name = "three-v1"

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: datetime
    ) -> ResearchPlan:
        return ResearchPlan(
            planner=self.name, queries=QUERIES, search_options=base_options, created_at=now
        )


class BlockingProvider(ScriptedSearchProvider):
    """Answers the first ``answer`` calls from the script, then never returns."""

    def __init__(self, steps: list[list[str]], answer: int) -> None:
        super().__init__("a", steps)
        self._answer = answer

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if len(self.calls) >= self._answer:
            self.calls.append(query)
            await asyncio.Event().wait()  # never set: only the stage timeout ends this call
        return await super().search(query, options)


def three_urls() -> ScriptedSearchProvider:
    return ScriptedSearchProvider(
        "a",
        [
            ["https://a.example/1", "https://a.example/2"],
            ["https://b.example/1"],
            ["https://c.example/1"],
        ],
    )


def make_runner(
    repo: InMemoryJobRepository,
    provider: ScriptedSearchProvider,
    *,
    three: bool = True,
    stage_timeout_s: float = 300.0,
    job_timeout_s: float = 600.0,
) -> JobRunner:
    service = SearchService([provider], max_retries=0, sleep=RecordingSleep())
    return JobRunner(
        repo,
        service,
        base_options=SearchOptions(),
        planner=ThreeQueries() if three else None,
        search_stage_timeout_s=stage_timeout_s,
        job_timeout_s=job_timeout_s,
    )


async def submit(runner: JobRunner) -> str:
    job, _ = await runner.submit(ResearchJobRequest(query="hardening query"))
    return job.id


async def event_types(repo: InMemoryJobRepository, job_id: str) -> list[str]:
    return [e.type for e in await repo.list_events(job_id)]


def progress_of(done: int) -> SavePredicate:
    return lambda j, e: (
        e == "job.search.query_finished" and j.progress is not None
        and j.progress.queries_done == done
    )  # fmt: skip


# -- M4: result retention ----------------------------------------------------------------


async def test_cancel_on_progress_save_keeps_completed_queries_and_marks_the_rest() -> None:
    repo = ScheduledRepository(progress_of(2), [user_cancel])
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    stored = await repo.get(job_id)
    assert final == stored  # what the runner returns is what is stored
    assert stored.status is S.CANCELLED and stored.error is None and stored.warnings == []
    assert provider.calls == QUERIES[:2]  # the third query never started
    assert stored.result is not None
    assert [o.status for o in stored.result.outcomes] == ["COVERED", "COVERED", "NOT_RUN"]
    assert [o.index for o in stored.result.outcomes] == [0, 1, 2]
    assert stored.result.coverage == "PARTIAL"
    response = stored.result.response
    assert response is not None
    assert response.queries == QUERIES[:2]  # both captured runs survive
    assert {r.result.url for r in response.results} == {
        "https://a.example/1",
        "https://a.example/2",
        "https://b.example/1",
    }
    for deduped in response.results:  # search provenance kept on every result
        assert deduped.providers == ["a"] and deduped.queries[0] in QUERIES[:2]
    assert all(o.attempts for o in stored.result.outcomes[:2])
    assert stored.progress is not None and stored.progress.queries_done == 2
    events = await event_types(repo, job_id)
    assert events[-1] == "job.cancelled" and events.count("job.cancelled") == 1
    assert "job.completed" not in events and "job.failed" not in events


@pytest.mark.parametrize(
    ("answer", "statuses", "coverage", "write"),
    [
        (1, ["COVERED", "INTERRUPTED", "NOT_RUN"], "PARTIAL", S.PARTIAL),
        (0, ["INTERRUPTED", "NOT_RUN", "NOT_RUN"], "NONE", S.FAILED),
    ],
)
async def test_cancel_on_timeout_terminal_write_keeps_final_outcomes(
    answer: int, statuses: list[str], coverage: str, write: JobState
) -> None:
    """Cancel lands on the terminal write of a stage timeout: the cancel-wins path must store the
    *final* result (INTERRUPTED / NOT_RUN markers), not only what progress saves stored (M4)."""
    repo = ScheduledRepository(lambda j, e: j.status is write, [user_cancel])
    provider = BlockingProvider([["https://a.example/1", "https://a.example/2"]], answer)
    runner = make_runner(repo, provider, stage_timeout_s=0.2)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    stored = await repo.get(job_id)
    assert repo.pending_actions == 0, "cancel was never injected"
    assert final == stored
    assert stored.status is S.CANCELLED and stored.error is None and stored.warnings == []
    assert stored.result is not None
    assert [o.status for o in stored.result.outcomes] == statuses
    assert stored.result.coverage == coverage
    if answer:
        assert stored.result.response is not None
        assert len(stored.result.response.results) == 2
    else:
        assert stored.result.response is None
    events = await event_types(repo, job_id)
    assert events[-1] == "job.cancelled" and events.count("job.cancelled") == 1


# -- M5: conflict retry --------------------------------------------------------------------

WRITE_SITES: dict[str, SavePredicate] = {
    "plan saved": lambda j, e: e == "job.plan_created",
    "SEARCHING transition": lambda j, e: j.status is S.SEARCHING and e is None,
    "query progress": progress_of(1),
    "COMPLETED write": lambda j, e: j.status is S.COMPLETED,
}


async def baseline_version() -> int:
    repo = ScheduledRepository()
    runner = make_runner(repo, three_urls())
    final = await runner.run(await submit(runner))
    assert final.status is S.COMPLETED
    return final.version


@pytest.mark.parametrize("conflicts", [1, 3])
@pytest.mark.parametrize("site", list(WRITE_SITES))
async def test_transient_conflicts_are_retried_and_applied_exactly_once(
    site: str, conflicts: int
) -> None:
    expected_version = await baseline_version()
    when = WRITE_SITES[site]
    repo = ScheduledRepository(when, [other_writer_bump] * conflicts)
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    assert repo.pending_actions == 0
    site_attempts = [a for a in repo.attempts if when(a[0], a[1])]
    assert len(site_attempts) == conflicts + 1  # every conflict was retried on a fresh version
    versions = [a[2] for a in site_attempts]
    assert versions == sorted(set(versions))  # each retry is based on a newer version
    assert final.status is S.COMPLETED and final.error is None
    assert final.version == expected_version + conflicts  # no extra (duplicate) writes
    assert provider.calls == QUERIES
    assert final.result is not None
    assert [o.status for o in final.result.outcomes] == ["COVERED"] * 3
    events = await event_types(repo, job_id)
    assert events.count("job.plan_created") == 1
    assert events.count("job.search.query_finished") == 3
    assert events.count("job.completed") == 1
    assert events.count("test.other_writer") == conflicts


async def test_conflicts_beyond_the_retry_limit_surface() -> None:
    """Documented limitation (R2): an unknown writer that keeps winning is not overwritten; after
    1 + 3 attempts the conflict surfaces and the job is left non-terminal."""
    when = WRITE_SITES["plan saved"]
    repo = ScheduledRepository(when, [other_writer_bump] * 4)
    runner = make_runner(repo, three_urls())
    job_id = await submit(runner)

    with pytest.raises(JobVersionConflictError):
        await runner.run(job_id)

    assert len([a for a in repo.attempts if a[1] == "job.plan_created"]) == 4
    stored = await repo.get(job_id)
    assert stored.status is S.PLANNING and stored.plan is None  # update never half-applied


@pytest.mark.parametrize("site", ["plan saved", "COMPLETED write"])
async def test_cancel_recorded_between_retries_wins(site: str) -> None:
    when = WRITE_SITES[site]
    repo = ScheduledRepository(when, [other_writer_bump, user_cancel])
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    assert repo.pending_actions == 0
    assert final.status is S.CANCELLED and final.error is None
    stored = await repo.get(job_id)
    assert stored == final and stored.cancel_requested
    if site == "plan saved":
        assert provider.calls == []
    else:
        assert stored.result is not None
        assert [o.status for o in stored.result.outcomes] == ["COVERED"] * 3
    assert (await event_types(repo, job_id)).count("job.cancelled") == 1


async def test_terminal_state_before_retry_is_returned_as_is() -> None:
    repo = ScheduledRepository(WRITE_SITES["plan saved"], [other_writer_finalizes_failed])
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    assert final.status is S.FAILED
    assert final.error is not None and final.error.message == "finalized by another writer"
    assert provider.calls == []
    assert [a[1] for a in repo.attempts] == ["job.plan_created"]  # no write after terminal
    stored = await repo.get(job_id)
    assert stored == final and stored.plan is None


# -- M6: terminal guard in _searching ------------------------------------------------------


async def test_job_finalized_before_searching_is_never_searched() -> None:
    """Reachable only through a non-cancel writer (valid for the ``JobRepository`` protocol, not
    present in the shipped system): the job turns terminal while the runner stores SEARCHING."""
    repo = ScheduledRepository(WRITE_SITES["SEARCHING transition"], [other_writer_finalizes_failed])
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    with capture_logs() as logs:
        final = await runner.run(job_id)

    assert provider.calls == []  # no search for a finished job
    searching_started = [
        e for e in logs if e["event"] == "job.stage_started" and e["stage"] == "SEARCHING"
    ]
    assert searching_started == []  # the SEARCHING stage is never started for a finished job
    assert final.status is S.FAILED
    assert final.error is not None and final.error.message == "finalized by another writer"
    stored = await repo.get(job_id)
    assert stored == final and stored.result is None and stored.plan is not None
    statuses = [a[0].status for a in repo.attempts]
    assert statuses[-1] is S.SEARCHING  # the conflicting attempt was the last runner write
    assert (await event_types(repo, job_id))[-1] == "test.other_writer"


# -- OI1-DEF-01: another writer finalizes the job while the runner is between/inside queries --
#
# Windows (plan of 3 queries; the job is finalized FAILED by a non-cancel writer):
#   D1  right before the progress save of query 2 (the save hits a CAS conflict)
#   D2  after query 2 was saved, before the loop's read for query 3      (W2)
#   D3  between the loop's read and _run_one's read for query 3          (W3)
#   D4  while the provider call of query 3 is running                    (W4)
# Invariant: the first committed terminal state is final - the stored job (status, error,
# result, version, timestamps) equals the snapshot taken when the other writer committed, no
# runner write lands afterwards, and no provider call starts after the runner observed it.


class WindowRepository(InMemoryJobRepository):
    """Hooks the k-th ``get`` after the progress save of query ``after_query`` succeeded.

    ``yields`` makes every call suspend that many times first (``asyncio.sleep(0)``), like the
    round trip of an async database driver: deterministic scheduling points, not timing."""

    def __init__(
        self,
        *,
        k: int = 0,
        after_query: int = 2,
        action: str = "finalize",
        yields: int = 0,
    ) -> None:
        super().__init__()
        self._k = k
        self._after_query = after_query
        self._action = action
        self._yields = yields
        self._gets_since: int | None = None
        self.snapshot: ResearchJob | None = None
        self.attempts_after_terminal: list[tuple[JobState, str | None]] = []
        self.hooked = False

    async def finalize(self, job_id: str) -> None:
        current = await InMemoryJobRepository.get(self, job_id)
        failed = current.model_copy(
            update={
                "status": S.FAILED,
                "completed_at": current.updated_at,
                "error": JobError(
                    code="INTERNAL_ERROR",
                    category="INTERNAL_ERROR",
                    step=current.status,
                    message="finalized by another writer",
                    retryable=False,
                ),
            }
        )
        self.snapshot = await InMemoryJobRepository.save(
            self, failed, expected_version=current.version, event="test.other_writer"
        )

    async def _fire(self, job_id: str) -> None:
        self.hooked = True
        if self._action == "finalize":
            await self.finalize(job_id)
        else:
            current = await InMemoryJobRepository.get(self, job_id)
            await InMemoryJobRepository.request_cancel(self, job_id, now=current.updated_at)

    async def _suspend(self) -> None:
        for _ in range(self._yields):
            await asyncio.sleep(0)

    async def get(self, job_id: str) -> ResearchJob:
        await self._suspend()
        if self._gets_since is not None and not self.hooked:
            self._gets_since += 1
            if self._gets_since == self._k:
                await self._fire(job_id)
        return await super().get(job_id)

    async def save(
        self,
        job: ResearchJob,
        *,
        expected_version: int,
        event: str | None = None,
        data: dict[str, object] | None = None,
    ) -> ResearchJob:
        await self._suspend()
        if self.snapshot is not None:
            self.attempts_after_terminal.append((job.status, event))
        saved = await super().save(job, expected_version=expected_version, event=event, data=data)
        if (
            self._k
            and event == "job.search.query_finished"
            and saved.progress is not None
            and saved.progress.queries_done == self._after_query
        ):
            self._gets_since = 0
        return saved


class HookedProvider(ScriptedSearchProvider):
    """Runs ``on_call`` when call number ``n`` starts; optionally that call then never returns."""

    def __init__(
        self, on_call: Callable[[], Awaitable[None]], n: int, *, hang: bool = False
    ) -> None:
        super().__init__(
            "a", [["https://a.example/1"], ["https://b.example/1"], ["https://c.example/1"]]
        )
        self._on_call = on_call
        self._n = n
        self._hang = hang

    async def search(self, query: str, options: SearchOptions) -> list[SearchResult]:
        if len(self.calls) + 1 == self._n:
            await self._on_call()
            if self._hang:
                self.calls.append(query)
                await asyncio.Event().wait()  # never set: only a time limit ends this call
        return await super().search(query, options)


def stage_log(logs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return next(e for e in logs if e["event"] == "job.stage_finished" and e["stage"] == "SEARCHING")


async def assert_terminal_snapshot_kept(
    repo: WindowRepository, job_id: str, final: ResearchJob
) -> None:
    stored = await repo.get(job_id)
    assert repo.snapshot is not None, "the other writer never finalized the job"
    assert stored == repo.snapshot  # status, error, result, version, updated_at: all untouched
    assert final == stored
    assert stored.status is S.FAILED
    assert stored.error is not None and stored.error.message == "finalized by another writer"
    events = await event_types(repo, job_id)
    assert events[-1] == "test.other_writer"  # no runner event after the terminal write


async def test_d1_terminal_before_progress_save_hits_cas_conflict() -> None:
    window = WindowRepository()  # holds the snapshot taken when the other writer commits

    async def finalize_into_repo(r: ScheduledRepository, job: ResearchJob) -> None:
        await other_writer_finalizes_failed(r, job)
        window.snapshot = await InMemoryJobRepository.get(r, job.id)

    repo = ScheduledRepository(progress_of(2), [finalize_into_repo])
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    stored = await repo.get(job_id)
    assert window.snapshot is not None and stored == window.snapshot and final == stored
    assert provider.calls == QUERIES[:2]
    assert stored.result is not None  # only query 1's progress was stored before the writer
    assert [o.status for o in stored.result.outcomes] == ["COVERED"]
    progress_attempts = [a for a in repo.attempts if a[1] == "job.search.query_finished"]
    assert len(progress_attempts) == 2  # the conflicting attempt was not retried or re-applied
    assert (await event_types(repo, job_id))[-1] == "test.other_writer"


@pytest.mark.parametrize("yields", [0, 5], ids=["in-memory", "yielding"])
async def test_d2_terminal_before_loop_read_stops_before_next_provider_call(yields: int) -> None:
    repo = WindowRepository(k=1, yields=yields)
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    with capture_logs() as logs:
        final = await runner.run(job_id)

    assert repo.hooked
    assert provider.calls == QUERIES[:2]  # query 3 never started, even with a yielding repository
    await assert_terminal_snapshot_kept(repo, job_id, final)
    assert repo.attempts_after_terminal == []
    assert repo.snapshot is not None and repo.snapshot.result is not None
    assert [o.status for o in repo.snapshot.result.outcomes] == ["COVERED", "COVERED"]
    assert stage_log(logs)["stopped_by"] == "FINALIZED_ELSEWHERE"
    assert any(e["event"] == "job.finalized_elsewhere" for e in logs)
    assert not any(e["event"] in ("job.cancelled", "job.failed", "job.completed") for e in logs)


async def test_d3_terminal_between_reads_does_not_start_provider() -> None:
    repo = WindowRepository(k=2)  # in-memory reads do not yield: the task cannot have started
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    with capture_logs() as logs:
        final = await runner.run(job_id)

    assert repo.hooked
    assert provider.calls == QUERIES[:2]
    await assert_terminal_snapshot_kept(repo, job_id, final)
    assert repo.attempts_after_terminal == []
    assert stage_log(logs)["stopped_by"] == "FINALIZED_ELSEWHERE"  # not mislabelled CANCELLED


async def test_d3_yielding_read_may_start_provider_but_never_writes() -> None:
    """With a read that suspends, the provider task created just before it can start during the
    suspension (asyncio semantics; measured: from 2 suspensions per read on): the runner did not
    yet know the job was terminal. Its result must still never reach the store (as in D4)."""
    repo = WindowRepository(k=2, yields=5)
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    final = await runner.run(job_id)

    assert repo.hooked
    assert provider.calls == QUERIES  # started during the suspended read, then cancelled
    await assert_terminal_snapshot_kept(repo, job_id, final)
    assert repo.attempts_after_terminal == []


async def test_d4_terminal_during_in_flight_query_is_not_overwritten() -> None:
    repo = WindowRepository()
    holder: dict[str, str] = {}

    async def finalize() -> None:
        await repo.finalize(holder["job_id"])

    provider = HookedProvider(finalize, 3)
    runner = make_runner(repo, provider)
    job_id = holder["job_id"] = await submit(runner)

    final = await runner.run(job_id)

    assert provider.calls == QUERIES  # the call already running completes (accepted, bounded)
    await assert_terminal_snapshot_kept(repo, job_id, final)
    assert repo.snapshot is not None and repo.snapshot.result is not None
    assert [o.status for o in repo.snapshot.result.outcomes] == ["COVERED", "COVERED"]
    assert repo.attempts_after_terminal == []  # query 3's result is never written


@pytest.mark.parametrize(
    ("stage_timeout_s", "job_timeout_s", "limit"),
    [(0.2, 600.0, "SEARCH_STAGE_TIMEOUT"), (300.0, 0.2, "JOB_DEADLINE_EXCEEDED")],
    ids=["stage-timeout", "job-deadline"],
)
async def test_d5_d6_terminal_then_time_limit_is_not_reclassified(
    stage_timeout_s: float, job_timeout_s: float, limit: str
) -> None:
    repo = WindowRepository()
    holder: dict[str, str] = {}

    async def finalize() -> None:
        await repo.finalize(holder["job_id"])

    provider = HookedProvider(finalize, 2, hang=True)  # query 2 hangs after the writer commits
    runner = make_runner(
        repo, provider, stage_timeout_s=stage_timeout_s, job_timeout_s=job_timeout_s
    )
    job_id = holder["job_id"] = await submit(runner)

    with capture_logs() as logs:
        final = await runner.run(job_id)

    await assert_terminal_snapshot_kept(repo, job_id, final)
    assert final.error is not None and final.error.code == "INTERNAL_ERROR"  # not ``limit``
    assert repo.attempts_after_terminal == []
    assert stage_log(logs)["stopped_by"] == limit  # the loop really ended by the time limit
    assert any(e["event"] == "job.finalized_elsewhere" for e in logs)
    assert not any(e["event"] in ("job.cancelled", "job.failed", "job.completed") for e in logs)


@pytest.mark.parametrize(
    ("k", "third"),
    [(1, "NOT_RUN"), (2, "INTERRUPTED")],  # at W3 query 3's task already exists, then is cancelled
    ids=["W2", "W3"],
)
async def test_d7_real_cancel_in_the_same_windows_still_ends_cancelled(k: int, third: str) -> None:
    repo = WindowRepository(k=k, action="cancel")
    provider = three_urls()
    runner = make_runner(repo, provider)
    job_id = await submit(runner)

    with capture_logs() as logs:
        final = await runner.run(job_id)

    assert repo.hooked
    stored = await repo.get(job_id)
    assert final == stored
    assert stored.status is S.CANCELLED and stored.error is None and stored.cancel_requested
    assert provider.calls == QUERIES[:2]
    assert stored.result is not None
    assert [o.status for o in stored.result.outcomes] == ["COVERED", "COVERED", third]
    assert stored.result.coverage == "PARTIAL"
    assert stage_log(logs)["stopped_by"] == "CANCELLED"
    assert not any(e["event"] == "job.finalized_elsewhere" for e in logs)
    assert (await event_types(repo, job_id))[-1] == "job.cancelled"


async def test_d8_save_update_never_writes_onto_a_terminal_job() -> None:
    repo = ScheduledRepository()
    runner = make_runner(repo, three_urls())
    job_id = await submit(runner)
    queued = await repo.get(job_id)
    cancelled = await repo.request_cancel(job_id, now=queued.updated_at)  # QUEUED → CANCELLED
    assert cancelled.is_terminal

    returned = await runner._save_update(
        cancelled,
        {"updated_at": cancelled.updated_at, "result": None, "warnings": []},
        event="job.search.query_finished",
    )

    assert returned == cancelled
    assert repo.attempts == []  # no save attempted at all
    assert await repo.get(job_id) == cancelled
