"""API process settings (Sprint 03). Separate from Sprint 01 ``Settings`` (not modified).

This is a local/internal API boundary: authentication and rate limiting are deferred, so the
server binds to 127.0.0.1 by default and must not be exposed publicly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_max_body_bytes: int = Field(default=16_384, ge=1_024, le=1_048_576)

    job_timeout_s: float = Field(default=600.0, gt=0, le=86_400)
    """Overall job deadline (Sprint 02 ``JobRunner.job_timeout_s``)."""
    job_search_timeout_s: float = Field(default=300.0, gt=0, le=86_400)
    """SEARCHING stage timeout (Sprint 02 ``JobRunner.search_stage_timeout_s``)."""

    max_concurrent_jobs: int = Field(default=2, ge=1, le=64)
    """Jobs executing at the same time. Conservative default: every job issues provider calls."""
    max_queued_jobs: int = Field(default=20, ge=0, le=10_000)
    """Accepted jobs waiting for an execution slot (status QUEUED). Beyond this, new jobs are
    rejected with 503 CAPACITY_EXHAUSTED."""
