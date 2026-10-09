# Sprint 06 — Normalization / Dedup (design)

Status: **DESIGN — not implemented.** No production code, no dependency. Decision classes:
**[EXISTING CONTRACT]** = fixed by existing code or an approved design; **[APPROVED DECISION]** = stated
by the founder in the Sprint 06 design review of `3a10e30` (2026-10-09); **[RECOMMENDATION]** = proposed,
not approved; **[OPEN]** = undecided. A recommendation is never treated as an approval.
§0 is the authoritative register; where older wording below differs, §0 prevails.
Evidence ids `E1…E10` refer to probes run for this design (scratch scripts, not committed).

## 0. Decision register (design review of `3a10e30`)

| # | Policy | Class | Basis / condition |
|---|---|---|---|
| P1 | **L1** groups documents by `source_identity = normalize_url(final_url or requested_url)` with the S01 normalizer unchanged | APPROVED DECISION (grouping) on EXISTING CONTRACT (normalizer semantics) | §6; E1, E6 |
| P2 | **L2** groups by raw `content_sha256` exactly as produced by S04 | APPROVED DECISION | §7; E3 (may group differently-decoded texts → flagged) |
| P3 | **L3** groups by `text_sha256`, **only `status == SUCCESS`** documents with non-empty text | APPROVED DECISION | §7; E5 (`PARTIAL` truncation twins) |
| P4 | **L4** near-duplicate detection is **not implemented in S06** | APPROVED DECISION | §7; E6 shows real near-duplicates exist — a later sprint may design it |
| P5 | **Provenance**: no document is ever removed or modified; no source reference is lost; no synthetic merged document | APPROVED DECISION | §9 |
| P6 | **Representative** = member with the lowest input position | APPROVED DECISION, **conditional** on upstream order | condition evidence E10: component order is guaranteed (S01 `to_response` ordering, tested; `Crawler.fetch_many` keeps input order, tested `test_crawler.py:837`); the *end-to-end* order of the S06 input is **not yet a contract** (no integration exists) → S06 contract: "caller passes documents in upstream (S01 result) order"; the integration sprint must guarantee and test it. Without that guarantee the representative is still deterministic for a given input sequence, but not meaningful as "first-ranked" |
| P7 | **No transitive merging** across L1/L2/L3; each level is its own group list | APPROVED DECISION | §7 |
| P8 | **Hash verification uses each hash's own input semantics**: `text_sha256` = SHA-256 of `text` as UTF-8 (S05) → recomputable and verifiable; `content_sha256` = SHA-256 of decompressed body bytes (S04) → **not verifiable in S06** (bytes are not carried; re-hashing text would be the wrong input, E3) → used as given, format-checked only | APPROVED DECISION (principle) on EXISTING CONTRACT (hash definitions) | §5 |
| P8a | `text_sha256` verification **enabled by default** | RECOMMENDATION (OD-7) | cost ≈ 2.9 ms per 1 M chars (E8) |
| P9 | **No second text normalization** in S06 | APPROVED DECISION | §4; E4 (no-op on S05 output) |
| P10 | **No tracking-parameter policy of S06's own**; only the S01 list applies; `ref` stays distinguishing | APPROVED DECISION on EXISTING CONTRACT | §6; C-6 |
| P11 | **Bot-challenge / interstitial detection is out of S06** | APPROVED DECISION | §23; C-7 |
| P12 | **No S06-specific document limit** (no `MAX_DOCUMENTS`); input contract checks only | RECOMMENDATION (OD-8) | §11.1 |
| P13 | Claimed canonical URL, `og:url`, JSON-LD ids never feed identity keys | EXISTING CONTRACT (S05: claimed metadata is unverified) | §6 |
| P14 | `trust="UNTRUSTED"` and `kind` preserved, never elevated | EXISTING CONTRACT (S05) | §9 |
| P15 | Pure, synchronous, offline function; output a pure function of the input sequence; keys never from `uuid4`/time | RECOMMENDATION | §10; E7 |
| P16 | Failure semantics: kept + excluded + error code; typed errors; exceptions propagate, never an empty result | RECOMMENDATION | §11 |

## 1. Scope and baseline

| Item | Value |
|---|---|
| Baseline | `55b858d` `fix(research): prevent writes after terminal job state`; HEAD = origin; clean tree |
| Suite at baseline | 743 passed, 0 failed, 4 skipped (live tests: 2 REQUIRES_NETWORK, 2 REQUIRES_CONFIGURATION) |
| Runtime | Python 3.11.15, Linux x86-64, 4 vCPU, 16 GB RAM (sandbox; shared, noisy) |

Pipeline position: `S04 Crawler → S05 Extraction → **S06 Normalization / Dedup** → S07 Verification →
S08 Synthesis/Report` (numbering: see C-2). S06 turns a sequence of `ExtractedDocument`s into a
**non-destructive** document set: the same documents, stable source identities, exact-duplicate groups
with full provenance, and explicit exclusions/errors. It does not rewrite text, judge truth, call an LLM,
fetch, or persist.

## 2. Existing contracts (read from code)

### 2.1 `ExtractedDocument` (S05, `src/research_agent/extraction/models.py`)

| Field | Semantics relevant to S06 |
|---|---|
| `kind="source_document"`, `trust="UNTRUSTED"` | constants; must be preserved [EXISTING CONTRACT] |
| `document_id` | `uuid4().hex` per extraction — **random, differs between runs** (E7) |
| `extractor_version` | `"s05.1"` |
| `status` | `SUCCESS`, `PARTIAL` (truncated / time budget), `EMPTY`, `NOT_FETCHED`, `UNSUPPORTED`, `FAILED` |
| `warnings`, `error` | enum codes / fixed messages; never page text |
| `fetch_status` | copied S04 `FetchStatus` |
| `title`, `title_source`, `claimed_language`, `language_source` | page claims; unverified |
| `text` | NFC, Markdown-oriented, structure-preserving; evidence offsets refer to it |
| `char_count`, `word_count` | over `text` |
| `text_sha256` | SHA-256 of `text` as UTF-8; `None` when `text == ""` |
| `links` | ≤ 500 http(s) links, each with `url` and `normalized_url` (S01 normalizer) |
| `claimed_metadata` | page claims incl. `canonical_url` (absolute http(s), fragment removed), `og_*`, `json_ld` raw |
| `stats` | counters (input chars, removed chars, dropped links …) |
| `provenance` (`DocumentProvenance`) | `crawl_id` (uuid4, random), `requested_url`, `final_url`, `http_status`, `content_type`, `charset`, `fetched_at`, `redirect_chain`, `robots`, `resolved_ip`, `content_sha256`, `source: SearchResult | None` |
| properties | `requested_url`, `final_url`, `content_type`, `charset`, `content_sha256` (over provenance) |

- `content_sha256` [EXISTING CONTRACT, S04 `crawler.py::_result`]: SHA-256 of the fetched, **decompressed** body
  bytes, before charset decoding; `None` when nothing was fetched.
- `final_url` [EXISTING CONTRACT, S04]: the URL of the last hop **as requested** (not normalized); for some
  pre-request refusals it is the requested URL.
- **Not present in any contract**: a stable cross-run document id, a normalized/canonical source URL field,
  a document-level language detection, an interstitial/challenge-page flag, a near-duplicate signature.

### 2.2 Upstream identity and ordering contracts

- `normalize_url` [EXISTING CONTRACT, S01 `core/urls.py`]: lower-case scheme/host, IDNA host, userinfo dropped,
  default port removed, path percent-encoding normalized (encoded reserved chars such as `%2F` kept),
  dot segments resolved, empty path → `/`, **trailing slash kept**, fragment removed, **tracking parameters
  removed** (`utm_*`, `fbclid`, `gclid`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`, `igshid`,
  `mc_cid`, `mc_eid`, `_ga`, `_gl`, `srsltid`; case-insensitive), remaining query sorted, http(s) only,
  ≤ 2048 chars.
- S01 search dedup [EXISTING CONTRACT, `pipeline/search.py`, `DedupedSearchResult`]: search hits deduplicated by
  normalized URL **before fetching**; order key (query order, provider priority, rank, index); the first
  hit is canonical; every provider and query is kept (`providers`, `queries`, `occurrences`). This is the
  project's precedent for winner selection (first in a deterministic order) and provenance retention.
- `Crawler.fetch_many` [EXISTING CONTRACT]: results in input order. Jobs: ≤ 8 planned queries
  (`ResearchPlan.queries`), ≤ 50 results per provider call (`SearchOptions.max_results`) → at most
  8 × 50 × 2 providers (fanout) = **800 search hits per job** before dedup.
- Extraction errors [EXISTING CONTRACT, S05]: extractor never raises for page content (`FAILED` with a fixed
  message); `CancelledError` propagates; `NOT_FETCHED` carries the fetch status.
- No integration exists yet: crawler and extractor are standalone (S04 I1, S05); JobRunner/API untouched.

## 3. Evidence / probes

| Id | Probe | Result |
|---|---|---|
| E1 | `normalize_url` variants | `/page` ≠ `/page/` (trailing slash kept); `#section` removed; `?utm_source=x`, `?UTM_SOURCE=x`, `fbclid`, `gclid` removed; **`?ref=abc` kept**; `EXAMPLE.com:443` → `example.com`; `http://…:80` → no port; `?b=2&a=1` → `?a=1&b=2`; `%7E` → `~`; `café` and `caf%c3%a9` → `caf%C3%A9`; `/a/../page` → `/page`; `www.` kept; `%2F` kept; userinfo dropped; `ftp:`/no host → `InvalidURLError` |
| E2 | `text_sha256` (S05) under HTML variations of one article | **equal** for: extra whitespace/newlines, CRLF, different nav/footer/script, different `<title>`, `&nbsp;` vs space, added class/id attributes, NFD vs NFC source; **different** for: `<h1>` → `<p><strong>` (Markdown marker), one added word ("released today"), a case change |
| E3 | Same raw bytes `b"<p>caf\xe9 \x93q\x94</p>"`, three charsets | one `content_sha256`, **three different texts/`text_sha256`** (`café q`, `café “q”`, `caf� �q�`) → raw-duplicate ≠ text-duplicate |
| E4 | Is S05 output already normalized? 9 fixtures + Zalgo, ZWJ emoji, NBSP/ZWSP/CRLF/BOM/SHY, Vietnamese NFD, text/plain | 14/14 already NFC, no CR, no NBSP outside fences, no ZWSP/BOM/SHY, no C0/C1 controls; ZWJ kept → re-normalizing in S06 is a no-op |
| E5 | Empty / failed / truncated documents | `EMPTY`: `text_sha256=None`; `NOT_FETCHED`: both hashes `None`; **two `PARTIAL` documents truncated at 1 M chars with different full content have the same `text_sha256`** |
| E6 | Real web (S04 → S05, egress-allowed host) | `pypi.org/project/httpx/`, `…/?utm_source=probe`, `…/#history` → same source identity, same `content_sha256` and `text_sha256`; `…/0.28.1/` → different hashes, line similarity 0.998 (near-duplicate); `…/project/httpx` (no slash) and `…/project/HTTPX/` → HTTP **200 "Client Challenge"** interstitial (209 chars, S05 `SUCCESS` + `LOW_CONFIDENCE_MAIN_CONTENT`), **identical hashes across two URLs** |
| E7 | Determinism of S04/S05 ids | `document_id`, `crawl_id` are `uuid4`; `fetched_at`/`extracted_at` are wall-clock → not usable as identity or ordering keys |
| E8 | Prototype grouping benchmark (§13) | grouping ≤ 135 ms for 10 000 documents, peak RSS growth ≤ 4.6 MB; hash re-verification ≈ 2.9 ms per 1 M chars; input texts dominate memory |
| E10 | Upstream ordering guarantees | S01: `SearchRun.to_response` orders by (query order, provider priority, rank, index) — `tests/unit/test_search_response.py::test_ordering_is_deterministic_regardless_of_completion_order`; S04: `fetch_many` returns results in input order — `tests/integration/test_crawler.py:837`; S05: one document per call (order = caller's). No component defines the order of the S06 input end to end (no integration yet) |
| E9 | Dependencies | runtime deps: fastapi, httpx, pydantic, pydantic-settings, structlog, uvicorn; nothing for similarity/URL/Unicode; `hashlib`/`unicodedata`/`difflib` (stdlib) suffice for this design |

## 4. Normalization semantics

| Aspect | Done by S05 (E2, E4) | S06 decision |
|---|---|---|
| Unicode | NFC everywhere; never NFKC | no further normalization; **never NFKC** [EXISTING CONTRACT S05 + APPROVED DECISION P9] |
| Whitespace / line endings | HTML whitespace collapsed; CRLF/CR → LF; ≤ 1 blank line; trailing spaces stripped | none [RECOMMENDATION] |
| NBSP, ZWSP, BOM, soft hyphen, C0/C1 | converted / removed | none [RECOMMENDATION] |
| ZWJ / ZWNJ, bidi, combining marks | kept | kept [EXISTING CONTRACT S05] |
| Punctuation, case, digits | untouched | untouched — case/punctuation carry meaning (`US`/`us`, `1.000` vs `1,000` in vi/en) [RECOMMENDATION] |
| Markdown structure, code fences, `<pre>`, tables, lists, headings | produced by S05 | untouched [RECOMMENDATION] |
| Empty text | `text == ""`, `text_sha256 = None` | never a dedup key [RECOMMENDATION] |
| Malformed / pathological text | bounded by S05 limits (≤ 1 M chars default) | no transformation; only hashing (linear) [RECOMMENDATION] |

**S06 performs no text normalization** [APPROVED DECISION P9]: E4 shows it would be a no-op on S05 output, and every
additional rule examined (case-folding, punctuation stripping, Markdown-marker stripping) either changes
meaning or only helps near-duplicates (E2), which are out of scope (§7, L4). The `text` of a document is
never modified, so S05 evidence offsets stay valid. A **markup-agnostic / case-folded text key** is not
created (P9).

## 5. Hash semantics

| Hash / identity | Input | Canonicalization | Purpose | Deterministic | Collision risk | False-dedup risk | Stored or recomputed |
|---|---|---|---|---|---|---|---|
| `content_sha256` [EXISTING CONTRACT S04] | decompressed body bytes | none (raw) | L2 exact raw duplicate | yes (bytes → SHA-256) | negligible (SHA-256) | identical responses that are not content (E6 interstitial); same bytes may yield different texts (E3) | stored in provenance; **not verifiable in S06** (no bytes; re-hashing `text` would be the wrong input — E3); used as given, format-checked (64 lower-case hex) [APPROVED DECISION P8] |
| `text_sha256` [EXISTING CONTRACT S05] | `ExtractedDocument.text` as UTF-8 | S05 extraction + normalization (E4) | L3 exact text duplicate | yes (pure function of FetchResult; E2) | negligible | **truncated `PARTIAL` documents** (E5); identical non-content pages (E6) | stored; S06 can **verify** it (same input semantics, P8); verification on by default is [RECOMMENDATION, OD-7] |
| `normalized_text_sha256` | — | — | — | — | — | — | **not created** [APPROVED DECISION P9 → no extra normalization]: `text_sha256` already is the normalized-text hash defined by S05 |
| `source_identity` [APPROVED DECISION P1] | `final_url`, else `requested_url` | `normalize_url` (S01, unchanged) | L1 same source | yes (pure string function) | n/a (string key, no hash) | `ref`-style non-tracking params keep URLs apart (false negative); a shared final URL for different requests merges (correct) | recomputed (cheap); kept in the output reference |
| `document_identity` [RECOMMENDATION] | — | — | instance reference inside one run | — | — | — | no new hash: a document is referenced by its **input position** and `document_id` (instance id). A persistent cross-run id belongs to the database sprint (C-4, C-5) |

Rules [RECOMMENDATION]: keys carry their level and algorithm (`L2:sha256:<hex>`, `L3:sha256:<hex>`,
`L1:url:<normalized>`); full 64-hex digests, never truncated; hashes are equality keys only — never
evidence of semantic equivalence.

## 6. Source / document identity

- **source_identity** [APPROVED DECISION P1] = `normalize_url(provenance.final_url or provenance.requested_url)`, using the
  S01 normalizer unchanged [EXISTING CONTRACT]: trailing slash, `www.`, path case and non-listed query parameters
  stay distinguishing (E1); the S01 tracking list applies (E6: `utm_source` and fragment variants collapse).
- Redirects: two requested URLs whose redirect chains end at the same final URL share the identity
  (that is what the final URL is for); `redirect_chain` is kept for audit.
- **Claimed canonical URL is never an identity input** [EXISTING CONTRACT S05: `claimed_metadata` is unverified]:
  a page claiming `canonical_url = victim` must not join the victim's group (provenance spoofing). It is
  passed through for later stages as a claim.
- `og:url`, JSON-LD `@id`/`url`, `SearchResult.metadata`: not identity inputs [RECOMMENDATION].
- `ref` and other parameters outside the S01 list stay distinguishing; S06 adds **no tracking-parameter
  policy of its own** [APPROVED DECISION P10 on EXISTING CONTRACT]. Extending the S01 list would be a separate
  S01 change request (OD-6).
- Invalid URL (`InvalidURLError`): no identity; the document is kept, excluded from L1, error
  `INVALID_SOURCE_URL` [RECOMMENDATION].

## 7. Dedup levels

Levels are computed **independently** and reported as separate group lists; there is no transitive
cross-level merging [APPROVED DECISION P7]. Grouping never removes a document from the output [APPROVED
DECISION P5].

| | L1 same source | L2 exact raw content | L3 exact extracted text | L4 near duplicate |
|---|---|---|---|---|
| Input | all documents | documents with a well-formed `content_sha256` (fetch `OK`) | `status == SUCCESS`, non-empty `text`, `text_sha256` present (and matching `text` when verification is on, OD-7) | — |
| Key | `source_identity` | `content_sha256` | `text_sha256` | — |
| Rule | equal key, ≥ 2 members | equal key, ≥ 2 members | equal key, ≥ 2 members | **not implemented in S06** [APPROVED DECISION P4] |
| Representative | lowest input position [APPROVED DECISION P6, conditional] | lowest input position | lowest input position | — |
| Provenance | every member reference (§9) | every member | every member | — |
| Group warnings [RECOMMENDATION] | `SOURCE_CONTENT_DIFFERS` (members' `content_sha256` differ, e.g. page changed between fetches), `MIXED_FETCH_STATUS` | `RAW_DUPLICATE_TEXT_DIFFERS` (members' `text_sha256` differ — E3), `CROSS_SOURCE_DUPLICATE` | `CROSS_SOURCE_DUPLICATE` (members from > 1 source identity / host — E6) | — |
| Exclusions [RECOMMENDATION] | `INVALID_SOURCE_URL` | `NO_CONTENT_HASH`, `INVALID_CONTENT_HASH` | `NOT_SUCCESS` (incl. `PARTIAL` — E5), `EMPTY_TEXT`, `TEXT_HASH_MISMATCH`, `MISSING_TEXT_HASH` | — |
| False positive risk | none beyond the normalizer's contract (same normalized URL = same resource) | identical non-content responses (interstitials, error pages) | identical non-content pages; truncation (excluded) | — |
| False negative risk | non-listed tracking params, `www.`/slash variants (E1, E6) | any byte difference (timestamps, nonces) | any text difference (E2: one word, case, markup) | — |

- **Why L1 is still needed after S01's pre-fetch dedup**: S01 groups *search URLs*; S06 groups *fetched
  documents* by where they actually came from (redirects), and works on documents from any batch.
- **Why L2 and L3 both exist**: L2 detects identical responses (byte level) even when decoding diverges
  (E3, flagged); L3 is the evidence-level duplicate S07/S08 care about. With equal bytes, charset and
  extractor version, L2 groups are also L3 groups.
- **L3 admits only `SUCCESS`** [APPROVED DECISION P3]: equal truncated prefixes do not prove equal documents (E5).
- **L4 near duplicates** — not implemented in S06 [APPROVED DECISION P4]; a future design is [OPEN, OD-4]: real evidence exists (E6: versioned PyPI page, similarity 0.998),
  but thresholds need a labelled sample and a policy on what "near" may merge. Candidate techniques (all
  stdlib-implementable): line/shingle Jaccard with MinHash (`hashlib`), SimHash. Deferred; no embeddings,
  no similarity library.

## 8. Winner selection

[APPROVED DECISION P6, conditional] **No quality-based winner.** Each group has a *representative* = the
member with the lowest input position, mirroring the S01 precedent (first occurrence in a deterministic
upstream order). Condition: the S06 input must arrive in upstream (S01 result) order — guaranteed per
component today (E10), to be guaranteed end to end by the integration sprint. All members stay in the group
with full provenance; consumers may use any member.

- For L3 the choice cannot change cited text (all members' texts are identical); it only decides which
  URL is listed first.
- Candidate criteria — extraction status, content completeness, metadata completeness, source quality,
  freshness, URL canonicality — are **not** adopted: no approved basis and some are page claims
  (metadata, canonical) or non-deterministic (`fetched_at`).

## 9. Provenance model

[RECOMMENDATION] S06 output (models in a new module, no copy of document text):

```text
DocumentRef            (one per input document, in input order)
  position             int — index in the input sequence (the S06-local document key)
  document_id, crawl_id   (S05/S04 instance ids, copied)
  source_identity      str | None
  requested_url, final_url
  status, fetch_status, warnings (S05 extraction warnings, copied)
  content_sha256, text_sha256
  kind="source_document", trust="UNTRUSTED"   (copied; never elevated)
  search_source        SearchResult | None    (S01 provenance, same object)

DuplicateGroup
  key                  "L1:url:…" | "L2:sha256:…" | "L3:sha256:…"
  level                L1 | L2 | L3
  representative       position
  members              positions, ascending (≥ 2)
  source_identities    distinct, in member order
  hosts                distinct hosts, in member order (input for S07 independence counting)
  warnings             group warnings (§7)

DocumentSet
  documents            the input ExtractedDocuments, same objects, same order (unchanged)
  refs                 list[DocumentRef]
  groups               {L1: [...], L2: [...], L3: [...]} ordered by representative position
  exclusions           (position, level, reason)
  errors               (position, code)    — never page text
  stats, s06_version
```

**Answering "where does this sentence come from?"** A sentence used downstream is cited as
(`position`/`document_id`, offsets) into that document's `text`. If the document is in an L3 group, the
identical text exists in every member, so the citation can list **all member URLs** (and their
`search_source`). For L2 groups flagged `RAW_DUPLICATE_TEXT_DIFFERS`, attribution is per document only.
Nothing is ever merged into a synthetic document.

## 10. Deterministic ordering

- Output is a **pure function of the input sequence** [RECOMMENDATION P15]: `refs` and `documents` keep input order;
  groups are ordered by representative position; members ascending; group keys derived from content/URL,
  never from `uuid4` or time (E7).
- Python `dict` insertion order (defined behaviour) may be used; set iteration order and `hash()` must
  not influence output.
- Reordered input: the same groups as member *sets* (keys are content-derived); representatives and group
  order follow the new order — intended, since the representative is defined by upstream order (P6).
  Order-invariant representatives are not part of S06.
- No parallelism: grouping is O(n) and sub-second at 10 000 documents (E8).

## 11. Failure semantics

| Situation | Behaviour [RECOMMENDATION P16] |
|---|---|
| A document's key cannot be derived (invalid URL, missing / mismatching `text_sha256`, malformed `content_sha256`, text longer than S05's configurable maximum of 10 M chars) | document **kept** in `documents`/`refs`; excluded from that level with a reason; error code recorded; never merged on a doubtful key (fail closed for merging) |
| `NOT_FETCHED` / `UNSUPPORTED` / `FAILED` / `EMPTY` / `PARTIAL` documents | kept; take part in L1 (and L2 when bytes were fetched); excluded from L3 with a reason (P3) |
| Input is not a sequence of `ExtractedDocument` | `TypeError` before processing (programming error, not data) |
| Unexpected exception | propagates; never an empty or "no duplicates" result. When integrated, the caller records a stage error (e.g. `DEDUP_FAILED`) — JobRunner contract unchanged in S06 |
| Deadline / cancellation | pure synchronous function; measured worst case at the job maximum ≈ 2.3 s (verification of 800 × 1 M chars, extrapolated from E8). Optional cooperative deadline raising a typed timeout, never partial grouping [OPEN, OD-8b] |
| Memory | references only; peak growth ≤ 4.6 MB at 10 000 documents (E8); verification encodes one text at a time (≤ 4 bytes/char) |

### 11.1 Document limit (`MAX_DOCUMENTS`) — evaluation

| Question | Answer (evidence) |
|---|---|
| Upstream bound today | ≤ 800 search hits per job (8 queries × 50 results × 2 providers, §2.2), already deduplicated by URL in S01; one ExtractedDocument per fetched URL |
| S06's own cost | O(n): 10 000 documents grouped in ≤ 135 ms with ≤ 4.6 MB peak growth (E8). The only text-proportional cost is `text_sha256` verification (≈ 2.9 ms per 1 M chars) |
| Memory | held by the caller before S06 runs (texts already extracted); a count limit in S06 would not reduce it |
| Does a count limit bound the real cost? | No — verification cost follows total characters, which S05 already caps per document (1 M default, 10 M max) |
| Conclusion | **No S06-specific document limit** [RECOMMENDATION P12, OD-8]: no evidence-based reason; it would add a failure mode without protecting anything. Input-contract checks stay (type, text length ≤ S05 maximum, hash format) |
| If the founder wants a limit anyway | apply it **before** any grouping (validate input size first); raise a typed `DedupInputTooLarge(count, limit)`; process nothing — no truncation, no partial groups, input untouched; value from evidence (e.g. 2 000 = 2.5 × the job maximum) |

## 12. Security limits

- All content and metadata stay **UNTRUSTED** [EXISTING CONTRACT]; S06 never sets or upgrades `trust`.
- **Provenance spoofing**: identity uses only crawler-observed URLs; claimed canonical / `og:url` /
  JSON-LD never feed keys (§6).
- **Duplicate poisoning / false corroboration**: exact copies across hosts (scrapers, mirrors, shared
  interstitials — E6) are flagged `CROSS_SOURCE_DUPLICATE`; S07 must count independent **groups**, not
  URLs (C-3). S06 does not decide that a page is junk — challenge-page detection is outside S06 (C-7).
- **Hash input limits**: hashes are recomputed only over `text` already bounded by S05 (≤ 1 M chars by
  default, ≤ 10 M by configuration); a longer text is a contract violation → excluded with an error.
- **Pathological Unicode**: no normalization in S06, so no NFC/NFKC amplification; hashing is linear
  (E8: 1 M combining-mark chars ≈ 2.9 ms).
- **Oversized input**: bounded upstream (≤ 800 hits per job, S05 per-document text cap); no S06 count
  limit (P12, §11.1); S06 adds no copies of texts.
- Logs: counts, levels and hashes only — no text, titles or full URLs with queries.

## 13. Performance measurements

Method: prototype grouping (L1 via `normalize_url`, L2/L3 via dict on keys, optional SHA-256
re-verification of `text`) over synthetic `ExtractedDocument`s; one process per scenario; best of 3
runs; peak RSS growth of the grouping step only (VmHWM reset after inputs are built); no `tracemalloc`.
Shared sandbox: run-to-run noise of ±2 s was observed in earlier sprints for multi-second workloads.

| Docs | Shape | Text | Input chars | Input RSS | Group (keys only) | Group + hash verify |
|---|---|---|---|---|---|---|
| 10 | unique / exact / groups | short | 1.6 k | 44 MB | 0.06 ms | 0.07 ms |
| 100 | unique | Unicode 10 k | 1 M | 48 MB | 0.55 ms | 3.3 ms |
| 1 000 | unique | short | 168 k | 50 MB | 9.8 ms | 10.7 ms |
| 1 000 | groups of 5 | Unicode 10 k | 10 M | 89 MB | 9.3 ms | 43 ms |
| 1 000 | unique | large 100 k (vi) | 98 M | 237 MB | 9.4 ms | 185 ms |
| 10 000 | unique | short | 1.7 M | 98 MB | 112 ms | 127 ms |
| 10 000 | exact | Unicode 10 k | 100 M | 482 MB | 58 ms | 382 ms |
| 10 000 | groups of 5 | Unicode 10 k | 100 M | 503 MB | 116 ms | 456 ms |
| 10 000 | unique | large 100 k (vi) | 998 M | 2 002 MB | 116 ms | 1 927 ms |
| 100 | unique / exact | pathological 1 M (combining marks) | 100 M | 237 MB | 0.6 ms | 306 ms |
| 1 000 | unique | pathological 1 M | 1 000 M | 1 959 MB | 8.7 ms | 2 922 ms |

Peak RSS growth of the grouping step: ≤ 4.6 MB in every scenario. 10 000 × 1 M-char pathological
documents (≈ 20 GB of input) was not run: the *inputs* exceed this machine, S06 is not the bottleneck.

Performance budgets for the implementation's benchmark test, derived from E8 [RECOMMENDATION]: at 2 000
documents (2.5 × the 800-hit job maximum — a test size, not a runtime limit, see §11.1) grouping ≤ 0.5 s,
grouping + verification of S05-capped texts ≤ 5 s, peak RSS growth ≤ 32 MB. Input memory is the caller's.

## 14. Dependency decision

**No new dependency** [RECOMMENDATION]. `hashlib`, `unicodedata`, existing `normalize_url` and pydantic cover the
design. Not added: `rapidfuzz` (ARCHITECTURE §9, entity matching — C-1), `datasketch`/`simhash` (L4
deferred), `xxhash`/`blake3` (SHA-256 verification costs ≈ 2.9 ms per 1 M chars, E8), URL libraries (S01
normalizer is the contract), Unicode libraries (no normalization in S06).

## 15. Test matrix (for implementation)

| Group | Cases |
|---|---|
| Normalization (P9) | S06 never changes `text`/title/metadata (object identity and equality); NFC/NFD sources (via S05) hash equal; NFKC-sensitive text (`mc²`, `①`, full-width) untouched; code fences/tables/lists untouched; empty text never keyed; Zalgo/ZWJ text hashed linearly |
| Identity (P1, P10, P13) | same URL; slash / fragment / `utm_*` / `ref` / host case / default port / percent-encoding variants (expectations = S01 normalizer, E1); `ref` stays distinguishing; redirect to a shared final URL groups at L1; `final_url` missing → `requested_url`; claimed canonical / `og:url` pointing elsewhere does **not** merge; invalid URL → `INVALID_SOURCE_URL`, document kept |
| L1/L2/L3 (P1–P3) | 2-way and N-way groups per level; L1 with different content (`SOURCE_CONTENT_DIFFERS`), mixed fetch statuses (`MIXED_FETCH_STATUS`); L2 with different texts (`RAW_DUPLICATE_TEXT_DIFFERS`, E3); L3 across hosts (`CROSS_SOURCE_DUPLICATE`); mixed unique/duplicates; duplicates with different metadata, warnings, statuses, search provenance — all retained |
| No transitive merging (P7) | A~B only at L2, B~C only at L3 → no group contains A and C; each level's groups computed from its own key only |
| Eligibility (P3) | `PARTIAL` truncation twins (E5) not grouped at L3 but still L1/L2; `EMPTY`/`NOT_FETCHED`/`FAILED`/`UNSUPPORTED` never L3; `NOT_FETCHED` still L1 |
| Hash semantics (P8) | `text_sha256` recomputed from `text` UTF-8 equals S05's; tampered `text_sha256` → `TEXT_HASH_MISMATCH`, excluded from L3, kept; `content_sha256` never recomputed from text (same bytes / different charsets stay one L2 group, E3); malformed `content_sha256` (not 64 lower-case hex) → `INVALID_CONTENT_HASH`, excluded from L2 |
| Provenance (P5, P14) | every input document appears exactly once in `refs`, fields equal to its source; `documents` are the same objects in the same order; groups list all members, `source_identities` and `hosts`; `trust`/`kind` preserved; `search_source` is the same object; a sentence of an L3 representative resolves to all member URLs |
| Representative / ordering (P6) | representative = lowest position in every group; groups ordered by representative; members ascending; reversed/shuffled input → identical member sets, representatives follow the new order |
| Determinism (P15) | same input × 50 runs → identical output; subprocess runs with different `PYTHONHASHSEED` → identical output; no `uuid4`/time in keys |
| Security | provenance-spoof page (claimed canonical = another URL); shared interstitial across hosts → one L2/L3 group with `CROSS_SOURCE_DUPLICATE`, all members kept; text longer than 10 M chars → excluded with error |
| Failure (P16) | one bad document among good ones (kept, excluded, error); all documents bad (no groups, every document kept with errors — not an exception); non-`ExtractedDocument` input → `TypeError`; injected exception inside grouping propagates (no empty result) |
| Limit (only if OD-8 adopts one) | limit + 1 documents → `DedupInputTooLarge` before processing, input untouched |
| Performance | §13 scenarios as a `benchmark`-marked test with the §13 budgets |
| Real world | §17 |

## 16. Mutation matrix (for implementation)

| # | Mutant | Must be caught by |
|---|---|---|
| N1 | S06 rewrites/normalizes `text` (any transform) | normalization "unchanged" tests |
| N2 | NFC → NFKC anywhere | NFKC-sensitive text test |
| N3 | `text_sha256` verification input changed (title included, `text.strip()`, other encoding) | hash-semantics tests |
| N4 | exact dedup disabled (L2 or L3 returns no groups) | 2-way/N-way tests |
| N5 | wrong key (L3 on `content_sha256`, L1 on `requested_url` when `final_url` exists, L1 on claimed canonical) | E3, redirect and spoof tests |
| N6 | provenance lost (member dropped, field not copied, `search_source` dropped, `hosts` missing) | provenance tests |
| N7 | non-deterministic representative (`set`, `hash()`-ordered, random, `fetched_at`/`document_id` based) | determinism tests (multiple seeds) |
| N8 | ordering reversed (groups or members) | ordering tests |
| N9 | duplicate group removed / members collapsed into one document | group-retention tests |
| N10 | key error swallowed (document dropped or silently grouped) | failure tests |
| N11 | input limit ignored (only if OD-8 adopts a limit) | limit test |
| N12 | `PARTIAL` admitted to L3 | truncation twin test |
| N13 | empty text keyed (`text_sha256 None` grouped) | empty test |
| N14 | trust elevated / `kind` changed | provenance test |
| N15 | `content_sha256` "verified" by re-hashing `text` (wrong input semantics) | E3 test (one L2 group must survive) |
| N16 | transitive merging across levels (union of L1/L2/L3) | no-transitive-merging test |
| N17 | tracking parameters stripped beyond the S01 list (e.g. `ref`) | `ref` identity test |
| N18 | representative = highest position / last member | representative test |

## 17. Real-world validation plan

S04 → S05 → S06 on egress-allowed hosts only (no bypass), opt-in `live_network` test:
`https://pypi.org/project/httpx/`, `…/?utm_source=probe`, `…/#history` (expect one L1/L2/L3 group of 3),
`…/0.28.1/` (expect no group: near duplicate, L4 out of scope), `…/project/httpx` and `…/project/HTTPX/`
(expect an L2/L3 group of two challenge pages flagged as duplicates across source identities — recorded,
not "fixed"). Records: levels, keys (hash prefixes), members, warnings, exclusions. Nothing sent to an LLM.

## 18. Architecture conflicts — dispositions

| Id | Description | Evidence | Impact | Disposition |
|---|---|---|---|---|
| C-1 | ARCHITECTURE §8/§9/§21 define Sprint 06 as normalization of **extracted entity fields** and **entity resolution** (`phonenumbers`, `rapidfuzz`, merge thresholds); the founder's brief defines S06 as **document-level** normalization/dedup | ARCHITECTURE §8, §9, §21 row 06; no entity records exist (AI structured extraction unscheduled since Sprint 05 OD-1) | entity work cannot be built without entities; document-level S06 needs no new dependency | **Resolved in scope**: S06 = document level (founder brief). Entity normalization/resolution is **not dropped**: it moves after AI structured extraction. ARCHITECTURE text change proposed in §24, **not applied** (needs approval) |
| C-2 | Roadmap: brief maps S07 = Verification, S08 = Synthesis/Report; ARCHITECTURE §21 has 07 Verification, 08 Database (must resolve PERSISTENCE-BLOCKER-01), 09 UI, 10 Export, 11 Hardening; no Synthesis sprint exists there | ARCHITECTURE §21; docs/sprint-03-research-api.md §22 | S07 maps consistently; S08 does not; the persistence blocker is tied to "the database sprint" | **OPEN (OD-1b)**: founder chooses (a) insert Synthesis/Report as 08 and shift Database → 09, or (b) keep Database 08 and schedule Synthesis later. S06 does not depend on it. Proposed wording for both options in §24, not applied |
| C-3 | ARCHITECTURE §10 verification counts independent domains and requires a document row per cited URL | ARCHITECTURE §10 | collapsing duplicates would lose URLs; counting URLs of an exact-copy group would inflate independence (E6) | **Resolved in S06 design** (P5): every member kept; groups expose `source_identities` and `hosts`. **Requirement handed to S07** (not decided here): S07 must distinguish *source URLs* from *independent evidence groups* |
| C-4 | ARCHITECTURE §5/§12: one `documents` row per `(url, content_hash)`; `content_hash` undefined (raw `content_sha256` vs text `text_sha256`), `url` undefined (requested / final / normalized) | ARCHITECTURE §5, §12 | ambiguity only matters for persistence | **Deferred to the database sprint (OD-10)**. Recommendation for that sprint: store both hashes under their explicit names and the normalized final URL; never an unqualified `content_hash` |
| C-5 | Provenance tables expect stable `document_id`s; S04/S05 ids are random per run | E7; ARCHITECTURE §11/§12 | no cross-run identity today | **Accepted limitation**: S06 references documents by input position + instance ids, keys from content/URL only; stable ids with persistence. No change to S04/S05 |
| C-6 | Tracking-parameter policy exists (S01) but excludes `ref` and other site-specific parameters | E1 | some duplicates stay separate (false negatives, never false merges) | **Resolved**: S06 uses the S01 normalizer as-is (P10); any extension is a separate S01 change request (OD-6) |
| C-7 | S05 has no interstitial/bot-challenge detection; such pages are `SUCCESS` documents (HTTP 200) | E6 (PyPI "Client Challenge") | non-content pages can form cross-source duplicate groups and reach S07 as evidence | **Out of S06 scope** (P11, §23): S06 groups and flags only; detection is a future S05/S07 decision (OD-11) |

## 19. Open decisions

Closed by the design review (now APPROVED DECISIONS in §0): former OD-2 (L3 = `SUCCESS` only → P3), OD-3
(representative = first input position → P6, conditional), OD-5 (no extra normalized-text key → P9), OD-9
(no cross-level merging → P7); OD-4 and OD-11 narrowed (not in S06 → P4, P11).

| Id | Question | Recommendation |
|---|---|---|
| OD-1 | Update ARCHITECTURE §8/§9/§21 for document-level S06 (C-1) | approve the §24 wording |
| OD-1b | Roadmap order after S07: Synthesis/Report vs Database as Sprint 08 (C-2) | founder choice; S06 unaffected |
| OD-4 | Design of near-duplicate detection (L4) in a later sprint | separate design with a labelled sample |
| OD-6 | Extend the S01 tracking-parameter list (`ref`, …) | separate S01 change request, not in S06 |
| OD-7 | `text_sha256` verification on by default | yes (≈ 2.9 ms per 1 M chars) |
| OD-8 | S06 document limit | none (§11.1); if wanted: checked before grouping, typed `DedupInputTooLarge`, no truncation |
| OD-8b | Cooperative deadline in S06 | not needed at measured costs; if wanted: typed timeout, no partial result |
| OD-10 | Persistent document identity `(url, content_hash)` | database sprint; store both hashes by name |
| OD-11 | Interstitial / bot-challenge detection | future S05/S07 decision |
| OD-12 | Module location / name | `src/research_agent/dedup/` (`models`, `identity`, `grouping`) |
| OD-13 | End-to-end input order contract for S06 (condition of P6) | integration sprint guarantees S01 result order and tests it |

## 20. Explicit non-goals

LLM calls, embeddings, vector stores, semantic similarity, near-duplicate merging, entity normalization
and resolution (C-1), verification / fact checking, citation generation, synthesis, report/export,
crawling, link following, persistence/database, JobRunner or API integration, UI, Deep Search
orchestration, changes to S01–S05 (including `normalize_url` and the tracking list), new dependencies.

## 21. Acceptance criteria (implementation sprint)

1. `group_documents(documents) -> DocumentSet` is pure, synchronous, offline and deterministic (§10).
2. No input document is ever dropped or modified; `trust`/`kind` preserved; `text` untouched (P5, P9, P14).
3. L1/L2/L3 exactly as P1–P3 and §7, separate lists (P7), representative per P6, warnings and exclusions as listed; L4 absent (P4).
4. Hash handling per P8: `text_sha256` verified with its own input semantics (if OD-7 approved); `content_sha256` used as given and format-checked, never recomputed from text.
5. Failure semantics of §11 (kept + excluded + error; typed errors; no empty result on exceptions); limit only if OD-8 adopts one.
6. Test matrix §15 implemented; mutation matrix §16 all killed or documented as equivalent with reason.
7. Real-world validation §17 recorded (opt-in); S01–S05 suites unchanged and green; ruff, mypy strict,
   bandit, pip-audit, secret scan, `git diff --check` clean; no new dependency.

## 22. Implementation plan (next sprint, after approval)

1. `src/research_agent/dedup/config.py` — only if needed (verification switch per OD-7; limit only if OD-8 adopts one).
2. `models.py` — `DocumentRef`, `DuplicateGroup`, `DocumentSet`, warning/exclusion/error enums.
3. `identity.py` — `source_identity()` over `normalize_url`; key builders with level prefixes.
4. `grouping.py` — single pass in input order, per-level dict grouping, eligibility, warnings, errors.
5. Tests: `tests/unit/test_dedup_identity.py`, `tests/unit/test_dedup_grouping.py`,
   `tests/integration/test_dedup.py` (with S05 fixtures), benchmark, live test.
6. Mutation run (§16); docs: as-built section here; ARCHITECTURE update only as approved under OD-1/OD-1b.

## 23. Bot challenges and evidence independence (out of S06 scope)

Position [APPROVED DECISION P11]:
- **HTTP 200 does not mean valid content.** E6: PyPI answered non-canonical URLs with HTTP 200 and a
  "Client Challenge" interstitial; S04 fetched it (no bypass) and S05 extracted it as `SUCCESS`.
- **Equal hashes across different URLs do not prove independent sources** — nor that the content is real.
  An exact-duplicate group may be a mirror, a scraper copy, a shared template or a shared interstitial.
- **S06 only groups and keeps provenance.** It does not judge authenticity, quality or truth, and it
  implements no bot-challenge detection. It surfaces the facts S07 needs: groups, `CROSS_SOURCE_DUPLICATE`,
  member `source_identities` and `hosts`, and each member's S05 status and warnings.
- **S07 must distinguish source URLs from independent evidence groups** (handed-over requirement, C-3);
  how it counts independence and treats interstitials is decided in S07 / a future S05 change (OD-11).

## 24. Proposed ARCHITECTURE.md changes — reported, NOT applied

Requires founder approval (OD-1, OD-1b); this review changes only this document.

1. §21 Sprint plan, row 06 — replace "normalizers, entity resolution, thresholds" with:
   "06 Normalization + Dedup (document level): source identity via the S01 normalizer, exact duplicate
   groups L1 (source) / L2 (raw bytes) / L3 (extracted text), provenance-preserving, no near-duplicates
   (docs/sprint-06-normalization-dedup-design.md)".
2. §8 Normalization and §9 Deduplication / entity resolution — add one line at the top of each:
   "Entity-level rules; they apply once AI structured extraction exists (not yet scheduled). Document-level
   dedup is Sprint 06."
3. §21 roadmap note — add: "Entity normalization and resolution (§8/§9) follow AI structured extraction."
4. C-2, depending on OD-1b: option (a) insert "08 Synthesis/Report" and renumber Database → 09 (and update
   the PERSISTENCE-BLOCKER-01 wording to "the database sprint"); option (b) keep 08 Database and add
   Synthesis/Report after it.

Impact: documentation only; no code, test or dependency change; `phonenumbers`/`rapidfuzz` remain future
mentions; PERSISTENCE-BLOCKER-01 unchanged (still PARTIALLY RESOLVED).
