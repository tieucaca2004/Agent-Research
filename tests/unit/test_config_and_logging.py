import json

import pytest
import structlog

from research_agent.config import Settings
from research_agent.logging import REDACTED, configure_logging, get_logger, redact_text

SECRET = "pplx-SUPERSECRET-123"


def make_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ("PERPLEXITY_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CSE_ID", "SEARCH_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings(monkeypatch)
    assert s.search_providers == ["perplexity", "google"]
    assert s.search_strategy == "fallback"
    assert s.perplexity_api_key is None
    assert s.google_api_key is None
    assert s.google_cse_id is None


def test_provider_list_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings(monkeypatch, SEARCH_PROVIDERS=" Google , perplexity ,")
    assert s.search_providers == ["google", "perplexity"]


def test_blank_secret_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings(monkeypatch, PERPLEXITY_API_KEY="   ", GOOGLE_CSE_ID="")
    assert s.perplexity_api_key is None
    assert s.google_cse_id is None


def test_secret_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    s = make_settings(monkeypatch, PERPLEXITY_API_KEY=SECRET)
    assert s.perplexity_api_key is not None
    assert s.perplexity_api_key.get_secret_value() == SECRET
    assert SECRET not in repr(s)
    assert SECRET not in str(s.model_dump())


@pytest.mark.parametrize(
    "text",
    [
        f"GET https://customsearch.googleapis.com/customsearch/v1?key={SECRET}&q=x",
        f"GET https://x.test/?cx=1&api_key={SECRET}",
        f"Authorization: Bearer {SECRET}",
    ],
)
def test_redact_text(text: str) -> None:
    redacted = redact_text(text)
    assert SECRET not in redacted
    assert REDACTED in redacted


def test_logger_redacts_sensitive_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    try:
        get_logger("t").info(
            "evt",
            api_key=SECRET,
            headers={"Authorization": f"Bearer {SECRET}"},
            url=f"https://x.test/?key={SECRET}",
            query="món Nhật Nha Trang",
        )
        line = capsys.readouterr().out.strip().splitlines()[-1]
    finally:
        structlog.reset_defaults()
    assert SECRET not in line
    record = json.loads(line)
    assert record["api_key"] == REDACTED
    assert record["query"] == "món Nhật Nha Trang"
