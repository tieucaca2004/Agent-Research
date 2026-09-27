"""Extraction settings (Sprint 05). Separate from the Sprint 01/03/04 settings (not modified)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtractionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXTRACT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    max_input_chars: int = Field(default=6_000_000, ge=1_000, le=20_000_000)
    """Longer input is truncated (``INPUT_TRUNCATED`` → ``PARTIAL``)."""
    max_tag_chars: int = Field(default=32_768, ge=1_024, le=1_048_576)
    """Markup constructs (``<…>``) at least this long are removed before parsing (design P5)."""
    max_elements: int = Field(default=100_000, ge=100, le=1_000_000)
    max_depth: int = Field(default=256, ge=8, le=256)
    """Element nesting kept in the tree; deeper text is attached to the deepest kept element."""
    max_text_chars: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    max_title_chars: int = Field(default=300, ge=10, le=10_000)
    max_links: int = Field(default=500, ge=0, le=10_000)
    max_metadata_chars: int = Field(default=1_000, ge=10, le=100_000)
    max_jsonld_blocks: int = Field(default=5, ge=0, le=100)
    max_jsonld_chars: int = Field(default=100_000, ge=100, le=1_000_000)
    min_main_chars: int = Field(default=200, ge=0, le=100_000)
    """A ``<main>``/``<article>`` with less text (and < 25 % of the body text) is not selected."""
    timeout_s: float = Field(default=10.0, gt=0, le=120)
    """Cooperative time budget per document; checked between parser chunks and while rendering."""
    concurrency: int = Field(default=2, ge=1, le=16)
    """Extractions running at the same time (threads) per ``Extractor`` in ``aextract``."""
