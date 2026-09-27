"""Sprint 05 test helpers: FetchResult builders and the extraction benchmark runner."""

from __future__ import annotations

import json
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_agent.core.models import SearchResult
from research_agent.crawler.models import FetchResult, FetchStatus, RedirectHop, RobotsDecision
from research_agent.extraction import ExtractedDocument, ExtractionSettings, Extractor

FIXTURES = Path(__file__).parent / "fixtures" / "extraction"


def search_result() -> SearchResult:
    return SearchResult(
        title="Search title",
        url="https://ex.test/page",
        original_url="https://ex.test/page?utm_source=x",
        snippet="snippet",
        source="perplexity",
        rank=1,
        query="phở nha trang",
        metadata={"k": "v"},
    )


def fetch_result(
    content: str | None,
    content_type: str | None = "text/html",
    *,
    status: FetchStatus = FetchStatus.OK,
    charset: str | None = "utf-8",
    final_url: str | None = "https://ex.test/dir/page",
    **overrides: Any,
) -> FetchResult:
    values: dict[str, Any] = {
        "crawl_id": "crawl-1",
        "requested_url": "http://ex.test/start",
        "final_url": final_url,
        "status": status,
        "http_status": 200 if status is FetchStatus.OK else None,
        "content_type": content_type,
        "charset": charset if content is not None else None,
        "content_length": None if content is None else len(content.encode("utf-8", "replace")),
        "content": content,
        "content_sha256": "ab" * 32 if content is not None else None,
        "redirect_chain": [
            RedirectHop(
                url="http://ex.test/start",
                http_status=301,
                location="https://ex.test/dir/page",
                connected_ip="93.184.215.14",
            )
        ],
        "resolved_ip": "93.184.215.14",
        "robots": RobotsDecision(
            robots_url="https://ex.test/robots.txt", outcome="ALLOWED", allowed=True
        ),
        "fetched_at": datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
        "duration_ms": 12,
        "source": search_result(),
    }
    values.update(overrides)
    return FetchResult(**values)


def settings(**values: Any) -> ExtractionSettings:
    return ExtractionSettings(_env_file=None, **values)


def extract(
    content: str, content_type: str = "text/html", **setting_values: Any
) -> ExtractedDocument:
    return Extractor(settings(**setting_values)).extract(fetch_result(content, content_type))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# -- benchmark (run in a subprocess so peak RSS belongs to one scenario) ----------------------

_PARA = (
    "<p>Phở bò Nha Trang — giá 45.000đ. Lorem ipsum dolor sit amet, "
    "<a href='/x?a=1'>link</a>.</p>\n"
)
MIB = 1024 * 1024


def benchmark_input(name: str) -> tuple[str, str]:
    """(content, content_type) for a benchmark scenario (design section 22)."""
    if name == "small_html":
        return (
            "<html><head><title>t</title></head><body>" + _PARA * 15 + "</body></html>",
            "text/html",
        )
    if name == "medium_article":
        nav = "<nav>" + "<a href=/n>n</a>" * 50 + "</nav>"
        return f"<html><body>{nav}<article>{_PARA * 800}</article></body></html>", "text/html"
    if name == "large_text":
        return "<html><body>" + _PARA * (5 * MIB // len(_PARA)) + "</body></html>", "text/html"
    if name == "large_html":
        cell = "<div class=c><span>x</span></div>"
        return "<html><body>" + cell * (5 * MIB // len(cell)) + "</body></html>", "text/html"
    if name == "pathological_start_tag":
        return "<div " + " ".join(
            f"a{i}=1" for i in range(600_000)
        ) + ">x</div><p>after</p>", "text/html"
    if name == "deep_nesting":
        return "<div>" * (5 * MIB // 5) + "deep", "text/html"
    if name == "large_metadata":
        meta = '<meta name="description" content="' + "d" * 8_000 + '">'
        return "<html><head>" + meta * 600 + "</head><body><p>body</p></body></html>", "text/html"
    if name == "lt_storm":
        return "<" * (5 * MIB), "text/html"
    if name == "emoji_vietnamese_text":
        return ("Phở bò 😀👨\u200d👩\u200d👧 cá hồi — 45.000đ\n" * (5 * MIB // 60)), "text/plain"
    raise ValueError(name)


BENCHMARKS = (
    "small_html",
    "medium_article",
    "large_text",
    "large_html",
    "pathological_start_tag",
    "deep_nesting",
    "large_metadata",
    "lt_storm",
    "emoji_vietnamese_text",
)


def _proc_kib(key: str) -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith(key):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _reset_peak_rss() -> bool:
    """Linux: reset VmHWM so the peak belongs to the extraction, not to input generation."""
    try:
        with open("/proc/self/clear_refs", "w", encoding="ascii") as refs:
            refs.write("5")
    except OSError:
        return False
    return True


def run_benchmark(name: str) -> None:
    content, content_type = benchmark_input(name)
    result = fetch_result(content, content_type)
    extractor = Extractor(settings())
    if _reset_peak_rss():
        before = _proc_kib("VmRSS") or 0
        peak_key = "VmHWM"
    else:
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_key = ""
    started = time.perf_counter()
    document = extractor.extract(result)
    duration = time.perf_counter() - started
    after = (_proc_kib(peak_key) if peak_key else None) or resource.getrusage(
        resource.RUSAGE_SELF
    ).ru_maxrss
    json.dump(
        {
            "scenario": name,
            "input_chars": len(content),
            "duration_s": round(duration, 3),
            "rss_growth_mb": round((after - before) / 1024, 1),
            "output_chars": document.char_count,
            "status": document.status.value,
            "warnings": [w.value for w in document.warnings],
        },
        sys.stdout,
    )


if __name__ == "__main__":
    run_benchmark(sys.argv[1])
