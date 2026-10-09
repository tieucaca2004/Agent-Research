"""HTTP contracts. Kept separate from domain models: nothing internal (version, worker id, plan
internals, attempt records, exceptions) is exposed, and the mapping is explicit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_agent.jobs.models import (
    TERMINAL_STATES,
    JobError,
    JobResult,
    JobState,
    ResearchJob,
)


class CreateResearchBody(BaseModel):
    """Transport shape only; domain rules (length, normalization, control characters) are
    enforced by ``ResearchJobRequest``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(max_length=2_000)
    language: str | None = Field(default=None, max_length=8)
    country: str | None = Field(default=None, max_length=8)


class ProgressView(BaseModel):
    queries_total: int
    queries_done: int
    queries_failed: int
    results_unique: int
    # Sprint 07 pipeline jobs (additive; null for Sprint 02 jobs)
    urls_total: int | None = None
    urls_done: int | None = None
    fetch_failed: int | None = None
    documents: int | None = None
    groups: int | None = None


class ProviderErrorView(BaseModel):
    provider: str
    code: str
    category: str


class JobErrorView(BaseModel):
    code: str
    category: str
    step: str
    message: str
    provider_errors: list[ProviderErrorView] = Field(default_factory=list)


class ResultSummaryView(BaseModel):
    coverage: str
    results: int


class JobView(BaseModel):
    job_id: str
    query: str
    status: str
    stage: str | None
    stages: list[str]
    cancel_requested: bool
    progress: ProgressView | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime | None
    result_summary: ResultSummaryView | None
    error: JobErrorView | None
    warnings: list[JobErrorView]


class JobCreatedView(BaseModel):
    job_id: str
    status: str
    created_at: datetime


class ResultItemView(BaseModel):
    url: str
    title: str
    snippet: str | None
    provider: str
    providers: list[str]
    rank: int
    published_at: datetime | None
    occurrences: int


class ProviderStatusView(BaseModel):
    provider: str
    status: str
    calls: int
    failed: int
    error_categories: list[str]


class QueryOutcomeView(BaseModel):
    index: int
    status: str
    result_count: int


class SourceSummaryView(BaseModel):
    """One selected source of a pipeline job. Never page text (decision D9)."""

    position: int
    url: str
    providers: list[str]
    queries: list[str]
    state: str
    reason: str | None
    fetch_status: str | None
    http_status: int | None
    final_url: str | None
    document_id: str | None
    document_status: str | None
    document_warnings: list[str]
    text_chars: int
    groups: dict[str, str]
    """Dedup level → group key, for the levels where this source is in a group."""


class DuplicateGroupView(BaseModel):
    level: str
    key: str
    representative: int
    """Source position of the representative (best-ranked member)."""
    members: list[int]
    """Source positions, ascending."""
    warnings: list[str]


class DedupSummaryView(BaseModel):
    documents: int
    groups: list[DuplicateGroupView]
    exclusions: int
    errors: int


class ResultsView(BaseModel):
    job_id: str
    status: str
    coverage: str
    results: list[ResultItemView]
    provider_statuses: list[ProviderStatusView]
    query_outcomes: list[QueryOutcomeView]
    warnings: list[JobErrorView]
    error: JobErrorView | None
    # Sprint 07 pipeline jobs (additive; null for Sprint 02 jobs)
    sources: list[SourceSummaryView] | None = None
    dedup: DedupSummaryView | None = None


def error_view(error: JobError, *, with_provider_errors: bool) -> JobErrorView:
    return JobErrorView(
        code=error.code,
        category=error.category,
        step=error.step.value,
        message=error.message,
        provider_errors=[
            ProviderErrorView(provider=e.provider, code=e.code, category=e.category)
            for e in error.provider_errors
        ]
        if with_provider_errors
        else [],
    )


def _is_pipeline_job(job: ResearchJob) -> bool:
    return JobState.CRAWLING in job.stages


def job_view(job: ResearchJob) -> JobView:
    progress = None
    if job.progress is not None:
        progress = ProgressView(
            queries_total=job.progress.queries_total,
            queries_done=job.progress.queries_done,
            queries_failed=job.progress.queries_failed,
            results_unique=job.progress.results_unique,
        )
        if _is_pipeline_job(job):
            progress = progress.model_copy(
                update={
                    "urls_total": job.progress.urls_total,
                    "urls_done": job.progress.urls_done,
                    "fetch_failed": job.progress.fetch_failed,
                    "documents": job.progress.documents,
                    "groups": job.progress.groups,
                }
            )
    summary = None
    if job.result is not None and job.status in TERMINAL_STATES:
        response = job.result.response
        summary = ResultSummaryView(
            coverage=job.result.coverage, results=len(response.results) if response else 0
        )
    running_stage = job.status.value if job.status in job.stages else None
    return JobView(
        job_id=job.id,
        query=job.request.query,
        status=job.status.value,
        stage=running_stage,
        stages=[s.value for s in job.stages],
        cancel_requested=job.cancel_requested,
        progress=progress,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.completed_at,
        deadline_at=job.deadline_at,
        result_summary=summary,
        error=error_view(job.error, with_provider_errors=False) if job.error else None,
        warnings=[error_view(w, with_provider_errors=False) for w in job.warnings],
    )


def has_results(job: ResearchJob) -> bool:
    return job.result is not None and job.result.response is not None


def results_view(job: ResearchJob) -> ResultsView:
    result = job.result
    response = result.response if result is not None else None
    return ResultsView(
        job_id=job.id,
        status=job.status.value,
        coverage=result.coverage if result is not None else "NONE",
        results=[
            ResultItemView(
                url=r.result.url,
                title=r.result.title,
                snippet=r.result.snippet,
                provider=r.result.source,
                providers=list(r.providers),
                rank=r.result.rank,
                published_at=r.result.published_at,
                occurrences=r.occurrences,
            )
            for r in (response.results if response else [])
        ],
        provider_statuses=[
            ProviderStatusView(
                provider=s.provider,
                status=s.status,
                calls=s.calls,
                failed=s.failed,
                error_categories=list(s.error_categories),
            )
            for s in (response.provider_statuses if response else [])
        ],
        query_outcomes=[
            QueryOutcomeView(index=o.index, status=o.status, result_count=o.result_count)
            for o in (result.outcomes if result is not None else [])
        ],
        warnings=[error_view(w, with_provider_errors=True) for w in job.warnings],
        error=error_view(job.error, with_provider_errors=True) if job.error else None,
        sources=_source_views(result) if _is_pipeline_job(job) else None,
        dedup=_dedup_view(result) if _is_pipeline_job(job) else None,
    )


def _source_views(result: JobResult | None) -> list[SourceSummaryView]:
    if result is None:
        return []
    membership: dict[int, dict[str, str]] = {}
    if result.dedup is not None:
        positions = result.dedup.positions
        for level, groups in result.dedup.result.groups.items():
            for group in groups:
                for member in group.members:
                    membership.setdefault(positions[member], {})[level.value] = group.key
    return [
        SourceSummaryView(
            position=s.position,
            url=s.url,
            providers=list(s.providers),
            queries=list(s.queries),
            state=s.state,
            reason=s.reason,
            fetch_status=s.fetch.status.value if s.fetch else None,
            http_status=s.fetch.http_status if s.fetch else None,
            final_url=s.fetch.final_url if s.fetch else None,
            document_id=s.document_id,
            document_status=s.document_status.value if s.document_status else None,
            document_warnings=[w.value for w in s.document_warnings],
            text_chars=s.text_chars,
            groups=membership.get(s.position, {}),
        )
        for s in result.sources
    ]


def _dedup_view(result: JobResult | None) -> DedupSummaryView | None:
    if result is None or result.dedup is None:
        return None
    positions = result.dedup.positions
    document_set = result.dedup.result
    return DedupSummaryView(
        documents=document_set.stats.documents,
        groups=[
            DuplicateGroupView(
                level=level.value,
                key=group.key,
                representative=positions[group.representative],
                members=[positions[m] for m in group.members],
                warnings=[w.value for w in group.warnings],
            )
            for level, groups in document_set.groups.items()
            for group in groups
        ],
        exclusions=document_set.stats.exclusions,
        errors=document_set.stats.errors,
    )


def envelope(
    data: BaseModel | dict[str, Any] | None,
    *,
    request_id: str,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    return {"data": payload, "error": error, "meta": {"request_id": request_id, **(meta or {})}}
