"""URL selection (Sprint 07, OD-13): the canonical-order prefix of the job's search results.

The canonical order is ``JobResult.response.results`` (S01 ``SearchRun.to_response``: query
order, provider priority, provider rank; one entry per normalized URL). Position ``p`` of a
selected source is its index in that list. No reordering, no per-host cap (D11).
"""

from __future__ import annotations

from dataclasses import dataclass

from research_agent.core.models import DedupedSearchResult, SearchResponse


@dataclass(frozen=True)
class Selection:
    sources: tuple[DedupedSearchResult, ...]
    not_selected: int


def select_sources(response: SearchResponse | None, max_urls: int) -> Selection:
    if max_urls < 1:
        raise ValueError("max_urls must be positive")
    results = response.results if response is not None else []
    chosen = tuple(results[:max_urls])
    return Selection(sources=chosen, not_selected=len(results) - len(chosen))
