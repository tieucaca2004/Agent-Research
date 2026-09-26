"""Live provider tests against the real APIs.

Without credentials each test is SKIPPED with reason ``REQUIRES_CONFIGURATION: <VARS>``.
A skip is never a PASS. Run explicitly with:  uv run pytest -m live -rs
"""

from __future__ import annotations

import httpx
import pytest

from research_agent.config import Settings
from research_agent.core.models import SearchOptions
from research_agent.providers.search import PROVIDERS

pytestmark = pytest.mark.live

QUERY = "Japanese restaurant Nha Trang menu"


@pytest.mark.parametrize("provider_name", sorted(PROVIDERS))
async def test_live_provider_returns_normalized_results(provider_name: str) -> None:
    settings = Settings()
    cls = PROVIDERS[provider_name]
    missing = cls.missing_settings(settings)
    if missing:
        pytest.skip(f"REQUIRES_CONFIGURATION: {', '.join(missing)}")

    async with httpx.AsyncClient() as client:
        provider = cls.from_settings(settings, client)
        results = await provider.search(
            QUERY, SearchOptions(max_results=5, country="VN", timeout_s=30)
        )

    assert results, f"{provider_name} returned no results for {QUERY!r}"
    for r in results:
        assert r.source == provider_name
        assert r.url.startswith(("http://", "https://"))
        assert r.title
        assert r.query == QUERY
