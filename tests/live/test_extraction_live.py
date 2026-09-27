"""Real-web extraction: S04 crawler → FetchResult → S05 extractor (opt-in).

Run: ``RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s tests/live/test_extraction_live.py``
(behind TLS inspection also set ``CRAWL_CA_BUNDLE``; verification stays on). Without
RUN_LIVE_NETWORK the test is skipped as REQUIRES_NETWORK — never a PASS. Hosts are ones the
Sprint 05 sandbox egress policy allows; the policy is never bypassed. Nothing is sent to an LLM.
Prints one record per URL (no page text).
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from research_agent.crawler import Crawler, CrawlerSettings, FetchStatus, FetchTarget
from research_agent.extraction import ExtractedDocument, ExtractionStatus, Extractor

pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_NETWORK") != "1",
        reason="REQUIRES_NETWORK: set RUN_LIVE_NETWORK=1 to fetch real websites",
    ),
]

URLS = {
    "html_page_metadata_links": "https://pypi.org/project/httpx/",
    "docs_page": "https://pypi.org/help/",
    "plain_text": "https://raw.githubusercontent.com/python/cpython/main/LICENSE",
    "redirect_http_to_https": "http://pypi.org/",
}


def record(name: str, document: ExtractedDocument) -> dict[str, object]:
    return {
        "case": name,
        "url": document.requested_url,
        "final_url": document.final_url,
        "status": document.status.value,
        "content_source": document.content_source,
        "title": document.title,
        "title_source": document.title_source,
        "claimed_language": document.claimed_language,
        "charset": document.charset,
        "chars": document.char_count,
        "words": document.word_count,
        "links": len(document.links),
        "og_title": document.claimed_metadata.og_title,
        "description": (document.claimed_metadata.description or "")[:60],
        "warnings": [w.value for w in document.warnings],
        "robots": document.provenance.robots.outcome if document.provenance.robots else None,
        "redirects": [h.http_status for h in document.provenance.redirect_chain],
        "duration_ms": document.stats.duration_ms,
    }


async def test_real_web_extraction() -> None:
    extractor = Extractor()
    documents: dict[str, ExtractedDocument] = {}
    async with Crawler(CrawlerSettings()) as crawler:
        for name, url in URLS.items():
            fetched = await crawler.fetch(FetchTarget(url=url))
            assert fetched.status is FetchStatus.OK, (name, fetched.status)
            document = await extractor.aextract(fetched)
            documents[name] = document
            print(json.dumps(record(name, document), ensure_ascii=False))
            # provenance and hashes on every document
            assert document.provenance.crawl_id == fetched.crawl_id
            assert document.provenance.content_sha256 == fetched.content_sha256
            assert document.final_url == fetched.final_url
            assert document.text_sha256 == hashlib.sha256(document.text.encode()).hexdigest()
            assert document.trust == "UNTRUSTED"

    html = documents["html_page_metadata_links"]
    assert html.status is ExtractionStatus.SUCCESS
    assert html.content_source == "MAIN"
    assert html.title is not None and "httpx" in html.title
    assert html.claimed_language == "en" and html.language_source == "HTML_LANG"
    assert html.claimed_metadata.og_title == "httpx"
    assert html.claimed_metadata.description
    assert "Skip to main content" not in html.text
    assert "HTTPX" in html.text or "httpx" in html.text
    assert 0 < len(html.links) <= 500
    assert all(link.url.startswith(("http://", "https://")) for link in html.links)
    assert any(link.in_main_content for link in html.links)

    docs = documents["docs_page"]
    assert docs.status is ExtractionStatus.SUCCESS
    assert "#" in docs.text and docs.char_count > 1_000

    text = documents["plain_text"]
    assert text.status is ExtractionStatus.SUCCESS and text.content_source == "PLAIN_TEXT"
    assert "PYTHON SOFTWARE FOUNDATION LICENSE" in text.text
    assert text.title is None and text.links == []

    redirected = documents["redirect_http_to_https"]
    assert redirected.final_url == "https://pypi.org/"
    assert redirected.provenance.redirect_chain
    assert redirected.provenance.redirect_chain[0].http_status in (301, 302, 308)
    assert redirected.status is ExtractionStatus.SUCCESS
