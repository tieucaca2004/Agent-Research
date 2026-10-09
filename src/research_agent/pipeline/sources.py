"""Source collection for the CRAWLING stage (Sprint 07): admitted S04 fetch + S05 extraction,
captured in position slots (OD-13).

- Position ``p`` = index in the canonical selection (``JobResult.response.results`` prefix).
  Every result is written to slot ``p``; nothing is appended in completion order.
- ``Crawler.fetch_many`` is never used and the crawler is never wrapped in an outer timeout that
  would discard results (P3, P4): each target runs in its own task, captured as it completes.
- Joins are checked (``source`` identity, requested URL, crawl id); a mismatch is a stage error.
- A fetch body is released right after extraction (``FetchRecord`` keeps everything but it).
- Per-target exceptions stop the stage (``StageFailure``); exceptions raised by the caller's hooks
  (``before_fetch``, ``on_capture``) propagate unchanged. Captured slots are never discarded.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlsplit

from research_agent.crawler import CrawlContext, Crawler, CrawlerSettings, FetchStatus, FetchTarget
from research_agent.crawler.models import FetchResult
from research_agent.dedup import DocumentSet, group_documents
from research_agent.extraction import ExtractedDocument, ExtractionSettings, Extractor
from research_agent.extraction.models import ExtractionStatus, ExtractionWarning
from research_agent.logging import get_logger
from research_agent.pipeline.admission import AdmissionTicket, BodyBudget, FetchAdmission
from research_agent.pipeline.config import PipelineSettings, check_text_budget
from research_agent.pipeline.discovery import Selection
from research_agent.pipeline.models import (
    CrawlStats,
    FetchRecord,
    SourceRecord,
    SourceState,
    StopReason,
)

log = get_logger(__name__)

BACKSTOP_GRACE_S = 5.0
"""Extra time after the stage deadline before in-flight work is cancelled (fetches and
extractions honour the deadline themselves; the backstop only catches a hang)."""

Deduplicate = Callable[[Sequence[ExtractedDocument]], DocumentSet]


StageErrorCode = Literal[
    "CRAWL_FAILED", "EXTRACTION_FAILED", "DEDUP_FAILED", "PIPELINE_BUDGET_EXCEEDED"
]


class StageFailure(Exception):
    """A stage error (D7). ``code`` is one of the ``JobErrorCode``s above; the message is fixed
    text (no URL, page text or exception text)."""

    def __init__(self, code: StageErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Pipeline:
    """Process-wide pipeline components: one crawler, one admission, one extractor."""

    settings: PipelineSettings
    crawler: Crawler
    extractor: Extractor
    admission: FetchAdmission
    bodies: BodyBudget
    deduplicate: Deduplicate = group_documents
    fetch_timeout_s: float = 15.0
    """S04 per-URL budget (``CRAWL_TIMEOUT_S``), to tell deadline-capped fetches apart."""

    @classmethod
    def build(
        cls,
        settings: PipelineSettings,
        crawler: Crawler,
        *,
        crawler_settings: CrawlerSettings,
        extractor: Extractor | None = None,
        extraction_settings: ExtractionSettings | None = None,
        max_concurrent_jobs: int = 1,
        extra_ports: frozenset[int] = frozenset(),
    ) -> Pipeline:
        extraction = extraction_settings or ExtractionSettings()
        check_text_budget(settings, extraction.max_text_chars)
        per_job = settings.job_fetch_concurrency or math.ceil(
            crawler_settings.concurrency / max(max_concurrent_jobs, 1)
        )
        return cls(
            settings=settings,
            crawler=crawler,
            extractor=extractor or Extractor(extraction),
            admission=FetchAdmission(
                global_limit=crawler_settings.concurrency,
                per_host_limit=crawler_settings.per_host_concurrency,
                per_job_limit=per_job,
                extra_ports=extra_ports,
            ),
            bodies=BodyBudget(
                crawler_settings.concurrency + settings.extraction_backlog + extraction.concurrency
            ),
            fetch_timeout_s=crawler_settings.timeout_s,
        )


_PENDING, _WAITING, _FETCHING, _FETCHED, _EXTRACTED = range(5)
_STATE: dict[int, SourceState] = {
    _PENDING: "NOT_RUN",
    _WAITING: "NOT_RUN",
    _FETCHING: "INTERRUPTED",
    _FETCHED: "FETCHED",
    _EXTRACTED: "EXTRACTED",
}


@dataclass
class SourceCapture:
    """Position-indexed slots, mutated in place while the stage runs (shared with the runner)."""

    selection: Selection
    fetches: list[FetchRecord | None] = field(default_factory=list)
    documents: list[ExtractedDocument | None] = field(default_factory=list)
    phase: list[int] = field(default_factory=list)
    timeout_kind: list[str | None] = field(default_factory=list)
    admitted: int = 0
    wait_ms: list[int] = field(default_factory=list)
    text_chars: int = 0
    deadline_hit: bool = False
    stop_reason: StopReason | None = None

    def __post_init__(self) -> None:
        n = len(self.selection.sources)
        self.fetches = [None] * n
        self.documents = [None] * n
        self.phase = [_PENDING] * n
        self.timeout_kind = [None] * n

    def documents_in_order(self) -> tuple[list[ExtractedDocument], list[int]]:
        """S06 input and its position map (strictly increasing)."""
        positions = [p for p, d in enumerate(self.documents) if d is not None]
        return [d for d in self.documents if d is not None], positions

    def records(self) -> list[SourceRecord]:
        out: list[SourceRecord] = []
        for p, hit in enumerate(self.selection.sources):
            phase, document = self.phase[p], self.documents[p]
            state = _STATE[phase]
            out.append(
                SourceRecord(
                    position=p,
                    url=hit.result.url,
                    original_url=hit.result.original_url,
                    providers=list(hit.providers),
                    queries=list(hit.queries),
                    state=state,
                    reason=None if state == "EXTRACTED" else self.stop_reason,
                    fetch=self.fetches[p],
                    document_id=document.document_id if document else None,
                    document_status=document.status if document else None,
                    document_warnings=list(document.warnings) if document else [],
                    text_chars=len(document.text) if document else 0,
                )
            )
        return out

    def stats(self) -> CrawlStats:
        fetched = [f for f in self.fetches if f is not None]
        documents = [d for d in self.documents if d is not None]
        kinds = Counter(k for k in self.timeout_kind if k is not None)
        return CrawlStats(
            selected=len(self.selection.sources),
            not_selected=self.selection.not_selected,
            admitted=self.admitted,
            fetched=len(fetched),
            fetch_ok=sum(1 for f in fetched if f.status is FetchStatus.OK),
            fetch_failed=sum(1 for f in fetched if f.status is not FetchStatus.OK),
            fetch_status_counts=dict(sorted(Counter(f.status.value for f in fetched).items())),
            fetch_timeout=sum(1 for f in fetched if f.status is FetchStatus.FETCH_TIMEOUT),
            fetch_timeout_cross_host_redirect=kinds["cross_host_redirect"],
            fetch_timeout_deadline_capped=kinds["deadline_capped"],
            fetch_timeout_other=kinds["other"],
            extracted=len(documents),
            extraction_status_counts=dict(
                sorted(Counter(d.status.value for d in documents).items())
            ),
            not_run=sum(1 for ph in self.phase if ph in (_PENDING, _WAITING)),
            interrupted=sum(1 for ph in self.phase if ph == _FETCHING),
            fetched_not_extracted=sum(1 for ph in self.phase if ph == _FETCHED),
            text_chars=self.text_chars,
            admission_wait_ms_total=sum(self.wait_ms),
            admission_wait_ms_max=max(self.wait_ms, default=0),
            deadline_hit=self.deadline_hit,
        )


def _timeout_kind(record: FetchRecord, *, capped: bool) -> str:
    initial = (urlsplit(record.requested_url).hostname or "").lower()
    hops = {(urlsplit(h.url).hostname or "").lower() for h in record.redirect_chain}
    # a hop's target is its Location (on a timeout S04 records no final URL)
    hops |= {
        (urlsplit(urljoin(h.url, h.location)).hostname or "").lower()
        for h in record.redirect_chain
        if h.location
    }
    if record.final_url is not None:
        hops.add((urlsplit(record.final_url).hostname or "").lower())
    if hops - {initial}:
        return "cross_host_redirect"
    return "deadline_capped" if capped else "other"


async def collect_sources(
    pipeline: Pipeline,
    capture: SourceCapture,
    *,
    job_id: str,
    deadline: float,
    before_fetch: Callable[[], Awaitable[None]],
    on_capture: Callable[[], Awaitable[None]],
) -> None:
    """Fetch and extract every selected source into ``capture`` (loop-time ``deadline``).

    ``before_fetch`` runs after admission, immediately before each fetch (the runner re-reads
    the job there and raises to stop); ``on_capture`` runs after each captured fetch and
    document (progress save). Both are the caller's: their exceptions propagate unchanged.
    """
    loop = asyncio.get_running_loop()
    sources = capture.selection.sources

    async def one(p: int) -> None:
        hit = sources[p]
        target = FetchTarget.from_search_result(hit.result)
        capture.phase[p] = _WAITING
        ticket: AdmissionTicket | None = await pipeline.admission.acquire(
            job_id, p, target.url, deadline=deadline
        )
        if ticket is None:
            capture.deadline_hit = True
            return
        capture.admitted += 1
        capture.wait_ms.append(int(ticket.waited_s * 1000))
        body_held = False
        try:
            body_held = await pipeline.bodies.acquire(deadline=deadline)
            if not body_held:
                capture.deadline_hit = True
                return
            await before_fetch()
            capture.phase[p] = _FETCHING
            remaining = deadline - loop.time()
            try:
                result = await pipeline.crawler.fetch(
                    target,
                    CrawlContext(
                        job_id=job_id, request_id=f"{job_id}:crawl:{p}", deadline=deadline
                    ),
                )
            except Exception as exc:
                raise StageFailure("CRAWL_FAILED", "fetch raised unexpectedly") from exc
            finally:
                pipeline.admission.release(ticket)
            if result.source is not hit.result or result.requested_url != target.url:
                raise StageFailure("CRAWL_FAILED", "fetch result does not match its source")
            record = FetchRecord.from_result(result)
            capture.fetches[p] = record
            capture.phase[p] = _FETCHED
            if record.status is FetchStatus.FETCH_TIMEOUT:
                capped = remaining < pipeline.fetch_timeout_s
                capture.timeout_kind[p] = _timeout_kind(record, capped=capped)
                if loop.time() >= deadline:
                    capture.deadline_hit = True
            await on_capture()
            document = await _extract(pipeline, result, deadline)
            del result  # the body is released with the last reference
            if document.provenance.crawl_id != record.crawl_id:
                raise StageFailure("EXTRACTION_FAILED", "document does not match its fetch")
            capture.documents[p] = document
            capture.phase[p] = _EXTRACTED
            capture.text_chars += len(document.text)
            if (
                ExtractionWarning.TIME_BUDGET_EXCEEDED in document.warnings
                and loop.time() >= deadline
            ):
                capture.deadline_hit = True
            if capture.text_chars > pipeline.settings.max_total_text_chars:
                raise StageFailure(
                    "PIPELINE_BUDGET_EXCEEDED", "extracted text exceeds the job budget"
                )
            await on_capture()
        finally:
            pipeline.admission.release(ticket)
            if body_held:
                pipeline.bodies.release()

    try:
        async with asyncio.timeout_at(deadline + BACKSTOP_GRACE_S):
            async with asyncio.TaskGroup() as group:
                for p in range(len(sources)):
                    group.create_task(one(p))
    except TimeoutError:
        capture.deadline_hit = True
    except BaseExceptionGroup as failure:
        raise _first(failure) from None


async def _extract(pipeline: Pipeline, result: FetchResult, deadline: float) -> ExtractedDocument:
    try:
        return await pipeline.extractor.aextract(result, CrawlContext(deadline=deadline))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise StageFailure("EXTRACTION_FAILED", "extraction raised unexpectedly") from exc


def _first(group: BaseExceptionGroup) -> BaseException:
    """The exception that stops the stage: a caller signal first, else the first stage failure."""
    leaves: list[BaseException] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for inner in exc.exceptions:
                walk(inner)
        elif not isinstance(exc, asyncio.CancelledError):
            leaves.append(exc)

    walk(group)
    for exc in leaves:
        if not isinstance(exc, StageFailure):
            return exc
    return leaves[0] if leaves else asyncio.CancelledError()


def usable_documents(documents: Sequence[ExtractedDocument]) -> int:
    return sum(
        1 for d in documents if d.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)
    )
