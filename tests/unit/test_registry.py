import httpx
import pytest
from pydantic import SecretStr

from research_agent.core.errors import NoSearchProviderConfiguredError
from research_agent.providers.search import PROVIDERS, build_search_providers, provider_statuses
from research_agent.providers.search.google import GoogleSearchProvider
from research_agent.providers.search.perplexity import PerplexitySearchProvider
from tests.conftest import FAKE_CSE_ID, FAKE_GOOGLE_KEY, FAKE_PPLX_KEY, build_settings


def test_registry_contains_primary_and_fallback() -> None:
    assert {"perplexity": PerplexitySearchProvider, "google": GoogleSearchProvider} == PROVIDERS


def test_statuses_without_credentials() -> None:
    statuses = provider_statuses(build_settings())
    assert [(s.name, s.status, s.missing) for s in statuses] == [
        ("perplexity", "REQUIRES_CONFIGURATION", ("PERPLEXITY_API_KEY",)),
        ("google", "REQUIRES_CONFIGURATION", ("GOOGLE_API_KEY", "GOOGLE_CSE_ID")),
    ]


def test_statuses_report_unknown_provider() -> None:
    [status] = provider_statuses(build_settings(search_providers=["bing"]))
    assert status.status == "REQUIRES_CONFIGURATION"
    assert status.missing == ("UNKNOWN_PROVIDER",)


def test_statuses_never_contain_secret_values() -> None:
    settings = build_settings(perplexity_api_key=SecretStr(FAKE_PPLX_KEY))
    assert FAKE_PPLX_KEY not in repr(provider_statuses(settings))


def test_build_fails_clearly_without_any_credentials(http_client: httpx.AsyncClient) -> None:
    with pytest.raises(NoSearchProviderConfiguredError) as info:
        build_search_providers(build_settings(), http_client)
    assert info.value.code == "REQUIRES_CONFIGURATION"
    assert "PERPLEXITY_API_KEY" in info.value.message
    assert "GOOGLE_CSE_ID" in info.value.message


def test_build_skips_unconfigured_and_keeps_order(http_client: httpx.AsyncClient) -> None:
    settings = build_settings(google_api_key=SecretStr(FAKE_GOOGLE_KEY), google_cse_id=FAKE_CSE_ID)
    providers = build_search_providers(settings, http_client)
    assert [p.name for p in providers] == ["google"]

    settings = build_settings(
        perplexity_api_key=SecretStr(FAKE_PPLX_KEY),
        google_api_key=SecretStr(FAKE_GOOGLE_KEY),
        google_cse_id=FAKE_CSE_ID,
        search_providers=["google", "perplexity"],
    )
    assert [p.name for p in build_search_providers(settings, http_client)] == [
        "google",
        "perplexity",
    ]
