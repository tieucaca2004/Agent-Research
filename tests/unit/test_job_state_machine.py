"""Sprint 02: job domain, state machine and in-memory repository."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from research_agent.jobs import (
    SPRINT_02_STAGES,
    IdempotencyConflictError,
    InMemoryJobRepository,
    InvalidJobTransitionError,
    JobNotClaimableError,
    JobNotFoundError,
    JobState,
    JobVersionConflictError,
    ResearchJob,
    ResearchJobRequest,
    allowed_targets,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
S = JobState


def req(query: str = "món Nhật Nha Trang", **kw: object) -> ResearchJobRequest:
    return ResearchJobRequest(query=query, **kw)


async def created(
    repo: InMemoryJobRepository | None = None,
) -> tuple[InMemoryJobRepository, ResearchJob]:
    repo = repo or InMemoryJobRepository()
    job, _ = await repo.create(req(), now=NOW)
    return repo, job


async def move(repo: InMemoryJobRepository, job: ResearchJob, to: JobState) -> ResearchJob:
    return await repo.save(job.model_copy(update={"status": to}), expected_version=job.version)


# --- request / job model ---------------------------------------------------------------


def test_request_normalizes_whitespace_and_unicode() -> None:
    r = req("  món   Nhật \n Nha Trang ")
    assert r.query == "món Nhật Nha Trang"


@pytest.mark.parametrize("query", ["", "ab", "x" * 501, "bad\x00query", "bell\x07here"])
def test_request_rejects_invalid_query(query: str) -> None:
    with pytest.raises(ValidationError):
        req(query)


@pytest.mark.parametrize(
    "stages",
    [
        (),
        (S.SEARCHING, S.PLANNING),  # wrong order
        (S.PLANNING, S.PLANNING),  # duplicate
        (S.PLANNING, S.COMPLETED),  # not a stage
        (S.QUEUED,),
    ],
)
def test_job_rejects_invalid_stages(stages: tuple[JobState, ...]) -> None:
    with pytest.raises(ValidationError):
        ResearchJob(
            id="j",
            version=0,
            request=req(),
            status=S.QUEUED,
            stages=stages,
            created_at=NOW,
            updated_at=NOW,
        )


# --- create / initial state --------------------------------------------------------------


async def test_create_job_initial_state_and_event() -> None:
    repo, job = await created()
    assert job.status is S.QUEUED
    assert job.version == 0
    assert job.stages == SPRINT_02_STAGES == (S.PLANNING, S.SEARCHING)
    assert len(job.id) == 32
    assert job.plan is None and job.result is None and job.error is None
    assert [e.type for e in await repo.list_events(job.id)] == ["job.created"]


async def test_get_unknown_job() -> None:
    with pytest.raises(JobNotFoundError):
        await InMemoryJobRepository().get("nope")


# --- transition table ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (S.QUEUED, {S.PLANNING, S.CANCELLED, S.FAILED}),
        (S.PLANNING, {S.SEARCHING, S.CANCELLED, S.FAILED}),
        (S.SEARCHING, {S.COMPLETED, S.PARTIAL, S.FAILED, S.CANCELLED}),
        (S.CRAWLING, set()),  # not configured for Sprint 02 jobs
        (S.COMPLETED, set()),
        (S.PARTIAL, set()),
        (S.FAILED, set()),
        (S.CANCELLED, set()),
    ],
)
def test_allowed_targets_for_sprint_02_pipeline(current: JobState, expected: set[JobState]) -> None:
    assert allowed_targets(SPRINT_02_STAGES, current) == expected


def test_partial_is_outcome_of_last_configured_stage_not_only_verifying() -> None:
    full = (S.PLANNING, S.SEARCHING, S.CRAWLING, S.EXTRACTING, S.NORMALIZING, S.VERIFYING)
    assert S.PARTIAL in allowed_targets(full, S.VERIFYING)
    assert S.PARTIAL not in allowed_targets(full, S.SEARCHING)  # not last there
    assert allowed_targets(full, S.SEARCHING) == {S.CRAWLING, S.CANCELLED, S.FAILED}
    assert S.PARTIAL in allowed_targets(SPRINT_02_STAGES, S.SEARCHING)


async def test_valid_lifecycle_through_repository() -> None:
    repo, job = await created()
    job = await repo.claim(job.id, worker_id="w", now=NOW, timeout_s=60)
    assert job.status is S.PLANNING
    assert job.deadline_at == NOW + timedelta(seconds=60)
    job = await move(repo, job, S.SEARCHING)
    job = await move(repo, job, S.COMPLETED)
    assert job.version == 3
    changes = [
        (e.data["from"], e.data["to"])
        for e in await repo.list_events(job.id)
        if e.type == "job.state_changed"
    ]
    assert changes == [
        ("QUEUED", "PLANNING"),
        ("PLANNING", "SEARCHING"),
        ("SEARCHING", "COMPLETED"),
    ]


@pytest.mark.parametrize(
    ("path", "bad_target"),
    [
        ([S.PLANNING, S.SEARCHING], S.PLANNING),  # backward
        ([S.PLANNING], S.QUEUED),  # back to QUEUED
        ([], S.SEARCHING),  # skip PLANNING
        ([], S.COMPLETED),  # QUEUED -> COMPLETED
        ([], S.PARTIAL),
        ([S.PLANNING], S.PARTIAL),  # PARTIAL only after last stage
        ([S.PLANNING], S.COMPLETED),  # skip SEARCHING
        ([S.PLANNING, S.SEARCHING], S.CRAWLING),  # stage not configured
        ([S.PLANNING, S.SEARCHING, S.COMPLETED], S.FAILED),  # from terminal
        ([S.PLANNING, S.SEARCHING, S.PARTIAL], S.COMPLETED),
        ([S.CANCELLED], S.PLANNING),
        ([S.FAILED], S.CANCELLED),
    ],
)
async def test_invalid_transitions_fail_loudly(path: list[JobState], bad_target: JobState) -> None:
    repo, job = await created()
    for state in path:
        job = await move(repo, job, state)
    with pytest.raises(InvalidJobTransitionError) as info:
        await move(repo, job, bad_target)
    assert info.value.code == "INVALID_JOB_TRANSITION"
    assert (await repo.get(job.id)).status is job.status  # nothing stored


async def test_self_transition_is_not_a_state_change() -> None:
    repo, job = await created()
    job = await move(repo, job, S.PLANNING)
    same = await repo.save(
        job.model_copy(update={"cancel_requested": False}), expected_version=job.version
    )
    assert same.status is S.PLANNING
    assert [e.type for e in await repo.list_events(job.id)].count("job.state_changed") == 1


async def test_optimistic_version_check() -> None:
    repo, job = await created()
    await move(repo, job, S.PLANNING)
    with pytest.raises(JobVersionConflictError):
        await move(repo, job, S.PLANNING)  # stale version 0


async def test_request_and_stages_are_immutable() -> None:
    repo, job = await created()
    with pytest.raises(ValueError, match="immutable"):
        await repo.save(job.model_copy(update={"request": req("other query")}), expected_version=0)
    with pytest.raises(ValueError, match="immutable"):
        await repo.save(job.model_copy(update={"stages": (S.PLANNING,)}), expected_version=0)


# --- claim / cancel / idempotency --------------------------------------------------------


async def test_claim_is_compare_and_set() -> None:
    repo, job = await created()
    await repo.claim(job.id, worker_id="a", now=NOW, timeout_s=60)
    with pytest.raises(JobNotClaimableError) as info:
        await repo.claim(job.id, worker_id="b", now=NOW, timeout_s=60)
    assert info.value.status == "PLANNING"
    assert (await repo.get(job.id)).worker_id == "a"


async def test_cancel_queued_job_is_immediate_and_final() -> None:
    repo, job = await created()
    cancelled = await repo.request_cancel(job.id, now=NOW)
    assert cancelled.status is S.CANCELLED
    assert cancelled.completed_at == NOW
    with pytest.raises(JobNotClaimableError):
        await repo.claim(job.id, worker_id="w", now=NOW, timeout_s=60)


async def test_cancel_running_job_sets_flag_and_is_idempotent() -> None:
    repo, job = await created()
    await repo.claim(job.id, worker_id="w", now=NOW, timeout_s=60)
    first = await repo.request_cancel(job.id, now=NOW)
    second = await repo.request_cancel(job.id, now=NOW)
    assert first.status is S.PLANNING and first.cancel_requested
    assert second.version == first.version


async def test_cancel_terminal_job_is_noop() -> None:
    repo, job = await created()
    for state in (S.PLANNING, S.SEARCHING, S.COMPLETED):
        job = await move(repo, job, state)
    after = await repo.request_cancel(job.id, now=NOW)
    assert after.status is S.COMPLETED
    assert after.version == job.version


async def test_idempotent_create() -> None:
    repo = InMemoryJobRepository()
    a, created_a = await repo.create(req(idempotency_key="k1"), now=NOW)
    b, created_b = await repo.create(req(idempotency_key="k1"), now=NOW)
    assert created_a and not created_b
    assert a.id == b.id
    with pytest.raises(IdempotencyConflictError) as info:
        await repo.create(req("different query", idempotency_key="k1"), now=NOW)
    assert "k1" not in info.value.message  # key itself is not echoed
    c, created_c = await repo.create(req(), now=NOW)
    d, created_d = await repo.create(req(), now=NOW)
    assert created_c and created_d and c.id != d.id  # no key → independent jobs
