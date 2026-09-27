"""Research planners.

Sprint 02 ships only ``FixedPlanner``: deterministic, no LLM, ``queries = [query]``.
The LLM planner (ARCHITECTURE §3) arrives with ``AIProvider`` in a later sprint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from research_agent.core.models import SearchOptions
from research_agent.jobs.models import ResearchJobRequest, ResearchPlan


class ResearchPlanner(Protocol):
    name: str

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: datetime
    ) -> ResearchPlan: ...


class FixedPlanner:
    name = "fixed-v1"

    def plan(
        self, request: ResearchJobRequest, *, base_options: SearchOptions, now: datetime
    ) -> ResearchPlan:
        overrides: dict[str, object] = {}
        if request.language is not None:
            overrides["language"] = request.language
        if request.country is not None:
            overrides["country"] = request.country
        return ResearchPlan(
            planner=self.name,
            queries=[request.query],
            search_options=base_options.model_copy(update=overrides),
            created_at=now,
        )
