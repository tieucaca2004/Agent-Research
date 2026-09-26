from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from research_agent.config import Settings

FAKE_PPLX_KEY = "pplx-test-key-not-real"
FAKE_GOOGLE_KEY = "AIza-test-key-not-real"
FAKE_CSE_ID = "cse-test-id"

_ENV_VARS = (
    "PERPLEXITY_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CSE_ID",
    "SEARCH_PROVIDERS",
    "SEARCH_STRATEGY",
)


def build_settings(**values: Any) -> Settings:
    """Settings isolated from the real environment and any .env file."""
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    # Live tests read real credentials; everything else must never see them.
    if request.node.get_closest_marker("live") is None:
        for name in _ENV_VARS:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client
