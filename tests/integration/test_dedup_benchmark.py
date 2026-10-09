"""Sprint 06 benchmark: design §13 budgets, one scenario per subprocess (no tracemalloc)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.dedup_support import BENCHMARKS

ROOT = Path(__file__).resolve().parents[2]
BUDGET_SECONDS = {"short_2000": 0.5, "large_2000": 5.0}
BUDGET_RSS_MB = 32.0

pytestmark = pytest.mark.benchmark


def run(name: str) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "tests.dedup_support", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    result: dict[str, object] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.parametrize("name", BENCHMARKS)
def test_benchmark_budget(name: str) -> None:
    result = run(name)
    print(json.dumps(result))
    assert result["refs"] == result["documents"]  # nothing truncated
    assert result["errors"] == 0
    if name in BUDGET_SECONDS:
        assert float(str(result["best_of_3_s"])) <= BUDGET_SECONDS[name]
        assert float(str(result["peak_rss_growth_mb"])) <= BUDGET_RSS_MB
    if name == "groups_10000":
        assert result["groups"] == {"L1": 0, "L2": 2000, "L3": 2000}  # raw bytes derive from text
