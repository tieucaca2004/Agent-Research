"""Real-web dedup: S04 crawler → S05 extractor → S06 grouping (opt-in, design §17).

Run: ``RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s tests/live/test_dedup_live.py``
(behind TLS inspection also set ``CRAWL_CA_BUNDLE``; verification stays on). Without
RUN_LIVE_NETWORK the test is skipped as REQUIRES_NETWORK — never a PASS. Only egress-allowed hosts;
the policy is never bypassed. Nothing is sent to an LLM. Prints groups (hash prefixes, no text).

Assertions are limited to what S06 decides (URL identity, verification, no false merge); what the
site serves (identical bytes per request, challenge pages) is recorded, not asserted."""

from __future__ import annotations

import json
import os

import pytest

from research_agent.crawler import Crawler, CrawlerSettings, FetchTarget
from research_agent.dedup import DedupLevel, group_documents
from research_agent.extraction import ExtractedDocument, Extractor

pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_NETWORK") != "1",
        reason="REQUIRES_NETWORK: set RUN_LIVE_NETWORK=1 to fetch real websites",
    ),
]

URLS = [
    "https://pypi.org/project/httpx/",
    "https://pypi.org/project/httpx/?utm_source=probe",
    "https://pypi.org/project/httpx/#history",
    "https://pypi.org/project/httpx/0.28.1/",
    "https://pypi.org/project/httpx",
    "https://pypi.org/project/HTTPX/",
]


async def test_real_web_dedup() -> None:
    extractor = Extractor()
    documents: list[ExtractedDocument] = []
    async with Crawler(CrawlerSettings()) as crawler:
        for url in URLS:
            documents.append(await extractor.aextract(await crawler.fetch(FetchTarget(url=url))))

    result = group_documents(documents)
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

    # nothing dropped, every document referenced
    assert len(result.documents) == len(result.refs) == len(URLS)
    # tracking parameter and fragment collapse onto the same S01 identity (when fetched)
    fetched = [r.position for r in result.refs[:3] if r.final_url is not None]
    if len(fetched) == 3:
        assert [0, 1, 2] in [g.members for g in result.groups[DedupLevel.L1]]
    # the version page is a different source and different text: never merged with the project page
    for level in DedupLevel:
        assert not any({0, 3} <= set(g.members) for g in result.groups[level])
    # stored text hashes were verified, not trusted: no hash errors on real S05 output
    assert not [e for e in result.errors if e.level is DedupLevel.L3]
