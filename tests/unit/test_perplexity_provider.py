import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from research_agent.core.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from research_agent.core.models import SearchOptions
from research_agent.providers.search.perplexity import PerplexitySearchProvider
from tests.conftest import FAKE_PPLX_KEY, build_settings

ENDPOINT = "https://api.perplexity.ai/search"

OK_BODY = {
    "id": "req-123",
    "results": [
        {
            "title": " Sushi Bar Nha Trang - Menu ",
            "url": "https://SushiBar.example/menu?utm_source=pplx",
            "snippet": "Sushi cá hồi 120.000đ",
            "date": "2025-03-20",
            "last_updated": "2025-06-01",
        },
        {"title": "Bad", "url": "javascript:alert(1)", "snippet": "x"},
        {"title": "No date", "url": "https://other.example/", "snippet": ""},
    ],
    "server_time": None,
}


@pytest.fixture
def provider(http_client: httpx.AsyncClient) -> PerplexitySearchProvider:
    return PerplexitySearchProvider(FAKE_PPLX_KEY, http_client)


def test_from_settings_requires_key(http_client: httpx.AsyncClient) -> None:
    with pytest.raises(ProviderConfigurationError) as info:
        PerplexitySearchProvider.from_settings(build_settings(), http_client)
    assert info.value.code == "REQUIRES_CONFIGURATION"
    assert info.value.missing == ("PERPLEXITY_API_KEY",)


def test_constructor_rejects_empty_key(http_client: httpx.AsyncClient) -> None:
    with pytest.raises(ProviderConfigurationError):
        PerplexitySearchProvider("", http_client)


def test_repr_hides_key(provider: PerplexitySearchProvider) -> None:
    assert FAKE_PPLX_KEY not in repr(provider)


@respx.mock
async def test_request_contract_and_mapping(provider: PerplexitySearchProvider) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=OK_BODY))
    options = SearchOptions(max_results=50, language="vi", country="VN", timeout_s=7)

    results = await provider.search("món Nhật Nha Trang", options)

    request = route.calls.last.request
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {FAKE_PPLX_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "query": "món Nhật Nha Trang",
        "max_results": 20,  # clamped to documented web-search maximum
        "country": "VN",
        "search_language_filter": ["vi"],
    }

    assert [r.url for r in results] == ["https://sushibar.example/menu", "https://other.example/"]
    first = results[0]
    assert first.title == "Sushi Bar Nha Trang - Menu"
    assert first.original_url == "https://SushiBar.example/menu?utm_source=pplx"
    assert first.snippet == "Sushi cá hồi 120.000đ"
    assert first.source == "perplexity"
    assert first.rank == 1
    assert first.query == "món Nhật Nha Trang"
    assert first.published_at == datetime(2025, 3, 20, tzinfo=UTC)
    assert first.metadata == {"provider_request_id": "req-123", "last_updated": "2025-06-01"}
    second = results[1]
    assert second.rank == 3  # provider rank preserved even though item 2 was skipped
    assert second.snippet is None
    assert second.published_at is None


@respx.mock
async def test_minimal_request_body(provider: PerplexitySearchProvider) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"id": "x", "results": []})
    )
    assert await provider.search("q", SearchOptions(max_results=5)) == []
    assert json.loads(route.calls.last.request.content) == {"query": "q", "max_results": 5}


@respx.mock
async def test_unparseable_date_is_none(provider: PerplexitySearchProvider) -> None:
    body = {
        "id": "x",
        "results": [
            {"title": "t", "url": "https://a.example", "snippet": "s", "date": "last Tuesday"}
        ],
    }
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    [result] = await provider.search("q", SearchOptions())
    assert result.published_at is None


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(401, json={"error": "bad key"}), ProviderAuthError),
        (httpx.Response(403), ProviderAuthError),
        (httpx.Response(400, json={"error": "bad"}), ProviderRequestError),
        (httpx.Response(422), ProviderRequestError),
        (httpx.Response(429, headers={"retry-after": "3"}), ProviderRateLimitedError),
        (httpx.Response(500), ProviderUnavailableError),
        (httpx.Response(503), ProviderUnavailableError),
        (httpx.Response(302, headers={"location": "https://evil.example"}), ProviderResponseError),
        (httpx.Response(200, text="<html>not json</html>"), ProviderResponseError),
        (httpx.Response(200, json={"results": "nope"}), ProviderResponseError),
        (httpx.Response(200, json=[1, 2, 3]), ProviderResponseError),
        (
            httpx.Response(200, json={"id": "x", "results": [{"title": "no url"}]}),
            ProviderResponseError,
        ),
    ],
)
@respx.mock
async def test_http_error_mapping(
    provider: PerplexitySearchProvider, response: httpx.Response, error_type: type[ProviderError]
) -> None:
    respx.post(ENDPOINT).mock(return_value=response)
    with pytest.raises(error_type) as info:
        await provider.search("q", SearchOptions())
    assert info.value.provider == "perplexity"
    assert FAKE_PPLX_KEY not in str(info.value)
    if isinstance(info.value, ProviderRateLimitedError):
        assert info.value.retry_after_s == 3.0
        assert info.value.retryable


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.ConnectError("down")]
)
@respx.mock
async def test_transport_failures_are_retryable(
    provider: PerplexitySearchProvider, exc: Exception
) -> None:
    respx.post(ENDPOINT).mock(side_effect=exc)
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.search("q", SearchOptions())
    assert info.value.retryable
    assert FAKE_PPLX_KEY not in str(info.value)
    assert info.value.__cause__ is None  # original exception (may hold request) not chained
