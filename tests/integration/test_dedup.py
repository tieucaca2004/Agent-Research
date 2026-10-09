"""Sprint 06 integration: real S05 extraction output fed into S06 grouping (no network).

The end-to-end order SearchService → crawler → extraction → dedup is NOT covered here: it does not
exist yet (OD-13, integration sprint). These tests use S04-shaped FetchResults built in-process."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

import pytest

from research_agent.crawler.models import FetchStatus
from research_agent.dedup import (
    DedupLevel,
    DocumentSet,
    ExclusionReason,
    GroupWarning,
    group_documents,
)
from research_agent.extraction import ExtractedDocument, Extractor
from research_agent.extraction.models import ExtractionStatus
from tests.extraction_support import FIXTURES, fetch_result, settings

L1, L2, L3 = DedupLevel.L1, DedupLevel.L2, DedupLevel.L3


def crawl(
    content: str | None,
    url: str,
    *,
    content_type: str = "text/html",
    raw: bytes | None = None,
    number: int = 0,
    **overrides: Any,
) -> ExtractedDocument:
    """Run real S05 extraction on an S04-shaped result whose content_sha256 is a real body hash."""
    body = raw if raw is not None else (content or "").encode("utf-8")
    result = fetch_result(
        content,
        content_type,
        final_url=url,
        requested_url=url,
        crawl_id=f"crawl-{number}",
        redirect_chain=[],
        content_sha256=hashlib.sha256(body).hexdigest() if content is not None else None,
        **overrides,
    )
    return Extractor(settings()).extract(result)


def members(result: DocumentSet, level: DedupLevel) -> list[list[int]]:
    return [g.members for g in result.groups[level]]


ARTICLE = (
    "<p>Phở bò Nha Trang giá 45.000đ một tô, mở cửa từ 6 giờ sáng.</p><p>Second paragraph.</p>"
)
SPACED = ARTICLE.replace("</p><p>", "</p>\n\n   <p>")


def test_markup_variants_extract_to_one_verified_text_group() -> None:
    pages = [
        f"<html><body><article>{ARTICLE}</article></body></html>",
        f"<html><body><nav><a href='/'>Home</a><a href='/m'>Menu</a></nav>"
        f"<article class='x'>{ARTICLE.replace('<p>', '<p  data-k=1>')}</article></body></html>",
        "<html>\n<body>\n  <article>\n" + SPACED + "\n</article></body></html>",
    ]
    docs = [crawl(page, f"https://site{i}.example/a", number=i) for i, page in enumerate(pages)]
    texts = {d.text for d in docs}
    assert len(texts) == 1, texts  # S05 normalizes markup; S06 only compares
    result = group_documents(docs)
    assert members(result, L3) == [[0, 1, 2]]
    assert result.groups[L3][0].warnings == [GroupWarning.CROSS_SOURCE_DUPLICATE]
    assert members(result, L2) == []  # different bytes
    assert result.errors == []


def test_nfd_source_is_normalized_by_s05_not_s06() -> None:
    nfc = f"<html><body><article>{ARTICLE}</article></body></html>"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    docs = [crawl(nfc, "https://a.example/", number=1), crawl(nfd, "https://b.example/", number=2)]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 1]]


def test_same_bytes_decoded_differently_form_an_l2_group_only() -> None:
    """E3: S04 hashes bytes, S05 sees two decodings → L2 with RAW_DUPLICATE_TEXT_DIFFERS."""
    raw = "<html><body><p>caf\xe9 \x93quoted\x94 text for the page body</p></body></html>".encode(
        "latin-1"
    )
    as_latin1 = raw.decode("latin-1")
    as_cp1252 = raw.decode("cp1252")
    docs = [
        crawl(as_latin1, "https://a.example/", raw=raw, charset="iso-8859-1", number=1),
        crawl(as_cp1252, "https://b.example/", raw=raw, charset="windows-1252", number=2),
    ]
    assert docs[0].text != docs[1].text
    result = group_documents(docs)
    assert members(result, L2) == [[0, 1]]
    assert result.groups[L2][0].warnings == [
        GroupWarning.RAW_DUPLICATE_TEXT_DIFFERS,
        GroupWarning.CROSS_SOURCE_DUPLICATE,
    ]
    assert members(result, L3) == []


def test_partial_twins_from_truncated_plain_text() -> None:
    shared = "x" * 1_000_000
    docs = [
        crawl(shared + "TAIL ONE", "https://a.example/", content_type="text/plain", number=1),
        crawl(shared + "TAIL TWO", "https://b.example/", content_type="text/plain", number=2),
    ]
    assert all(d.status is ExtractionStatus.PARTIAL for d in docs)
    assert docs[0].text == docs[1].text  # identical truncated prefixes
    result = group_documents(docs)
    assert members(result, L3) == []
    assert {(e.position, e.level, e.reason) for e in result.exclusions} == {
        (0, L3, ExclusionReason.NOT_SUCCESS),
        (1, L3, ExclusionReason.NOT_SUCCESS),
    }


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.html")))
def test_every_s05_fixture_document_verifies(name: str) -> None:
    content = (FIXTURES / name).read_text(encoding="utf-8")
    docs = [
        crawl(content, "https://a.example/f", number=1),
        crawl(content, "https://b.example/f", number=2),
    ]
    result = group_documents(docs)
    assert result.errors == []
    assert members(result, L2) == [[0, 1]]
    if docs[0].status is ExtractionStatus.SUCCESS:
        assert members(result, L3) == [[0, 1]]


def test_interstitial_pages_group_across_hosts_and_are_only_flagged() -> None:
    """Bot-challenge detection is out of scope (§23): the group is recorded, documents kept."""
    page = (
        "<html><body><h1>Just a moment...</h1>"
        "<p>Checking your browser before accessing the site.</p></body></html>"
    )
    docs = [crawl(page, f"https://shop{i}.example/item/{i}", number=i) for i in range(3)]
    result = group_documents(docs)
    assert members(result, L3) == [[0, 1, 2]] and members(result, L2) == [[0, 1, 2]]
    assert len(result.documents) == 3


def test_fetch_failures_and_redirect_targets() -> None:
    failed = crawl(None, "https://a.example/p", status=FetchStatus.HTTP_ERROR, number=1)
    ok = crawl(
        f"<html><body><article>{ARTICLE}</article></body></html>",
        "https://a.example/p?utm_campaign=z",
        number=2,
    )
    result = group_documents([failed, ok])
    assert failed.status is ExtractionStatus.NOT_FETCHED
    assert members(result, L1) == [[0, 1]]
    assert result.groups[L1][0].warnings == [GroupWarning.MIXED_FETCH_STATUS]
    assert result.errors == []
