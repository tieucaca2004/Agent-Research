"""Sprint 06: source identity (P1, P10, P13) — S01 normalizer as-is, crawler-observed URLs."""

from __future__ import annotations

import pytest

from research_agent.core.urls import normalize_url
from research_agent.dedup import DedupLevel, group_documents, source_identity
from research_agent.dedup.identity import host_of, is_sha256_hex, l1_key, l2_key, l3_key
from tests.dedup_support import make_doc

BASE = "https://example.com/page"


@pytest.mark.parametrize(
    ("variant", "same"),
    [
        ("https://example.com/page/", False),  # trailing slash kept by S01
        ("https://example.com/page#section", True),  # fragment removed
        ("https://example.com/page?utm_source=x", True),  # S01 tracking list
        ("https://example.com/page?UTM_SOURCE=x", True),
        ("https://example.com/page?fbclid=1&gclid=2", True),
        ("https://example.com/page?ref=abc", False),  # not in the S01 list: S06 adds no policy
        ("https://EXAMPLE.com:443/page", True),  # host case, default port
        ("https://www.example.com/page", False),  # www. distinguishing
        ("http://example.com/page", False),  # scheme distinguishing
        ("https://example.com/p%61ge", True),  # percent-encoded unreserved char
        ("https://user:pw@example.com/page", True),  # userinfo dropped
    ],
)
def test_identity_follows_the_s01_normalizer(variant: str, same: bool) -> None:
    identity = source_identity(make_doc(url=variant))
    assert identity == normalize_url(variant)  # S06 applies S01 exactly, nothing more
    assert (identity == normalize_url(BASE)) is same


def test_query_order_is_canonical() -> None:
    a = source_identity(make_doc(url="https://example.com/page?b=2&a=1"))
    b = source_identity(make_doc(url="https://example.com/page?a=1&b=2"))
    assert a == b == "https://example.com/page?a=1&b=2"


def test_final_url_preferred_and_requested_url_fallback() -> None:
    redirected = make_doc(url="http://short.example/x", final_url="https://example.com/page")
    assert source_identity(redirected) == "https://example.com/page"
    no_final = make_doc(url="https://example.com/page?utm_medium=y", final_url=None)
    assert source_identity(no_final) == "https://example.com/page"


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",  # InvalidURLError
        "https://example.com:99999/",  # InvalidURLError (port)
        "http://[::1",  # ValueError from urlsplit (escapes normalize_url)
        "https://example.com/" + "a" * 3000,  # too long
    ],
)
def test_unusable_urls_have_no_identity(bad: str) -> None:
    assert source_identity(make_doc(url=bad, final_url=None)) is None


def test_claimed_canonical_and_json_ld_never_feed_identity() -> None:
    victim = make_doc("victim text", url="https://victim.example/article")
    spoof = make_doc(
        "spoof text",
        url="https://attacker.example/copy",
        canonical_url="https://victim.example/article",
        json_ld=[
            '{"@id": "https://victim.example/article", "url": "https://victim.example/article"}'
        ],
    )
    assert source_identity(spoof) == "https://attacker.example/copy"
    result = group_documents([victim, spoof])
    assert result.groups[DedupLevel.L1] == []  # claims never create an L1 group


def test_host_and_key_helpers() -> None:
    assert host_of("https://sub.example.com/x") == "sub.example.com"
    assert host_of(None) is None
    assert is_sha256_hex("a" * 64) and not is_sha256_hex("A" * 64) and not is_sha256_hex("a" * 63)
    assert l1_key("https://x/") == "L1:url:https://x/"
    assert l2_key("f" * 64) == "L2:sha256:" + "f" * 64
    assert l3_key("0" * 64) == "L3:sha256:" + "0" * 64
