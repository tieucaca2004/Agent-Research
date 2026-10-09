"""Pipeline integration settings (Sprint 07). Separate from the S01-S06 settings (not modified).

The pipeline (CRAWLING = S04 fetch + S05 extraction, NORMALIZING = S06 grouping) is **off by
default** (decision D10). Limits are the founder-approved budgets (D2): at most 30 URLs and
30 000 000 extracted text characters per job.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from research_agent.core.errors import ResearchAgentError

MAX_URLS_PER_JOB = 30
MAX_TOTAL_TEXT_CHARS = 30_000_000


class PipelineConfigurationError(ResearchAgentError):
    code = "PIPELINE_CONFIGURATION_ERROR"
    category = "CONFIGURATION_ERROR"


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIPELINE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = False
    """Run new jobs as PLANNING → SEARCHING → CRAWLING → NORMALIZING (D10: default off)."""
    max_urls: int = Field(default=MAX_URLS_PER_JOB, ge=1, le=MAX_URLS_PER_JOB)
    """Canonical-order prefix of the search results that is fetched (D2)."""
    max_total_text_chars: int = Field(
        default=MAX_TOTAL_TEXT_CHARS, ge=1_000, le=MAX_TOTAL_TEXT_CHARS
    )
    """Upper bound of extracted text per job (D2); checked statically and at runtime."""
    crawl_stage_timeout_s: float = Field(default=180.0, gt=0, le=3_600)
    extraction_backlog: int = Field(default=4, ge=1, le=64)
    """Fetched bodies waiting for extraction, per process (backpressure)."""
    job_fetch_concurrency: int | None = Field(default=None, ge=1, le=64)
    """Admitted fetches per job; ``None`` → ceil(crawler concurrency / max concurrent jobs)."""


def check_text_budget(settings: PipelineSettings, max_text_chars: int) -> None:
    """Static D2 check: ``max_urls * EXTRACT_MAX_TEXT_CHARS <= max_total_text_chars``."""
    if settings.max_urls * max_text_chars > settings.max_total_text_chars:
        raise PipelineConfigurationError(
            "pipeline text budget exceeded by configuration: "
            f"{settings.max_urls} URLs x {max_text_chars} chars > "
            f"{settings.max_total_text_chars} chars"
        )
