"""Real-web crawler checks (opt-in).

Run: ``RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s tests/live/test_crawler_live.py``
(behind TLS inspection also set ``CRAWL_CA_BUNDLE`` to the inspection CA bundle — verification
stays on). Without RUN_LIVE_NETWORK the tests are skipped as REQUIRES_NETWORK — never a PASS.
Prints one record per URL (no bodies).
"""

from __future__ import annotations

import json
import os

import pytest

from research_agent.crawler import Crawler, CrawlerSettings, FetchResult, FetchStatus, FetchTarget

pytestmark = [
    pytest.mark.live_network,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_NETWORK") != "1",
        reason="REQUIRES_NETWORK: set RUN_LIVE_NETWORK=1 to fetch real websites",
    ),
]

S = FetchStatus


def record(result: FetchResult) -> dict[str, object]:
    return {
        "url": result.requested_url,
        "status": result.status.value,
        "http_status": result.http_status,
        "final_url": result.final_url,
        "content_type": result.content_type,
        "bytes": result.content_length,
        "duration_ms": result.duration_ms,
        "robots": result.robots.outcome if result.robots else None,
        "redirects": [(h.http_status, h.location) for h in result.redirect_chain],
        "resolved_ip": result.resolved_ip,
    }


async def test_real_web_fetches() -> None:
    # Hosts chosen to be reachable from the Sprint 04 sandbox egress policy (other hosts such as
    # example.com were denied by that policy with a 403 before reaching the site).
    urls = {
        "https_html": "https://pypi.org/project/httpx/",
        "plain_text": "https://raw.githubusercontent.com/python/cpython/main/LICENSE",
        "redirect_http_to_https": "http://pypi.org/",
        "robots_wildcard_disallowed": "https://pypi.org/pypi/httpx/json",
        "robots_prefix_wildcard_disallowed": "https://pypi.org/search/?q=httpx",
        "ssrf_metadata": "http://169.254.169.254/latest/meta-data/",
        "ssrf_localhost": "http://localhost/",
        "policy_port": "https://pypi.org:8443/",
    }
    async with Crawler(CrawlerSettings()) as crawler:
        results = {name: await crawler.fetch(FetchTarget(url=url)) for name, url in urls.items()}
    for name, result in results.items():
        print(json.dumps({"case": name, **record(result)}, default=str))

    html = results["https_html"]
    assert html.status is S.OK and html.content_type == "text/html"
    assert html.resolved_ip and html.content_sha256 and html.title
    assert html.robots is not None and html.robots.outcome == "ALLOWED"

    text = results["plain_text"]
    assert text.status is S.OK and text.content_type == "text/plain"
    assert text.content is not None and "PYTHON SOFTWARE FOUNDATION LICENSE" in text.content

    redirected = results["redirect_http_to_https"]
    assert redirected.status is S.OK
    assert redirected.redirect_chain and redirected.redirect_chain[0].http_status in (301, 302, 308)
    assert redirected.final_url == "https://pypi.org/"

    for case in ("robots_wildcard_disallowed", "robots_prefix_wildcard_disallowed"):
        blocked = results[case]
        assert blocked.status is S.ROBOTS_BLOCKED and blocked.robots is not None
        assert blocked.robots.outcome == "DISALLOWED"

    assert results["ssrf_metadata"].status is S.SSRF_BLOCKED
    assert results["ssrf_localhost"].status is S.SSRF_BLOCKED
    assert results["policy_port"].status is S.POLICY_BLOCKED
