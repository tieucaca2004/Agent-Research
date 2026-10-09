"""Sprint 06 test helpers: deterministic ExtractedDocument builders and the dedup benchmark."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from research_agent.core.models import SearchResult
from research_agent.crawler.models import FetchStatus
from research_agent.extraction.models import (
    ClaimedMetadata,
    DocumentProvenance,
    ExtractedDocument,
    ExtractionStatus,
    ExtractionWarning,
)

AUTO: Any = object()
"""Sentinel: derive the value the way S04/S05 would."""
FIXED_TIME = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
_ids = itertools.count()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def search_hit(url: str, query: str = "phở nha trang", rank: int = 1) -> SearchResult:
    return SearchResult(
        title="hit",
        url=url,
        original_url=url,
        snippet=None,
        source="perplexity",
        rank=rank,
        query=query,
    )


def make_doc(
    text: str = "Python 3.13 was released with a new REPL.",
    *,
    url: str = "https://a.example/page",
    final_url: Any = AUTO,
    status: ExtractionStatus = ExtractionStatus.SUCCESS,
    fetch_status: FetchStatus = FetchStatus.OK,
    raw: str | None = None,
    content_sha256: Any = AUTO,
    text_sha256: Any = AUTO,
    warnings: list[ExtractionWarning] | None = None,
    canonical_url: str | None = None,
    json_ld: list[str] | None = None,
    source: Any = AUTO,
    doc_id: str | None = None,
) -> ExtractedDocument:
    """Build an ExtractedDocument without running S04/S05.

    ``content_sha256`` defaults to the hash of ``raw`` (default: ``"raw:" + text``), i.e. distinct
    raw bytes per text; ``text_sha256`` defaults to S05's definition; ``final_url`` defaults to
    ``url``."""
    number = next(_ids)
    fetched = fetch_status is FetchStatus.OK
    if content_sha256 is AUTO:
        content_sha256 = sha(raw if raw is not None else "raw:" + text) if fetched else None
    if text_sha256 is AUTO:
        text_sha256 = sha(text) if text else None
    if final_url is AUTO:
        final_url = url
    if source is AUTO:
        source = search_hit(url)
    provenance = DocumentProvenance(
        crawl_id=f"crawl-{number}",
        requested_url=url,
        final_url=final_url,
        http_status=200 if fetched else None,
        content_type="text/html" if fetched else None,
        charset="utf-8" if fetched else None,
        fetched_at=FIXED_TIME,
        redirect_chain=[],
        robots=None,
        resolved_ip=None,
        content_sha256=content_sha256,
        source=source,
    )
    return ExtractedDocument(
        document_id=doc_id or f"doc-{number}",
        extractor_version="s05.1",
        status=status,
        warnings=list(warnings or []),
        fetch_status=fetch_status,
        text=text,
        char_count=len(text),
        text_sha256=text_sha256,
        claimed_metadata=ClaimedMetadata(canonical_url=canonical_url, json_ld=list(json_ld or [])),
        provenance=provenance,
        extracted_at=FIXED_TIME,
    )


def not_fetched(url: str = "https://a.example/page") -> ExtractedDocument:
    return make_doc(
        "", url=url, status=ExtractionStatus.NOT_FETCHED, fetch_status=FetchStatus.HTTP_ERROR
    )


# -- benchmark (one scenario per subprocess; peak RSS of the grouping step only) --------------

BENCHMARKS = ("short_2000", "large_2000", "groups_10000")


def _proc_kib(key: str) -> int:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith(key):
                return int(line.split()[1])
    return 0


def benchmark_documents(name: str) -> list[ExtractedDocument]:
    if name == "short_2000":
        return [
            make_doc(f"Document {i // 5}: Python 3.13 was released.", url=f"https://s{i}.example/p")
            for i in range(2000)
        ]
    if name == "large_2000":
        body = "Đoạn văn dài về phở bò Nha Trang, giá 45.000đ. " * 2100  # ~100 k chars
        return [
            make_doc(f"{i}" + body[:99_990], url=f"https://l{i}.example/p") for i in range(2000)
        ]
    if name == "groups_10000":
        return [
            make_doc(f"Group {i // 5} text.", url=f"https://g{i}.example/p") for i in range(10_000)
        ]
    raise ValueError(name)


def run_benchmark(name: str) -> None:
    from research_agent.dedup import group_documents

    documents = benchmark_documents(name)
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as refs:
            refs.write("5")  # Linux: reset the peak so it belongs to the grouping step
    except OSError:
        pass
    before = _proc_kib("VmRSS")
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        result = group_documents(documents)
        best = min(best, time.perf_counter() - started)
    peak = _proc_kib("VmHWM") - before
    json.dump(
        {
            "scenario": name,
            "documents": len(documents),
            "input_chars": sum(len(d.text) for d in documents),
            "best_of_3_s": round(best, 4),
            "peak_rss_growth_mb": round(peak / 1024, 1),
            "groups": {k.value: v for k, v in result.stats.groups.items()},
            "refs": len(result.refs),
            "errors": result.stats.errors,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    run_benchmark(sys.argv[1])
