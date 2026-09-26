"""Mocked integration: Settings → registry → real adapters → SearchService, with the provider
HTTP endpoints mocked by respx (no network, no credentials needed).

These tests prove wiring and failure handling. They do NOT prove the live APIs behave as
documented — that is the job of tests/live (REQUIRES_CONFIGURATION without keys).
"""

import json

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr

from research_agent.config import Settings
from research_agent.core.errors import AllSearchProvidersFailedError
from research_agent.logging import configure_logging
from research_agent.pipeline.search import build_search_service, default_search_options
from tests.conftest import FAKE_CSE_ID, FAKE_GOOGLE_KEY, FAKE_PPLX_KEY, build_settings

PPLX = "https://api.perplexity.ai/search"
GOOGLE = "https://customsearch.googleapis.com/customsearch/v1"


def both_configured(**overrides: object) -> Settings:
    return build_settings(
        perplexity_api_key=SecretStr(FAKE_PPLX_KEY),
        google_api_key=SecretStr(FAKE_GOOGLE_KEY),
        google_cse_id=FAKE_CSE_ID,
        **overrides,
    )


def pplx_body(*urls: str) -> dict[str, object]:
    return {
        "id": "r",
        "results": [{"title": u, "url": u, "snippet": "s"} for u in urls],
    }


def google_body(*urls: str) -> dict[str, object]:
    return {"items": [{"title": u, "link": u, "snippet": "s"} for u in urls]}


@respx.mock
async def test_primary_down_falls_back_to_google(http_client: httpx.AsyncClient) -> None:
    pplx = respx.post(PPLX).mock(return_value=httpx.Response(503))
    google = respx.get(GOOGLE).mock(
        return_value=httpx.Response(200, json=google_body("https://g.example/menu"))
    )
    svc = build_search_service(both_configured(search_max_retries=1), http_client)
    svc._sleep = _no_sleep  # keep test fast; backoff values are unit-tested

    run = await svc.search(["món Nhật Nha Trang"], default_search_options(both_configured()))

    assert pplx.call_count == 2  # 1 try + 1 retry
    assert google.call_count == 1
    assert [(r.source, r.url) for r in run.results] == [("google", "https://g.example/menu")]
    assert [(a.provider, a.status) for a in run.attempts] == [
        ("perplexity", "FAILED"),
        ("google", "OK"),
    ]


@respx.mock
async def test_primary_ok_google_never_called(http_client: httpx.AsyncClient) -> None:
    respx.post(PPLX).mock(return_value=httpx.Response(200, json=pplx_body("https://p.example/")))
    google = respx.get(GOOGLE).mock(return_value=httpx.Response(500))
    svc = build_search_service(both_configured(), http_client)
    run = await svc.search(["q"], default_search_options(both_configured()))
    assert google.call_count == 0
    assert run.results[0].source == "perplexity"


@respx.mock
async def test_fanout_dedups_across_providers(http_client: httpx.AsyncClient) -> None:
    respx.post(PPLX).mock(
        return_value=httpx.Response(
            200, json=pplx_body("https://a.example/x?utm_source=p", "https://b.example/")
        )
    )
    respx.get(GOOGLE).mock(
        return_value=httpx.Response(
            200, json=google_body("https://A.example/x#frag", "https://c.example/")
        )
    )
    settings = both_configured(search_strategy="fanout")
    run = await build_search_service(settings, http_client).search(
        ["q"], default_search_options(settings)
    )
    assert [r.url for r in run.results] == [
        "https://a.example/x",
        "https://b.example/",
        "https://c.example/",
    ]
    assert run.duplicate_count == 1


@respx.mock
async def test_only_google_configured(http_client: httpx.AsyncClient) -> None:
    pplx = respx.post(PPLX)
    respx.get(GOOGLE).mock(return_value=httpx.Response(200, json=google_body("https://g/")))
    settings = build_settings(google_api_key=SecretStr(FAKE_GOOGLE_KEY), google_cse_id=FAKE_CSE_ID)
    svc = build_search_service(settings, http_client)
    assert svc.provider_names == ["google"]
    await svc.search(["q"], default_search_options(settings))
    assert pplx.call_count == 0


@respx.mock
async def test_everything_down_fails_clearly_and_logs_no_secrets(
    http_client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(PPLX).mock(side_effect=httpx.ConnectTimeout("timeout"))
    respx.get(GOOGLE).mock(return_value=httpx.Response(403, json={"error": "bad key"}))
    configure_logging("DEBUG", "json")
    try:
        svc = build_search_service(both_configured(search_max_retries=0), http_client)
        with pytest.raises(AllSearchProvidersFailedError) as info:
            await svc.search(["q"], default_search_options(both_configured()))
        output = capsys.readouterr().out
    finally:
        structlog.reset_defaults()

    assert {e.code for e in info.value.errors} == {"PROVIDER_UNAVAILABLE", "PROVIDER_AUTH_FAILED"}
    assert FAKE_PPLX_KEY not in output
    assert FAKE_GOOGLE_KEY not in output
    events = [json.loads(line)["event"] for line in output.splitlines() if line.startswith("{")]
    assert events.count("search.provider_failed") == 2


async def _no_sleep(_: float) -> None:
    return None


@respx.mock
async def test_response_statuses_with_real_adapters(http_client: httpx.AsyncClient) -> None:
    """Fanout: Perplexity 200 + Google timeout → results kept, Google reported TIMEOUT.
    Fallback default: Perplexity 502 → Google used, Perplexity reported PROVIDER_ERROR."""
    respx.post(PPLX).mock(return_value=httpx.Response(200, json=pplx_body("https://p.example/")))
    respx.get(GOOGLE).mock(side_effect=httpx.ReadTimeout("slow"))
    settings = both_configured(search_strategy="fanout", search_max_retries=0)
    response = (
        await build_search_service(settings, http_client).search(
            ["q"], default_search_options(settings), request_id="it-1"
        )
    ).to_response()
    by_name = {s.provider: s for s in response.provider_statuses}
    assert response.request_id == "it-1"
    assert [r.result.url for r in response.results] == ["https://p.example/"]
    assert (by_name["perplexity"].status, by_name["google"].status) == ("SUCCESS", "FAILED")
    assert by_name["google"].error_categories == ["TIMEOUT"]

    respx.post(PPLX).mock(return_value=httpx.Response(502))
    respx.get(GOOGLE).mock(return_value=httpx.Response(200, json=google_body("https://g.example/")))
    settings = both_configured(search_max_retries=0)
    response = (
        await build_search_service(settings, http_client).search(
            ["q"], default_search_options(settings)
        )
    ).to_response()
    by_name = {s.provider: s for s in response.provider_statuses}
    assert response.strategy == "fallback"
    assert by_name["perplexity"].error_categories == ["PROVIDER_ERROR"]
    assert by_name["google"].status == "SUCCESS"
    assert [r.result.source for r in response.results] == ["google"]
