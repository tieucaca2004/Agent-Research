"""Runtime configuration, read from environment variables (and optional ``.env``).

Secrets are ``SecretStr`` so they are masked in ``repr``/logs. No defaults for secrets.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    search_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["perplexity", "google"]
    )
    search_strategy: Literal["fallback", "fanout"] = "fallback"

    perplexity_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    google_cse_id: str | None = None

    search_timeout_s: float = Field(default=15.0, gt=0, le=120)
    search_max_results: int = Field(default=10, ge=1, le=50)
    search_max_retries: int = Field(default=1, ge=0, le=5)
    search_concurrency: int = Field(default=4, ge=1, le=32)
    search_circuit_breaker_threshold: int = Field(default=3, ge=1, le=100)

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    def safe_summary(self) -> dict[str, object]:
        """Configuration suitable for logging: secrets reported only as CONFIGURED/MISSING.

        Credentials are a list of ``{"name", "status"}`` records so that the log redaction
        filter (which masks values under secret-looking keys) does not hide the status.
        """
        credentials = [
            ("PERPLEXITY_API_KEY", self.perplexity_api_key),
            ("GOOGLE_API_KEY", self.google_api_key),
            ("GOOGLE_CSE_ID", self.google_cse_id),
        ]
        return {
            "search_providers": list(self.search_providers),
            "search_strategy": self.search_strategy,
            "search_timeout_s": self.search_timeout_s,
            "search_max_results": self.search_max_results,
            "search_max_retries": self.search_max_retries,
            "search_concurrency": self.search_concurrency,
            "search_circuit_breaker_threshold": self.search_circuit_breaker_threshold,
            "credentials": [
                {"name": name, "status": "CONFIGURED" if value is not None else "MISSING"}
                for name, value in credentials
            ],
        }

    @field_validator("search_providers", mode="before")
    @classmethod
    def _split_providers(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip().lower() for p in value.split(",") if p.strip()]
        return value

    @field_validator("perplexity_api_key", "google_api_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("google_cse_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value
