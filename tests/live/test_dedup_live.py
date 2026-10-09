"""Real-web dedup: S04 crawler → S05 extractor → S06 grouping (opt-in, design §17).

Run: ``RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s tests/live/test_dedup_live.py``
(behind TLS inspection also set ``CRAWL_CA_BUNDLE``; verification stays on). Without
RUN_LIVE_NETWORK the test is skipped as REQUIRES_NETWORK — never a PASS. Only egress-allowed hosts;
the policy is never bypassed. Nothing is sent to an LLM. Prints groups (hash prefixes, no text).

A PASS requires every URL to be fetched (``OK`` with a body) and extracted (``SUCCESS``) before any
dedup assertion runs: a failed fetch (TLS, HTTP, missing body) fails the test instead of leaving
the dedup assertions vacuously true. ``check_live_dedup`` holds every assertion and is exercised
offline by ``tests/unit/test_dedup_live_checks.py``. The challenge pages (positions 4, 5) are
recorded, not asserted (§17, §23)."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

import pytest

from research_agent.crawler import Crawler, CrawlerSettings, FetchResult, FetchStatus, FetchTarget
from research_agent.dedup import DedupLevel, DocumentSet, group_documents
from research_agent.extraction import ExtractedDocument, ExtractionStatus, Extractor

URLS = [
    "https://pypi.org/project/httpx/",
    "https://pypi.org/project/httpx/?utm_source=probe",
    "https://pypi.org/project/httpx/#history",
    "https://pypi.org/project/httpx/0.28.1/",
    "https://pypi.org/project/httpx",
    "https://pypi.org/project/HTTPX/",
]
VARIANTS = [0, 1, 2]
"""Same page via tracking parameter and fragment: one group of 3 at L1, L2 and L3 (§17)."""
VERSION_PAGE = 3
"""Near duplicate of the project page (L4, out of scope): in no group with the variants."""
VARIANT_IDENTITY = "https://pypi.org/project/httpx/"


def check_live_dedup(
    fetches: Sequence[FetchResult],
    documents: Sequence[ExtractedDocument],
    result: DocumentSet,
) -> None:
    """Every live-test assertion; raises ``AssertionError`` unless the run proves §17."""
    assert len(fetches) == len(documents) == len(URLS), "one fetch and one document per URL"
    # 1. fetch evidence first: no dedup assertion may run on unfetched documents
    for position, fetched in enumerate(fetches):
        assert fetched.status is FetchStatus.OK, (position, fetched.status.value)
        assert fetched.content is not None, (position, "OK without a body")
        assert fetched.content_sha256 is not None, (position, "OK without a body hash")
    for position, (fetched, document) in enumerate(zip(fetches, documents, strict=True)):
        assert document.provenance.crawl_id == fetched.crawl_id, position
        assert document.provenance.content_sha256 == fetched.content_sha256, position
        assert document.fetch_status is FetchStatus.OK, position
        assert document.status is ExtractionStatus.SUCCESS, (position, document.status.value)
    # 2. S06 output covers every document, nothing excluded or in error
    assert result.documents == list(documents)
    assert [ref.position for ref in result.refs] == list(range(len(URLS)))
    assert result.exclusions == [] and result.errors == []
    # 3. design §17 expectations
    for position in VARIANTS:
        assert result.refs[position].source_identity == VARIANT_IDENTITY, position
    for level in DedupLevel:
        level_members = [group.members for group in result.groups[level]]
        assert VARIANTS in level_members, (level.value, level_members)
        assert not any(VERSION_PAGE in members for members in level_members), level.value


def record(result: DocumentSet) -> None:
    for ref in result.refs:
        print(
            json.dumps(
                {
                    "position": ref.position,
                    "requested_url": ref.requested_url,
                    "final_url": ref.final_url,
                    "identity": ref.source_identity,
                    "fetch_status": ref.fetch_status.value,
                    "status": ref.status.value,
                    "content_sha256": (ref.content_sha256 or "")[:12],
                    "text_sha256": (ref.text_sha256 or "")[:12],
                    "warnings": [w.value for w in ref.warnings],
                }
            )
        )
    for level in DedupLevel:
        for group in result.groups[level]:
            print(
                json.dumps(
                    {
                        "level": level.value,
                        "key": group.key[:24],
                        "members": group.members,
                        "warnings": [w.value for w in group.warnings],
                    }
                )
            )
    print(json.dumps({"exclusions": [e.model_dump(mode="json") for e in result.exclusions]}))


@pytest.mark.live_network
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_NETWORK") != "1",
    reason="REQUIRES_NETWORK: set RUN_LIVE_NETWORK=1 to fetch real websites",
)
async def test_real_web_dedup() -> None:
    extractor = Extractor()
    fetches: list[FetchResult] = []
    documents: list[ExtractedDocument] = []
    async with Crawler(CrawlerSettings()) as crawler:
        for url in URLS:
            fetched = await crawler.fetch(FetchTarget(url=url))
            fetches.append(fetched)
            documents.append(await extractor.aextract(fetched))

    result = group_documents(documents)
    record(result)
    check_live_dedup(fetches, documents, result)
