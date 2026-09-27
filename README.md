# Search & Research Agent V1

Natural-language query → research plan → web search → crawl → extraction → normalization →
dedup → verification → provenance → results. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

Current state: **Sprint 02 — Research Job** (job lifecycle in `src/research_agent/jobs/`, see
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
uv run bandit -q -r src -c pyproject.toml             # static security scan
```

Live tests without credentials are **skipped** with reason `REQUIRES_CONFIGURATION: <VARS>` —
a skip is never counted as a pass.
