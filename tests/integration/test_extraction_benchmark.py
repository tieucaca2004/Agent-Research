"""Sprint 05 performance baseline (design section 22). Each scenario runs in its own process so
the peak-RSS figure belongs to that extraction. Budgets are safety limits, not targets; timing is
the best of up to two runs (shared machines are noisy). Results are printed with ``-s``."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.extraction_support import BENCHMARKS

pytestmark = pytest.mark.benchmark

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 10.0
# scenario -> (max seconds, expected statuses)
BUDGETS: dict[str, tuple[float, set[str]]] = {
    "small_html": (0.02, {"SUCCESS"}),
    "medium_article": (0.3, {"SUCCESS"}),
    "large_text": (3.0, {"SUCCESS"}),
    "large_html": (5.0, {"SUCCESS"}),
    "pathological_start_tag": (1.0, {"SUCCESS"}),
    "deep_nesting": (TIMEOUT_S, {"SUCCESS", "PARTIAL"}),
    "large_metadata": (1.0, {"SUCCESS"}),
    "lt_storm": (TIMEOUT_S + 1.0, {"PARTIAL"}),
    "emoji_vietnamese_text": (3.0, {"SUCCESS", "PARTIAL"}),
}
MAX_RSS_GROWTH_MB = 128.0


def run(name: str) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and module, no shell
        [sys.executable, "-m", "tests.extraction_support", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    line = [x for x in completed.stdout.splitlines() if x.startswith('{"scenario"')][-1]
    result: dict[str, object] = json.loads(line)
    return result


@pytest.mark.parametrize("name", BENCHMARKS)
def test_benchmark(name: str) -> None:
    budget, statuses = BUDGETS[name]
    result = run(name)
    if float(str(result["duration_s"])) > budget:
        retry = run(name)
        if float(str(retry["duration_s"])) < float(str(result["duration_s"])):
            result = retry
    print(json.dumps(result))
    assert result["status"] in statuses
    assert float(str(result["duration_s"])) <= budget
    assert float(str(result["rss_growth_mb"])) < MAX_RSS_GROWTH_MB
    if name == "pathological_start_tag":
        assert "OVERSIZED_TAG_REMOVED" in str(result["warnings"])
    if name == "lt_storm":
        assert result["output_chars"] == 1_000_000
