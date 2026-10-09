# Sprint 07 — Pipeline integration: Search → Crawler → Extraction → Dedup (design)

Status: **IMPLEMENTED behind `PIPELINE_ENABLED` (default off)** — decisions locked on `39e23dc`/`12dadc7`;
founder decisions OD-A = A1, OD-B = B2, OD-C granted, X-1 unchanged. As-built record and evidence: §19.
No production code, test, dependency or ARCHITECTURE change in this or the previous design turn.
§0 is the authoritative decision register; where older wording elsewhere differs, §0 prevails.

Decision classes:
**[APPROVED DECISION]** = locked by the founder (decision-lock brief, items 1–12);
**[EXISTING CONTRACT]** = fixed by frozen code or an approved earlier design (§3);
**[RECOMMENDATION]** = proposed here, not approved; may be changed without reopening a decision;
**[OPEN — FOUNDER DECISION]** = undecided; implementation of the affected part must not start until decided.
An item is never both APPROVED and OPEN: where an approved direction depends on something undecided, the
dependent part is listed separately as OPEN. Evidence ids `P1…P7` are probes run for this design (scratch
scripts against local 127.0.0.1 servers with the S04 test harness; not committed).

## 0. Decision register

| # | Decision | Class | Basis / note |
|---|---|---|---|
| D1 | **OD-13** — the canonical order is `JobResult.response.results`; hit / fetch / document are mapped by **position slots**; the representative of every S06 group reflects the canonical hit order, never fetch speed | APPROVED DECISION | §7; C1, C2, C9; P2 |
| D2 | **OD-14** — at most **30 URLs per job** and at most **30 000 000 extracted text characters per job** | APPROVED DECISION | §8; P6 |
| D2a | Enforcement of D2: static check at wiring (`max_urls × EXTRACT_MAX_TEXT_CHARS ≤ max_total_text_chars`, else configuration error when the pipeline is enabled) plus a runtime counter that raises a stage error, never truncates | RECOMMENDATION | §8.1 |
| D2b | Benchmark thresholds (§8.4) are an **acceptance proposal**, to be measured in the implementation sprint, not limits | RECOMMENDATION | §8.4 |
| D3 | S05 content extraction runs in **CRAWLING**; S06 runs in **NORMALIZING**; **EXTRACTING** is reserved for AI structured extraction (not configured) | APPROVED DECISION | ARCHITECTURE §3 rows 6 and 9; §5 |
| D4 | S04-F1 is addressed **preferably by admission control** at the integration layer; it is **not claimed resolved** until the admission tests (§9.8) pass | APPROVED DECISION | §9; P1, P1b, P5 |
| D4a | Admission removes queue time caused by over-submission (global and same-host), not queue time caused by a **cross-host redirect** into a host that is busy (P7), nor the S04 per-host minimum interval and robots lock waits | EXISTING CONTRACT (S04 behaviour, reproduced) | §9.1, §9.7; P7 |
| D4b | Whether the residual of D4a is accepted for S07 or S04 is reopened | **OPEN — FOUNDER DECISION (OD-A)** | §9.7 |
| D5 | The frozen `JobRunner` is **not modified in the design turns**; an additive, method-by-method change plan with regression tests is presented (§12) | APPROVED DECISION | brief item 5 |
| D5a | Permission to modify the frozen S02/S03 files (`jobs/runner.py`, `jobs/models.py`, `api/*`) in the implementation sprint, per §12 | **OPEN — FOUNDER DECISION (OD-C)** | §12, §13 |
| D6 | `JobResult`, `JobProgress` and `JobErrorCode` are extended **additively only** (defaults, no renamed/removed field or code, S02-only jobs unchanged) | APPROVED DECISION | §12.2 |
| D6a | The concrete field and code list (§12.2) | RECOMMENDATION | §12.2 |
| D7 | An unexpected exception in a stage **stops the stage**, keeps every captured result and produces an explicit stage error; never an empty successful result | APPROVED DECISION | §10.1 |
| D7a | Stage error code names `CRAWL_FAILED`, `EXTRACTION_FAILED`, `DEDUP_FAILED` (category `INTERNAL_ERROR`) | RECOMMENDATION | §10.1 |
| D8 | Terminal precedence **cancel > job deadline > stage timeout > result**; `NO_DOCUMENTS` → `FAILED`; per-URL fetch/extraction failures are **warnings** | APPROVED DECISION | §10.2 |
| D8a | Where a stage error ranks relative to a cancel recorded before it: frozen S02 code finalizes `FAILED/INTERNAL_ERROR` without checking `cancel_requested` (`runner.py:240–251`, planner path `:173–180`) | **OPEN — FOUNDER DECISION (OD-B)** | §10.3 |
| D9 | The API exposes **source summaries only**, never page text | APPROVED DECISION | §14 |
| D9a | Exact API fields (§14) | RECOMMENDATION | §14 |
| D10 | The pipeline is enabled by a **setting, default off**; with the setting off, job execution is unchanged from `75ce79e` (same stages, statuses, results, events); the only visible difference is the additive `null` keys on `/results` (§14) | APPROVED DECISION | §12.3, §14 |
| D11 | **No new per-host URL cap** in this sprint (selection is the canonical prefix) | APPROVED DECISION | §8.1 |
| D12 | **ARCHITECTURE.md is not edited** and roadmap numbering is not changed by this work | APPROVED DECISION | §16 |
| D12a | Roadmap numbering (X-1) | **OPEN — FOUNDER DECISION** | §16 |
| R1 | Admission limits: process-wide global = `CRAWL_CONCURRENCY` (5); per host = `CRAWL_PER_HOST_CONCURRENCY` (1), host key = S04's own `check_url` host; per job = ⌈global ÷ `max_concurrent_jobs`⌉ (3 with defaults); grant order lowest position per job, round-robin across jobs | RECOMMENDATION | §9.2 |
| R2 | Extraction backlog ≤ 4 fetched-not-extracted bodies per process; `FetchResult.content` released after extraction, never stored | RECOMMENDATION | §8.2 |
| R3 | `PIPELINE_CRAWL_STAGE_TIMEOUT_S = 180`; S06 called via `asyncio.to_thread`, no S06 timeout | RECOMMENDATION | §8.3 |
| R4 | Fetch and extraction pipelined per source; S06 input = documents in ascending position with an explicit `positions` map | RECOMMENDATION (realizes D1) | §7 |
| R5 | X-2 … X-5 dispositions (§16) | RECOMMENDATION (ARCHITECTURE text unchanged, D12) | §16 |

Unchanged and still open from earlier sprints: S06 F-2 (warning semantics, S06 production not changed), OD-11
(bot challenges), OD-4 (L4), OD-10 (persistent identity), PERSISTENCE-BLOCKER-01 (PARTIALLY RESOLVED).

## 1. Baseline

| Item | Value |
|---|---|
| Code baseline | `75ce79e` (S06 + F-1 fix). Full suite there: **845 passed, 5 skipped** (3 `live_network` without `RUN_LIVE_NETWORK=1`; 2 live search without API keys) |
| Design baseline | `39e23dc` (this document, first version; changed nothing but this file) |
| Frozen components | S01 search (`pipeline/search.py`, `core/`, `providers/`), S02 jobs (`jobs/`), S03 API (`api/`), S04 crawler (`crawler/`), S05 extraction (`extraction/`), S06 dedup (`dedup/`) |
| Read | ARCHITECTURE.md; docs/sprint-02 … sprint-06; code of `SearchService`/`SearchRun`, `Crawler` (`fetch`, `fetch_many`, robots, host slots, requests), `Extractor.aextract`, `group_documents`, `JobRunner`, job models, state machine, repository, API app/service/schemas, and the API tests' exact-shape assertions |

There is no separate S01 design document; S01 contracts are read from code and ARCHITECTURE §4.2.

## 2. Scope and non-goals

**Scope (implementation sprint, after OD-A/OD-B/OD-C):** jobs with stages
`PLANNING → SEARCHING → CRAWLING → NORMALIZING` behind a setting (D10); CRAWLING = selection + admitted S04
fetch + S05 extraction (D3); NORMALIZING = S06 grouping; the order contract (D1), budgets (D2), admission (D4),
error and terminal rules (D7, D8), source summaries in the API (D9).

**Non-goals:** persistence / database (PERSISTENCE-BLOCKER-01 unchanged); AI extraction (`EXTRACTING` unconfigured);
verification and synthesis/reporting (`VERIFYING` unconfigured); bot-challenge detection (OD-11); L4
near-duplicates; any change to S06 semantics (F-2 open); link following (`CRAWL_MAX_DEPTH`); per-host URL cap
(D11); new dependencies; any S04/S05/S06 code change unless OD-A reopens S04.

## 3. Existing contracts (read from code at `75ce79e`)

| # | Contract | Source | Consequence |
|---|---|---|---|
| C1 | `SearchRun.to_response()` orders hits by `(query index, provider priority, provider rank, arrival index)` and keeps the first hit per normalized URL as `DedupedSearchResult.result`, with all contributing `providers`/`queries` | `pipeline/search.py:122–158`; tests `test_to_response_sorts_out_of_order_hits`, `test_ordering_is_deterministic_regardless_of_completion_order` | canonical rank order (D1). `SearchRun.results` (first occurrence in append order) is a different order and is not used |
| C2 | The runner calls `SearchService.search([query])` once per planned query, sequentially in plan order, and builds `JobResult.response = merged.to_response()` | `jobs/runner.py:264–301, 434–466` (S02 D3-B) | job-level canonical order is `JobResult.response.results` |
| C3 | Providers set `rank = index` (1-based) | `providers/search/google.py:159`, `perplexity.py:136` | rank is provider-local |
| C4 | `FetchTarget.from_search_result(r)` fetches `r.original_url`, carries `r` as `source`; `FetchResult.source` is that object | `crawler/models.py:95–105` | provenance join on every result |
| C5 | `Crawler.fetch_many` = `asyncio.gather(fetch(t) …)`: input order; one exception loses all results (P4); an outer timeout loses all results (P3) | `crawler/crawler.py:328–332` | not used by the integration |
| C6 | `Crawler.fetch`: the per-URL budget (`CRAWL_TIMEOUT_S` = 15 s, capped by `CrawlContext.deadline`) is an `asyncio.timeout` that encloses the wait for the global semaphore; failures are returned as `FetchStatus` | `crawler/crawler.py:334–364` | over-submission consumes budgets (P1) |
| C7 | Per-host slot (`CRAWL_PER_HOST_CONCURRENCY` = 1) is taken **per HTTP request** — robots.txt request, every redirect hop, the page — inside the budget; inside the slot the crawler sleeps until `per_host_min_interval_s` (1 s) after the previous request start to that host | `crawler/crawler.py:541–557` | same-host queueing (P5) and redirect-hop queueing (P7) consume budgets |
| C8 | robots.txt: per-origin `asyncio.Lock`, cached per origin (TTL 24 h), fetched within the page's budget (≤ `CRAWL_ROBOTS_TIMEOUT_S` 5 s) | `crawler/crawler.py:473–520` | a fetch can wait for another fetch's robots request on the same origin |
| C9 | S04 sets `content_sha256` only for `OK` fetches with a body; `content` ≤ 5 MiB decoded bytes | `crawler/crawler.py:694–715` | content must be released after extraction (R2) |
| C10 | `Extractor.aextract(fetch, context)`: worker thread, ≤ `EXTRACT_CONCURRENCY` (2) at once, honours `context.deadline`, on cancellation stops the worker then re-raises `CancelledError`; never raises for page content; one document per `FetchResult` (`NOT_FETCHED` for failed fetches) | `extraction/extractor.py:294–372`; test `test_aextract_cancellation_propagates_and_stops_worker` | one document per fetched slot |
| C11 | `group_documents(documents)`: pure, synchronous, deterministic for the input sequence; representative = lowest input position; no limit/deadline; per-document problems never raise | S06 design §0, §25 | caller owns order, volume, failure mapping |
| C12 | Job `stages` = ordered subset of `STAGE_ORDER`; transitions only to the next configured stage or a terminal state; `repository.create(…, stages=…)` already accepts stages | `jobs/models.py:33–48, 213–224`; `jobs/state_machine.py`; `jobs/repository.py:89–112` | `(PLANNING, SEARCHING, CRAWLING, NORMALIZING)` is valid |
| C13 | `JobRunner.run` rejects stages ≠ `SPRINT_02_STAGES`; `JobResult` holds search data only; `JobErrorCode` is a closed `Literal`; `_inflight` is typed `dict[str, Task[SearchRun]]` | `jobs/runner.py:112, 143–147`; `jobs/models.py:132–189` | frozen S02 code must be extended (OD-C) |
| C14 | Runner race-safety (OI-1, OI1-DEF-01): writes via `_save_update` (CAS + re-read), no write onto a terminal job, terminal check before each provider task, precedence cancel > deadline > stage timeout > outcome, runner-task cancel → `RUNNER_INTERRUPTED` | docs/sprint-03-research-api.md §22; `test_job_cancel_race*.py` | every new write site and loop follows the same rules |
| C15 | Stage exceptions: planner and search-stage exceptions finalize `FAILED/INTERNAL_ERROR` via `_finish` without consulting `cancel_requested` (a cancel only wins if its write causes a version conflict) | `jobs/runner.py:173–180, 240–251`; conflict path `:565–604` (`_save_update`, cancel raised at `:601`) | basis of OD-B |
| C16 | API: non-terminal statuses are "not finished" via `is_terminal`; views are mapped field by field; error codes are `str`; tests assert exact shapes of `JobCreatedView` keys and `result_summary` (`test_api.py:105, 113`), not of `ResultsView` or `progress` | `api/service.py:107`, `api/schemas.py`, `tests/integration/test_api.py` | additive API fields only on `ResultsView`/`ProgressView` (§14) |

## 4. Probe evidence

Local server, robots.txt → 404 (allow), S04 test harness (`make_crawler`; `per_host_min_interval_s = 0` unless stated).

| Id | Setup | Observation |
|---|---|---|
| P1 | `fetch_many`, 6 distinct hosts, `CRAWL_CONCURRENCY=2`, budget 1.0 s, pages answer after 0.6 s | `OK ×2, FETCH_TIMEOUT ×4` in 1.04 s; 4 page requests reached the server (2 cut mid-flight) |
| P1b | same load, caller admits ≤ 2 at a time, calls `fetch` | `OK ×6`, 1.84 s |
| P2 | `fetch_many`, completion forced in reverse order | completion f…a; results a…f (input order) |
| P3 | stage limit 1.0 s; 2 fast pages, 4 pages of 3 s | outer `asyncio.timeout` → `TimeoutError`, all results lost; `CrawlContext(deadline)` → `OK ×2, FETCH_TIMEOUT ×4`, all returned |
| P4 | `fetch_many`, one `fetch` raises `RuntimeError` | exception propagates; 5 successful results not returned |
| P5 | 6 URLs on one host, per-host 1, budget 1.0 s, page 0.6 s | no admission: `OK, FETCH_TIMEOUT ×5`; per-host admission ≤ 1: `OK ×6` in 3.66 s |
| P6 | S06 as-built: 30 documents × 1 000 000 chars, best of 3, VmHWM reset | 0.073 s; peak RSS growth 1.2 MB |
| P7 | per-initial-host admission satisfied (`a.test`, `b.test` both admitted); `a.test/r` → 302 → `b.test/p` (0.1 s) while `b.test/slow` holds the `b.test` slot; budget 1.0 s | `b.test/slow` = 0.3 s → redirected fetch `OK` (420 ms); `b.test/slow` = 0.95 s → redirected fetch **`FETCH_TIMEOUT`** (1001 ms) although its own request needs 0.1 s |

## 5. Architecture and data flow

```text
QUEUED → PLANNING → SEARCHING ─────────→ CRAWLING ─────────────────────────────────────→ NORMALIZING → terminal
          planner     S01 per query (C2)   select = response.results[:30]                    S06 group_documents
                      JobResult.response   per position p:                                    (worker thread)
                                             wait admission → S04 fetch → FetchRecord[p]
                                             → S05 aextract → document[p] → release content
```

| Stage | Input | Captured output | Component |
|---|---|---|---|
| SEARCHING | `plan.queries` | `response: SearchResponse` (unchanged) | S01 via runner (C2) |
| CRAWLING / select | `response.results` | `SourceRecord[p]`, `not_selected` count | `pipeline/discovery.py` (new) |
| CRAWLING / fetch | `FetchTarget.from_search_result(results[p].result)` | `FetchRecord` (no content) in slot p | `FetchAdmission` → S04 `Crawler.fetch` |
| CRAWLING / extract | `FetchResult` of slot p | `ExtractedDocument` in slot p | S05 `Extractor.aextract` |
| NORMALIZING | documents in ascending p | `DedupRecord(positions, DocumentSet)` | S06 `group_documents` |

## 6. Interface contracts (integration layer, new modules)

[RECOMMENDATION — names and fields; the behaviours they implement are D1/D2/D4/D7.]

```text
PipelineSettings (env prefix PIPELINE_)                     pipeline/config.py
  enabled: bool = False                                     # D10
  max_urls: int = 30                                        # D2
  max_total_text_chars: int = 30_000_000                    # D2
  crawl_stage_timeout_s: float = 180                        # R3
  extraction_backlog: int = 4                               # R2
  job_fetch_concurrency: int | None = None                  # R1; None → ceil(CRAWL_CONCURRENCY / max_concurrent_jobs)

select_sources(response, max_urls) -> Selection             pipeline/discovery.py (pure)
  sources = response.results[:max_urls]; not_selected = len(response.results) - len(sources)

FetchAdmission(crawler, *, global_limit, per_host_limit)   pipeline/admission.py; one per process
  async with admission.slot(job_key, url): ...              # waits for a global, per-host and per-job slot
  counters: in_flight_total, in_flight_by_host, in_flight_by_job, waiting (for tests / logs)

collect_sources(selection, admission, crawler, extractor, *, deadline, on_capture) -> SourceCapture
                                                            pipeline/sources.py
  never uses fetch_many; never wraps the crawler in an outer timeout that discards results (P3, P4)

SourceRecord   position, url, providers, queries, state, fetch: FetchRecord | None, document_id
               state ∈ {NOT_RUN, INTERRUPTED, FETCHED, EXTRACTED}; reason for NOT_RUN/INTERRUPTED
FetchRecord    every FetchResult field except `content` and `title`
DedupRecord    positions: list[int] (strictly increasing), result: DocumentSet (S06 output unchanged)
StageOutcome   stage, status ∈ {COMPLETED, PARTIAL, FAILED, INTERRUPTED, NOT_RUN}, counts, error code
```

## 7. OD-13 — end-to-end order contract [APPROVED DECISION D1]

The contract is the acceptance criterion; that each component preserves its own input order (C1, C5, C10, C11)
is necessary but not sufficient and is not claimed as proof. Proof = the E2E tests of §15.

1. **Canonical order** = `JobResult.response.results` (C1, C2). "Best-ranked" = lowest index. The same normalized
   URL from several queries/providers is one source (search-level dedup before fetch; fetched once).
2. **Hit → fetch request**: `sources = response.results[:max_urls]`; position `p` = list index; target
   `p` = `FetchTarget.from_search_result(sources[p].result)` (C4).
3. **Parallel completion**: every completed `FetchResult` is written to **slot `p`**; nothing is appended in
   completion order. Admission chooses which pending `p` starts next (start order may differ from `p`; storage
   order never does).
4. **Join** by slot, checked: `result.source is sources[p].result` and `result.requested_url == target.url`;
   a mismatch is a stage error (`CRAWL_FAILED`), never a silent re-pairing.
5. **Extraction** result of slot `p` goes to slot `p`; checked `document.provenance.crawl_id == fetch.crawl_id`
   (mismatch → `EXTRACTION_FAILED`).
6. **S06 input**: `documents = [doc[p] for p ascending if doc[p] exists]`, `positions` = the same `p`
   (strictly increasing). Slots without a document (`NOT_RUN`, `INTERRUPTED`, fetched-but-not-extracted) are
   skipped and recorded, never filled with placeholders.
7. **Representative**: for every S06 group of every level, `positions[group.representative]` = minimum source
   position of its members = the best-ranked hit (S06's lowest-input-position rule through a monotonic map), not
   the first fetch to finish.

## 8. OD-14 — resource budget [APPROVED DECISION D2; mechanisms RECOMMENDATION]

S06 figures are the **as-built** measurements (S06 design §25, P6), never the E8 prototype figures.

### 8.1 Volumes

| Quantity | Bound | Basis | When exceeded |
|---|---|---|---|
| Search hits | unchanged: `plan.queries` ≤ 8 × `max_results` ≤ 50 × providers; today FixedPlanner 1 query × default 10 (fallback) | C2, `ResearchPlan`, `SearchOptions` | — |
| URLs fetched per job | **30** (D2) = canonical prefix; no per-host cap (D11) | ARCHITECTURE §5 `CRAWL_MAX_URLS=30` | the rest counted as `not_selected`; search results stay in `response`; not an error |
| Raw body per URL | 5 MiB decoded (S04, unchanged) | C9 | S04 `RESPONSE_TOO_LARGE` |
| Text per document | `EXTRACT_MAX_TEXT_CHARS` (default 1 M, S05 allows ≤ 10 M) | S05 §16 | S05 `PARTIAL` / `TEXT_TRUNCATED` |
| Text per job | **30 000 000 chars** (D2). Static check when the pipeline is enabled: `max_urls × EXTRACT_MAX_TEXT_CHARS ≤ 30 000 000` (holds at defaults: 30 × 1 M), else configuration error at startup (D2a). Runtime counter re-checks; exceeding it is a stage error `PIPELINE_BUDGET_EXCEEDED`, never truncation | P6 (S06 at exactly this size: 0.073 s, 1.2 MB) | cannot happen with a valid configuration |

S06 keeps no limit of its own (S06 P12); its input volume is bounded by configuration before any job runs.
S05's own setting range is not changed: the static check only refuses combinations that would exceed D2 when the
pipeline is enabled.

### 8.2 Memory

- Bodies held at once per process ≤ in-flight fetches (5) + backlog (4) + extracting (2) = 11; worst case
  ≈ 11 × 21 MB (5 MiB of 4-byte characters, S05 §16) ≈ 231 MB. Holding all 30 bodies until extraction would be
  ≈ 630 MB per job — rejected (R2).
- Extraction peak ≤ 2 × 73 MB (S05 benchmark `large_text`); stored documents ≤ 30 M chars per job (≤ 120 MB in the
  4-byte worst case); S06 ≤ a few MB (P6).
- These are estimates from S04/S05/S06 evidence, to be measured (§8.4).

### 8.3 Time

| Layer | Mechanism | Value |
|---|---|---|
| Per URL | S04 budget, starts when admission grants the slot | 15 s (unchanged) |
| Per document | S05 cooperative budget | 10 s (unchanged) |
| CRAWLING stage | `deadline = now + min(stage timeout, job deadline − now)` given to admission waits, every `fetch` (`CrawlContext.deadline`) and every `aextract`; a backstop timeout around the capture loop cancels only in-flight work, never captured slots | 180 s (R3): 30 ÷ 5 × 15 s = 90 s if every fetch hangs on distinct hosts, extraction pipelined (worst observed 5.3 s/document, S05), ×2 headroom |
| NORMALIZING | `asyncio.to_thread(group_documents, …)`; boundaries check cancel/deadline | no S06 timeout (P6: 0.073 s at the maximum) |
| Job | `JOB_TIMEOUT_S` 600 | unchanged |

### 8.4 Measurement method and acceptance proposal [RECOMMENDATION D2b]

Method (as S05/S06): one subprocess per scenario, inputs built first, VmHWM reset, best of 3, local server only.

| Scenario | Proposed threshold |
|---|---|
| (a) 30 sources, 100 KB HTML each, no latency | CRAWLING ≤ 10 s; peak RSS growth ≤ 128 MB |
| (b) 30 sources × 5 MiB of 4-byte text | completes; documents `PARTIAL`/`TEXT_TRUNCATED` as S05 defines; peak RSS growth ≤ 512 MB; bodies held ≤ 11 (instrumented) |
| (c) NORMALIZING at 30 M chars | ≤ 1 s |
| (d) 2 concurrent jobs × (a) | no queue-caused `FETCH_TIMEOUT`; peak RSS growth ≤ 256 MB |

Thresholds are regression bounds on this sandbox, not runtime limits; final values are set after measurement.

## 9. Admission control for S04-F1 [APPROVED DECISION D4; limits RECOMMENDATION R1]

### 9.1 Where queue time enters the S04 budget

| Source (C6–C8) | Removed by admission? |
|---|---|
| Global semaphore wait (more fetches than `CRAWL_CONCURRENCY` inside the crawler) | **yes** — admission never lets more than the global limit into the crawler, across all jobs (P1b) |
| Per-host slot wait for the **initial** host (several targets on one host) | **yes** — per-host admission with S04's own host key (P5) |
| Per-host slot wait on a **redirect hop** into a host busy with another admitted fetch | **no** — admission cannot know redirect targets before the fetch (P7) |
| Per-host minimum interval (≤ 1 s per request, inside the slot) | **no** — S04 politeness; bounded by `per_host_min_interval_s` |
| robots.txt lock wait (another fetch fetching the same origin's robots) | initial origin: yes (same as per-host); redirect into an origin: **no**; bounded by `CRAWL_ROBOTS_TIMEOUT_S` (5 s) |
| Retry backoff | not queue time; S04 contract (inside budget by design) |

### 9.2 Limits

| Scope | Limit | Notes |
|---|---|---|
| Process (all jobs) | in-flight fetches ≤ `CRAWL_CONCURRENCY` (5) | one `FetchAdmission` around the one shared `Crawler` (shared robots cache and politeness are correct per host) |
| Per host | in-flight fetches ≤ `CRAWL_PER_HOST_CONCURRENCY` (1) by **initial** host | host key computed with S04's `check_url` (read-only use, no S04 change); a URL `check_url` rejects is admitted with a global slot only (S04 returns `POLICY_BLOCKED`/`SSRF_BLOCKED` without network) |
| Per job | in-flight fetches ≤ ⌈5 ÷ `max_concurrent_jobs`⌉ = 3 | prevents one job's 30 targets from starving another job's stage timeout; grant order: lowest pending position within a job, round-robin across jobs |
| Extraction | ≤ `EXTRACT_CONCURRENCY` (2, S05) running, ≤ 4 waiting (R2) | admission pauses new fetches while the backlog is full |

Time spent waiting for admission counts against the **stage** deadline (wall clock), not the S04 per-URL budget.

### 9.3 Slots and partial results

Per position: `PENDING → WAITING → FETCHING → FETCHED → EXTRACTING → EXTRACTED`. A `FetchRecord` is captured in
slot p when the fetch returns, before any further await; the document is captured when extraction returns; the
body is released right after extraction. The runner persists progress after each capture (≤ 60 writes per job).
Captured slots are never removed by a later timeout, cancel or stage error.

### 9.4 Timeouts

- Stage deadline reached while a target is `WAITING` → `NOT_RUN` (reason `STAGE_TIMEOUT` or `JOB_DEADLINE`); no
  `FetchResult` is fabricated, no document exists, the position is skipped in the S06 input.
- An admitted fetch receives `CrawlContext(deadline = stage deadline)`; S04 returns `FETCH_TIMEOUT` when it is
  reached (P3) — a genuine stage cut, recorded as such.
- Admission is not granted when the remaining stage time is ≤ 0.

### 9.5 Cancellation

- Job cancel or stage stop: `WAITING` → `NOT_RUN`; `FETCHING` → task cancelled, S04 propagates `CancelledError`,
  slot `INTERRUPTED` (no `FetchResult`); `EXTRACTING` → S05 stops its worker, slot stays `FETCHED` (fetch record
  kept, no document).
- Every admission slot is released in `finally` (cancellation, exception, success); counters return to zero.
- Runner-task cancellation (shutdown) stays `RUNNER_INTERRUPTED` (C14).

### 9.6 Exceptions

An unexpected exception from admission, `Crawler.fetch` or a join check stops the stage (D7): other in-flight
work is cancelled and awaited, slots released, captured slots kept, job `FAILED/CRAWL_FAILED`; from `aextract`:
`EXTRACTION_FAILED`.

### 9.7 Residual and the S04 question [OPEN — FOUNDER DECISION OD-A]

Admission control is **not sufficient to remove S04-F1 completely without changing S04**. P7 shows a spurious
`FETCH_TIMEOUT` when a redirect hop enters a host whose slot another admitted fetch holds; the wait is bounded by
that other request's duration (up to its own 15 s budget) plus the minimum interval. Same-origin robots waits on
redirects behave alike (≤ 5 s). The frequency in real search results is unknown (typical trigger: `example.com`
→ `www.example.com` while another selected URL on `www.example.com` is in flight).

| Option | Effect | Cost |
|---|---|---|
| A1 — accept the residual for S07 | admission as designed; tests assert the over-submission cases are fixed and that the redirect residual is **reported**, not hidden (§9.8 A8); live/benchmark runs count `FETCH_TIMEOUT` with a cross-host redirect chain | spurious timeouts remain possible on busy redirect targets |
| A2 — reopen S04 (minimal) | per-URL budget excludes waits for the global semaphore and per-host slots (budget measured over network work only); S04 design, tests and mutation updated | changes frozen S04; needs its own design/approval |

Recommendation: A1 for S07 with measurement; A2 if the measured rate of redirect-caused timeouts is material.
Until OD-A is decided, S04-F1 is recorded as **partially addressed by design, not resolved**.

### 9.8 Admission test matrix (to add; none written or run)

| Id | Case | Expected |
|---|---|---|
| A1 | 6 targets completing in reverse order | slots in position order; documents and S06 input in position order |
| A2 | long queue: 30 targets on distinct hosts, global 5, pages 0.6 s, budget 1.0 s | all `OK`; max in-flight = 5; no `FETCH_TIMEOUT` (P1 regression) |
| A3 | per-host contention: 6 targets on one host, budget 1.0 s, page 0.6 s | all `OK`; host in-flight ≤ 1 (P5 regression) |
| A4 | two jobs sharing admission, 30 targets each | per-job in-flight ≤ 3; both progress; no queue-caused `FETCH_TIMEOUT` |
| A5 | stage deadline reached while targets wait | waiting → `NOT_RUN`; in-flight → `FETCH_TIMEOUT` via context deadline; captured kept |
| A6 | cancel while waiting / fetching / extracting | `NOT_RUN` / `INTERRUPTED` / `FETCHED`; captured kept; extraction worker stopped |
| A7 | `fetch` raises unexpectedly | `CRAWL_FAILED`; other work cancelled; captured kept |
| A8 | P7 redirect contention | redirected fetch `FETCH_TIMEOUT` is counted as redirect-contention residual in stage counts (documented limitation, OD-A) |
| A9 | URL rejected by `check_url` | admitted with global slot only; `POLICY_BLOCKED`/`SSRF_BLOCKED` without network |
| A10 | slow extractor | bodies held ≤ backlog + extracting; admission pauses |
| A11 | 1000 seeded random cancel/timeout/exception schedules | admission counters return to 0; no leaked slot; no stuck job |
| A12 | seeded random completion delays × 20 runs | identical `DedupRecord` (ids/timestamps excluded); representatives = min positions |

## 10. Error, terminal status and cancellation

### 10.1 Error classes [APPROVED DECISION D7, D8]

| Class | Examples | Handling |
|---|---|---|
| Per-URL fetch outcome | `HTTP_ERROR`, `TLS_ERROR`, `ROBOTS_BLOCKED`, `SSRF_BLOCKED`, `FETCH_TIMEOUT`, `RESPONSE_TOO_LARGE` | `FetchRecord`; `NOT_FETCHED` document still produced (C10); warning `FETCH_FAILURES` (per-status counts) |
| Per-document extraction outcome | `PARTIAL`, `EMPTY`, `UNSUPPORTED`, `FAILED` | kept; warning `EXTRACTION_DEGRADED` (counts) |
| Per-document S06 errors | non-empty `DocumentSet.errors` on S05 output | S06 output kept; warning `DEDUP_CONTRACT_ERRORS` (counts) |
| Stage error | unexpected exception (fetch side / extractor / S06), join-check failure, runtime budget counter | stage stops, captured kept; job `FAILED`: `CRAWL_FAILED`, `EXTRACTION_FAILED`, `DEDUP_FAILED`, `PIPELINE_BUDGET_EXCEEDED` (category `INTERNAL_ERROR`); never an empty successful result |

Messages are fixed text (no URL, page text or exception text), as in S02.

### 10.2 Final status for pipeline jobs [APPROVED DECISION D8], evaluated in order

| # | Condition | Status | Recorded |
|---|---|---|---|
| 1 | cancel requested (any stage) | `CANCELLED` | everything captured (search, sources, documents, dedup if finished); unfinished slots `NOT_RUN`/`INTERRUPTED` |
| 2 | stage error | `FAILED` | error + step; captured kept — ranking against row 1: OD-B (§10.3) |
| 3 | SEARCHING: no query covered | `FAILED` | `ALL_SEARCH_PROVIDERS_FAILED` (S02 unchanged); later stages `NOT_RUN` |
| 4a | job deadline during SEARCHING | S02 rule: `PARTIAL` if ≥ 1 query covered, else `FAILED` | `JOB_DEADLINE_EXCEEDED`; CRAWLING/NORMALIZING `NOT_RUN` |
| 4b | job deadline in/after CRAWLING | `PARTIAL` if ≥ 1 `SUCCESS`/`PARTIAL` document, else `FAILED` | `JOB_DEADLINE_EXCEEDED`; NORMALIZING `NOT_RUN` if not started |
| 5 | CRAWLING stage timeout | NORMALIZING runs on captured documents; final `PARTIAL` (or row 8 if no usable document) | warning `CRAWL_STAGE_TIMEOUT` |
| 6 | SEARCHING stage timeout or incomplete coverage with ≥ 1 query covered | pipeline continues; final at best `PARTIAL` | S02 warnings carried |
| 7 | search covered, 0 results | `COMPLETED` | 0 sources; CRAWLING/NORMALIZING `COMPLETED` with 0 |
| 8 | ≥ 1 source selected, no `SUCCESS`/`PARTIAL` document | `FAILED` | **`NO_DOCUMENTS`** + `FETCH_FAILURES`/`EXTRACTION_DEGRADED` counts |
| 9 | otherwise | `COMPLETED` | warnings `FETCH_FAILURES` / `EXTRACTION_DEGRADED` / `DEDUP_CONTRACT_ERRORS` if any |

Precedence cancel > job deadline > stage timeout > result (C14) holds across stages. Per-URL failures alone never
make a job `PARTIAL` (mirrors S02 `PROVIDER_FAILURES`).

### 10.3 Stage error vs a recorded cancel [OPEN — FOUNDER DECISION OD-B]

Evidence (C15): today a planner or search-stage exception finalizes `FAILED/INTERNAL_ERROR` even when
`cancel_requested` is already set; only a cancel whose write races the terminal write wins (CAS conflict path).
D8's precedence list does not mention stage errors.

| Option | Effect |
|---|---|
| B1 — same as S02 for the new stages | a stage error is `FAILED` even with a prior cancel; consistent across all stages; no S02 change |
| B2 — cancel first in the new stages only | `CANCELLED` with the stage error kept as a warning; inconsistent with PLANNING/SEARCHING unless S02 is also changed |
| B3 — cancel first in all stages | requires changing frozen S02 behaviour (`_planning`, `_searching`) and its tests' expectations — not allowed without a reproduced defect |

Recommendation: B1 (no frozen behaviour changes; the exception stays visible).

## 11. Provenance and evidence independence

- **Chain** (object identity / ids, checked §7): `DedupedSearchResult` (providers, queries) → `SourceRecord.position`
  → `FetchResult.source` (canonical hit) → `FetchRecord.crawl_id` → `ExtractedDocument.provenance` (crawl id,
  URLs, redirect chain, `content_sha256`, `source`) → `DocumentSet.refs[i]` with `positions[i]` → groups.
- **Requirements handed to the verification stage** (nothing implemented here): URL, host or provider counts are
  not evidence counts; one L2 or L3 group = one evidence unit; `CROSS_SOURCE_DUPLICATE` marks copies;
  a member without source identity is an unknown source; `RAW_DUPLICATE_TEXT_DIFFERS` (or its absence) is never
  used to accept independence or to attribute text across L2 members — cross-member attribution only via L3
  (S06 F-2 open; S06 production unchanged); ARCHITECTURE §10's `n_independent_domains` must be redefined over
  evidence groups (X-5).
- **Bot challenges** (OD-11): duplicated challenge pages group at L2/L3 and are flagged; a single challenge page is
  not flagged and reaches later stages as a `SUCCESS` document. Residual risk.
- Every document stays `trust="UNTRUSTED"`; page content never drives control flow.

## 12. JobRunner additive change plan [D5 APPROVED as plan; execution OPEN — OD-C]

### 12.1 Method-by-method (`jobs/runner.py`)

| Method / attribute | Change | S02 path |
|---|---|---|
| `__init__` | new keyword-only optional parameters `sources: SourceCollector | None = None`, `pipeline: PipelineSettings | None = None`, `deduplicate: Callable[[Sequence[ExtractedDocument]], DocumentSet] = group_documents` (injectable for `DEDUP_FAILED` tests); `stages` derived: `PIPELINE_STAGES` only if `pipeline.enabled` and `sources` given | defaults reproduce today's runner exactly |
| `_inflight` | type widened to `dict[str, asyncio.Task[Any]]` (holds the query task in SEARCHING, the stage task in CRAWLING / NORMALIZING) | same use |
| `submit` | passes the runner's configured `stages` to `repository.create` (already supported, C12); idempotent hits return the stored job unchanged | stages = `SPRINT_02_STAGES` |
| `cancel` | unchanged (cancels whatever task is registered) | unchanged |
| `run` | accepts `SPRINT_02_STAGES` or `PIPELINE_STAGES` (the latter only when configured) | unchanged |
| `_execute` | after SEARCHING: if the job is non-terminal and has CRAWLING → `_crawling` → `_normalizing` | unchanged |
| `_searching` | at the end, S02 jobs call the existing `_conclude`; pipeline jobs call new `_conclude_search` (rows 1–4a and 6 of §10.2), then `_boundary` | S02 branch byte-for-byte the same |
| `_crawling` (new) | `_save_update` to `CRAWLING` + progress; select (D2); register stage task in `_inflight`; capture loop with a job re-read before each admission (terminal → stop, `FINALIZED_ELSEWHERE`; cancel → stop); capture each slot before further awaits; progress save per capture; stage deadline per §8.3; errors per §10.1 | — |
| `_normalizing` (new) | `_boundary` (cancel/deadline); `_save_update` to `NORMALIZING`; `asyncio.to_thread(deduplicate, documents)`; `DEDUP_FAILED` on exception; capture `DedupRecord`; `_conclude_pipeline` (§10.2) | — |
| `_finish` | progress block fills the new optional counts when pipeline data exist | same values for S02 jobs |
| `_error` | default categories for the new codes | unchanged for existing codes |
| `_boundary`, `_save_update`, `_record_runner_interrupted`, `_finish_cancelled`, `_run_queries`, `_run_one`, `_result` | unchanged | unchanged |

### 12.2 Models (`jobs/models.py`) — additive [D6 APPROVED; list RECOMMENDATION D6a]

- `PIPELINE_STAGES = (PLANNING, SEARCHING, CRAWLING, NORMALIZING)`; `SPRINT_02_STAGES` unchanged.
- `JobErrorCode` += `CRAWL_STAGE_TIMEOUT`, `FETCH_FAILURES`, `EXTRACTION_DEGRADED`, `NO_DOCUMENTS`, `CRAWL_FAILED`,
  `EXTRACTION_FAILED`, `DEDUP_FAILED`, `DEDUP_CONTRACT_ERRORS`, `PIPELINE_BUDGET_EXCEEDED`.
- `JobResult` += `sources: list[SourceRecord] = []`, `documents: list[ExtractedDocument] = []`,
  `dedup: DedupRecord | None = None`, `stage_outcomes: list[StageOutcome] = []`, `not_selected: int = 0`.
- `JobProgress` += `urls_total`, `urls_done`, `fetch_failed`, `documents`, `groups` (all `int = 0`).
- No field renamed, removed or re-typed; `extra="forbid"` stays; S02 jobs serialize the new fields at defaults.

### 12.3 Regression gates before and during implementation

1. Before the first change: full suite on the implementation base = 845 passed / 5 skipped (recorded).
2. With the pipeline **disabled**: every existing test unchanged and passing; a new equivalence test runs scripted
   S02 jobs and compares stored jobs and event sequences with the `75ce79e` behaviour (status, error, warnings,
   result, progress, events).
3. Cancel-race and hardening suites extended to every new write site (CRAWLING transition, each capture save,
   NORMALIZING transition, terminal writes) and to the new loop (terminal before admission, during an in-flight
   fetch, during extraction, during NORMALIZING); seeded randomized suite: 0 stuck, 0 cancel → `FAILED` (except
   OD-B's stage-error case), no write after terminal.
4. Mutation of the new runner logic (captured slots dropped on cancel, loop-top terminal check removed, NORMALIZING
   after deadline, cancel registration missing for the stage task).

## 13. Files expected to change (implementation sprint)

| File | Change | Frozen? |
|---|---|---|
| `src/research_agent/pipeline/config.py`, `discovery.py`, `admission.py`, `sources.py`, `models.py` | new | no |
| `src/research_agent/jobs/models.py` | additive (§12.2) | S02 — OD-C |
| `src/research_agent/jobs/runner.py` | additive (§12.1) | S02 — OD-C |
| `src/research_agent/api/app.py`, `api/config.py`, `api/schemas.py` | wiring of one shared `Crawler` / `FetchAdmission` / `Extractor` (closed on shutdown), `PIPELINE_ENABLED`, additive views (§14) | S03 — OD-C |
| `tests/…` | new pipeline unit/integration/live tests and `tests/pipeline_support.py`; race suites extended by new tests (existing tests unchanged) | — |
| `docs/sprint-07-pipeline-integration-design.md` | as-built section | — |

Not changed: `crawler/` (unless OD-A = A2), `extraction/`, `dedup/`, `pipeline/search.py`, `core/`, `providers/`,
dependencies, ARCHITECTURE.md.

## 14. API compatibility [D9 APPROVED; fields RECOMMENDATION D9a]

- Status handling unchanged (C16): `CRAWLING`/`NORMALIZING` are non-terminal → `/results` 409 `JOB_NOT_FINISHED`.
- **Unchanged shapes** (asserted exactly by existing tests): `JobCreatedView`, `ResultSummaryView`.
- **Additive** (new optional keys, `null` for S02 jobs): `ResultsView.sources: list[SourceSummaryView] | None`
  (position, url, providers, fetch status, http status, final URL, extraction status, extraction warnings,
  document id, group keys per level), `ResultsView.dedup: DedupSummaryView | None` (group counts per level, groups
  as member positions + warnings, S06 error counts); `ProgressView` += optional `urls_total`, `urls_done`,
  `fetch_failed`, `documents`, `groups`.
- **Never exposed**: `ExtractedDocument.text`, titles/metadata from pages, `FetchResult.content`, raw JSON-LD.
- Error codes are already `str` in views; new codes need no schema change.
- With `PIPELINE_ENABLED=false` (default) every API response is identical to `75ce79e` except the new keys present
  as `null` on `/results`; tests assert this.

## 15. Test matrix

**EXISTS** = in the suite at `75ce79e` (passed there); **ADD** = to write in the implementation sprint (not
written, not run); **E2E** = requires the implementation. No ADD/E2E test is claimed to pass.

| Area | Case | Status |
|---|---|---|
| Search order | `to_response` sorting; order independent of provider completion; one entry per normalized URL with all providers/queries | EXISTS (`tests/unit/test_search_response.py`) |
| Fetch order | `fetch_many` keeps input order; per-host/global bounds | EXISTS (`test_per_host_and_global_concurrency_bounded`) |
| Selection | prefix of `response.results`; 30 cut; `not_selected`; 0 results | ADD |
| Admission | A1–A12 (§9.8) | ADD / E2E |
| Order E2E (D1) | reverse completion; seeded random completion × 20; representative = min position for every group of every level | E2E |
| Failures | one / several / all fetches fail → `NOT_FETCHED` documents in their slots; §10.2 rows 8/9 | E2E |
| Redirect | two sources redirect to one final URL → L1 group, representative = better-ranked; chain kept; P7 residual counted | E2E |
| Multi query / provider | same URL from 2 queries × 2 providers → fetched once; providers/queries kept | E2E |
| No document | stage timeout / cancel before fetch → `NOT_RUN`/`INTERRUPTED`, skipped, `positions` strictly increasing | E2E |
| Multi-level identity | L1 with one source, L3 with another → separate groups, correct representatives, no transitive merge | E2E |
| Join checks | injected mismatch → `CRAWL_FAILED`/`EXTRACTION_FAILED`, captured kept | ADD |
| Extraction | `PARTIAL`/`EMPTY`/`UNSUPPORTED`/`FAILED` kept; not in L3 | EXISTS for S05/S06 units; E2E in pipeline |
| L2/L3 | identical pages on 2 hosts → L2+L3, `CROSS_SOURCE_DUPLICATE`; same bytes, other charset → L2 only | EXISTS (`tests/integration/test_dedup.py`); E2E through the crawler |
| Errors | S06 raising → `DEDUP_FAILED`, sources/documents kept, no empty dedup; extractor raising → `EXTRACTION_FAILED`; crawler raising → `CRAWL_FAILED`; OD-B behaviour per decision | E2E |
| Terminal rules | every row of §10.2 | E2E |
| Cancellation / race | cancel at every new write site; OI1-DEF-01 windows for the new loop; seeded randomized suite | E2E (new tests in the race suites) |
| Deadlines | CRAWLING timeout → NORMALIZING on captured, `PARTIAL`; job deadline in CRAWLING → NORMALIZING `NOT_RUN`; precedence | E2E |
| Budgets | static check rejects `30 × 2 M`; runtime counter (injected) → `PIPELINE_BUDGET_EXCEEDED`; §8.4 benchmark | ADD + benchmark |
| Content lifetime | no `FetchResult.content` reachable from `JobResult` | ADD |
| Provenance | full chain per source; `search_source` identity; trust/kind | E2E |
| Disabled pipeline | S02-equivalence test (§12.3-2); API identical except `null` keys | ADD |
| API | summaries without text; 409 for CRAWLING/NORMALIZING | ADD |
| Live | opt-in pipeline job on egress-allowed hosts; PASS only with fetched documents (F-1 rule) | ADD (opt-in) |
| Regression | full S01–S06 suite unchanged | EXISTS (845 passed, 5 skipped at `75ce79e`) |
| Mutation | completion-order append; `fetch_many`/outer timeout; admission removed / per-host removed / per-job removed; join check removed; `positions` off by one; captured dropped on cancel; NORMALIZING after deadline; content retained; budget check removed | ADD (reported KILLED/SURVIVED/EQUIVALENT) |

## 16. Conflicts with ARCHITECTURE.md (reported; ARCHITECTURE not edited, D12)

| Id | Conflict | Disposition |
|---|---|---|
| X-1 | §21 lists Sprint 07 = Verification, 08 = Database; this integration is called Sprint 07; §21's note "Sprint 07 adds the Postgres implementation" is stale | **OPEN — FOUNDER DECISION** (D12a). Numbering unchanged by this work |
| X-2 | §13 says "FAILED if … zero documents fetched" and also "COMPLETED with 0 results is allowed (e.g. all sources blocked)" | pipeline jobs follow D8: 0 search results → `COMPLETED`; sources selected but no usable document → `FAILED/NO_DOCUMENTS` (all sources blocked included). RECOMMENDATION: align §13 text when ARCHITECTURE edits are approved |
| X-3 | stage name `EXTRACTING` could be read as S05 content extraction | D3: S05 runs in CRAWLING; `EXTRACTING` = AI extraction. RECOMMENDATION: add a clarifying sentence to ARCHITECTURE §3/§13 later |
| X-4 | §5 lists `CRAWL_MAX_DEPTH=1` and `CRAWL_RETRIES=2`; S04 contract: no link following, retries ≤ 1 (S04 OD-10) | integration uses depth 0 (search hits only) and S04's retries unchanged; RECOMMENDATION: mark §5 values as superseded by S04 when ARCHITECTURE is edited |
| X-5 | §10 confidence term `n_independent_domains` counts domains | must count independent evidence groups (§11); dependency of the verification stage; RECOMMENDATION only |

## 17. Open items (founder)

| Id | Question | Recommendation |
|---|---|---|
| OD-A | S04-F1 residual (P7): accept for S07 with measurement (A1) or reopen S04 to exclude queue waits from the per-URL budget (A2) | A1 now; A2 if measured material |
| OD-B | Stage error vs earlier cancel in the new stages (B1/B2/B3) | B1 (consistent with frozen S02) |
| OD-C | Permission to modify frozen `jobs/runner.py`, `jobs/models.py`, `api/app.py`, `api/config.py`, `api/schemas.py` additively per §12–§14 | grant, with the §12.3 gates |
| X-1 | Roadmap numbering | founder choice |
| D2a / D2b / D6a / D7a / D9a / R1–R5 | Approve the recommended values and lists (thresholds, codes, fields, admission limits, stage timeout) | approve as proposed; thresholds final after measurement |

## 18. Acceptance criteria (implementation sprint)

1. **D1**: all order E2E tests (§15) pass, including reverse and seeded random completion; representative = min
   position asserted for every group of every level.
2. **D2**: static bound enforced at wiring; runtime counter tested; S06 never called with more than 30 URLs' or 30 M
   chars' worth of documents; §8.4 measured and reported with the stated method.
3. **D4**: admission tests A1–A12 pass; S04-F1 reported as resolved for over-submission only, with the P7 residual
   handled per OD-A (counted, or S04 changed under its own approval).
4. **D7/D8**: every §10.1/§10.2 row tested; no stage error yields an empty successful result; captured results present
   after every stage error, cancel and timeout; OD-B behaviour as decided.
5. **D5/D6/D10**: pipeline disabled ⇒ S02-equivalence test and the full existing suite pass unchanged; all model
   changes additive.
6. **Race-safety**: race suites extended to all new write sites and loops; seeded randomized suite: 0 stuck jobs,
   no write after terminal, cancel never `FAILED` except the OD-B case as decided.
7. **D9**: API summaries without page text; exact-shape views unchanged.
8. Provenance chain test passes; no `FetchResult.content` in `JobResult`.
9. Mutation (§15) reported mutant by mutant; no unexplained survivor.
10. Gates: full regression, ruff, mypy strict, bandit, pip-audit, secret scan, `git diff --check`; no new dependency.
11. Opt-in live pipeline test passes only with real fetched documents, or is reported as not run.

## 19. Implementation status and evidence (as built)

Founder decisions applied: **OD-A = A1** (admission control, S04 residual accepted and measured, S04 not
reopened), **OD-B = B2** (in CRAWLING/NORMALIZING a recorded cancel wins over a stage error; PLANNING/SEARCHING
unchanged), **OD-C** (additive changes to `jobs/runner.py`, `jobs/models.py`, `api/app.py`, `api/schemas.py`),
**X-1** (roadmap numbering unchanged). Baseline before the change: `12dadc7` (code = `75ce79e`), 845 passed /
5 skipped.

**Files.** New: `pipeline/config.py` (`PipelineSettings`, prefix `PIPELINE_`, `enabled=False`, `max_urls ≤ 30`,
`max_total_text_chars ≤ 30 000 000`, static check), `pipeline/models.py` (`FetchRecord`, `SourceRecord`,
`DedupRecord`, `StageOutcome`, `CrawlStats`), `pipeline/discovery.py` (`select_sources`), `pipeline/admission.py`
(`FetchAdmission`, `BodyBudget`), `pipeline/sources.py` (`Pipeline`, `SourceCapture`, `collect_sources`,
`StageFailure`). Additive: `jobs/models.py` (`PIPELINE_STAGES`, 9 error codes, `JobResult`/`JobProgress` fields),
`jobs/runner.py` (`pipeline=` parameter; new `_conclude_search`, `_enter_stage`, `_finish_deadline`,
`_finish_partial`, `_crawling`, `_normalizing`, `_conclude_pipeline`; `_finish` adds pipeline counts — empty for
Sprint 02 results), `api/app.py` (one shared crawler/admission/extractor when enabled; closed on shutdown),
`api/schemas.py` (`ResultsView.sources/dedup`, `ProgressView` counts — `null` for Sprint 02 jobs). Not changed:
S01, S04, S05, S06 code, `jobs/state_machine.py`, `jobs/repository.py`, `api/config.py`, dependencies,
ARCHITECTURE.md, every existing test.

**As-built deviations from the design text (reported, no decision changed).**

| # | Design text | As built | Reason / evidence |
|---|---|---|---|
| B-1 | §10.2 rows 4a/4b: "PARTIAL … NORMALIZING `NOT_RUN`" | before PARTIAL, the remaining configured stages are **entered without running** (`_finish_partial`: transitions through `_save_update`, outcome `NOT_RUN`, no dedup), so the state path shows e.g. `CRAWLING → NORMALIZING → PARTIAL` | the frozen state machine (`jobs/state_machine.py`, not in OD-C) allows `PARTIAL`/`COMPLETED` only from the last configured stage; `CRAWLING → PARTIAL` raised `InvalidJobTransitionError` in the first test run. Covered by tests (row 4a, 4b, clock-skew case, cancel on a skip transition) and mutant M24 |
| B-2 | §12.1: re-read the job before each admission | re-read **after the admission grant, immediately before each fetch** (`before_fetch`), plus one read before the stage task runs | the read closest to the fetch is the one that guarantees "no fetch after a terminal state / cancel"; the pre-start read keeps a cancelled job from taking shared slots (mutants M18, M19) |
| B-3 | §8.2: backlog ≤ 4 waiting bodies | one `BodyBudget` per process, capacity = global fetches + backlog + extraction concurrency (5 + 4 + 2 = 11), taken after admission and released after extraction | a body is counted from fetch to end of extraction; measured `bodies_max_held` = 10 in the benchmarks |
| B-4 | categories not fixed | `FETCH_FAILURES`/`NO_DOCUMENTS` → `FETCH_ERROR`, `EXTRACTION_DEGRADED` → `EXTRACTION_ERROR`, `CRAWL_STAGE_TIMEOUT` → `TIMEOUT`, stage errors and `DEDUP_CONTRACT_ERRORS` → `INTERNAL_ERROR` | documented in `JobError` |
| B-5 | B2 | when a cancel wins over a stage error, the stage error is kept as a **warning** of the `CANCELLED` job | the exception stays visible |
| B-6 | §6 lists `api/config.py` | unchanged; the flag is `PIPELINE_ENABLED` in `PipelineSettings` | separate settings class, as for S04/S05 |

**Defects found and fixed during implementation (new S07 code, before commit).** (1) `FetchAdmission` granted a
slot to a waiter whose task had been cancelled while queued (asyncio cancels the awaited future before the
coroutine runs again) → `InvalidStateError` and a leaked slot; found by the seeded randomized test (A11, seed 2),
fixed by dropping done futures before granting; mutant M05. (2) Timeout classification ignored the redirect
`Location` (S04 records no final URL on a timeout), so P7 residuals were counted as "other"; found by A8, fixed;
mutant M30. Test-harness corrections (not product code): benchmark body sized by UTF-8 bytes (was over the 5 MiB
cap), more test hosts, one racy test expectation.

**Tests.** 76 new tests: unit admission 9, unit pipeline 9, integration E2E 42, admission A2–A10 7, API 5,
benchmark 4 (+ 1 opt-in live). Full suite: **921 passed, 6 skipped** (skips: 4 `live_network` without
`RUN_LIVE_NETWORK=1`, 2 live search without API keys). No existing test was edited.

| Area | Evidence |
|---|---|
| OD-13 order | reverse completion (6 sources) and seeded random completion (20 runs, identical results); slots by position; `positions` strictly increasing incl. a gap (NOT_RUN between finished positions); representative = min source position asserted for every group; multi query × provider (fetched once, providers/queries kept); redirects to one final URL (better-ranked representative although it finished last); L1 vs L3 groups with no transitive merge |
| Provenance | `document.provenance.source is response.results[p].result` and `DocumentRef.search_source` identity end to end; crawl ids joined; join mismatches → `CRAWL_FAILED` / `EXTRACTION_FAILED` |
| Errors / status | per-URL failures (404, robots, timeout) → warning `FETCH_FAILURES`, `NOT_FETCHED` documents in their slots; `NO_DOCUMENTS` → FAILED; 0 results → COMPLETED; fetch / extractor / dedup exceptions → stage errors with captured results; runtime budget → `PIPELINE_BUDGET_EXCEEDED`, nothing truncated |
| Cancellation / race | cancel during fetch, while waiting, during extraction; B2 in CRAWLING and NORMALIZING; SEARCHING unchanged under B2; cancel landing on every new write site (CRAWLING / NORMALIZING transitions, each capture save, COMPLETED, FAILED, skip transition); finalized elsewhere before and during CRAWLING (no later fetch, nothing written after); runner shutdown → `RUNNER_INTERRUPTED` with captured results; slots and bodies back to 0 |
| Deadlines | crawl stage timeout → NORMALIZING on captured documents, PARTIAL; job deadline in SEARCHING (row 4a) and CRAWLING (row 4b, incl. a frozen-clock case); search stage timeout with coverage continues |
| Admission (A1–A12) | A2: 30 targets, 5 slots, 0.6 s pages, 1.0 s budget → 30 OK, 0 `FETCH_TIMEOUT` (P1 regression); A3: 6 same-host → all OK, host in-flight 1 (P5 regression); A4: two jobs → per-job ≤ 3, both complete, 0 timeouts; A5: stage deadline while waiting → `NOT_RUN`, deadline-capped timeout counted; A8: P7 residual reproduced through the pipeline and counted in `fetch_timeout_cross_host_redirect`; A9: policy-rejected URL needs no host slot, no network; A10: backpressure (`max_held` = capacity during slow extraction); A11: 1000 seeded schedules, no leaked slot; A1/A6/A7/A12 in the E2E suite |
| Pipeline off | runner with a disabled `Pipeline` = runner without one (stored job, events, stages); API: `JobCreatedView` and `result_summary` unchanged, new keys `null`; the 845 baseline tests unchanged and passing |

**Mutation (scratch copy, new tests + S02 runner/race/API suites): 31 mutants, 31 KILLED, 0 SURVIVED, 0
EQUIVALENT.** Admission: per-host / per-job limits ignored, no round-robin, FIFO instead of lowest position,
cancelled waiters granted, grant/cancel race not released, release not idempotent, limits not from the crawler,
body released before extraction. Slots/joins: completion-order storage, dense position map, join checks removed,
results captured only at the end. Cancellation/race: B2 removed (CRAWLING, NORMALIZING), captured results dropped
on cancel, pre-start check removed, before-fetch re-read removed, stage task not registered for `cancel()`.
Transitions: NORMALIZING after a job deadline, stage timeout not degrading, `NO_DOCUMENTS` removed, PARTIAL without
the stage walk, search deadline through the Sprint 02 conclusion, search timeout ending the job. Budget/flag:
runtime and static text checks removed, flag ignored, redirect `Location` ignored, selection not cut. In the first
run M18, M19 and M21 survived; the tests that were missing (cancel/finalize *during* CRAWLING, pre-start cancel,
clock skew) were added and the full matrix re-run.

**Benchmark (§8.4 method; subprocess per scenario, VmHWM reset after setup, local server).**

| Scenario | Result | Proposed threshold |
|---|---|---|
| (a) 30 sources × ≈ 100 KB HTML | 0.57–0.60 s (whole job), peak RSS growth 11.4–12.1 MB, 30 OK, 0 timeouts | ≤ 10 s, ≤ 128 MB — met |
| (b) 30 sources × 5 MiB (≈ 2.3 M chars, mostly 4-byte UTF-8) | 8.65–8.67 s, 185.9–186.3 MB, 30 `PARTIAL`/`TEXT_TRUNCATED`, text exactly 30 000 000 chars, bodies held ≤ 10 | ≤ 512 MB — met |
| (c) NORMALIZING, 30 × 1 M chars (`to_thread`) | 0.070 s, 1.4 MB | ≤ 1 s — met |
| (d) 2 jobs × (a) concurrently | 1.18–1.19 s, 19.1–19.2 MB, 60 OK, 0 timeouts | ≤ 256 MB — met |

Machine-dependent; asserted as regression bounds in `tests/integration/test_pipeline_benchmark.py`.

**Real web.** Opt-in `tests/live/test_pipeline_live.py` (scripted search hits, real S04/S05/S06, TLS verification on
with `CRAWL_CA_BUNDLE`, egress policy not bypassed): PASS — 4 hits → 3 sources (the `utm_source` variant merged by
S01 before fetching), 3/3 fetched `OK`, 3/3 `SUCCESS`, COMPLETED, 0 `FETCH_TIMEOUT`; admission serialized the
three same-host fetches (max admission wait 2.0 s, outside the S04 budget). Without `CRAWL_CA_BUNDLE` the same test
FAILS at the fetch check (F-1 rule: no PASS without fetched documents).

**Feature flag.** `PIPELINE_ENABLED` defaults to **false**; with it off the API creates Sprint 02 jobs exactly as
before. Enabling it with `max_urls × EXTRACT_MAX_TEXT_CHARS > 30 000 000` fails the startup.

**Residual risks / not verified.**
- **S04-F1 residual (OD-A = A1, accepted):** a redirect hop into a host another admitted fetch is using still waits
  inside the S04 budget (P7, A8: reproduced and counted). Its frequency on real search results is **not measured**
  beyond the 3-source live run (0 timeouts); `CrawlStats.fetch_timeout_cross_host_redirect` and the
  `job.stage_finished` log make it measurable. Per-host minimum interval and robots-lock waits also remain inside
  the S04 budget (not separately distinguishable from evidence; counted as `other`/`deadline_capped`).
- Memory with `max_concurrent_jobs` jobs all at the worst-case page size was not measured (only (b) for one job and
  (d) with small pages); worst case ≈ 2 × (b) is an estimate.
- Persistence: in-memory repository only; documents (text) are held in `JobResult` until the process ends;
  PERSISTENCE-BLOCKER-01 unchanged.
- Bot-challenge pages (OD-11) and S06 F-2 unchanged; evidence-independence requirements of §11 are for the
  verification stage and not enforced here.
