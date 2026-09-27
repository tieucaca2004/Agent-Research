# Sprint 03 — Research API / execution boundary (design)

Status: **DESIGN — awaiting approval of §20 open decisions. Nothing here is implemented.**
Baseline: `42d1628` (Sprint 02 implementation; Sprint 01 frozen at `cdd1234`, Sprint 02 design `c9bd503`).
This document changes no code. Probe scripts ran from a scratch directory against test doubles and were deleted.

---

## 1. Current architecture (verified against code at `42d1628`)

| Component | What exists | Relevant facts for an API |
|---|---|---|
| `SearchService` (Sprint 01) | stateless across runs; per-call breaker/semaphore | one instance can serve every job (Sprint 02 P1; probe A1) |
| `JobRunner` (Sprint 02) | `submit(request) -> (job, created)`, `run(job_id) -> job` (awaits the whole job), `cancel(job_id) -> job` | `run` blocks until terminal ⇒ an API must execute it in a background task. `cancel` can interrupt only a search task registered in **the same runner instance** (`_inflight`, keyed by `job_id`). `run` raises `JobNotClaimableError` if the job is not QUEUED |
| `InMemoryJobRepository` | one `asyncio.Lock`, never suspends while holding state (probe A3) | atomic per call; data lost on restart; no TTL |
| `ResearchJobRequest` | `query` 3..500 (NFC, whitespace-collapsed, control chars rejected), optional `language` `^[a-z]{2}$`, `country` `^[A-Z]{2}$`, `idempotency_key` 1..128 | idempotency fingerprint = `(query, language, country)`; same key + same fingerprint → existing job, `created=False`; different → `IdempotencyConflictError` |
| Logging | structlog, contextvars; `job_id` bound by runner; `request_id` = **search execution id** `"{job_id}:search:{i}"` (Sprint 01/02) | see conflict C2-b |
| Dependencies | `httpx, pydantic, pydantic-settings, structlog`; no web framework | ARCHITECTURE §1 already selects FastAPI |
| ARCHITECTURE §14 | `POST /research` 202 `{id,status}`, `GET /research/:id`, `/status`, `POST …/cancel` (202; 409 if terminal), `/results`, `/sources`, `/export`, `/health`; envelope `{data, error, meta{request_id}}`; codes `VALIDATION_ERROR, NOT_FOUND, CONFLICT, RATE_LIMITED, REQUIRES_CONFIGURATION, INTERNAL_ERROR` | contract to follow unless a conflict is approved |

### Probe evidence (test doubles, scratch scripts, deleted afterwards)

| # | Question | Result |
|---|---|---|
| A1 | 3 jobs on one runner concurrently, cancel B | `[COMPLETED, CANCELLED, COMPLETED]`, `_inflight` empty afterwards — cancellation is per job |
| A2 | Log correlation if the HTTP layer binds `request_id` and spawns the job task | `job.*` lines inherit the HTTP id; `search.*` and `job.search.query_finished` carry the search id `…:search:0` under the **same key** → key collision |
| A3 | Does the in-memory repository yield to the event loop? | 0 foreign ticks in 100 calls — every repository call is atomic |
| A4 | Cancel at every point of a job's life (k = 0..59 loop yields), in-memory repo | 60/60 consistent: 54 `COMPLETED`, 5 `CANCELLED`, 1 cancelled before claim (`run()` → `JobNotClaimableError`, job `CANCELLED`) |
| A5 | Same sweep with a repository that suspends once per call (models real I/O, e.g. PostgreSQL) | **5/60 runs raise `JobVersionConflictError` leaving the job stuck in `PLANNING`/`SEARCHING`; 1/60 turns a user cancel into `FAILED`** — Sprint 02 correctness relies on A3 |
| A6 | FastAPI availability (PyPI metadata, not installed) | fastapi 0.141.1 (2026-07-29, MIT, py≥3.10, starlette≥0.46, pydantic≥2.9); starlette 1.7.0; uvicorn 0.54.0; repo has pydantic 2.13.5, anyio 4.15.1 |

## 2. Sprint 03 scope (proposed)

- HTTP API (FastAPI) exposing create / get / results / cancel / health for Research Jobs.
- `ResearchService` (thin application service) + `JobExecutor` (in-process background task owner).
- API contracts (request/response schemas) separate from domain models; error mapping; request-id middleware;
  body-size limit; sanitized errors; API settings.
- Uses Sprint 01 `SearchService` and Sprint 02 `JobRunner`/`InMemoryJobRepository` **unchanged**.

## 3. Non-goals (Sprint 03)

Authentication, rate limiting, persistence/PostgreSQL, queue/worker, process-restart recovery, LLM planner /
`AIProvider`, crawler/extraction/normalization/verification, `/sources`, `/export`, result pagination, frontend.

## 4. API contract

All responses use the ARCHITECTURE §14 envelope: `{"data": …, "error": null|{…}, "meta": {"request_id": "<http request id>"}}`
and echo the id in the `X-Request-ID` response header.

### 4.1 `POST /research` — create and start a job

Request body (JSON, `Content-Type: application/json`, max 16 KiB):

| Field | Type | Validation (owner) | Why |
|---|---|---|---|
| `query` | string | 3..500 after NFC + whitespace collapse, no control chars (Sprint 02 domain model) | research question |
| `language` | string? | `^[a-z]{2}$` (domain) | forwarded to search options by the fixed planner; already in the domain model |
| `country` | string? | `^[A-Z]{2}$` (domain) | same |

Header `Idempotency-Key` (optional, see OD9): `^[A-Za-z0-9._:-]{1,128}$` (API layer, stricter than the domain's
length-only check). Unknown body fields → 422.

| Outcome | HTTP | `data` |
|---|---|---|
| new job created and scheduled | **202 Accepted** | `{job_id, status: "QUEUED", created_at}` (+ `meta.idempotent_replay: false`) |
| same key + same request (replay) | **200 OK** | current job summary `{job_id, status, created_at}` + `meta.idempotent_replay: true`; nothing is executed again |
| same key + different request | 409 or 422 (OD3) | error `CONFLICT` / reason `IDEMPOTENCY_KEY_REUSED` |
| no search provider configured | 503 | error `REQUIRES_CONFIGURATION` (names of missing variables only) |

`data.job_id` replaces §14's `id` for clarity (C2-c). Internal fields (`version`, `worker_id`, plan internals,
attempt records) are never exposed.

### 4.2 `GET /research/{job_id}` — job status (also the polling endpoint)

`job_id` must match `^[0-9a-f]{32}$`, otherwise 404 without lookup.

```json
{ "job_id": "…", "query": "…", "status": "SEARCHING", "stage": "SEARCHING", "stages": ["PLANNING","SEARCHING"],
  "cancel_requested": false,
  "progress": {"queries_total": 1, "queries_done": 0, "queries_failed": 0, "results_unique": 0},
  "created_at": "…", "started_at": "…", "finished_at": null, "deadline_at": "…",
  "result_summary": null | {"coverage": "COMPLETE|PARTIAL|NONE", "results": 12},
  "error": null | {"code", "category", "step", "message"}, "warnings": [{"code","category","step","message"}] }
```

`stage` = current running stage, `null` when QUEUED or terminal. `finished_at` = domain `completed_at`.
Provider error details appear only in `/results`. The response echoes the job's own query (it is the
resource); logs never contain it.

### 4.3 `GET /research/{job_id}/results` — result retrieval

Path keeps §14's plural `/results` (OD2).

| Job status | HTTP | Body |
|---|---|---|
| QUEUED / PLANNING / SEARCHING | **409** | error `CONFLICT`, reason `JOB_NOT_FINISHED`, `details.status` — the resource exists but has no final representation yet; 404 would be false, 202 would imply this request started work |
| COMPLETED | 200 | full result |
| PARTIAL | 200 | captured results + `coverage` + warnings (timeout / provider failures) |
| CANCELLED | 200 | results captured before cancellation (may be empty) + `coverage` |
| FAILED | **200** (OD4) | `results` (possibly empty) + `error` contract (`code, category, step, message, provider_errors[provider,code,category]`) — the HTTP request succeeded; the *job* failed. A 5xx would signal a server fault and invite blind retries; a 4xx would blame the client |

Result body: `{job_id, status, coverage, results: [{url, title, snippet, provider, providers[], rank, published_at, occurrences}],
provider_statuses: [{provider, status, calls, failed, error_categories}], query_outcomes: [{index, status, result_count}],
warnings[], error}`. Mapped from Sprint 01 `SearchResponse` + Sprint 02 `JobResult`; no pagination in Sprint 03
(one query × ≤ 20 results with the fixed planner).

### 4.4 `POST /research/{job_id}/cancel`

| Current state | HTTP | Effect |
|---|---|---|
| QUEUED | 200 | `CANCELLED` immediately (repository); the scheduled task later gets `JobNotClaimableError` and exits quietly (A4) |
| PLANNING / SEARCHING | **202** | `cancel_requested=true`; in-flight search task cancelled; job ends `CANCELLED` asynchronously |
| running, already cancel_requested | 202 | idempotent, no new event |
| CANCELLED | 200 | idempotent |
| COMPLETED / PARTIAL / FAILED | **409** | reason `JOB_ALREADY_FINISHED` (§14: "409 if terminal") |
| unknown id | 404 | reason `JOB_NOT_FOUND` |

Races (Sprint 02 semantics unchanged): cancel vs completion — whichever reaches the repository first wins; both
orders end consistently (A4). Cancel vs deadline — cancel takes precedence (`CANCELLED`). Deadline → `PARTIAL`/`FAILED`
per Sprint 02 rules; internal failure → `FAILED`.

### 4.5 `GET /health`

`{status: "ok", providers: [{name, status: CONFIGURED|REQUIRES_CONFIGURATION, missing}], jobs: {running: n}}` —
no secret values; no DB section (no DB yet).

## 5. Job lifecycle mapping

| Domain status | `status` | `stage` | results endpoint | cancel endpoint |
|---|---|---|---|---|
| QUEUED | QUEUED | null | 409 | 200 → CANCELLED |
| PLANNING | PLANNING | PLANNING | 409 | 202 |
| SEARCHING | SEARCHING | SEARCHING | 409 | 202 |
| COMPLETED / PARTIAL | same | null | 200 | 409 |
| FAILED | FAILED | null | 200 + error | 409 |
| CANCELLED | CANCELLED | null | 200 | 200 |

No state-machine change (no C3 conflict).

## 6. Idempotency

- Key source: `Idempotency-Key` header → `ResearchJobRequest.idempotency_key`; fingerprint and conflict detection are
  Sprint 02's (`query` after normalization, `language`, `country`).
- Replay returns the **current** state of the original job (it may have finished); no second execution — the service
  schedules execution only when `created=True`, and `run` is compare-and-set anyway.
- Concurrent POSTs with one key: `create` is atomic (A3) → exactly one job.
- Keys live as long as the process (in-memory, no TTL); keys are global because there is no authentication.
- Without a key every POST creates a new job.

## 7. Cancellation

API `cancel` → `ResearchService.cancel` → `JobRunner.cancel` of the **single process-wide runner** (required because
`_inflight` is per runner instance). Client disconnect never cancels a job: the job runs in an executor-owned task,
not in the request task; only the cancel endpoint or process shutdown stops it. Shutdown cancels running job tasks;
Sprint 02 records them `FAILED/RUNNER_INTERRUPTED` and re-raises (tested in Sprint 02).

## 8. Timeout boundaries

| Layer | Owner | Value | Relationship |
|---|---|---|---|
| HTTP request handling | server | POST returns after create+schedule (no job work in the request) | independent of job duration |
| Client connection / disconnect | client | — | does **not** affect the job |
| Job deadline | Sprint 02 runner | `JOB_TIMEOUT_S` (600) | starts at claim |
| SEARCHING stage timeout | Sprint 02 runner | `JOB_SEARCH_TIMEOUT_S` (300) | `min(stage, deadline − now)` |
| Provider attempt / retry | Sprint 01 | `SEARCH_TIMEOUT_S`, `SEARCH_MAX_RETRIES` | unchanged |

Sprint 03 reads the two job limits from environment in a new `ApiSettings` (Sprint 02 left them as constructor
parameters because no process owner existed). Sprint 01 `Settings` is not modified.

## 9. Concurrency (current behaviour, documented; no limit added)

- Each created job gets its own `asyncio` task; jobs run concurrently without an upper bound (OD7).
- Shared: one `SearchService` (stateless across runs), one `httpx.AsyncClient` (connection pool), one `JobRunner`
  (`_inflight` keyed by `job_id`, single event loop), one repository (atomic calls, A3).
- Cancelling A does not affect B (A1).
- Risk: many concurrent jobs multiply provider calls/cost; there is no admission control until OD7 is decided.

## 10. Error mapping

| Source | Application error | HTTP | `error.code` (§14) | `error.reason` |
|---|---|---|---|---|
| request body / header / path validation | ValidationError | 422 | `VALIDATION_ERROR` | — (`details`: field locations + messages, **input values never echoed**) |
| body > 16 KiB | — | 413 | `VALIDATION_ERROR` | `PAYLOAD_TOO_LARGE` |
| `JobNotFoundError`, malformed id | NotFound | 404 | `NOT_FOUND` | `JOB_NOT_FOUND` |
| `IdempotencyConflictError` | Conflict | 409/422 (OD3) | `CONFLICT` | `IDEMPOTENCY_KEY_REUSED` |
| results of unfinished job | Conflict | 409 | `CONFLICT` | `JOB_NOT_FINISHED` |
| cancel of finished job | Conflict | 409 | `CONFLICT` | `JOB_ALREADY_FINISHED` |
| `NoSearchProviderConfiguredError` | Unavailable | 503 | `REQUIRES_CONFIGURATION` | — |
| provider failure / timeouts / cancel | **not HTTP errors** — job data (`status`, `error`, `warnings`) | 200 | — | — |
| `InvalidJobTransitionError`, `JobVersionConflictError`, anything unexpected | Internal | 500 | `INTERNAL_ERROR` | — (message generic; `meta.request_id` for tracing) |

`reason` is an additive field so §14's code list stays valid (C2-d). Never returned: stack traces, exception text,
provider secrets, file paths, raw validation input.

## 11. Security

Input validation (domain + API regexes above); body limit 16 KiB checked before parsing; `job_id` format check;
`X-Request-ID` accepted only if `^[A-Za-z0-9._-]{1,64}$`, otherwise generated (prevents log injection);
catch-all handler → sanitized 500; debug off; CORS off; secret-safe logging through the existing redaction processor;
query never logged (only Sprint 01 `query_hash`). **Authentication and rate limiting are deferred** (ARCHITECTURE §16
lists rate limiting) → the server binds `127.0.0.1` by default and must not be exposed publicly until they exist (OD6).
Without auth any caller can read/cancel any job and replay any idempotency key.

## 12. Observability

Reuse Sprint 01/02 events and logs unchanged. Add only:
- `http.request` log per request: `http_request_id`, method, route template, status, duration_ms, `job_id` when known.
- repository event `api.job_submitted {http_request_id}` (via existing `append_event`) linking request → job.
- job tasks start in a **fresh context** (`asyncio.create_task(..., context=contextvars.Context())`), so execution logs
  carry `job_id` (+ Sprint 01 search `request_id`) but not the id of the HTTP request that created them (fixes A2 ambiguity).
- Log key for the HTTP id is `http_request_id` (OD5) because `request_id` already means "search execution id" in
  Sprint 01/02 logs (A2). The response envelope keeps `meta.request_id` as §14 specifies.
- Executor done-callback logs unexpected task exceptions (`job.task_error`, type only); `JobNotClaimableError` after a
  pre-start cancel is logged at debug level.

## 13. Process boundary

```text
client ──HTTP──▶ FastAPI app (one process, one event loop)
                   │  request-id middleware → routes → ResearchService
                   │                              ├─▶ JobRunner.submit / cancel / repository reads
                   │                              └─▶ JobExecutor.spawn(job_id)  (only when created)
                   ▼
                 JobExecutor: asyncio tasks (fresh context) ──▶ JobRunner.run ──▶ SearchService ──▶ providers
                 lifespan: build httpx client + SearchService + repository + runner + executor;
                           shutdown → cancel running tasks (→ RUNNER_INTERRUPTED), close client
```

`ResearchService` exists because it owns one rule the HTTP layer must not get wrong — *schedule execution only when a
job was newly created* — and keeps routes testable without HTTP. No other abstraction is added. Starlette
`BackgroundTasks` are not used: they run inside the request's ASGI call after the response, leaving task ownership,
cancellation and shutdown implicit; the executor makes them explicit.

## 14. Worker decision — **DEFERRED**

| | In-process executor | Queue + worker |
|---|---|---|
| Complexity | one small component | queue, worker loop, claim/lease, second process |
| Reliability on restart | jobs lost (same as today) | only meaningful with a durable queue ⇒ needs persistence (Sprint 07) |
| Cancellation | direct `JobRunner.cancel` (A1) | needs cross-process signalling (flag polling) |
| Concurrency | unbounded tasks (OD7) | bounded by worker count |
| Observability | single process logs | two processes, correlation via job_id |
| Persistence dependency | none | hard dependency |

A worker without persistence adds moving parts but no durability. Keep `JobRunner` in-process (ARCHITECTURE §1/§2 describe
a DB-polling worker — deviation recorded as C1-a until Sprint 07).

## 15. Persistence decision — **DEFERRED (Option A)**

API serves in-process jobs only; a restart loses all jobs, keys and events (documented limitation). No conflict forces
PostgreSQL earlier. **But** A5 shows the Sprint 02 runner assumes non-suspending repository calls; a real I/O
repository breaks cancel/complete races (stuck jobs). That must be resolved before or with Sprint 07 (C5/C6-a).

## 16. Framework decision

| Candidate | Reason | Trade-offs | Impact |
|---|---|---|---|
| **FastAPI** (+ uvicorn to serve) | already chosen in ARCHITECTURE §1; Pydantic v2 native (repo uses pydantic 2.13.5); async; OpenAPI | adds starlette/annotated-doc deps; default 422 body echoes input → must override | runtime deps `fastapi`, `uvicorn`; tests use existing `httpx.ASGITransport` (no new test deps; lifespan must be entered explicitly in fixtures — to verify in Phase 4) |
| Starlette only | fewer deps | manual validation/schema wiring | more code |
| aiohttp / Flask | — | second HTTP stack / sync-first | against ARCHITECTURE §1 |

Nothing is installed until approval (OD11).

## 17. Test strategy

- Unit: request schema (query bounds, control chars, unknown fields, language/country), `Idempotency-Key` and
  `X-Request-ID` validation, response mapping for every status, error mapping table (§10), no input echo in 422.
- Integration (ASGI, fake providers; no network): POST → 202 + QUEUED; polling GET → terminal; results for
  COMPLETED/PARTIAL/CANCELLED/FAILED; results while running → 409; cancel in each state incl. twice and terminal;
  cancel propagates to runner (provider task cancelled); not found / malformed id; same key replay → 200 same job,
  single provider call; conflicting payload → OD3 status; concurrent POSTs (distinct and same key); client disconnect
  does not cancel; shutdown marks running jobs RUNNER_INTERRUPTED; 503 without providers; 413 body limit; no query or
  secret in logs; log correlation (`http_request_id` ↔ `api.job_submitted` ↔ `job_id`).
- Regression: Sprint 01 180 tests + Sprint 02 67 tests unchanged; `git diff 42d1628 -- src/research_agent/{core,pipeline,providers,jobs,config.py,logging.py}` empty.

## 18. Risks

1. Unbounded concurrent jobs (cost, provider rate limits) until OD7.
2. No auth / rate limiting — localhost only.
3. Restart loses everything; clients must tolerate 404 for old ids.
4. A5 latent race blocks a real persistent repository (Sprint 07).
5. Idempotency keys never expire in-process (memory grows with jobs; all jobs are kept anyway).
6. Live providers still `REQUIRES_CONFIGURATION`; API tests use fakes.

## 19. Deferred items

Auth, rate limiting, persistence + restart recovery, worker/queue, admission control (unless OD7), `/sources`,
`/export`, pagination, LLM planner, crawler/extraction/normalization/verification, fix for A5 (with Sprint 07).

## 20. Conflicts and open decisions (require approval)

| Id | Class | Conflict | Proposal |
|---|---|---|---|
| C7-a / **OD1** | C7 scope | ARCHITECTURE §21 roadmap has Sprint 03 = Crawler, API not scheduled on its own | Sprint 03 = Research API; shift Crawler→04 … Hardening→11; update §21 |
| C1-a | C1 doc | §1/§2 describe a DB-polling `JobWorker` | in-process executor until persistence (Sprint 07); annotate §1/§2 |
| C2-a / **OD2** | C2 API | brief uses `/result`, §14 uses `/results` | keep `/results` |
| C2-b / **OD5** | C2 API | `request_id` already means search-execution id in logs (A2) | HTTP id logged as `http_request_id`; envelope keeps `meta.request_id` |
| C2-c | C2 API | §14 returns `{id,status}` | return `{job_id,status,created_at}` |
| C2-d | C2 API | §14 generic codes vs Sprint 02 specific errors | keep §14 `code`, add `reason` |
| C2-e / **OD8** | C2 API | §14 lists `/status` | omit; `GET /research/{id}` already is the lightweight poll (or add as alias) |
| **OD3** | C2 API | idempotency key reuse with different payload: 409 (consistent with §14 `CONFLICT`) vs 422 (IETF Idempotency-Key draft recommendation — recalled, **UNVERIFIED**, docs not reachable here) | 409 |
| **OD4** | C2 API | FAILED job result status code | 200 with `error` in body |
| C7-b / **OD6** | C7 scope | §16 requires rate limiting on public endpoints; auth undefined | defer both; bind 127.0.0.1 by default |
| **OD7** | concurrency | no admission limit today | none in Sprint 03 (document) vs `MAX_CONCURRENT_JOBS` |
| **OD9** | idempotency | key optional in Sprint 02 | optional |
| C5-a / C6-a / **OD10** | C5/C6 | runner correct only with non-suspending repository (A5) | no change now; make it a Sprint 07 prerequisite |
| **OD11** | deps | add `fastapi`, `uvicorn` | approve |
| C3, C4 | — | none found | — |

## 21. Architecture Decision Record

| Decision | Options | Chosen (proposed) | Reason | Trade-off |
|---|---|---|---|---|
| API/process boundary | same-process executor / separate worker | same process, `JobExecutor` tasks | JobRunner is async & in-process; cancel needs same runner (A1) | single point of failure; restart loses jobs |
| Worker | none / queue+worker | none (deferred) | no durability without persistence | no horizontal scaling |
| Persistence | in-memory / PostgreSQL now | in-memory (Option A) | roadmap Sprint 07; no forcing conflict | data loss on restart; A5 to fix later |
| Framework | FastAPI / Starlette / other | FastAPI + uvicorn | ARCHITECTURE §1, Pydantic v2 | extra deps; override 422 echo |
| Cancellation | API→runner direct / flag-only | direct `JobRunner.cancel`; disconnect ≠ cancel | reuses Sprint 02 semantics | only works in the owning process |
| Idempotency | header / body; 409 / 422 | `Idempotency-Key` header; replay 200; conflict 409 (OD3) | Sprint 02 fingerprint reused | global keys without auth |
| Error mapping | domain codes / §14 codes | §14 `code` + `reason` | keeps §14, adds precision | two fields to read |
| Concurrency | unbounded / cap | unbounded, documented (OD7) | no requirement yet | cost/rate-limit risk |
| Authentication | now / deferred | deferred, localhost binding | not approved for this sprint | not deployable publicly |
| Timeout boundary | tie job to request / decouple | decoupled (4 layers + HTTP) | job must outlive requests | clients must poll |
