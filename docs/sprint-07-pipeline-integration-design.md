# Sprint 07 — Pipeline integration: Search → Crawler → Extraction → Dedup (design)

Status: **DESIGN — not implemented.** No production code, test or dependency change in this sprint.
Decision classes (as in Sprint 06): **[EXISTING CONTRACT]** = fixed by frozen code or an approved design;
**[RECOMMENDATION]** = proposed here, not approved; **[OPEN]** = undecided, founder decision required (§16).
Nothing in this document is an approved decision yet. Evidence ids `P1…P6` are probes run for this design
(scratch scripts against local 127.0.0.1 servers, not committed); `E…` ids refer to earlier sprint documents.

## 1. Baseline

| Item | Value |
|---|---|
| Commit | `75ce79e` (S06 implementation `ae7218f` + post-review fix F-1); local HEAD = remote branch HEAD, tree clean |
| Full suite at baseline | 845 passed, 5 skipped (3 `live_network` without `RUN_LIVE_NETWORK=1`; 2 live search without API keys) — run for this design |
| Frozen components | S01 search (`pipeline/search.py`, `core/`), S02 jobs (`jobs/`), S03 API (`api/`), S04 crawler (`crawler/`), S05 extraction (`extraction/`), S06 dedup (`dedup/`) |
| Read for this design | ARCHITECTURE.md; docs/sprint-02-research-job.md, sprint-03-research-api.md, sprint-04-crawler.md, sprint-05-extraction-design.md, sprint-06-normalization-dedup-design.md (there is no separate S01 design document: S01 contracts are read from code and ARCHITECTURE §4.2); code of `SearchService`/`SearchRun`, `Crawler.fetch`/`fetch_many`, `Extractor.aextract`, `group_documents`, `JobRunner`, job models, state machine, API wiring |

## 2. Scope and non-goals

**Scope (implementation sprint, after approval):** a job type whose configured stages run
`PLANNING → SEARCHING → CRAWLING → NORMALIZING`, where CRAWLING = URL selection + S04 fetch + S05 content
extraction, and NORMALIZING = S06 grouping; end-to-end order contract (OD-13); resource budgets (OD-14);
stage error and cancellation semantics; provenance records linking every stage.

**Non-goals:** persistence / database (PERSISTENCE-BLOCKER-01 stays as is); AI structured extraction (the
`EXTRACTING` stage stays unconfigured); verification / S07-of-ARCHITECTURE (`VERIFYING` unconfigured);
synthesis / reporting; bot-challenge detection (OD-11); near-duplicates (L4); any change to S06 semantics
(F-2 stays open, §10); link following (`CRAWL_MAX_DEPTH`, ARCHITECTURE §5) — only search hits are fetched;
new dependencies.

## 3. Existing contracts (read from code at `75ce79e`)

| # | Contract | Source | Consequence for integration |
|---|---|---|---|
| C1 | `SearchRun.to_response()` orders hits by `(query index, provider priority, provider rank, arrival index)` and keeps the **first hit per normalized URL** as `DedupedSearchResult.result`, with all contributing `providers`/`queries` | `pipeline/search.py:122–158`; tests `test_to_response_sorts_out_of_order_hits`, `test_ordering_is_deterministic_regardless_of_completion_order` | this is the canonical rank order (§7). `SearchRun.results` (first occurrence in *append* order) is a different order and must not be used |
| C2 | The job runner calls `SearchService.search([query])` once per planned query, sequentially in plan order, and merges captured runs into `JobResult.response = merged.to_response()` | `jobs/runner.py:264–301, 434–466` (decision D3-B) | the job-level canonical order is `JobResult.response.results` |
| C3 | Providers set `rank = index` (1-based position in their list) | `providers/search/google.py:159`, `perplexity.py:136` | rank is provider-local; the cross-provider order is C1 |
| C4 | `FetchTarget.from_search_result(r)` fetches `r.original_url` and carries `r` as `source` unchanged; `FetchResult.source` is that object | `crawler/models.py:95–105` | provenance join is available on every result |
| C5 | `Crawler.fetch_many` = `asyncio.gather(fetch(t) for t in targets)`: results in **input order** | `crawler/crawler.py:328–332`; test `test_per_host_and_global_concurrency_bounded` | order is safe; cancellation / exception semantics are not (P3, P4) |
| C6 | `Crawler.fetch` per-URL budget `CRAWL_TIMEOUT_S` (15 s) is an `asyncio.timeout` that **encloses the wait for the global semaphore and the per-host slot**; `CrawlContext.deadline` caps the budget; failures are returned as `FetchStatus`, not raised | `crawler/crawler.py:334–364, 542–557` | queued URLs consume their budget while waiting (P1, P5) |
| C7 | S04 sets `content_sha256` only for `OK` fetches with a body; `FetchResult.content` holds the decoded body (≤ 5 MiB decoded bytes) | `crawler/crawler.py:694–715`; S06 §25 F-3 | S06 L2 sees hashes only for fetched documents; content must be dropped after extraction (§8) |
| C8 | `Extractor.aextract(fetch, context)` runs in a worker thread (≤ `EXTRACT_CONCURRENCY` = 2), honours `context.deadline`, propagates `CancelledError` after stopping the worker; never raises for page content (→ `FAILED`); returns exactly one `ExtractedDocument` per `FetchResult` (`NOT_FETCHED` for failed fetches) | `extraction/extractor.py:294–372`; test `test_aextract_cancellation_propagates_and_stops_worker` | one document per fetched target |
| C9 | `group_documents(documents)` is pure, synchronous, deterministic for the input sequence; representative = lowest input position; no limit, no deadline; per-document problems never raise; unexpected exceptions propagate | S06 design §0, §25 | caller owns order, volume and failure mapping |
| C10 | Job `stages` is any ordered subset of `STAGE_ORDER`; transitions only to the next configured stage or a terminal state | `jobs/models.py:33–48, 213–224`; `jobs/state_machine.py` | `(PLANNING, SEARCHING, CRAWLING, NORMALIZING)` is a valid job |
| C11 | `JobRunner.run` rejects any job whose stages ≠ `SPRINT_02_STAGES`; `JobResult` holds search data only; `JobErrorCode` is a closed `Literal` | `jobs/runner.py:143–147`; `jobs/models.py:132–189` | the implementation must extend frozen S02 code (§11) |
| C12 | Runner race-safety (OI-1, OI1-DEF-01): every write through `_save_update` (CAS + re-read), no write onto a terminal job, terminal check before each provider task, precedence cancel > deadline > stage timeout > outcome, runner-task cancel → `RUNNER_INTERRUPTED` | docs/sprint-03-research-api.md §22; tests `test_job_cancel_race*.py` | every new write site and loop must follow the same rules |
| C13 | API treats any non-terminal status as "not finished" (`is_terminal`), `/results` maps `JobResult` | `api/service.py:107`, `api/app.py:266` | new stages need no API status change; result exposure is a decision (§12) |

## 4. Probe evidence (this design)

All probes use the frozen S04 crawler with the S04 test harness (`tests/crawler_support.py`: local server,
`make_crawler`), 6 targets, robots.txt → 404 (allow).

| Id | Setup | Observation |
|---|---|---|
| P1 | `fetch_many`, 6 distinct hosts, `CRAWL_CONCURRENCY=2`, `CRAWL_TIMEOUT_S=1.0`, each page answers after 0.6 s | `OK, OK, FETCH_TIMEOUT ×4` in 1.04 s; the server saw 4 page requests (2 started late and were cut). Queue wait for the global semaphore consumed the per-URL budget |
| P1b | same load; caller admits ≤ 2 fetches at a time and calls `fetch` | `OK ×6` in 1.84 s |
| P2 | `fetch_many`, completion forced in reverse order (delays 0.5 … 0.0 s) | completion order f…a; result order a…f (input order) |
| P3 | stage limit 1.0 s; 2 pages fast, 4 pages 3 s | outer `asyncio.timeout` around `fetch_many` → `TimeoutError`, **all** results lost (incl. the 2 fetched); `CrawlContext(deadline=now+1.0)` → `OK, OK, FETCH_TIMEOUT ×4`, all results returned |
| P4 | `fetch_many` where one `fetch` raises `RuntimeError` | exception propagates; the 5 successful results are not returned |
| P5 | 6 URLs on one host, `CRAWL_PER_HOST_CONCURRENCY=1`, `CRAWL_TIMEOUT_S=1.0`, page 0.6 s | without caller admission: `OK, FETCH_TIMEOUT ×5`; with caller per-host admission (≤ 1): `OK ×6` in 3.66 s |
| P6 | S06 as-built, 30 documents × 1 000 000 chars (S05 default cap), best of 3, VmHWM reset | 30 M chars grouped and verified in 0.073 s; peak RSS growth 1.2 MB |

Finding **S04-F1** (reproduced, P1/P5): the per-URL budget includes queue time. Any caller that submits more
targets than free slots — `fetch_many` with > `CRAWL_CONCURRENCY` targets, several same-host targets, or two
jobs sharing one crawler — gets `FETCH_TIMEOUT`s that no remote server caused. The existing S04 test
(`test_per_host_and_global_concurrency_bounded`, 0.05 s pages, 5 s budget) does not reach this case.
Disposition: [RECOMMENDATION] solve it at the integration layer by admission control (§8.2), **no S04 change**;
reopening S04 to start the budget after slot acquisition is an alternative for the founder (OD-S07-2).

## 5. Architecture and data flow

```text
QUEUED → PLANNING → SEARCHING ───────────────→ CRAWLING ───────────────────────────────→ NORMALIZING → terminal
          planner     S01 per query (C2)        select(response.results, MAX_URLS)          S06 group_documents
                      JobResult.response        ├─ admit (global + per-host)  → S04 fetch    (worker thread)
                                                ├─ capture FetchRecord[p]
                                                ├─ S05 aextract → ExtractedDocument[p]
                                                └─ drop FetchResult.content
```

| Stage | Input | Output (captured in `JobResult`) | Component |
|---|---|---|---|
| SEARCHING | `plan.queries` | `response: SearchResponse` (unchanged) | S01 via runner (C2) |
| CRAWLING — select | `response.results` | `SourceRecord[p]` for p < `PIPELINE_MAX_URLS`; `not_selected` count | new `pipeline/discovery.py` |
| CRAWLING — fetch | `FetchTarget.from_search_result(results[p].result)` | `FetchRecord` (FetchResult without `content`) in slot p | S04 `Crawler.fetch` behind admission |
| CRAWLING — extract | `FetchResult` of slot p | `ExtractedDocument` in slot p | S05 `Extractor.aextract` |
| NORMALIZING | documents in ascending p | `DocumentSet` + `positions` map (§7) | S06 `group_documents` |

Stage mapping [RECOMMENDATION, OD-S07-1]: S05 content extraction runs inside **CRAWLING**, as ARCHITECTURE §3
places the parser (row 6) in `CRAWLING`; `EXTRACTING` remains reserved for AI structured extraction and is not
configured. S06 runs in **NORMALIZING** (ARCHITECTURE §3 row 9 puts dedup there). Fetch and extraction are
pipelined per source, so a document is extracted as soon as its fetch completes.

## 6. Interface contracts (new, integration layer)

[RECOMMENDATION] Names are proposals.

```text
PipelineSettings (env prefix PIPELINE_)          new, pipeline/config.py
  max_urls: int = 30                             # ARCHITECTURE §5 CRAWL_MAX_URLS, S04 design §12
  crawl_stage_timeout_s: float = 180             # §8.4
  max_total_text_chars: int = 30_000_000         # §8.3
  extraction_backlog: int = 4                    # §8.2 (2 × EXTRACT_CONCURRENCY)

select_sources(response: SearchResponse, max_urls) -> Selection          pipeline/discovery.py (pure)
  Selection.sources: list[DedupedSearchResult]   # response.results[:max_urls], same order
  Selection.not_selected: int                    # len(response.results) - len(sources)

FetchAdmission(crawler, concurrency, per_host)   pipeline/sources.py; ONE per process, shared by jobs
  async fetch(target, context) -> FetchResult    # waits for a global and a per-host slot (host of target.url),
                                                 # then calls Crawler.fetch: the S04 budget starts after the wait

collect_sources(selection, admission, extractor, *, context, on_source) -> SourceCapture
                                                 pipeline/sources.py
  per position p: admit → fetch → record → extract → record; on_source(p) after each capture
  never uses fetch_many; never wraps the crawler in an outer timeout (P3/P4)

SourceRecord (frozen)                            pipeline/models.py
  position: int                                  # index in Selection.sources (= index in response.results)
  url: str                                       # normalized search URL (DedupedSearchResult.result.url)
  providers, queries: list[str]                  # from DedupedSearchResult
  state: NOT_RUN | INTERRUPTED | FETCHED | EXTRACTED
  fetch: FetchRecord | None                      # crawl_id, requested_url, final_url, status, http_status,
                                                 # content_type, charset, content_length, content_sha256,
                                                 # redirect_chain, robots, resolved_ip, attempts, fetched_at,
                                                 # duration_ms, error — everything except `content`/`title`
  document_id: str | None

DedupRecord                                      pipeline/models.py
  positions: list[int]                           # positions[i] = source position of S06 input i (strictly increasing)
  result: DocumentSet                            # S06 output unchanged
```

`JobResult` gains optional, defaulted fields (`sources`, `documents`, `dedup`, `stages`) — additive, so Sprint
02/03 jobs and tests are unaffected (OD-S07-4). `ExtractedDocument`s are held in `JobResult.documents` by
position (text included, untrusted); `FetchResult.content` is never stored.

## 7. OD-13 — end-to-end order contract

[RECOMMENDATION] The contract below is the acceptance criterion for OD-13. It is a property of the
integration, proven by end-to-end tests (§13); that each component preserves its input order (C1, C5, C8, C9) is
necessary but **not** sufficient and is not claimed as proof.

1. **Search order.** The canonical rank order is `JobResult.response.results` (C1, C2): sorted by (query index in
   plan order, provider priority, provider rank, arrival index), one entry per S01-normalized URL, first entry
   wins; all contributing providers/queries are kept on the entry. "Best-ranked" means "lowest index in this
   list". Hits for the same normalized URL from several queries/providers are **one** source (search-level
   dedup happens before fetch; never fetched twice).
2. **Hit → fetch request.** `Selection.sources = response.results[:max_urls]`; source position `p` = list index.
   Target `p` = `FetchTarget.from_search_result(sources[p].result)` (fetches the provider's `original_url`,
   carries the canonical hit as `source`, C4).
3. **Parallel completion.** Fetches complete in any order. Each completed `FetchResult` is written to **slot
   `p`**; nothing is ever appended in completion order. Admission picks the lowest pending `p` whose host has a
   free slot (start order may differ from `p` order; storage order never does).
4. **Join FetchResult ↔ hit.** By slot. Checked, not assumed: `result.source is sources[p].result` and
   `result.requested_url == target.url`; a mismatch is a programming error → stage error (§9), never a silent
   re-pairing.
5. **Extraction order.** The document of slot `p` is stored in slot `p`; checked:
   `document.provenance.crawl_id == fetch.crawl_id`.
6. **S06 input.** `documents = [doc[p] for p in ascending order if doc[p] exists]`;
   `positions = [p for the same p]` (strictly increasing). Positions without a document (NOT_RUN, INTERRUPTED)
   are skipped and recorded, never filled with placeholders.
7. **Representative.** For every S06 group, `positions[group.representative]` is the minimum source position
   of its members (monotonic map of S06's lowest-input-position rule), i.e. the best-ranked hit by (1) — not the
   first fetch to finish. A test asserts this for every group of every level.

Consequences: a redirect target shared by two sources forms an L1 group whose representative is the better-ranked
source; a source whose fetch failed still yields a `NOT_FETCHED` document (C8) and keeps its position.

## 8. OD-14 — resource budget proposal

No value below is final; each is a [RECOMMENDATION] with its basis, the behaviour when exceeded, and the
measurement that the implementation sprint must report. S06 evidence uses the **as-built** measurements of
the S06 design §25 and P6 — not the E8 prototype figures.

### 8.1 Volumes

| Quantity | Bound today | Proposed | Basis | When exceeded |
|---|---|---|---|---|
| Search hits per job | `plan.queries` ≤ 8 × `max_results` ≤ 50 × providers (fallback: 1 per query; fanout: all). Today: FixedPlanner 1 query × default 10 (fallback) | no new limit | C2; `ResearchPlan`, `SearchOptions` | — |
| Unique search results | ≤ hits | — | C1 | — |
| URLs fetched per job | not bounded (S04 has no `MAX_URLS`) | `PIPELINE_MAX_URLS = 30` | ARCHITECTURE §5 `CRAWL_MAX_URLS=30`; S04 design §12 | deterministic cut in canonical order; the rest counted as `not_selected` (search results stay in `response`); not an error, not a warning |
| Raw content per URL | ≤ 5 MiB decoded bytes (`CRAWL_MAX_RESPONSE_BYTES`, C7) | unchanged | S04 | S04 status `RESPONSE_TOO_LARGE` |
| Raw content held at once | — | ≤ (in-flight fetches + backlog + extracting) bodies = 5 + 4 + 2 = 11 per process | §8.2 | admission waits (backpressure); no data dropped |
| Extracted text per document | ≤ 1 M chars default (`EXTRACT_MAX_TEXT_CHARS`, configurable ≤ 10 M) | unchanged | S05 §16 | S05 `PARTIAL` + `TEXT_TRUNCATED` |
| Extracted text per job | ≤ 30 × 1 M = 30 M chars by default, **but 300 M if `EXTRACT_MAX_TEXT_CHARS` is raised to 10 M** | `PIPELINE_MAX_TOTAL_TEXT_CHARS = 30 000 000`, enforced **statically** at wiring: `max_urls × EXTRACT_MAX_TEXT_CHARS ≤ max_total_text_chars`, else startup configuration error | P6 (S06 at exactly this size: 0.073 s, 1.2 MB); S05 default | cannot occur at runtime with a valid configuration; a runtime counter re-checks it and, if ever exceeded, raises a stage error `PIPELINE_BUDGET_EXCEEDED` — never truncation |

This closes the gap named in the brief: S06 keeps no limit of its own (S06 P12), and its input volume is bounded
by configuration before any job runs.

### 8.2 Concurrency, admission and memory

- **Admission** [RECOMMENDATION]: one process-wide `FetchAdmission` around the one shared `Crawler` admits a
  fetch only when a global slot (`CRAWL_CONCURRENCY`, 5) and a per-host slot (`CRAWL_PER_HOST_CONCURRENCY`, 1,
  host of the target URL) are free. Evidence: P1/P1b, P5. Process-wide because jobs share the crawler
  (`max_concurrent_jobs` = 2 by default); per-job admission would recreate S04-F1 across jobs. Residual: two
  targets whose redirects converge on one host can still queue inside S04 (counted as `FETCH_TIMEOUT`; tested,
  not prevented).
- **Backpressure**: at most `PIPELINE_EXTRACTION_BACKLOG` (4) fetched-but-not-extracted results per process;
  admission pauses while the backlog is full, so completed bodies cannot pile up.
- **Content lifetime**: `FetchResult.content` is released after extraction; only `FetchRecord` (no content) is
  kept. Holding all bodies until extraction would cost up to 30 × ≈ 21 MB (5 MiB of 4-byte characters, S05 §16)
  ≈ 630 MB per job — rejected.
- **Memory estimate (to be measured):** bodies ≤ 11 × ≈ 21 MB worst case ≈ 231 MB; extraction peak ≤ 2 × 73 MB
  (S05 benchmark worst `large_text`); stored documents ≤ 30 M chars (≤ 120 MB in the 4-byte worst case, typical
  ≪ 1 MB per document); S06 ≤ a few MB (P6; S06 §25: 24.2 MB single call at 10 000 documents).

### 8.3 Time

| Layer | Mechanism | Proposed | Basis |
|---|---|---|---|
| Per URL | S04 `CRAWL_TIMEOUT_S` (15 s), starts after admission | unchanged | C6, P1b |
| Per document | S05 `EXTRACT_TIMEOUT_S` (10 s, cooperative) | unchanged | C8 |
| CRAWLING stage | `CrawlContext(deadline = now + min(stage timeout, job deadline − now))` passed to every fetch and extraction; plus an outer `asyncio.timeout` **only as a backstop** around the capture loop, which cancels in-flight tasks but never discards captured slots | `PIPELINE_CRAWL_STAGE_TIMEOUT_S = 180` | 30 URLs ÷ 5 slots × 15 s = 90 s if every fetch hangs on distinct hosts; extraction overlaps (pipelined), worst observed 5.3 s per document (S05); headroom ×2. Same-host-heavy result sets can exceed it → cut, reported (§9) |
| NORMALIZING | `asyncio.to_thread(group_documents, …)`; no S06 timeout (S06 P12); deadline/cancel checked at the stage boundaries | none | P6: 0.073 s at the static maximum; a thread keeps the event loop (API, cancel) responsive |
| Job deadline | `JOB_TIMEOUT_S` = 600, unchanged; every stage runs under `min(stage limit, deadline − now)` | unchanged | S02 §8 |

Acceptance thresholds for the implementation benchmark [RECOMMENDATION — values for founder approval,
measured with the S05/S06 method: subprocess, best of 3, VmHWM reset]: (a) 30 sources on the local server,
100 KB HTML each, no latency: CRAWLING ≤ 10 s, peak RSS growth ≤ 128 MB; (b) 30 sources × 5 MiB of 4-byte
text: completes, every document `PARTIAL`/`TEXT_TRUNCATED` as S05 defines, peak RSS growth ≤ 512 MB;
(c) NORMALIZING ≤ 1 s at the static text maximum. These are regression bounds on this sandbox, not runtime
limits.

## 9. Error, cancellation and deadline semantics

### 9.1 Error classes

| Class | Examples | Handling |
|---|---|---|
| Per-URL fetch outcome | `HTTP_ERROR`, `TLS_ERROR`, `ROBOTS_BLOCKED`, `SSRF_BLOCKED`, `FETCH_TIMEOUT`, `RESPONSE_TOO_LARGE` | recorded in `FetchRecord`; a `NOT_FETCHED` document is still produced (C8); never a stage error |
| Per-document extraction outcome | `PARTIAL`, `EMPTY`, `UNSUPPORTED`, `FAILED` (+ warnings) | recorded in the document; never a stage error |
| Per-document S06 exclusion / error | `INVALID_SOURCE_URL`, `TEXT_HASH_MISMATCH`, … | kept in `DocumentSet.errors`; S06 output kept; a non-empty `errors` list on S05-produced input indicates a contract violation → warning `DEDUP_CONTRACT_ERRORS` (counts only) |
| Stage error (unexpected exception) | crawler/admission/extractor raising anything except cancellation; join check (§7.4/7.5) failing; S06 raising (`TypeError` or other) | stage stops (in-flight tasks cancelled and awaited), every captured slot kept; job `FAILED` with `error = {code, category: INTERNAL_ERROR, step}`: `CRAWL_FAILED` (step CRAWLING, fetch side), `EXTRACTION_FAILED` (step CRAWLING, extractor), `DEDUP_FAILED` (step NORMALIZING). Never converted into an empty or "no duplicates" result |

Fail-fast on an unexpected per-target exception is a [RECOMMENDATION] (OD-S07-5): such an exception is a bug
signal (S04/S05 report all page/network conditions as statuses); recording it per source and continuing is
the alternative.

New `JobErrorCode` values (additive, OD-S07-4): `CRAWL_STAGE_TIMEOUT` (TIMEOUT), `FETCH_FAILURES` (warning,
per-status counts), `EXTRACTION_DEGRADED` (warning, per-status counts), `NO_DOCUMENTS` (error),
`CRAWL_FAILED`, `EXTRACTION_FAILED`, `DEDUP_FAILED`, `DEDUP_CONTRACT_ERRORS` (warning),
`PIPELINE_BUDGET_EXCEEDED`. Messages are fixed text; no URL, page text or exception text.

### 9.2 Final status (pipeline jobs), evaluated in this order

| # | Condition | Job status | Recorded |
|---|---|---|---|
| 1 | cancellation requested (any stage) | `CANCELLED` | everything captured so far (search, sources, documents, dedup if finished); unfinished positions `INTERRUPTED`/`NOT_RUN` |
| 2 | stage error (§9.1) | `FAILED` | error with step; captured results kept |
| 3 | SEARCHING ends with no query covered | `FAILED` | `ALL_SEARCH_PROVIDERS_FAILED` (S02 rule, unchanged); later stages `NOT_RUN` |
| 4a | job deadline reached during SEARCHING | S02 rule unchanged: `PARTIAL` if ≥ 1 query covered, else `FAILED` | `JOB_DEADLINE_EXCEEDED`; result = search only; CRAWLING/NORMALIZING `NOT_RUN` |
| 4b | job deadline reached in or after CRAWLING | `PARTIAL` if ≥ 1 `SUCCESS`/`PARTIAL` document, else `FAILED` | `JOB_DEADLINE_EXCEEDED`; NORMALIZING `NOT_RUN` if the deadline passed before it started (the deadline is not extended for it) |
| 5 | CRAWLING stage timeout | continue to NORMALIZING with captured documents; final `PARTIAL` | warning `CRAWL_STAGE_TIMEOUT` |
| 6 | search coverage incomplete (S02 `PARTIAL` cases) | continue; final at best `PARTIAL` | S02 warnings carried |
| 7 | search covered, 0 results | `COMPLETED` | nothing to fetch; CRAWLING/NORMALIZING complete with 0 sources |
| 8 | ≥ 1 source selected, no `SUCCESS`/`PARTIAL` document | `FAILED` | `NO_DOCUMENTS` + `FETCH_FAILURES`/`EXTRACTION_DEGRADED` counts (OD-S07-6) |
| 9 | otherwise | `COMPLETED` | warnings `FETCH_FAILURES` / `EXTRACTION_DEGRADED` if any source degraded (per-URL failures alone do not make a job `PARTIAL`, mirroring S02's `PROVIDER_FAILURES` rule) |

Precedence stays cancel > job deadline > stage timeout > outcome (C12). A search-stage timeout with ≥ 1 query
covered no longer ends the job: the pipeline continues with what was found and ends `PARTIAL` (rule 6).

### 9.3 Cancellation

- `cancel(job_id)` keeps the S02 mechanism: flag + cancel the registered in-flight task. In CRAWLING the
  registered task is the stage task; it cancels its fetch/extract children, awaits them (S05 stops its worker
  thread, C8), keeps every captured slot, and finishes `CANCELLED`.
- A runner-task cancellation (shutdown) stays `RUNNER_INTERRUPTED` (told apart by `Task.cancelling()`), as today.
- NORMALIZING runs in a thread and cannot be interrupted; a cancel during it finishes `CANCELLED` without
  waiting for or storing the dedup result (S06 is pure, so abandoning it has no side effect).

## 10. Provenance and evidence independence

- **Chain** (all by object or id, checked §7): `DedupedSearchResult` (all providers/queries) → `SourceRecord.position`
  → `FetchTarget.source` / `FetchResult.source` (canonical hit, same object) → `FetchRecord.crawl_id` →
  `ExtractedDocument.provenance` (crawl id, URLs, redirect chain, `content_sha256`, `source`) →
  `DocumentSet.refs[i]` with `positions[i]` → groups.
- **Not evidence of independence** (requirements handed to the verification stage; nothing implemented here):
  the number of URLs, hosts or search providers; members of one L2 or L3 group count as **one** evidence unit;
  `CROSS_SOURCE_DUPLICATE` marks copies, not corroboration; a member without source identity is an unknown source,
  not an independent one; `RAW_DUPLICATE_TEXT_DIFFERS` (or its absence) is never used to accept sources as
  independent or to attribute text across L2 members — cross-member text attribution only via L3 groups (S06 F-2
  stays open; S06 production is not changed). ARCHITECTURE §10's confidence term `n_independent_domains` must be
  redefined over evidence groups before verification is built (dependency, not decided here).
- **Bot challenges** (OD-11, out of scope): identical challenge pages form L2/L3 groups across sources and are
  flagged `CROSS_SOURCE_DUPLICATE` (S06 §17/§25: PyPI); a challenge page that occurs once is not flagged at all
  and reaches later stages as an ordinary `SUCCESS` document (often with `LOW_CONFIDENCE_MAIN_CONTENT` /
  `THIN_CONTENT`). Residual risk recorded; detection remains a separate item.
- Trust: every document stays `trust="UNTRUSTED"`; nothing from page text drives control flow.

## 11. JobRunner integration (implementation sprint; frozen code touched with approval)

[RECOMMENDATION, OD-S07-3] Extend `JobRunner` rather than add a second runner, so OI-1/OI1-DEF-01 race-safety
is reused, not re-implemented:

- constructor gains optional collaborators (`collector`, `pipeline_settings`); `run` accepts
  `SPRINT_02_STAGES` (unchanged behaviour) or `PIPELINE_STAGES = (PLANNING, SEARCHING, CRAWLING, NORMALIZING)`;
  `submit` creates jobs with the configured stages (API wiring decides which, §12);
- SEARCHING's conclusion is split: for pipeline jobs the S02 terminal rules apply only to rules 1–4a of §9.2;
  otherwise the job transitions to CRAWLING through `_boundary`;
- **race rules for the new stages** (C12): every write via `_save_update`; the capture loop re-reads the job
  before admitting each fetch and stops on terminal (`FINALIZED_ELSEWHERE`) or cancel; each completed slot is
  captured before any further await; `_inflight` holds the stage task so `cancel()` reaches it; no write after a
  terminal state; precedence unchanged; progress saved after each captured source (≤ 30 writes per job);
- PERSISTENCE-BLOCKER-01 is unchanged (in-memory repository only); the new write sites must be added to the
  cancel-race and hardening suites (§13).

## 12. API and observability

- No status mapping change (C13). Result exposure is OD-S07-7: [RECOMMENDATION] additive `/results` fields with
  per-source summaries (position, url, fetch status, http status, extraction status, warnings, document id,
  group memberships) and group lists — **no page text** over the API in this sprint.
- `JobProgress` gains optional counts (`urls_total`, `urls_done`, `fetch_failed`, `documents`, `groups`) —
  ARCHITECTURE §13 progress counts.
- Logs: `pipeline.source_captured` (position, crawl_id, statuses, durations — no URL path/query, no text),
  stage start/finish with counts; existing S04/S05/S06 log events unchanged.

## 13. Test matrix

Legend: **EXISTS** = in the suite at `75ce79e` (passed in the baseline run, §1); **ADD** = new test for the
implementation sprint (not written, not run); **E2E** = needs the implementation (cannot run before it).
No ADD/E2E test is claimed to pass.

| Area | Case | Status |
|---|---|---|
| Search order | `to_response` sorts out-of-order hits; order independent of provider completion; one entry per normalized URL with all providers/queries | EXISTS (`test_search_response.py`) |
| Selection | `select_sources` = prefix of `response.results`; `max_urls` cut deterministic; `not_selected` count; 0 results | ADD (unit) |
| Order E2E (OD-13) | local server completes fetches in **reverse** order → slots, documents and S06 input in position order; representative = min position for every group of every level | E2E |
| Order E2E | seeded random completion delays × 20 runs → identical `DedupRecord` (ids/timestamps excluded); representatives = min positions | E2E |
| Failures | one / several / all fetches fail (HTTP 404, TLS, robots, timeout) → `NOT_FETCHED` documents in their slots; order of the rest unchanged; §9.2 rules 8/9 | E2E |
| Redirect | two sources redirect to one final URL → one L1 group, representative = better-ranked source; redirect chain kept | E2E |
| Multi query / provider | same URL from 2 queries × 2 providers → fetched once; `SourceRecord.providers/queries` complete | E2E |
| No document | stage timeout / cancel before a fetch starts → position `NOT_RUN`/`INTERRUPTED`, skipped in S06 input, `positions` strictly increasing | E2E |
| Multi-level identity | documents sharing identity at L1 with one source and text at L3 with another → separate groups, correct representatives, no transitive merge | E2E |
| Join checks | `source`/`requested_url`/`crawl_id` mismatch injected → `CRAWL_FAILED`/`EXTRACTION_FAILED`, captured slots kept | ADD (unit, injected) |
| Admission (S04-F1) | 6 distinct hosts, concurrency 2, 0.6 s pages, 1 s budget → all `OK` through `FetchAdmission` (P1b); 6 same-host URLs → all `OK` (P5); two jobs sharing admission → no queue-caused `FETCH_TIMEOUT` | ADD (integration, local server) |
| Backpressure | slow extractor → at most `extraction_backlog` bodies held (instrumented) | ADD |
| Content lifetime | no `FetchResult.content` reachable from `JobResult` | ADD |
| Extraction | `PARTIAL` (truncated), `EMPTY`, `UNSUPPORTED`, `FAILED` documents kept with status; L3 excludes non-`SUCCESS` (S06 rule) | EXISTS for S05/S06 units (`test_extraction.py`, `test_dedup*.py`); E2E for the pipeline |
| L2/L3 grouping | identical pages on 2 hosts → L2+L3 group, `CROSS_SOURCE_DUPLICATE`; same bytes, different charset → L2 only | EXISTS (`test_dedup.py`, S05 output); E2E through the crawler |
| Error propagation | S06 raising (monkeypatched) → `FAILED/DEDUP_FAILED`, sources/documents kept, no empty dedup result; extractor raising → `EXTRACTION_FAILED`; crawler raising → `CRAWL_FAILED` | E2E |
| Cancellation | cancel at each new write site (selection save, each source capture, CRAWLING→NORMALIZING transition, NORMALIZING terminal write) → `CANCELLED`, captured kept; cancel during an in-flight fetch and during extraction (worker stopped) | E2E (extend `test_job_cancel_race*.py`) |
| Race hardening | OI1-DEF-01 windows for the new loop: terminal before admission, during an in-flight fetch, during NORMALIZING; no write after terminal; no fetch admitted after terminal observed | E2E (extend hardening suite) |
| Deadline / timeouts | CRAWLING stage timeout → NORMALIZING runs on captured documents, `PARTIAL` + `CRAWL_STAGE_TIMEOUT`; job deadline in CRAWLING → `PARTIAL`/`FAILED`, NORMALIZING `NOT_RUN`; precedence cancel > deadline > stage timeout | E2E |
| Budgets | static check rejects `max_urls × EXTRACT_MAX_TEXT_CHARS > max_total_text_chars`; runtime counter → `PIPELINE_BUDGET_EXCEEDED` (injected); benchmark thresholds §8.3 | ADD (unit) + benchmark |
| Provenance | every `SourceRecord` ↔ `FetchRecord` ↔ document ↔ `DocumentRef` linked; `search_source` identity; trust/kind unchanged | E2E |
| API | `/results` for pipeline jobs (if OD-S07-7 approved): summaries, no text; non-terminal → 409 for CRAWLING/NORMALIZING | ADD |
| Live | opt-in `live_network`: one pipeline job on egress-allowed hosts; PASS only with fetched documents (same rule as S06 F-1) | ADD (opt-in) |
| Regression S01–S06 | the full existing suite, unchanged (845 passed, 5 skipped at baseline) | EXISTS — must stay green; no existing test edited |
| Mutation | mutants: completion-order append, `fetch_many` + outer timeout, missing admission, join check removed, representative mapping off-by-one, captured slots dropped on cancel, NORMALIZING run after deadline, content retained, budget check removed | ADD (scratch, reported KILLED/SURVIVED) |

## 14. Risks

| Risk | Impact | Mitigation / status |
|---|---|---|
| S04-F1 queue time inside the per-URL budget | spurious `FETCH_TIMEOUT` | admission (§8.2); residual for converging redirects; S04 change is OD-S07-2 |
| Same-host-heavy result sets | serialized fetching; stage timeout cuts them | reported `PARTIAL`; per-host cap is OD-S07-8 |
| Memory with 2 concurrent jobs at worst-case pages | ≈ 2 × estimate §8.2 | backpressure + content release; benchmark (b) |
| Stored document texts in the in-memory job store | memory held until the process ends | persistence sprint; no API text exposure |
| Extending frozen S02 runner/models | regression of race-safety | additive changes; reuse `_save_update`; extend race suites; full regression |
| Bot-challenge pages | non-content treated as documents | flagged when duplicated only; OD-11 |
| Evidence independence over-counted downstream | false corroboration | §10 requirements; F-2 open |
| Live behaviour of real sites | flaky live test | opt-in only; F-1 rule (no PASS without fetches) |

## 15. Conflicts with ARCHITECTURE.md / earlier documents (reported, not applied)

| Id | Conflict | Proposed disposition |
|---|---|---|
| X-1 | ARCHITECTURE §21: Sprint 07 = Verification, 08 = Database; the brief names this integration Sprint 07. The §21 note "Sprint 07 adds the Postgres implementation" is stale (Database is 08) | founder: renumber (integration 07, verification 08, database 09 …) or name this an integration step without a number; related to S06 OD-1b. ARCHITECTURE not edited |
| X-2 | ARCHITECTURE §13: "FAILED if … zero documents fetched" vs "COMPLETED with 0 results is allowed and states why (e.g. all sources blocked)" | §9.2 rules 7/8: 0 search results → `COMPLETED`; sources selected but no usable document → `FAILED/NO_DOCUMENTS` (OD-S07-6) |
| X-3 | ARCHITECTURE §3 places the parser in `CRAWLING`; the stage name `EXTRACTING` could be read as S05 "content extraction" | S05 in CRAWLING, `EXTRACTING` reserved for AI extraction (OD-S07-1) |
| X-4 | ARCHITECTURE §5 lists `CRAWL_MAX_DEPTH=1` (follow same-site links) and `CRAWL_RETRIES=2` | depth 0 only in this integration; retries stay S04's 1 (S04 OD-10) |
| X-5 | ARCHITECTURE §10 confidence uses `n_independent_domains` | must become evidence groups (§10); verification-sprint dependency |

## 16. Decisions required (founder)

| Id | Question | Recommendation |
|---|---|---|
| OD-13 | Accept the end-to-end order contract §7 (canonical order = `JobResult.response.results`) as the acceptance criterion | accept |
| OD-14 | Accept the budget proposal §8 (values, static text bound, admission, backpressure, stage timeout, benchmark thresholds) | accept as proposed values, final after the implementation benchmark |
| OD-S07-1 | Stage mapping: S05 in CRAWLING, S06 in NORMALIZING, EXTRACTING/VERIFYING unconfigured | accept |
| OD-S07-2 | S04-F1: integration admission (no S04 change) vs reopening S04 so the budget starts after slot acquisition | admission now; S04 change only if a later caller needs `fetch_many` with more targets than slots |
| OD-S07-3 | Extend the frozen `JobRunner` (additive) vs a separate pipeline runner | extend (reuses the verified race-safety) |
| OD-S07-4 | Additive changes to frozen S02 models: `JobResult` fields, `JobProgress` counts, new `JobErrorCode` values | accept (backward compatible; existing tests unchanged) |
| OD-S07-5 | Unexpected exception for one target: fail the stage (keep captured) vs record per source and continue | fail the stage |
| OD-S07-6 | Final status rules §9.2 (esp. `NO_DOCUMENTS` → `FAILED`; per-URL failures → warning only; CRAWLING timeout → still run NORMALIZING) | accept |
| OD-S07-7 | API exposure of pipeline results: additive summaries without text vs no API change in the implementation sprint | summaries without text |
| OD-S07-8 | Per-host cap on selected URLs (fairness) | not now (changes which hits are fetched); revisit with data |
| OD-S07-9 | Default job type of the API: pipeline stages behind a setting (default off) vs always on | behind a setting, default off until the live pipeline test passes |
| X-1 | Roadmap numbering | founder choice |

Still open from earlier sprints and unchanged here: S06 F-2 (warning semantics), OD-11 (bot challenges), OD-4
(L4), OD-10 (persistent identity), PERSISTENCE-BLOCKER-01.

## 17. Acceptance criteria (implementation sprint)

1. OD-13: all §13 order E2E tests pass, including reverse completion, seeded random completion (20 runs), failures,
   redirects, multi query/provider, missing documents and multi-level groups; representative = min position for
   every group asserted.
2. OD-14: static text bound enforced at wiring; admission and backpressure tests pass; benchmark (a)–(c) reported
   with the stated method and within the approved thresholds; S06 not called with more than the static maximum.
3. Error semantics: every §9.1/§9.2 row covered by a test; no stage error yields an empty successful result;
   captured results present after every stage error, cancel and timeout.
4. Race-safety: cancel-race and hardening suites extended to every new write site and loop; 0 stuck jobs, 0
   cancel → `FAILED`, no write after terminal, in the seeded randomized suite.
5. Provenance chain test passes for every source; no `FetchResult.content` in `JobResult`.
6. Mutation (§13 list) reported mutant by mutant; no unexplained survivor.
7. Full regression: all S01–S06 tests unchanged and passing; ruff, mypy strict, bandit, pip-audit, secret scan,
   `git diff --check` clean; no new dependency.
8. Opt-in live pipeline test passes only with real fetched documents, or is reported as not run.

## 18. Files expected to change in the implementation sprint

| File | Change |
|---|---|
| `src/research_agent/pipeline/config.py` | new — `PipelineSettings` |
| `src/research_agent/pipeline/discovery.py` | new — `select_sources` |
| `src/research_agent/pipeline/sources.py` | new — `FetchAdmission`, `collect_sources` |
| `src/research_agent/pipeline/models.py` | new — `SourceRecord`, `FetchRecord`, `DedupRecord`, stage outcome |
| `src/research_agent/jobs/models.py` | additive (OD-S07-4): `PIPELINE_STAGES`, `JobResult`/`JobProgress` fields, error codes |
| `src/research_agent/jobs/runner.py` | extended (OD-S07-3): CRAWLING and NORMALIZING stages |
| `src/research_agent/api/app.py`, `api/config.py`, `api/schemas.py` | wiring of shared `Crawler`/`FetchAdmission`/`Extractor`, pipeline setting (OD-S07-9), result summaries (OD-S07-7) |
| `tests/unit/test_pipeline_*.py`, `tests/integration/test_pipeline*.py`, `tests/live/test_pipeline_live.py`, `tests/pipeline_support.py` | new |
| `tests/integration/test_job_cancel_race*.py` | extended with new write sites (existing tests unchanged) |
| `docs/sprint-07-pipeline-integration-design.md` | as-built section |

Not changed: `crawler/`, `extraction/`, `dedup/`, `pipeline/search.py`, `core/`, providers, dependencies,
ARCHITECTURE.md (unless X-1/OD-S07-x approvals ask for it).
