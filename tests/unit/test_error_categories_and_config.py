"""Sprint 01 delta: normalized error categories (codes unchanged) and secret-safe config logging."""

import json

import pytest
import structlog
from pydantic import SecretStr

from research_agent.core.errors import (
    InvalidURLError,
    NoSearchProviderConfiguredError,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderError,
    ProviderNetworkError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from research_agent.logging import configure_logging, get_logger
from tests.conftest import FAKE_CSE_ID, FAKE_GOOGLE_KEY, FAKE_PPLX_KEY, build_settings


@pytest.mark.parametrize(
    ("error", "code", "category", "retryable"),
    [
        (
            ProviderConfigurationError("p", ["X"]),
            "REQUIRES_CONFIGURATION",
            "CONFIGURATION_ERROR",
            False,
        ),
        (
            NoSearchProviderConfiguredError([]),
            "REQUIRES_CONFIGURATION",
            "CONFIGURATION_ERROR",
            False,
        ),
        (ProviderAuthError("p", "401"), "PROVIDER_AUTH_FAILED", "AUTHENTICATION_ERROR", False),
        (ProviderRateLimitedError("p", "429"), "PROVIDER_RATE_LIMITED", "RATE_LIMITED", True),
        (ProviderTimeoutError("p", "t"), "PROVIDER_UNAVAILABLE", "TIMEOUT", True),
        (ProviderNetworkError("p", "n"), "PROVIDER_UNAVAILABLE", "NETWORK_ERROR", True),
        (ProviderUnavailableError("p", "5xx"), "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR", True),
        (ProviderRequestError("p", "400"), "PROVIDER_REQUEST_REJECTED", "PROVIDER_ERROR", False),
        (
            ProviderResponseError("p", "bad"),
            "PROVIDER_MALFORMED_RESPONSE",
            "INVALID_RESPONSE",
            False,
        ),
        (ProviderError("p", "x"), "PROVIDER_ERROR", "PROVIDER_ERROR", False),
        (InvalidURLError("x"), "INVALID_URL", "INVALID_INPUT", False),
    ],
)
def test_category_mapping_keeps_codes_backward_compatible(
    error: ProviderError, code: str, category: str, retryable: bool
) -> None:
    assert error.code == code
    assert error.category == category
    assert error.retryable is retryable
    as_dict = error.to_dict()
    assert as_dict["code"] == code
    assert as_dict["category"] == category


def test_timeout_and_network_are_still_unavailable_errors() -> None:
    assert issubclass(ProviderTimeoutError, ProviderUnavailableError)
    assert issubclass(ProviderNetworkError, ProviderUnavailableError)


def test_safe_summary_reports_status_only() -> None:
    settings = build_settings(
        perplexity_api_key=SecretStr(FAKE_PPLX_KEY), google_cse_id=FAKE_CSE_ID
    )
    summary = settings.safe_summary()
    assert summary["credentials"] == [
        {"name": "PERPLEXITY_API_KEY", "status": "CONFIGURED"},
        {"name": "GOOGLE_API_KEY", "status": "MISSING"},
        {"name": "GOOGLE_CSE_ID", "status": "CONFIGURED"},
    ]
    assert FAKE_PPLX_KEY not in repr(summary)
    assert FAKE_CSE_ID not in repr(summary)


def test_logged_config_shows_configured_and_never_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = build_settings(
        perplexity_api_key=SecretStr(FAKE_PPLX_KEY),
        google_api_key=SecretStr(FAKE_GOOGLE_KEY),
        google_cse_id=FAKE_CSE_ID,
    )
    configure_logging("INFO", "json")
    try:
        get_logger("t").info("search.configuration", **settings.safe_summary())
        line = capsys.readouterr().out.strip().splitlines()[-1]
    finally:
        structlog.reset_defaults()
    for secret in (FAKE_PPLX_KEY, FAKE_GOOGLE_KEY, FAKE_CSE_ID):
        assert secret not in line
    record = json.loads(line)
    assert {c["name"]: c["status"] for c in record["credentials"]} == {
        "PERPLEXITY_API_KEY": "CONFIGURED",
        "GOOGLE_API_KEY": "CONFIGURED",
        "GOOGLE_CSE_ID": "CONFIGURED",
    }
