"""Offline proof that the live dedup test cannot pass without real fetches (review finding F-1).

``check_live_dedup`` (tests/live/test_dedup_live.py) is fed S04-shaped FetchResults through the real
S05 extractor and S06 grouping: failed fetches (TLS, HTTP, missing body) must fail it, a run that
matches design §17 must pass it, and wrong grouping must still fail it."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from research_agent.crawler import FetchResult, FetchStatus
from research_agent.dedup import group_documents
from research_agent.extraction import Extractor
from tests.extraction_support import fetch_result, settings
from tests.live.test_dedup_live import URLS, check_live_dedup

PROJECT = (
    "<html><body><main><h1>httpx 0.28.1</h1><p>The next generation HTTP client.</p>"
    "<p>HTTPX is a fully featured HTTP client library for Python 3.</p></main></body></html>"
)
VERSION = PROJECT.replace("<h1>httpx 0.28.1</h1>", "<h1>httpx 0.28.1 (release page)</h1>")
CHALLENGE = "<html><body><h1>Client Challenge</h1><p>JavaScript is disabled.</p></body></html>"
PAGES = [PROJECT, PROJECT, PROJECT, VERSION, CHALLENGE, CHALLENGE]


def ok(position: int, content: str, **overrides: Any) -> FetchResult:
    values: dict[str, Any] = {
        "crawl_id": f"crawl-{position}",
        "requested_url": URLS[position],
        "redirect_chain": [],
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    values.update(overrides)
    return fetch_result(content, final_url=URLS[position], **values)


def failed(position: int, status: FetchStatus, **overrides: Any) -> FetchResult:
    values: dict[str, Any] = {
        "crawl_id": f"crawl-{position}",
        "requested_url": URLS[position],
        "redirect_chain": [],
    }
    values.update(overrides)
    return fetch_result(None, status=status, final_url=URLS[position], **values)


def run_checks(fetches: list[FetchResult]) -> None:
    extractor = Extractor(settings())
    documents = [extractor.extract(fetched) for fetched in fetches]
    check_live_dedup(fetches, documents, group_documents(documents))


def successful() -> list[FetchResult]:
    return [ok(position, page) for position, page in enumerate(PAGES)]


def test_successful_run_matching_the_design_passes() -> None:
    run_checks(successful())


@pytest.mark.parametrize(
    "status",
    [FetchStatus.TLS_ERROR, FetchStatus.HTTP_ERROR, FetchStatus.CONNECTION_ERROR],
)
def test_every_fetch_failed_cannot_pass(status: FetchStatus) -> None:
    """The F-1 scenario: final_url is still set on failure, so it proves nothing."""
    fetches = [failed(position, status) for position in range(len(URLS))]
    assert all(f.final_url is not None for f in fetches)
    with pytest.raises(AssertionError, match=status.value):
        run_checks(fetches)


@pytest.mark.parametrize("position", range(len(URLS)))
def test_one_tls_failure_at_any_position_cannot_pass(position: int) -> None:
    fetches = successful()
    fetches[position] = failed(position, FetchStatus.TLS_ERROR)
    with pytest.raises(AssertionError, match="TLS_ERROR"):
        run_checks(fetches)


def test_http_error_with_a_body_cannot_pass() -> None:
    fetches = successful()
    fetches[0] = ok(0, PAGES[0], status=FetchStatus.HTTP_ERROR, http_status=404)
    with pytest.raises(AssertionError, match="HTTP_ERROR"):
        run_checks(fetches)


def test_ok_without_a_body_cannot_pass() -> None:
    fetches = successful()
    fetches[1] = failed(1, FetchStatus.OK)
    with pytest.raises(AssertionError, match="without a body"):
        run_checks(fetches)


def test_ok_without_a_body_hash_cannot_pass() -> None:
    fetches = successful()
    fetches[2] = ok(2, PAGES[2], content_sha256=None)
    with pytest.raises(AssertionError, match="without a body hash"):
        run_checks(fetches)


def test_variant_that_does_not_group_fails() -> None:
    pages = list(PAGES)
    pages[1] = PROJECT.replace("next generation", "previous generation")
    with pytest.raises(AssertionError):
        run_checks([ok(position, page) for position, page in enumerate(pages)])


def test_version_page_merged_with_the_variants_fails() -> None:
    pages = list(PAGES)
    pages[3] = PROJECT
    with pytest.raises(AssertionError):
        run_checks([ok(position, page) for position, page in enumerate(pages)])


def test_missing_document_fails() -> None:
    fetches = successful()
    extractor = Extractor(settings())
    documents = [extractor.extract(fetched) for fetched in fetches]
    with pytest.raises(AssertionError):
        check_live_dedup(fetches, documents[:-1], group_documents(documents[:-1]))
