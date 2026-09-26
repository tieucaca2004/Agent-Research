# Search provider contracts

Recorded 2026-09-26 (Sprint 01). Official documentation sites (`docs.perplexity.ai`,
`developers.google.com`) were **blocked by the build environment's egress proxy**, so each
contract was taken from the provider's **official, machine-generated client package** on PyPI.
Live behaviour stays **UNVERIFIED** until `uv run pytest -m live -rs` passes with real credentials.

## Perplexity Search API — primary

| | |
|---|---|
| Source of contract | `perplexityai` SDK **v0.43.6** (PyPI upload 2026-09-25), files `types/search_create_params.py`, `types/search_create_response.py`, `resources/search.py`, `_client.py` ("generated from our OpenAPI spec by Stainless"). Diffed against v0.42.0: unchanged. |
| Endpoint | `POST https://api.perplexity.ai/search` |
| Auth | `Authorization: Bearer $PERPLEXITY_API_KEY` |
| Request (used) | `query: str` (required), `max_results: int`, `country: str\|null`, `search_language_filter: list[str]\|null` |
| Request (documented, unused in V1) | `max_tokens`, `max_tokens_per_page`, `search_context_size` (`low\|medium\|high`), `search_domain_filter`, `search_recency_filter` (`hour\|day\|week\|month\|year`), `search_mode` (`web\|academic\|sec`), `search_type` (`web\|people`), `search_after_date_filter`, `search_before_date_filter`, `last_updated_after_filter`, `last_updated_before_filter`, `display_server_time`. `query` may also be a list (not used). |
| Limits | SDK docstring: "max_results above 20 is only supported for people search" → adapter clamps to 20. |
| Response 200 | `{"id": str, "results": [{"title": str, "url": str, "snippet": str, "date": str\|null, "last_updated": str\|null}], "server_time": str\|null}` |
| Mapping | `url` → normalized `url` (+ raw `original_url`); `date` → `published_at` (ISO date/datetime, else `null`); `last_updated`, `id` → `metadata`. |
| Status | Contract: **VERIFIED (SDK)** · Live: **REQUIRES_CONFIGURATION** (`PERPLEXITY_API_KEY`). Note: `api.perplexity.ai` was also unreachable from the build container. |

## Google Custom Search JSON API — fallback

| | |
|---|---|
| Source of contract | Discovery document `customsearch.v1.json` (revision 20240821) shipped in `google-api-python-client` **v2.200.0** (PyPI upload 2026-09-01). |
| Endpoint | `GET https://customsearch.googleapis.com/customsearch/v1` |
| Auth | query parameter `key` (API key) |
| Request (used) | `cx` (engine ID), `q`, `num` (1..10), `gl` (2-letter country), `hl` (interface language) |
| Not used | `lr`: documented value list has no Vietnamese (`lang_vi`). `start` paging: not needed in V1. |
| Response 200 | `Search` object; `items[]` (absent when no results) of `Result {title, link, snippet, displayLink, pagemap, ...}` |
| Mapping | `link` → `url`; `pagemap.metatags[*].article:published_time / og:published_time / datepublished` → `published_at` if present. |
| **Availability** | Google announced the API is **closed to new customers** and will be **discontinued on 2027-01-01** (public reports, e.g. dev.to / cloro.dev / octoparse, Jan 2026). Only pre-existing credentials can work, and only until that date. A replacement fallback provider will be needed. |
| Status | Contract: **VERIFIED (discovery doc)** · Live: **REQUIRES_CONFIGURATION** (`GOOGLE_API_KEY`, `GOOGLE_CSE_ID`). |

## Bing Web Search API

Not implemented. Microsoft retired the Bing Search APIs (Aug 2025). Slot intentionally left empty.

## Secret handling in adapters

- Keys come only from `Settings` (`SecretStr`); adapters' `__repr__` omit them.
- Error messages are built by `send_json_request` and never include request URL/headers
  (Google's key travels in the query string). Original httpx exceptions are not chained.
- `httpx`/`httpcore` loggers are raised to WARNING; the structlog redaction processor masks
  `key=`/`token=` query params, `Bearer` tokens and any field named like a secret.

## Adding a provider

1. Create `src/research_agent/providers/search/<name>.py` with a `SearchProvider` subclass
   (`name`, `required_settings`, `from_settings`, `search`), mapping errors via
   `send_json_request` and returning `SearchResult`s with normalized URLs.
2. Add its settings to `config.Settings` and names to `.env.example`.
3. Register it in `providers/search/registry.py::PROVIDERS`.
4. Add respx unit tests + it is picked up automatically by `tests/live`.

`SearchService` is not modified.
