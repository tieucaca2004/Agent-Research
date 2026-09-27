# Search & Research Agent V1

Natural-language query → research plan → web search → crawl → extraction → normalization →
dedup → verification → provenance → results. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

Current state: **Sprint 04 — Secure crawler** (standalone `src/research_agent/crawler/`: SSRF-safe
fetching, robots.txt, bounded bodies; not yet wired into jobs — see
[docs/sprint-04-crawler.md](docs/sprint-04-crawler.md)) on top of **Sprint 03 — Research API** (local/internal HTTP API, see
[docs/sprint-03-research-api.md](docs/sprint-03-research-api.md)) on top of **Sprint 02 — Research Job** (job lifecycle in `src/research_agent/jobs/`, see
[docs/sprint-02-research-job.md](docs/sprint-02-research-job.md)) on top of **Sprint 01 — Core Search** (search provider abstraction, Perplexity + Google
CSE adapters, URL normalization, `SearchService`). Provider contracts: [docs/providers.md](docs/providers.md).

## Development

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # install
cp .env.example .env                      # optional; fill in keys (never commit .env)

uv run ruff format --check . && uv run ruff check .   # format + lint
uv run mypy                                           # strict type check (src + tests)
uv run pytest                                         # unit + mocked integration (+ live, skipped without keys)
uv run pytest -m live -rs                             # live provider tests only
RUN_LIVE_NETWORK=1 uv run pytest -m live_network -s   # real-web crawler test (opt-in)
uv run bandit -q -r src -c pyproject.toml             # static security scan

uv run python -m research_agent.api                   # local API on 127.0.0.1:8000
```

The API is a **local/internal boundary**: no authentication and no rate limiting yet — do not expose it
publicly. Jobs are kept in memory and lost on restart.

Live tests without credentials are **skipped** with reason `REQUIRES_CONFIGURATION: <VARS>` —
a skip is never counted as a pass.
The real-web crawler test is skipped with `REQUIRES_NETWORK` unless `RUN_LIVE_NETWORK=1`.
