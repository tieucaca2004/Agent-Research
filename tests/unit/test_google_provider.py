from datetime import UTC, datetime

import httpx
import pytest
import respx

from research_agent.core.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from research_agent.core.models import SearchOptions
from research_agent.providers.search.google import GoogleSearchProvider
from tests.conftest import FAKE_CSE_ID, FAKE_GOOGLE_KEY, build_settings

ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"

OK_BODY = {
    "kind": "customsearch#search",
    "searchInformation": {"totalResults": "2"},
    "items": [
        {
            "kind": "customsearch#result",
            "title": "Nhà hàng Nhật Nha Trang",
            "link": "https://www.Example.vn/menu/?gclid=abc#top",
            "displayLink": "www.example.vn",
            "snippet": "Sashimi 250.000đ",
            "pagemap": {"metatags": [{"article:published_time": "2024-11-02T08:00:00Z"}]},
        },
        {"title": "No snippet", "link": "https://b.example/"},
        {"title": "Bad scheme", "link": "ftp://c.example/"},
    ],
}


@pytest.fixture
def provider(http_client: httpx.AsyncClient) -> GoogleSearchProvider:
    return GoogleSearchProvider(FAKE_GOOGLE_KEY, FAKE_CSE_ID, http_client)


@pytest.mark.parametrize(
    ("values", "missing"),
    [
        ({}, ("GOOGLE_API_KEY", "GOOGLE_CSE_ID")),
        ({"google_api_key": FAKE_GOOGLE_KEY}, ("GOOGLE_CSE_ID",)),
        ({"google_cse_id": FAKE_CSE_ID}, ("GOOGLE_API_KEY",)),
    ],
)
def test_from_settings_reports_missing(
    http_client: httpx.AsyncClient, values: dict[str, str], missing: tuple[str, ...]
) -> None:
    with pytest.raises(ProviderConfigurationError) as info:
        GoogleSearchProvider.from_settings(build_settings(**values), http_client)
    assert info.value.missing == missing


def test_repr_hides_key(provider: GoogleSearchProvider) -> None:
    assert FAKE_GOOGLE_KEY not in repr(provider)


@respx.mock
async def test_request_contract_and_mapping(provider: GoogleSearchProvider) -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=OK_BODY))

    results = await provider.search(
        "món Nhật Nha Trang", SearchOptions(max_results=25, language="vi", country="VN")
    )

    request = route.calls.last.request
    assert request.method == "GET"
    assert dict(request.url.params) == {
        "key": FAKE_GOOGLE_KEY,
        "cx": FAKE_CSE_ID,
        "q": "món Nhật Nha Trang",
        "num": "10",  # clamped to documented maximum
        "gl": "vn",
        "hl": "vi",
    }
    assert "lr" not in request.url.params

    assert [r.url for r in results] == ["https://www.example.vn/menu/", "https://b.example/"]
    first = results[0]
    assert first.source == "google"
    assert first.original_url == "https://www.Example.vn/menu/?gclid=abc#top"
    assert first.published_at == datetime(2024, 11, 2, 8, 0, tzinfo=UTC)
    assert first.metadata == {"display_link": "www.example.vn"}
    assert results[1].snippet is None
    assert results[1].published_at is None


@respx.mock
async def test_no_items_means_no_results(provider: GoogleSearchProvider) -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"searchInformation": {"totalResults": "0"}})
    )
    assert await provider.search("q", SearchOptions()) == []


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(403, json={"error": {"message": "API key not valid"}}), ProviderAuthError),
        (httpx.Response(429), ProviderRateLimitedError),
        (httpx.Response(500), ProviderUnavailableError),
        (httpx.Response(200, text="oops"), ProviderResponseError),
        (httpx.Response(200, json=["x"]), ProviderResponseError),
        (httpx.Response(200, json={"items": [{"title": "missing link"}]}), ProviderResponseError),
        (httpx.Response(200, json={"items": "nope"}), ProviderResponseError),
    ],
)
@respx.mock
async def test_error_mapping_never_leaks_key(
    provider: GoogleSearchProvider, response: httpx.Response, error_type: type[ProviderError]
) -> None:
    respx.get(ENDPOINT).mock(return_value=response)
    with pytest.raises(error_type) as info:
        await provider.search("q", SearchOptions())
    assert FAKE_GOOGLE_KEY not in str(info.value)
    assert FAKE_GOOGLE_KEY not in repr(info.value.to_dict())


@respx.mock
async def test_timeout_does_not_leak_key(provider: GoogleSearchProvider) -> None:
    respx.get(ENDPOINT).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.search("q", SearchOptions())
    assert FAKE_GOOGLE_KEY not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None or FAKE_GOOGLE_KEY not in str(info.value.__context__)
