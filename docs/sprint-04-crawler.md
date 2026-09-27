# Sprint 04 — Web crawler / source fetching (design)

Status: **DESIGN — awaiting approval of §17 open decisions. Nothing here is implemented.**
Baseline: `8792166` (Sprint 03; Sprint 01 `cdd1234`, Sprint 02 `42d1628` frozen). No code changed.
Probe scripts ran from a scratch directory against local servers on 127.0.0.1 and test doubles; deleted afterwards.
Note: the Sprint 04 brief received was truncated after section "10."; sections 10+ of the brief were not
available. This design covers the full boundary listed in brief §1 (robots, response validation, content
extraction, provenance) and flags anything that may need the missing instructions (§17 OD-0).

---

## 1. Current code facts (answers to brief §4)

| # | Question | Answer (verified in code at `8792166`) |
|---|---|---|
| 1 | Where are URLs normalized? | `core/urls.normalize_url`, called only by the search adapters (Perplexity, Google) when building `SearchResult` |
| 2 | What does `SearchResult` carry? | `url` (normalized: scheme/host lower-case, IDNA, default port dropped, userinfo dropped, fragment dropped, tracking params removed, **query params sorted**, path percent-encoding normalized), `original_url` (as returned by provider), `title`, `snippet`, `source` (provider), `rank`, `query`, `published_at`, `metadata` |
| 3 | How does `SearchResponse` pass URLs? | `results[DedupedSearchResult]` → canonical `SearchResult` + `providers[]` + `queries[]`; Sprint 02 stores it in `JobResult.response` |
| 4 | HTTP client abstraction? | none generic; search adapters take an `httpx.AsyncClient`, send with `follow_redirects=False`. The API builds `httpx.AsyncClient()` with defaults (**`trust_env=True`**) |
| 5 | HTTP dependencies | `httpx 0.28.1`, `httpcore 1.0.9`, `anyio 4.15.1`, `h11 0.16.0`, `certifi`, `idna`. No HTML parser (`selectolax`/`trafilatura` named in ARCHITECTURE §1 are **not installed**) |
| 6 | Logging policy | structlog + redaction; `job_id`, `http_request_id`, `request_id` (= search execution id); queries only as `query_hash` |
| 7 | Reusable error taxonomy? | categories `TIMEOUT, NETWORK_ERROR, RATE_LIMITED, AUTHENTICATION_ERROR, INVALID_RESPONSE, INVALID_INPUT, INTERNAL_ERROR, CONFIGURATION_ERROR, PROVIDER_ERROR`; `ProviderError` subclasses are provider-call exceptions (carry a provider name) — not a fit for per-URL outcomes |
| 8 | Timeouts/sizes config | Sprint 01 `Settings` (search), Sprint 03 `ApiSettings` (job/API). No crawl settings exist |
| 9 | Must the API expose crawler errors? | only once crawling is part of a job (integration, §13) |
| 10 | Must `JobRunner` change? | **yes, to run a CRAWLING stage**: it rejects any job whose `stages != (PLANNING, SEARCHING)` (`runner.py:131`) → conflict C-5 |

## 2. Probe evidence (Python 3.11.15, httpx 0.28.1, httpcore 1.0.9)

| # | Probe | Result | Design consequence |
|---|---|---|---|
| E1 | `normalize_url` + `ipaddress` + OS resolver on `2130706433`, `0x7f000001`, `017700000001`, `127.1`, `0x7f.0.0.1` | all **pass** `normalize_url`, are **not** IP literals for `ipaddress`, and **resolve to 127.0.0.1** via `getaddrinfo`; `0` → `0.0.0.0`; `%31%32%37.0.0.1` → DNS failure | a hostname-string blocklist is bypassable; the decision must be made on **resolved addresses** |
| E2 | `ipaddress` flags | `224.0.0.1`, `ff02::1` (multicast) → `is_global=True`; **`64:ff9b::7f00:1` (NAT64 embedding 127.0.0.1) → `is_global=True`**; `100.64.0.1` (CGNAT) `is_private=False`, `is_global=False`; `::ffff:127.0.0.1` → loopback, `ipv4_mapped=127.0.0.1` | policy = `not is_global` **plus** explicit multicast, NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`), 6to4 (`2002::/16`) and Teredo (`2001::/32`) embedded-address checks, IPv4-mapped unwrapping |
| E3 | Guarded `httpcore` network backend (resolve → validate every address → connect to the validated IP in the same call) | all five E1 forms + `localhost` + `[::ffff:127.0.0.1]` → `SSRF_BLOCKED` at connect; positive control (loopback allowed in test mode) fetched 200 and connected to the validated IP | connect-time enforcement works with existing dependencies |
| E4 | httpcore TLS | `start_tls(server_hostname = origin host)` (`httpcore/_async/connection.py:151`) independent of the IP passed to `connect_tcp` | IP pinning keeps SNI + certificate verification on the hostname |
| E5 | DNS rebinding (resolver returns public, then 127.0.0.1) | 1st call connected to exactly the validated public IP; 2nd call `SSRF_BLOCKED` | validate-and-connect on one resolution → no TOCTOU window |
| E6 | Redirect to `http://2130706433:<port>/` | `follow_redirects=False` exposes `Location`; re-fetch through the guard → `SSRF_BLOCKED` | manual redirect loop; every hop re-validated |
| E7 | `httpx.AsyncClient()` in this environment | `HTTPS_PROXY`/`NO_PROXY` are set; default client mounts proxy transports (`trust_env=True`) | through a proxy the target is resolved by the proxy — the local guard would only see the proxy; crawler must use `trust_env=False`, no proxy |
| E8 | Custom transport without full exception mapping | `httpcore.ConnectTimeout` escaped unmapped | reuse `httpx.AsyncHTTPTransport` (its complete httpcore→httpx mapping) and replace only its connection pool |
| E9 | gzip bomb (49 KiB wire → 50 MiB) read with `aiter_bytes()` and a 1 MiB stop | **50 MiB decoded in one chunk, 127 MiB peak memory** — the stop never triggers in time | never use httpx decoding for bodies; read `aiter_raw()` and decode ourselves |
| E10 | same bomb with `zlib.decompressobj(...).decompress(buf, max_length)` | stopped at ~1 MiB, **264 KiB** peak memory | bounded decoding is possible with stdlib; only `gzip`/`deflate` are available anyway (`br`, `zstd` decoders not installed) |
| E11 | Error chains | refused → `ConnectError…ConnectionRefusedError`; DNS → `…gaierror`; TLS → `…SSLError`; slow server → `ReadTimeout` | fine-grained codes are derivable (and DNS is resolved by our backend anyway) |
| E12 | `urllib.robotparser` (ARCHITECTURE §5 choice) | **ignores `*` and `$`**: `Disallow: /*.pdf$` and `Disallow: /private*/` → **allowed** (site owner's rule violated); longest-match `Allow: /a/b` vs `Disallow: /a` → blocked (too strict); UA-group selection OK; unparsed parser → disallow | stdlib parser is unsafe for politeness; implement an RFC 9309 matcher (no dependency) or adopt a library (OD-3) |
| E13 | Cookie persistence | a second request on the same `AsyncClient` sent the cookie set by the first response | cookie jar must be disabled (cross-site leakage) |
| E14 | Connected IP from the response | `network_stream.get_extra_info("server_addr")` → `None` after the response | the guarded backend must record the connected IP itself (per-fetch context) for provenance |

## 3. Scope and non-goals

**In scope (proposed, see OD-1/OD-4):** a standalone crawler package (`research_agent/crawler/`) — URL policy, SSRF-guarded
transport, robots.txt (RFC 9309 matcher), manual redirect handling, bounded fetch/decoding, response validation,
minimal content extraction (OD-4), per-URL provenance, per-host politeness, bounded concurrency, typed fetch outcomes, tests.

**Non-goals:** search providers, planning, entity extraction, dedup, verification, LLMs, answers; JS rendering /
headless browser; PDF/OCR; link following beyond the given URLs (depth 0, OD-6); persistent cache/`documents` table
(needs persistence, Sprint 08); proxy support (OD-7); auth/paywall/captcha bypass (never).

## 4. Crawler boundary

```text
Crawler.fetch(target: FetchTarget, *, deadline) -> FetchResult          # one URL, never raises for URL-level failures
Crawler.fetch_many(targets, *, deadline) -> list[FetchResult]           # bounded concurrency, input order preserved
```

`FetchTarget` (built from a Sprint 01 `SearchResult`, unchanged model):
`url_to_fetch` = `original_url` with fragment removed (userinfo → rejected, not stripped), `normalized_url` = `SearchResult.url`
(identity/dedup key), `provenance {provider, providers[], query_index, rank, search_request_id}`.
Fetch the provider's original URL, not the normalized one: normalization sorts query parameters (Sprint 01 deferred
risk) and could change what a server returns.

`FetchResult` (Pydantic, frozen):

| Field | Meaning |
|---|---|
| `requested_url`, `normalized_url`, `final_url` | what we asked for, identity key, where content came from |
| `status` | `FetchStatus` (§9); `OK` only when content passed every check |
| `http_status`, `content_type`, `charset`, `content_length` (decoded), `wire_bytes` | response facts |
| `fetched_at`, `duration_ms` | timing |
| `redirect_chain[{url, http_status, location, connected_ip}]` | every hop, in order |
| `connected_ip` | address actually connected for the final hop (recorded by the guarded backend, E14) |
| `robots {robots_url, outcome: ALLOWED/DISALLOWED/UNAVAILABLE_ALLOW/UNREACHABLE_DISALLOW, fetched_at}` | robots decision |
| `content_sha256`, `document` (§11) | body identity + extracted document (only when `OK`) |
| `error {code, category, message}` | fixed messages, no exception text |
| `provenance` | copied from the target (+ `job_id` when integrated) |

Cancellation is not an outcome: `CancelledError` always propagates (Sprint 02 rule).

## 5. URL validation (before any network activity)

| Check | Rule |
|---|---|
| scheme | `http`, `https` only; everything else (`file`, `ftp`, `gopher`, `data`, `javascript`, `blob`, `ws`…) → `BLOCKED_URL` (normalizer already rejects non-http(s) — reused) |
| userinfo | URL containing `user:pass@` → `BLOCKED_URL` (never sent, never silently stripped for fetching) |
| host | required; IDNA via normalizer; trailing dot removed; IPv6 zone ids (`%25eth0`) rejected |
| port | allowlist, default `{80, 443}` (OD-5); explicit other ports → `BLOCKED_PORT` |
| length | ≤ 2048 chars (normalizer) |
| literal IP hosts | allowed syntactically but still subject to the address policy at connect (§6) |
| non-canonical numeric hosts (`2130706433`, `0x7f…`, `127.1`, octal) | rejected up-front as `BLOCKED_URL` when the host is all-numeric/hex-dotted but not a canonical dotted quad (defence in depth; the connect-time check would also block them, E3) |

## 6. SSRF protection

```text
URL policy (§5) → robots (§8, itself fetched through this guard) → GuardedNetworkBackend.connect_tcp(host, port):
    resolve host (getaddrinfo, async) → validate EVERY returned address → connect to the first allowed
    address of the SAME answer (no re-resolution) → record connected IP → TLS with SNI = hostname (E4)
```

- **Resolution**: once per connection attempt, inside the connect call; never trusted across calls (E5).
- **Any** disallowed address in the answer → block the whole host (prevents mixed public/private answers).
- **Address policy** (after unwrapping IPv4-mapped IPv6): block if `not is_global`, or multicast, or unspecified,
  or inside NAT64 `64:ff9b::/96` / `64:ff9b:1::/48`, 6to4 `2002::/16`, Teredo `2001::/32` (E2). This covers loopback,
  RFC1918, link-local incl. `169.254.169.254` metadata, CGNAT `100.64/10`, `0.0.0.0`, broadcast, documentation,
  benchmarking, reserved, ULA `fc00::/7`, `fe80::/10`.
- **Metadata endpoints**: covered by link-local (`169.254.0.0/16`) and ULA; plus hostname denylist
  `metadata.google.internal`, `metadata` (defence in depth).
- **Redirects**: every hop goes through §5 and the guarded connect again (E6).
- **No proxy**: `trust_env=False`, explicit `proxy=None` (E7). Unix sockets refused by the backend.
- **Keep-alive**: connections are pooled per origin (host+port) only, so a pooled connection was validated for that
  host; pool is per crawler instance.
- **Test override**: loopback may be allowed only via a constructor argument used by tests; no environment variable
  or setting can enable it.
- SSRF-safety is claimed only for what the test matrix (§16) proves.

## 7. Redirect policy

| Situation | Rule |
|---|---|
| max hops | `CRAWL_MAX_REDIRECTS=5` (ARCHITECTURE §5) → `REDIRECT_LIMIT` |
| loop | normalized URL seen twice in one chain → `REDIRECT_LOOP` |
| codes | follow 301, 302, 303, 307, 308 with GET (no bodies are ever sent) |
| http → https | allowed |
| https → http (downgrade) | **blocked** → `REDIRECT_BLOCKED` (OD-8) |
| different host | allowed, fully re-validated (URL policy, robots for the new origin, SSRF) |
| private / localhost target | blocked by the guard (`BLOCKED_SSRF`) |
| `Location` with userinfo, non-http(s), relative | userinfo/other scheme → `REDIRECT_BLOCKED`; relative resolved against the current URL |
| provenance | every hop recorded in `redirect_chain` with status and connected IP |

## 8. robots.txt

- One robots.txt per origin (`scheme://host:port/robots.txt`), fetched through the same guarded fetcher (SSRF, ≤5
  redirects, size cap 512 KiB, total timeout 5 s), cached in memory per crawler instance (TTL 24 h, OD-2 scope).
- Outcome mapping (RFC 9309 as recalled — the RFC text could not be fetched here, **UNVERIFIED reference**; matches
  ARCHITECTURE §5): 2xx → parse; 4xx (incl. 401/403) → *unavailable* → allow; 5xx, network error, timeout →
  *unreachable* → **disallow everything** for that origin (`BLOCKED_ROBOTS`).
- Matching: own RFC 9309 matcher (E12): group by product token (case-insensitive; our token `ResearchAgentBot`,
  else `*`), `*` and `$` patterns, **longest match wins, `Allow` wins ties**, percent-encoding normalized for
  comparison. Crawl-delay ignored (non-standard) but per-host interval applies (§12).
- User-Agent `ResearchAgentBot/1.0 (+<contact URL>)` — the contact URL must be provided by the owner (OD-9).

## 9. Network errors and outcomes (`FetchStatus`)

Per-URL outcomes are **data** (the job continues), not exceptions. Codes are crawler-specific; categories reuse the
existing set, plus one new category `POLICY_BLOCKED` for deliberate refusals.

| `FetchStatus` | Category | Retry (§12) |
|---|---|---|
| `OK` | — | — |
| `BLOCKED_URL`, `BLOCKED_PORT`, `BLOCKED_SSRF`, `BLOCKED_ROBOTS`, `REDIRECT_BLOCKED` | `POLICY_BLOCKED` | never |
| `DNS_ERROR` (our resolver: NXDOMAIN / no address) | `NETWORK_ERROR` | no |
| `CONNECTION_ERROR` (refused, reset, unreachable) | `NETWORK_ERROR` | once |
| `CONNECT_TIMEOUT`, `READ_TIMEOUT`, `FETCH_TIMEOUT` (total wall clock) | `TIMEOUT` | once (not `FETCH_TIMEOUT`) |
| `TLS_ERROR` (handshake / certificate) | `NETWORK_ERROR` | never; certificates always verified |
| `REDIRECT_LIMIT`, `REDIRECT_LOOP` | `INVALID_RESPONSE` | never |
| `ACCESS_DENIED` (401, 403, paywall/captcha markers — no bypass) | `AUTHENTICATION_ERROR` | never |
| `RATE_LIMITED` (429) | `RATE_LIMITED` | once, honouring `Retry-After` ≤ budget |
| `HTTP_ERROR` (other 4xx, 5xx) | `INVALID_RESPONSE` | 5xx once |
| `UNSUPPORTED_CONTENT_TYPE`, `UNSUPPORTED_ENCODING`, `TOO_LARGE`, `DECODE_ERROR`, `EMPTY_CONTENT` | `INVALID_RESPONSE` | never |

Mapping from httpx exceptions uses the exception chain (E11). No raw exception text leaves the crawler.

## 10. Response validation

- Content types: `text/html`, `application/xhtml+xml`, `text/plain` (ARCHITECTURE §5); others → `UNSUPPORTED_CONTENT_TYPE`
  (checked from headers before reading the body).
- Size: `CRAWL_MAX_BYTES` (5 MB) on **decoded** bytes, plus a wire cap; `Content-Length` over the cap → reject before
  reading; streamed bodies counted while reading → `TOO_LARGE` (reject, no truncated documents).
- Encoding: request `Accept-Encoding: gzip, deflate`; read `aiter_raw()`; decode with `zlib` + `max_length` (E10);
  any other `Content-Encoding` → `UNSUPPORTED_ENCODING`.
- Charset: `Content-Type` charset → BOM → `<meta charset>` in the first 4 KiB → UTF-8; decoding errors replaced and
  counted; unknown charset names → `DECODE_ERROR`.
- No cookies (E13): a cookie policy that refuses all cookies; no `Authorization`, no `Referer`.

## 11. Content extraction (OD-4) and document

Minimal document, deterministic: `{title, text (NFC, whitespace-collapsed, script/style/noscript/template removed),
links[{url (resolved + normalized), text}] (recorded, not followed), meta {lang, description, canonical},
json_ld[] (raw strings, size-capped, never executed)}`. Options: (A) stdlib `html.parser` — no dependency, good
enough for title/text/links/JSON-LD; (B) ARCHITECTURE §1 `selectolax` + `trafilatura` — better main-content
extraction, adds dependencies (incl. lxml). Proposal: A in Sprint 04; revisit B with the Extraction sprint.
Web content is untrusted data: stored and passed as data only; nothing from it is interpreted as instructions.

## 12. Timeouts, retries, politeness, concurrency

| Layer | Mechanism | Default |
|---|---|---|
| connect / read / write / pool | httpx `Timeout` | 5 / 10 / 5 / 5 s |
| per-fetch total wall clock (slow-drip defence, all hops + robots) | `asyncio.timeout` | `CRAWL_TIMEOUT_S=15` |
| per-URL retry | once for transient codes (§9), backoff 1 s, `Retry-After` ≤ remaining budget | `CRAWL_RETRIES=1` (ARCHITECTURE says 2; OD-10) |
| per-host politeness | 1 in-flight request per host + min interval | `CRAWL_PER_HOST_RPS=1` |
| global concurrency | semaphore | `CRAWL_CONCURRENCY=5` |
| per-job cap | first N distinct normalized URLs in result order | `CRAWL_MAX_URLS=30` |
| CRAWLING stage / job deadline | outer `asyncio.timeout` (Sprint 02 pattern) when integrated | new `JOB_CRAWL_TIMEOUT_S`, job deadline unchanged |

## 13. Integration with the job pipeline (conflict C-5 / OD-1)

- **I1 (proposed for Sprint 04):** crawler as a standalone, fully tested subsystem; no change to JobRunner/API.
- **I2 (follow-up, needs approval to touch frozen code):** jobs with `stages = (PLANNING, SEARCHING, CRAWLING)`;
  JobRunner runs CRAWLING over `JobResult.response.results` (≤ `CRAWL_MAX_URLS`), capturing each `FetchResult`
  immediately (Sprint 02 capture rule); API `/results` gains per-source fetch status; `sources` endpoint (§14 of
  ARCHITECTURE) becomes possible. Final status: CRAWLING partial failures → warnings / `PARTIAL` per Sprint 02 rules.

## 14. Configuration

New `CrawlerSettings` (separate class; Sprint 01/03 settings untouched): `CRAWL_MAX_URLS`, `CRAWL_TIMEOUT_S`,
`CRAWL_CONNECT_TIMEOUT_S`, `CRAWL_READ_TIMEOUT_S`, `CRAWL_MAX_BYTES`, `CRAWL_MAX_REDIRECTS`, `CRAWL_RETRIES`,
`CRAWL_CONCURRENCY`, `CRAWL_PER_HOST_RPS`, `CRAWL_ALLOWED_PORTS`, `CRAWL_USER_AGENT_CONTACT`. No setting can
disable SSRF protection or robots.txt.

## 15. Observability

`crawl.fetch` log per URL: `job_id` (when integrated), `url_hash` (+ host), status, category, http_status,
redirects, bytes, duration_ms, connected IP only when blocked (for audits). Full URLs are not logged by default
(they can carry personal data in query strings) — host + hash only. Events `crawl.robots` (origin, outcome),
`crawl.blocked` (reason). No bodies in logs.

## 16. Test strategy

- Unit: address policy table (every E1/E2 case incl. NAT64, 6to4, Teredo, mapped, multicast, CGNAT, metadata);
  URL policy (schemes, userinfo, ports, numeric hosts, zone ids); robots matcher (RFC 9309 examples, wildcards,
  `$`, longest match, tie → allow, UA groups, 4xx/5xx mapping); redirect policy table; error-chain mapping; bounded
  decoder (bomb ≤ limit and bounded memory); charset detection; HTML extraction; FetchResult model.
- Integration (local servers on 127.0.0.1, loopback allowed only through the test constructor; fake resolver for
  public-hostname cases): redirect chains (limit, loop, downgrade, cross-host, to private via numeric host); DNS
  rebinding; mixed public/private DNS answers; proxy env set → ignored; TLS with a self-signed cert → `TLS_ERROR`;
  slow-drip server → `FETCH_TIMEOUT`; oversize by header and by stream; gzip bomb; unsupported content-type/encoding;
  401/403/429/5xx; cookie isolation; robots allow/deny/4xx/5xx/timeout/redirect; per-host interval and global
  concurrency timing; cancellation propagates and closes connections.
- Live (opt-in marker, e.g. `-m live_network`): one known public page; reported `REQUIRES_NETWORK` when egress is
  unavailable — never counted as PASS.
- Regression: Sprint 01–03 suites unchanged (301 passed, 2 skipped); no diff in their files.

## 17. Conflicts and open decisions

| Id | Class | Conflict / question | Proposal |
|---|---|---|---|
| **OD-0** | process | brief truncated after §10 — later requirements unknown | confirm or resend §10+ before implementation |
| C-1 / **OD-3** | doc/correctness | ARCHITECTURE §5 names `urllib.robotparser`; E12 shows it allows wildcard-disallowed paths | own RFC 9309 matcher (no dependency); alternative: `protego` (new dependency) |
| C-2 / **OD-4** | doc/deps | ARCHITECTURE §1/§6: `selectolax` + `trafilatura` (not installed; adds lxml etc.) | stdlib `html.parser` minimal extraction in Sprint 04 |
| C-3 | doc | ARCHITECTURE §5 cache (TTL, `documents` rows) needs persistence | in-memory per crawler/job only; persistent cache with Sprint 08 |
| C-4 / **OD-6** | scope | ARCHITECTURE §5 `CRAWL_MAX_DEPTH=1` (follow same-site links) | depth 0 in Sprint 04 (links recorded, not followed) |
| C-5 / **OD-1** | state machine / scope | running CRAWLING needs JobRunner (frozen, hard-coded stages) and API changes | I1 now (standalone); I2 as a separate approved step |
| C-6 | Sprint 01 risk | normalized URL sorts query params | fetch `original_url`; use normalized URL only as identity |
| C-7 / **OD-7** | environment | this environment has `HTTPS_PROXY`; crawler must not use proxies (E7); if egress is only possible via proxy, live crawling here is impossible and SSRF would have to be enforced at the proxy | no proxy support; live tests `REQUIRES_NETWORK` |
| **OD-2** | scope | robots/politeness state scope | per crawler instance (one per job when integrated) |
| **OD-5** | policy | port allowlist | `{80, 443}` default, configurable |
| **OD-8** | policy | https→http redirect | block |
| **OD-9** | owner input | User-Agent contact URL | owner provides a real URL/email; placeholder not allowed in production |
| **OD-10** | doc | retries: ARCHITECTURE says 2 | 1 (bounded by 15 s budget); or keep 2 |
| **OD-11** | implementation | IP pinning requires replacing `AsyncHTTPTransport._pool` (private attribute) or re-implementing httpx's exception mapping (E8) | replace `_pool`, pin `httpx==0.28.*`/`httpcore==1.0.*`, add a test that fails loudly if internals change |

## 18. Architecture Decision Record

| Decision | Options | Proposed | Reason | Trade-off |
|---|---|---|---|---|
| SSRF enforcement point | URL-string check / resolve-then-connect separately / validate inside connect | inside `connect_tcp` of a guarded httpcore backend | E1 (string checks bypassable), E5 (no TOCTOU), E4 (TLS intact) | relies on httpcore backend API + private `_pool` (OD-11) |
| Address policy | `is_private` / `not is_global` / explicit list | `not is_global` + multicast + NAT64/6to4/Teredo + mapped unwrap | E2 shows `is_global` gaps | must track Python `ipaddress` behaviour in tests |
| HTTP client | new library / httpx | existing httpx/httpcore, `trust_env=False`, no cookies | no new dependency; E7, E13 | private attribute use |
| Redirects | httpx automatic / manual | manual, ≤5, re-validate each hop, block downgrade | E6 | more code |
| Body decoding | httpx decoders / own bounded zlib | own bounded decoding of `aiter_raw()` | E9 vs E10 | only gzip/deflate supported |
| robots.txt | stdlib / own matcher / protego | own RFC 9309 matcher | E12 | code to maintain |
| Content extraction | stdlib / selectolax+trafilatura | stdlib minimal (OD-4) | no deps now | weaker boilerplate removal |
| Outcomes | exceptions / typed results | `FetchResult.status` data; `CancelledError` propagates | pipeline continues per URL | callers must inspect status |
| Integration | now / later | standalone now (I1) | frozen JobRunner/API | crawler not yet visible via API |
| Proxy | support / none | none | SSRF guarantees | no crawling where egress requires a proxy |

## 19. Risks and deferred items

Risks: private-API coupling (OD-11); `ipaddress` semantics differ across Python versions (tests pin behaviour);
DNS answers containing both allowed and blocked addresses make some CDNs unreachable (accepted: block); sites
requiring JS/cookies return little content (`EMPTY_CONTENT`); live verification depends on environment egress.
Deferred: job/API integration (I2), persistent cache, link following, JS rendering, PDF, proxy support,
PERSISTENCE-BLOCKER-01 (unchanged).

## 20. As built (Sprint 04 implementation)

Code: `src/research_agent/crawler/` (`config`, `models`, `policy`, `robots`, `decoding`, `network`, `crawler`).
Standalone (I1): JobRunner, API, SearchService and Sprint 01–03 files are unchanged. No dependency added.

Differences from / refinements of the design above:

- **Settings** (`CrawlerSettings`, prefix `CRAWL_`): `TIMEOUT_S` 15, `CONNECT_TIMEOUT_S` 5, `READ_TIMEOUT_S` 10,
  `MAX_RESPONSE_BYTES` 5 MiB (decoded), `MAX_REDIRECTS` 5, `RETRIES` 1 (max 1), `RETRY_BACKOFF_S` 1,
  `CONCURRENCY` 5, `PER_HOST_CONCURRENCY` 1, `PER_HOST_MIN_INTERVAL_S` 1, `ROBOTS_TIMEOUT_S` 5,
  `ROBOTS_MAX_BYTES` 512 KiB, `ROBOTS_CACHE_TTL_S` 86400 (in memory, per crawler), `CA_BUNDLE` (optional).
  `CRAWL_MAX_URLS` belongs to job integration (I2) and is not implemented. Ports are fixed to `{80, 443}`
  (not configurable; only the test-only `UnsafeTestOverrides` can add a port). No setting disables SSRF, robots,
  TLS verification or the port policy.
- **User-Agent**: `ResearchAgentCrawler/<version>`, no contact URL (OD-9: owner has not provided one; none invented).
- **Robots failure mapping**: 2xx → rules; 4xx → allow (`UNAVAILABLE_ALLOW`); 5xx, timeout, oversize, or a
  connection dropped after connect → deny (`ROBOTS_BLOCKED`, `UNREACHABLE_DISALLOW`). Connection-level failures
  of the robots fetch (DNS, connect refused/reset, connect timeout, TLS) are reported with that network status
  (e.g. `DNS_ERROR`) and robots outcome `UNREACHABLE_DISALLOW` — the page is still never fetched, but the result
  says *why* the host was unreachable instead of masking it as a robots decision. robots.txt redirects follow the
  same policy (≤ 5, every hop checked, https→http denied).
- **`RETRY_EXHAUSTED`** is returned only after a real second attempt failed (`error.cause` = last status); with
  no retry performed (retries = 0, non-retryable status, or a `Retry-After` beyond the deadline) the original
  status (e.g. `HTTP_ERROR` 429) is returned with `attempts = 1`.
- **Logging**: `crawl.fetch` carries crawl_id, job_id, request_id, host, status, http_status, duration_ms, bytes,
  redirects, attempts. No URL path/query, headers, cookies or bodies.
- **Proxy / trust**: the client and transport use `trust_env=False`; an explicit transport means httpx never reads
  proxy variables, and an explicit `SSLContext` means `SSL_CERT_FILE`/`SSL_CERT_DIR` are ignored. The only way to
  change trusted CAs is `CRAWL_CA_BUNDLE` (verification stays on).
- **Fail closed**: if httpx internals change (`_pool` missing or of another type) construction raises
  `CrawlerSetupError`; a response on a connection the guarded backend did not record → `SSRF_BLOCKED`.

### Mutation testing (each mutant applied alone, restored afterwards)

| Mutant | Result |
|---|---|
| M1 connect-time SSRF check removed / M1b URL-level SSRF check removed | killed / killed |
| M2 redirect hop not re-validated | killed |
| M3 client `trust_env=True` | killed (`test_client_never_trusts_environment`) |
| M3b transport `trust_env=True` | **equivalent**: the transport's pool is replaced and the `SSLContext` is explicit, so the flag has no reachable effect in httpx 0.28.1 |
| M3c default TLS context built with `trust_env=True` | killed (`test_ssl_cert_file_env_does_not_change_trust`) |
| M4 decompression limit removed / M4b httpx `aiter_bytes` decoder | killed / killed |
| M5 https→http page redirect allowed / M5b robots redirect downgrade allowed | killed / killed |
| M6 robots disallow ignored | killed |
| M7 port policy removed | killed |
| M8 `CancelledError` swallowed | killed |
| M9 cookies accepted | killed |
| M10 permanent failures retried | killed |
| M11 unrecorded-connection check removed | killed |
| M12 caller deadline ignored | killed |
| M13 per-host limit / M14 global limit removed | killed / killed |
| M15 address policy reduced to `not is_global` | killed |

### Real-web verification (environment note)

Run: `RUN_LIVE_NETWORK=1 CRAWL_CA_BUNDLE=<inspection CA bundle> uv run pytest -m live_network -s tests/live`.
In the Sprint 04 sandbox outbound TLS is re-terminated by an egress proxy (hence `CRAWL_CA_BUNDLE`; verification
on), and the egress policy denies many hosts (example.com, rfc-editor.org, python.org returned 403 from the
policy layer, not from the site). These denials were not routed around; the live test uses reachable hosts
(pypi.org, raw.githubusercontent.com). Result: HTML page, plain text, http→https redirect OK; robots wildcard
disallows (`/pypi/*/json`, `/search*`), metadata IP, localhost and port 8443 blocked before any request.
