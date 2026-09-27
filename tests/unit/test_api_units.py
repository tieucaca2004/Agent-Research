"""Sprint 03 unit tests: executor admission accounting, cancel status mapping, view mapping."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.api import ApiSettings
from research_agent.api.errors import ApiError
from research_agent.api.executor import JobExecutor
from research_agent.api.schemas import has_results, job_view
from research_agent.api.service import cancel_http_status
from research_agent.core.models import SearchOptions
from research_agent.jobs import (
    SPRINT_02_STAGES,
    InMemoryJobRepository,
    JobRunner,
    JobState,
    ResearchJob,
    ResearchJobRequest,
)
from research_agent.pipeline.search import SearchService
from tests.fakes import ScriptedSearchProvider

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def job(status: JobState, **kw: object) -> ResearchJob:
    return ResearchJob(
        id="a" * 32,
        version=1,
        request=ResearchJobRequest(query="món Nhật Nha Trang"),
        status=status,
        stages=SPRINT_02_STAGES,
        created_at=NOW,
        updated_at=NOW,
        **kw,
    )


def executor(max_concurrent: int = 1, max_queued: int = 1) -> JobExecutor:
    runner = JobRunner(
        InMemoryJobRepository(),
        SearchService([ScriptedSearchProvider("p", [["https://a/"]])]),
        base_options=SearchOptions(),
    )
    return JobExecutor(runner, max_concurrent=max_concurrent, max_queued=max_queued)


async def test_admission_capacity_is_concurrent_plus_queued() -> None:
    ex = executor(max_concurrent=1, max_queued=1)
    assert ex.try_reserve() and ex.try_reserve()
    assert not ex.try_reserve()
    assert ex.queued == 2
    ex.release()
    assert ex.try_reserve()


async def test_release_and_spawn_require_a_reservation() -> None:
    ex = executor()
    with pytest.raises(RuntimeError):
        ex.release()
    with pytest.raises(RuntimeError):
        ex.spawn("b" * 32)


def test_executor_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        executor(max_concurrent=0)
    with pytest.raises(ValueError):
        executor(max_queued=-1)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (JobState.CANCELLED, 200),
        (JobState.PLANNING, 202),
        (JobState.SEARCHING, 202),
        (JobState.QUEUED, 202),
    ],
)
def test_cancel_http_status(status: JobState, expected: int) -> None:
    assert cancel_http_status(job(status)) == expected


@pytest.mark.parametrize("status", [JobState.COMPLETED, JobState.PARTIAL, JobState.FAILED])
def test_cancel_finished_job_raises_409(status: JobState) -> None:
    with pytest.raises(ApiError) as info:
        cancel_http_status(job(status))
    assert (info.value.status_code, info.value.reason) == (409, "JOB_ALREADY_FINISHED")


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (JobState.QUEUED, None),
        (JobState.PLANNING, "PLANNING"),
        (JobState.SEARCHING, "SEARCHING"),
        (JobState.COMPLETED, None),
        (JobState.FAILED, None),
    ],
)
def test_job_view_stage(status: JobState, stage: str | None) -> None:
    view = job_view(job(status))
    assert view.stage == stage
    assert view.status == status.value
    assert not has_results(job(status))


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_concurrent_jobs": 0},
        {"max_queued_jobs": -1},
        {"job_timeout_s": 0},
        {"api_max_body_bytes": 10},
        {"api_port": 0},
    ],
)
def test_api_settings_validation(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApiSettings(_env_file=None, **overrides)  # type: ignore[arg-type]
