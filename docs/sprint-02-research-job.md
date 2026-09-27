# Sprint 02 — Research Job lifecycle (architecture proposal)

Status: **IMPLEMENTED** (design commit `c9bd503`; decisions D1–D5 confirmed, see §10 and §12).
Baseline: Sprint 01 frozen at `cdd1234`. Sprint 02 does not modify any Sprint 01 file.
Code: `src/research_agent/jobs/` (`models`, `state_machine`, `repository`, `planner`, `runner`, `errors`).

```text
ResearchJob ──▶ ResearchPlan ──▶ Search execution ──▶ (future) Crawl ──▶ (future) Extract
                                                     ──▶ (future) Normalize ──▶ (future) Verify
             Sprint 02 scope: ResearchJob lifecycle + ResearchPlan (deterministic) + Search execution
```

---

## 1. What Sprint 01 actually provides (verified against code at `cdd1234`)

| Item | Actual behaviour | Where |
|---|---|---|
| `SearchService` | Holds only immutable config (providers, strategy, retries, concurrency, breaker threshold, backoff, sleep). Circuit breaker + semaphore are created **per `search()` call**. Instance is reusable and stateless across runs (probe P1). | `pipeline/search.py` |
| `search(queries, options, *, request_id=None) -> SearchRun` | All-or-nothing: returns a `SearchRun` or raises. Raises `AllSearchProvidersFailedError` only when no hits **and** every attempt failed/skipped. | same |
| `SearchRun` | Plain dataclass: `hits`, `attempts: list[SearchAttempt]`, `request_id`, `strategy`, `queries`, `providers`; `to_response()` → `SearchResponse`. | same |
| `SearchResponse` | Pydantic, frozen, JSON-serializable: `request_id, strategy, queries, results[DedupedSearchResult], provider_statuses[ProviderExecutionStatus], total_hits, duplicate_count`. **No per-(query, provider) attempt detail.** | `core/models.py` |
| Provider status | `SUCCESS/EMPTY/PARTIAL/FAILED/SKIPPED/NOT_CALLED` aggregated per provider across queries. | `core/models.py` |
| Error category | `CONFIGURATION_ERROR, AUTHENTICATION_ERROR, RATE_LIMITED, TIMEOUT, NETWORK_ERROR, PROVIDER_ERROR, INVALID_RESPONSE` (+ `INTERNAL_ERROR`, `INVALID_INPUT`); codes unchanged. | `core/errors.py` |
| Timeouts | (a) httpx per-operation timeout = `options.timeout_s`; (b) service wall-clock timeout **per provider try** = `options.timeout_s`. **No overall search deadline.** | `providers/search/base.py`, `pipeline/search.py` |
| Retry | Transient errors retried `max_retries` times, backoff `0.5·2^(n-1)` s capped 30 s, `Retry-After` honoured; per-run circuit breaker. | `pipeline/search.py` |
| `request_id` | Caller-supplied or uuid4 hex; bound with `structlog.contextvars.bound_contextvars` for the duration of `search()`; outer context vars (e.g. `job_id`) are **merged**, not replaced (probe P7). | same |
| `query_hash` | SHA-256 prefix (16 hex); raw query never logged by search. | same |
| Layout | `src/research_agent/{config,logging}.py`, `core/`, `providers/search/`, `pipeline/search.py`. | — |
| Tests | `tests/unit`, `tests/integration` (respx), `tests/live` (`-m live`, skip = `REQUIRES_CONFIGURATION`), doubles in `tests/fakes.py` (`ScriptedSearchProvider`, `RecordingSleep`). 180 passed / 2 skipped. | — |

### Probe evidence (scratch scripts, test doubles only, repo untouched)

| # | Question | Result |
|---|---|---|
| P1 | Does `SearchService` keep state between runs? | No — instance attributes identical before/after two runs. |
| P2 | Can an outer deadline interrupt a search whose provider try timeout is longer? | Yes — cut at 0.30 s; fallback **not** called afterwards. |
| P3 | Is a retry backoff sleep interruptible by an outer deadline? | Yes — cut at 0.20 s during a 10 s backoff. |
| P4 | Does task cancellation propagate into providers? | Yes — `CancelledError` propagates, 0 in-flight provider calls afterwards, fallback not called. |
| P5 | Are completed queries kept when the deadline hits mid-run? | **No — `search()` returns nothing; results of finished queries are lost.** |
| P6 | What survives `AllSearchProvidersFailedError`? | Only `errors[(provider, code, category)]` — no attempts, durations or per-query mapping. |
| P7 | Can job correlation coexist with `request_id`? | Yes — log lines carry both `job_id` and `request_id`. |
| P8 | Worst-case search duration at defaults | ≈61 s per query (fallback, 2 providers, 1 retry, 15 s timeout); 8 queries at concurrency 4 ≈122 s. |
| Race | Job deadline and provider try timeout expiring together (3×300 runs, Python 3.11.15) | Deadline always wins; never converted into a provider error; fallback never runs after the deadline. With try timeout clearly shorter (0.02 s vs 0.2 s) fallback runs as designed (300/300). |

---

## 2. States and transitions

States: `QUEUED, PLANNING, SEARCHING, CRAWLING, EXTRACTING, NORMALIZING, VERIFYING` (running) and
`COMPLETED, PARTIAL, FAILED, CANCELLED` (terminal).

Each job records its **configured pipeline** at creation: `stages: tuple[Stage, ...]`.
Sprint 02 jobs use `stages = (PLANNING, SEARCHING)`; later sprints append stages. This is what lets a
Sprint 02 job finish after SEARCHING without pretending to crawl.

**Allowed**

| From | To |
|---|---|
| `QUEUED` | first configured stage (`PLANNING`), `CANCELLED`, `FAILED` |
| running stage *S* | next configured stage after *S*; `CANCELLED`; `FAILED` |
| last configured stage | `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED` |
| running stage that is not last | `PARTIAL` is **not** allowed (partial is decided by the last stage) |

**Forbidden** (raise `InvalidJobTransitionError`, a programming error — never silently ignored)
- any transition out of a terminal state (terminal = immutable);
- backwards transitions (e.g. `SEARCHING → PLANNING`), re-entering `QUEUED`;
- skipping a configured stage (e.g. `QUEUED → SEARCHING`), entering a stage not in `stages`;
- `QUEUED → COMPLETED/PARTIAL` (nothing ran);
- self-transitions (`SEARCHING → SEARCHING`).

All transitions go through one function `transition(job, to, *, reason)` that validates, bumps
`version`, sets timestamps and appends a `job_events` row atomically.

## 3. Identity and correlation

- **Job ID**: `uuid4().hex`, generated at creation, immutable, primary key.
- **Correlation**: `job_id` bound via `structlog.contextvars` for the whole execution.
- **Search request ID**: `f"{job_id}:search:{query_index}"` passed to `SearchService.search(request_id=...)`;
  every search log line therefore carries `job_id` **and** `request_id` (P7). `request_id` is *not*
  reused as the job ID: one job issues several search executions.

## 4. Input and plan

- **Job input** (`ResearchJobRequest`): `query: str` (NFC, trimmed, 3..500 chars, control characters
  rejected), optional `language` (ISO 639-1), `country` (ISO 3166-1), optional `idempotency_key`
  (1..128 chars). Raw query is stored in the job but logged only as `query_hash`.
- **ResearchPlan** (Pydantic, frozen): `planner` (name+version), `queries: list[str]` (1..8),
  `search_options: SearchOptions`, `created_at`.
  Sprint 02 planner = **deterministic pass-through** (`passthrough-v1`): `queries = [job.query]`.
  The LLM planner (ARCHITECTURE §3/§4.1) needs `AIProvider`, which does not exist yet → see decision D1.

## 5. Search execution (relation Job ↔ SearchService)

`SearchService` stays unchanged and unaware of jobs. The `JobRunner` depends on it by injection.

Proposed (decision D3, option B): the runner calls `SearchService.search([query], …)` **once per
planned query** (bounded concurrency `JOB_SEARCH_CONCURRENCY`), each under its own
`request_id`, and merges the per-query `SearchRun`s (`hits` + `attempts` concatenated,
same `providers`/`strategy`) into one `SearchRun` whose `to_response()` gives the job's
`SearchResponse`. Consequences:
- results of queries finished before a deadline/cancel are **kept** (fixes P5 without touching Sprint 01);
- a query whose providers all fail yields `AllSearchProvidersFailedError` for that query only;
  its `errors` are stored as the query outcome (P6 limitation accepted);
- trade-off: the Sprint 01 circuit breaker is per `search()` call, so it no longer spans queries.
  A dead provider is tried once per query (bounded by its retries) instead of being skipped after
  `SEARCH_CIRCUIT_BREAKER_THRESHOLD` failures.

Stored per job: the `SearchResponse`, plus a `QueryOutcome` per planned query
`{index, query_hash, request_id, status: COVERED|FAILED|NOT_RUN, attempts[provider, status, tries,
duration_ms, error{code, category}], errors[]}` (JSON, no exceptions, no secrets).

### Final status after the last stage (Sprint 02: SEARCHING), deterministic

A query outcome is `COVERED` (≥1 provider attempt ended `OK`/`EMPTY`), `FAILED`
(`AllSearchProvidersFailedError`), `INTERRUPTED` (running when cancel/timeout hit) or `NOT_RUN`.
`JobResult.coverage` = `COMPLETE` (all covered) / `PARTIAL` (some) / `NONE`.

| Condition (evaluated in this order) | Job status | Recorded as |
|---|---|---|
| cancellation requested | `CANCELLED` | status only (`error = None`); captured results kept, `coverage` shows what was obtained |
| job deadline or stage timeout hit, ≥1 query covered | `PARTIAL` | warning `JOB_DEADLINE_EXCEEDED` / `SEARCH_STAGE_TIMEOUT` (category `TIMEOUT`) |
| job deadline or stage timeout hit, nothing covered | `FAILED` | error `JOB_DEADLINE_EXCEEDED` / `SEARCH_STAGE_TIMEOUT` |
| no query covered | `FAILED` | error `ALL_SEARCH_PROVIDERS_FAILED` + `provider_errors[provider, code, category]` |
| some but not all queries covered | `PARTIAL` | warning `PROVIDER_FAILURES` |
| all queries covered (even with 0 results, even via fallback) | `COMPLETED` | warning `PROVIDER_FAILURES` if any provider call failed |

The job deadline is the tighter of the two time limits when `deadline − now ≤ stage timeout`,
in which case it is reported as `JOB_DEADLINE_EXCEEDED`; otherwise `SEARCH_STAGE_TIMEOUT`.

## 6. Progress

`progress = {stage, stage_index, total_stages, counts: {queries_total, queries_done,
queries_failed, results_unique}}`, updated after the plan and after each query outcome.

## 7. Error model

`JobError {code, category, step, message, retryable, details}` stored on the job when it ends
`FAILED` (and warnings list for `COMPLETED`/`PARTIAL`). Messages are built by our code — never raw
exception text from providers.

| Code | Category | When |
|---|---|---|
| `ALL_SEARCH_PROVIDERS_FAILED` | `PROVIDER_ERROR` | no planned query covered (provider errors preserved) |
| `PROVIDER_FAILURES` | `PROVIDER_ERROR` | warning: some provider calls failed but the job produced results |
| `SEARCH_STAGE_TIMEOUT` | `TIMEOUT` | SEARCHING exceeded `search_stage_timeout_s` |
| `JOB_DEADLINE_EXCEEDED` | `TIMEOUT` | job exceeded `job_timeout_s` |
| `RUNNER_INTERRUPTED` | `INTERNAL_ERROR` | the runner's own task was cancelled (recorded, then re-raised) |
| `INTERNAL_ERROR` | `INTERNAL_ERROR` | unexpected exception (type name only, message never copied) |
| `WORKER_LOST` | `INTERNAL_ERROR` | *designed only* — lease-expiry recovery is deferred (D5) |

Every provider error keeps Sprint 01 `provider`, `code`, `category` (`ProviderErrorRecord`); no new
provider taxonomy. `CANCELLED` is a status, not an error. Configuration errors
(`REQUIRES_CONFIGURATION`) surface when the `SearchService` is built, before any job runs.
Invalid transitions, version conflicts, idempotency conflicts and double execution raise to the
caller (`InvalidJobTransitionError`, `JobVersionConflictError`, `IdempotencyConflictError`,
`JobNotClaimableError`) and are never stored as job failures.

## 8. Timeouts, retry, cancellation, process failure — one mechanism per layer

| Layer | Mechanism | Setting | Owner | Effect |
|---|---|---|---|---|
| Provider HTTP operation | httpx timeout | `SEARCH_TIMEOUT_S` (15) | Sprint 01 adapter | `ProviderTimeoutError` (retryable) |
| Provider try (wall clock) | `asyncio.timeout` per try | `SEARCH_TIMEOUT_S` (15) | Sprint 01 service | `ProviderTimeoutError` → retry / fallback |
| Provider retry | backoff inside `SearchService` | `SEARCH_MAX_RETRIES` (1) | Sprint 01 service | transient errors only |
| Search stage | `asyncio.timeout` around the whole SEARCHING stage | `JobRunner(search_stage_timeout_s=300)` | Sprint 02 runner | `PARTIAL` (results kept) or `FAILED`, code `SEARCH_STAGE_TIMEOUT` |
| Job deadline | absolute `deadline_at = started_at + job_timeout_s`, re-checked at stage boundaries | `JobRunner(job_timeout_s=600)` | Sprint 02 runner | `PARTIAL` or `FAILED`, code `JOB_DEADLINE_EXCEEDED`; the stage runs under `min(stage timeout, deadline − now)` |
| Job retry | **none automatic** in V1 | — | caller | re-run = new job (`retry_of` link); terminal jobs never restart |
| Cancellation | `cancel_requested` flag + cancel the in-flight asyncio task | — | Sprint 02 runner | `CANCELLED` |
| Process failure | lease + heartbeat, recovery sweep | `JOB_LEASE_S` | **deferred (D5)** | designed only; not implemented, not verified |

Why retries cannot overrun the deadline: the stage/deadline scope is an outer `asyncio.timeout`.
Probes P2/P3/Race show it interrupts in-flight provider calls and backoff sleeps, and is never
converted into a provider error. Worst-case search time at defaults (P8, ≈61 s/query) fits the
600 s job deadline; lower deadlines are still enforced by hard cut.

Precedence when several happen together: cancel > job deadline > stage timeout > search outcome.

## 9. Cancellation, persistence, idempotency, recovery, observability

**Cancellation.** `cancel(job_id)`: `QUEUED` → `CANCELLED` immediately (atomic). Running → set
`cancel_requested`; the runner cancels its in-flight task and also checks the flag between stages
and between queries → `CANCELLED`. Terminal → no-op returning current status (idempotent).
Already-finished query outcomes are kept on a cancelled job.

**Persistence boundary.** `JobRepository` protocol (async, implemented): `create`
(idempotent), `get`, `save` (optimistic `version` check + state-machine validation +
`job.state_changed` event), `claim` (compare-and-set `QUEUED → first stage`, sets `deadline_at`),
`request_cancel`, `append_event`, `list_events`. Lease/heartbeat/expired-lease queries are
deferred with recovery (D5). Sprint 02 ships `InMemoryJobRepository`
(one `asyncio.Lock`); PostgreSQL implementation is Sprint 07 (ARCHITECTURE §21 note) behind the
same protocol. No API/HTTP in Sprint 02.

**Idempotency.** `create(request)` with `idempotency_key`: same key + same normalized payload →
returns the existing job (`created=False`); same key + different payload → `IdempotencyConflictError`.
Execution: `claim` is compare-and-set, so a second `run(job_id)` gets `JobAlreadyClaimedError` and
never executes search twice; search outcome can be saved once per job.

**Recovery after restart — DEFERRED (D5), design only.** On start-up `recover(now)`: every non-terminal, non-`QUEUED` job whose
lease expired → `FAILED/WORKER_LOST` with `step` = its last stage. `QUEUED` jobs stay queued.
No mid-pipeline resume (ARCHITECTURE §13). Not implemented in Sprint 02: with the in-memory repository a process restart loses all jobs,
and no recovery behaviour is claimed or verified until a persistent store and worker exist.

**Observable events** (`job_events` rows + structured logs, all with `job_id`):
`job.created`, `job.idempotent_hit`, `job.claimed`, `job.state_changed{from,to,reason}`,
`job.stage_started{stage}`, `job.stage_finished{stage,duration_ms,counts}`,
`job.plan_created{planner,queries}`, `job.search.query_finished{index,query_hash,request_id,status,
results,duration_ms}`, `job.cancel_requested`, `job.cancelled`, `job.deadline_exceeded{step}`,
`job.failed{code,category,step}`, `job.completed{status,results}`, `job.recovered_lost`.
Raw query text is never logged (only `query_hash`).

## 10. Conflicts with ARCHITECTURE.md / Sprint 01 — decisions required before implementation

| # | Conflict | Evidence | Proposal |
|---|---|---|---|
| **D1** | ARCHITECTURE §21 puts "worker, API `POST/GET/status/cancel`, planner via AIProvider" in Sprint 02; the Sprint 02 brief limits scope to job lifecycle + search integration. `AIProvider` does not exist yet (Sprint 04). | §21 table; `src/` has no `providers/ai` | Sprint 02 = domain model, state machine, in-memory repository, in-process `JobRunner`, deterministic `passthrough-v1` planner. Defer HTTP API, background worker loop and LLM planner to a later sprint; update §21. |
| **D2** | ARCHITECTURE §13 defines a strictly linear pipeline and `PARTIAL` only from `VERIFYING`; a Sprint 02 job must finish after `SEARCHING`. | §13 diagram | Per-job configured `stages`; transitions per §2 of this document; update §13. |
| **D3** | `SearchService.search()` is all-or-nothing: a job deadline or cancel mid-search discards completed queries. | Probe P5 | **B**: runner calls `search()` once per query and merges runs (no Sprint 01 change; circuit breaker becomes per query). Alternatives: **A** accept loss (job `FAILED`, no results); **C** add deadline/partial-return to `SearchService` (modifies frozen Sprint 01). |
| D4 | `AllSearchProvidersFailedError` carries only `(provider, code, category)`. | Probe P6 | Accept for Sprint 02 (store those per query). No Sprint 01 change. |
| D5 | Restart recovery cannot be truly verified with an in-memory store. | ARCHITECTURE §21 note (Postgres in Sprint 07) | Implement + unit-test recovery logic against the repository protocol; mark real restart recovery UNVERIFIED until Sprint 07. |

Non-conflicts confirmed: `SearchService` stateless (P1); `request_id` composes with `job_id` (P7);
outer deadlines/cancellation interrupt search, retries and backoff (P2–P4, Race); Sprint 01 defaults,
`SearchResponse`, URL normalizer and logging semantics need no change.

## 11. Test plan (after confirmation)

Unit: create job + initial state `QUEUED`; every allowed transition; every forbidden class above;
terminal immutability; plan creation; final-status table (§5) incl. ties; error mapping;
idempotent create / conflicting key; double `run` → `JobAlreadyClaimedError`; cancel in each state;
recovery with injected clock; event sequence per scenario.
Integration (fakes + respx-backed real adapters): successful search → `COMPLETED`; one query failing →
`PARTIAL`; all failing → `FAILED/ALL_SEARCH_PROVIDERS_FAILED`; provider timeout with fallback →
`COMPLETED` + warning; stage timeout and job deadline (fake slow provider) → `FAILED` with completed
query outcomes retained; cancel mid-search → `CANCELLED` with no provider call after cancel; retry
interaction (retries cut by deadline); every log line of a job carries `job_id`, search lines also
`request_id`; no raw query in logs. Regression: full Sprint 01 suite unchanged and green.

## 12. Implementation notes (Sprint 02, as built)

- Decisions applied: **D1** scope (no HTTP API, worker, PostgreSQL, LLM planner or `AIProvider`);
  **D2** per-job `stages`, Sprint 02 pipeline `PLANNING → SEARCHING → terminal`, `PARTIAL` is the
  outcome of the *last configured* stage; **D3-B** per-query orchestration, no change to
  `SearchService`; **D4** Sprint 01 `provider/code/category` preserved; **D5** recovery deferred.
- Capture guarantee: each finished query's outcome is appended to the runner's in-memory capture
  *before* any further `await`, then persisted; the terminal save always contains every captured
  query, so a deadline or cancel that lands during an intermediate save cannot lose it.
- Cancellation paths: `JobRunner.cancel(job_id)` flags the job and cancels the in-flight search
  task → `CANCELLED`, `run()` returns normally. Cancelling the task that awaits `run()` →
  job recorded `FAILED/RUNNER_INTERRUPTED`, `CancelledError` re-raised.
- The two time limits are constructor parameters of `JobRunner`; they are not read from the
  environment in Sprint 02 (no worker/API process exists to own them yet).
- Trade-off accepted (D3): the Sprint 01 circuit breaker is scoped to one `SearchService.search()`
  call, i.e. to one query. A provider that is down is retried (per its retry policy) on every query
  instead of being skipped after `SEARCH_CIRCUIT_BREAKER_THRESHOLD` failures.
- Queries run sequentially in plan order (deterministic; the fixed planner yields one query).
- Bug found during diff review (before commit): any `TimeoutError` escaping the search stage was
  labelled `SEARCH_STAGE_TIMEOUT`/`JOB_DEADLINE_EXCEEDED`, even when raised by the search layer
  rather than by the runner's own limit. Root cause: the handler did not check which scope
  expired. Fix: only `asyncio.timeout(...).expired()` produces the timeout codes; anything else
  becomes `INTERNAL_ERROR`. Regression test:
  `test_raw_timeout_error_from_search_is_not_mislabelled_as_stage_timeout` (fails on the pre-fix
  runner, passes after).
