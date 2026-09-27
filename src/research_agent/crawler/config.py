"""Crawler settings (Sprint 04). Separate from Sprint 01/03 settings (not modified).

No setting can disable SSRF protection, robots.txt, TLS verification or the port policy.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class CrawlerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRAWL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    timeout_s: float = Field(default=15.0, gt=0, le=120)
    """Total wall-clock budget per URL (all attempts, redirects and robots.txt)."""
    connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    read_timeout_s: float = Field(default=10.0, gt=0, le=60)
    max_response_bytes: int = Field(default=5 * MIB, ge=1_024, le=50 * MIB)
    """Limit on *decoded* body bytes."""
    max_redirects: int = Field(default=5, ge=0, le=10)
    retries: int = Field(default=1, ge=0, le=1)
    """At most one retry (two attempts) for transient failures."""
    retry_backoff_s: float = Field(default=1.0, ge=0, le=10)
    concurrency: int = Field(default=5, ge=1, le=64)
    """URLs fetched at the same time by one crawler instance."""
    per_host_concurrency: int = Field(default=1, ge=1, le=8)
    per_host_min_interval_s: float = Field(default=1.0, ge=0, le=60)
    """Minimum spacing between request starts to the same host (≈ 1 request/s)."""
    robots_timeout_s: float = Field(default=5.0, gt=0, le=30)
    robots_max_bytes: int = Field(default=512 * 1024, ge=1_024, le=5 * MIB)
    robots_cache_ttl_s: float = Field(default=86_400.0, ge=0)
    ca_bundle: Path | None = None
    """Optional CA bundle for TLS verification (e.g. a corporate / sandbox TLS-inspection CA).
    Verification is always on; this only changes which CAs are trusted."""
