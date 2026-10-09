"""Sprint 07 benchmark (design §8.4, acceptance proposal D2b): one subprocess per scenario,
VmHWM reset after setup, local server only. Thresholds are regression bounds, not runtime limits."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.pipeline_support import BENCHMARKS

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.benchmark

THRESHOLDS = {
    "crawl_30_small": {"duration_s": 10.0, "peak_rss_growth_mb": 128.0},
    "crawl_30_large_unicode": {"peak_rss_growth_mb": 512.0},
    "normalize_30m": {"duration_s": 1.0},
    "two_jobs_30_small": {"peak_rss_growth_mb": 256.0},
}


def run(name: str) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "tests.pipeline_support", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    result: dict[str, object] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.parametrize("name", BENCHMARKS)
def test_pipeline_benchmark(name: str) -> None:
    result = run(name)
    print(json.dumps(result))
    for key, bound in THRESHOLDS[name].items():
        assert float(str(result[key])) <= bound, (key, result[key], bound)
    if name == "normalize_30m":
        assert result["input_chars"] == 30_000_000
        return
    jobs = int(str(result["jobs"]))
    assert result["fetch_ok"] == result["extracted"] == 30 * jobs
    assert result["fetch_timeout"] == 0  # no queue-caused timeout (S04-F1 over-submission)
    assert int(str(result["bodies_max_held"])) <= 5 + 4 + 2
    assert int(str(result["admission_max_in_flight"])) <= 5
    if name == "crawl_30_large_unicode":
        assert result["extraction"] == [{"PARTIAL": 30}]  # S05 truncates at 1 M chars
        assert result["text_chars"] == 30_000_000  # exactly the D2 budget, nothing cut by S07
    else:
        extraction = result["extraction"]
        assert isinstance(extraction, list)
        assert all(set(e) == {"SUCCESS"} for e in extraction)
