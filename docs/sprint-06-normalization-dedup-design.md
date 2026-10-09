# Sprint 06 — Normalization / Dedup (design)

Status: **DESIGN — not implemented.** No production code, no dependency. Every decision is tagged:
**[CONTRACT]** = approved by an existing contract (code or approved design), **[REC]** = recommendation
awaiting founder approval, **[OPEN]** = open decision. Recommendations are not approvals.
Evidence ids `E1…E9` refer to probes run for this design (scratch scripts, not committed).

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
| `kind="source_document"`, `trust="UNTRUSTED"` | constants; must be preserved [CONTRACT] |
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

- `content_sha256` [CONTRACT, S04 `crawler.py::_result`]: SHA-256 of the fetched, **decompressed** body
  bytes, before charset decoding; `None` when nothing was fetched.
- `final_url` [CONTRACT, S04]: the URL of the last hop **as requested** (not normalized); for some
  pre-request refusals it is the requested URL.
- **Not present in any contract**: a stable cross-run document id, a normalized/canonical source URL field,
  a document-level language detection, an interstitial/challenge-page flag, a near-duplicate signature.

### 2.2 Upstream identity and ordering contracts

- `normalize_url` [CONTRACT, S01 `core/urls.py`]: lower-case scheme/host, IDNA host, userinfo dropped,
  default port removed, path percent-encoding normalized (encoded reserved chars such as `%2F` kept),
  dot segments resolved, empty path → `/`, **trailing slash kept**, fragment removed, **tracking parameters
  removed** (`utm_*`, `fbclid`, `gclid`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`, `igshid`,
  `mc_cid`, `mc_eid`, `_ga`, `_gl`, `srsltid`; case-insensitive), remaining query sorted, http(s) only,
  ≤ 2048 chars.
- S01 search dedup [CONTRACT, `pipeline/search.py`, `DedupedSearchResult`]: search hits deduplicated by
  normalized URL **before fetching**; order key (query order, provider priority, rank, index); the first
  hit is canonical; every provider and query is kept (`providers`, `queries`, `occurrences`). This is the
  project's precedent for winner selection (first in a deterministic order) and provenance retention.
- `Crawler.fetch_many` [CONTRACT]: results in input order. Jobs: ≤ 8 planned queries
  (`ResearchPlan.queries`), ≤ 50 results per provider call (`SearchOptions.max_results`) → at most
  8 × 50 × 2 providers (fanout) = **800 search hits per job** before dedup.
- Extraction errors [CONTRACT, S05]: extractor never raises for page content (`FAILED` with a fixed
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
| E9 | Dependencies | runtime deps: fastapi, httpx, pydantic, pydantic-settings, structlog, uvicorn; nothing for similarity/URL/Unicode; `hashlib`/`unicodedata`/`difflib` (stdlib) suffice for this design |

## 4. Normalization semantics

| Aspect | Done by S05 (E2, E4) | S06 decision |
|---|---|---|
| Unicode | NFC everywhere; never NFKC | no further normalization; **never NFKC** [CONTRACT S05 + REC] |
| Whitespace / line endings | HTML whitespace collapsed; CRLF/CR → LF; ≤ 1 blank line; trailing spaces stripped | none [REC] |
| NBSP, ZWSP, BOM, soft hyphen, C0/C1 | converted / removed | none [REC] |
| ZWJ / ZWNJ, bidi, combining marks | kept | kept [CONTRACT S05] |
| Punctuation, case, digits | untouched | untouched — case/punctuation carry meaning (`US`/`us`, `1.000` vs `1,000` in vi/en) [REC] |
| Markdown structure, code fences, `<pre>`, tables, lists, headings | produced by S05 | untouched [REC] |
| Empty text | `text == ""`, `text_sha256 = None` | never a dedup key [REC] |
| Malformed / pathological text | bounded by S05 limits (≤ 1 M chars default) | no transformation; only hashing (linear) [REC] |

**S06 performs no text normalization** [REC]: E4 shows it would be a no-op on S05 output, and every
additional rule examined (case-folding, punctuation stripping, Markdown-marker stripping) either changes
meaning or only helps near-duplicates (E2), which are out of scope (§7, L4). The `text` of a document is
never modified, so S05 evidence offsets stay valid. A **markup-agnostic / case-folded text key** is
[OPEN] (OD-5, recommendation: no).

## 5. Hash semantics

| Hash / identity | Input | Canonicalization | Purpose | Deterministic | Collision risk | False-dedup risk | Stored or recomputed |
|---|---|---|---|---|---|---|---|
| `content_sha256` [CONTRACT S04] | decompressed body bytes | none (raw) | L2 exact raw duplicate | yes (bytes → SHA-256) | negligible (SHA-256) | identical responses that are not content (E6 interstitial); same bytes may yield different texts (E3) | stored in provenance; S06 cannot recompute (no bytes) |
| `text_sha256` [CONTRACT S05] | `ExtractedDocument.text` as UTF-8 | S05 extraction + normalization (E4) | L3 exact text duplicate | yes (pure function of FetchResult; E2) | negligible | **truncated `PARTIAL` documents** (E5); identical non-content pages (E6) | stored; S06 **verifies** it [REC, OD-7] |
| `normalized_text_sha256` | — | — | — | — | — | — | **not created** [REC]: no extra normalization (§4); `text_sha256` already is the normalized-text hash defined by S05 |
| `source_identity` [REC] | `final_url`, else `requested_url` | `normalize_url` (S01, unchanged) | L1 same source | yes (pure string function) | n/a (string key, no hash) | `ref`-style non-tracking params keep URLs apart (false negative); a shared final URL for different requests merges (correct) | recomputed (cheap); kept in the output reference |
| `document_identity` [REC] | — | — | instance reference inside one run | — | — | — | no new hash: a document is referenced by its **input position** and `document_id` (instance id). A persistent cross-run id belongs to the database sprint (C-4, C-5) |

Rules [REC]: keys carry their level and algorithm (`L2:sha256:<hex>`, `L3:sha256:<hex>`,
`L1:url:<normalized>`); full 64-hex digests, never truncated; hashes are equality keys only — never
evidence of semantic equivalence.

## 6. Source / document identity

- **source_identity** [REC] = `normalize_url(provenance.final_url or provenance.requested_url)`, using the
  S01 normalizer unchanged [CONTRACT]: trailing slash, `www.`, path case and non-listed query parameters
  stay distinguishing (E1); the S01 tracking list applies (E6: `utm_source` and fragment variants collapse).
- Redirects: two requested URLs whose redirect chains end at the same final URL share the identity
  (that is what the final URL is for); `redirect_chain` is kept for audit.
- **Claimed canonical URL is never an identity input** [CONTRACT S05: `claimed_metadata` is unverified]:
  a page claiming `canonical_url = victim` must not join the victim's group (provenance spoofing). It is
  passed through for later stages as a claim.
- `og:url`, JSON-LD `@id`/`url`, `SearchResult.metadata`: not identity inputs [REC].
- `ref` and other parameters outside the S01 list: no change in S06 [CONTRACT]; extending the list is
  [OPEN] (OD-6) and would be an S01 change.
- Invalid URL (`InvalidURLError`): no identity; the document is kept, excluded from L1, error
  `INVALID_SOURCE_URL` [REC].

## 7. Dedup levels

Levels are computed **independently** and reported as separate group lists; there is no transitive
cross-level merging [REC, OD-9]. Grouping never removes a document from the output.

| | L1 same source | L2 exact raw content | L3 exact extracted text | L4 near duplicate |
|---|---|---|---|---|
| Input | all documents | documents with `content_sha256` (fetch `OK`) | `status == SUCCESS`, non-empty `text`, verified `text_sha256` | — |
| Key | `source_identity` | `content_sha256` | `text_sha256` | — |
| Rule | equal key, ≥ 2 members | equal key, ≥ 2 members | equal key, ≥ 2 members | **not implemented** [OPEN, OD-4] |
| Representative | lowest input position [REC, OD-3] | lowest input position | lowest input position | — |
| Provenance | every member reference (§9) | every member | every member | — |
| Group warnings [REC] | `SOURCE_CONTENT_DIFFERS` (members' `content_sha256` differ, e.g. page changed between fetches), `MIXED_FETCH_STATUS` | `RAW_DUPLICATE_TEXT_DIFFERS` (members' `text_sha256` differ — E3), `CROSS_SOURCE_DUPLICATE` | `CROSS_SOURCE_DUPLICATE` (members from > 1 source identity / host — E6) | — |
| Exclusions [REC] | `INVALID_SOURCE_URL` | `NO_CONTENT_HASH` | `NOT_SUCCESS` (incl. `PARTIAL` — E5), `EMPTY_TEXT`, `TEXT_HASH_MISMATCH`, `MISSING_TEXT_HASH` | — |
| False positive risk | none beyond the normalizer's contract (same normalized URL = same resource) | identical non-content responses (interstitials, error pages) | identical non-content pages; truncation (excluded) | — |
| False negative risk | non-listed tracking params, `www.`/slash variants (E1, E6) | any byte difference (timestamps, nonces) | any text difference (E2: one word, case, markup) | — |

- **Why L1 is still needed after S01's pre-fetch dedup**: S01 groups *search URLs*; S06 groups *fetched
  documents* by where they actually came from (redirects), and works on documents from any batch.
- **Why L2 and L3 both exist**: L2 detects identical responses (byte level) even when decoding diverges
  (E3, flagged); L3 is the evidence-level duplicate S07/S08 care about. With equal bytes, charset and
  extractor version, L2 groups are also L3 groups.
- **L3 excludes `PARTIAL`** [REC, OD-2]: equal truncated prefixes do not prove equal documents (E5).
- **L4 near duplicates** [OPEN, OD-4]: real evidence exists (E6: versioned PyPI page, similarity 0.998),
  but thresholds need a labelled sample and a policy on what "near" may merge. Candidate techniques (all
  stdlib-implementable): line/shingle Jaccard with MinHash (`hashlib`), SimHash. Deferred; no embeddings,
  no similarity library.

## 8. Winner selection

[REC] **No quality-based winner.** Each group has a *representative* = the member with the lowest input
position, mirroring the S01 precedent (first occurrence in a deterministic upstream order). All members
stay in the group with full provenance; consumers may use any member.

- For L3 the choice cannot change cited text (all members' texts are identical); it only decides which
  URL is listed first.
- Candidate criteria — extraction status, content completeness, metadata completeness, source quality,
  freshness, URL canonicality — are **not** adopted: no approved basis and some are page claims
  (metadata, canonical) or non-deterministic (`fetched_at`). [OPEN, OD-3]

## 9. Provenance model

[REC] S06 output (models in a new module, no copy of document text):

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

- Output is a **pure function of the input sequence** [REC]: `refs` and `documents` keep input order;
  groups are ordered by representative position; members ascending; group keys derived from content/URL,
  never from `uuid4` or time (E7).
- Python `dict` insertion order (defined behaviour) may be used; set iteration order and `hash()` must
  not influence output.
- Reordered input: the same groups as member *sets* (keys are content-derived); representatives and group
  order follow the new order. Order-invariance (e.g. ordering by `(source_identity, content_sha256)`) is
  possible but [OPEN, OD-3].
- No parallelism: grouping is O(n) and sub-second at 10 000 documents (E8).

## 11. Failure semantics

| Situation | Behaviour [REC] |
|---|---|
| A document's key cannot be derived (invalid URL, missing / mismatching `text_sha256`, text longer than S05's maximum) | document **kept** in `documents`/`refs`; excluded from that level with a reason; error code recorded; it is never merged on a doubtful key (fail closed for merging) |
| `NOT_FETCHED` / `UNSUPPORTED` / `FAILED` / `EMPTY` documents | kept; take part in L1 (and L2 when bytes exist); excluded from L3 with a reason |
| Input over the document limit | typed error **before** processing (`DedupInputTooLarge`); no partial result |
| Unexpected exception | propagates; never an empty or "no duplicates" result. When integrated, the caller records a stage error (e.g. `DEDUP_FAILED`) — JobRunner contract unchanged in S06 |
| Deadline / cancellation | pure synchronous function; worst measured case within limits ≈ 3 s (hash verification of 1 G chars, E8). Optional cooperative deadline check per 256 documents raising a typed timeout (no partial grouping) [OPEN, OD-8] |
| Memory | references only; peak growth ≤ 4.6 MB at 10 000 documents (E8); hash verification encodes one text at a time (≤ 4 bytes/char) |

## 12. Security limits

- All content and metadata stay **UNTRUSTED** [CONTRACT]; S06 never sets or upgrades `trust`.
- **Provenance spoofing**: identity uses only crawler-observed URLs; claimed canonical / `og:url` /
  JSON-LD never feed keys (§6).
- **Duplicate poisoning / false corroboration**: exact copies across hosts (scrapers, mirrors, shared
  interstitials — E6) are flagged `CROSS_SOURCE_DUPLICATE`; S07 must count independent **groups**, not
  URLs (C-3). S06 does not decide that a page is junk — challenge-page detection is outside S06 (C-7).
- **Hash input limits**: hashes are recomputed only over `text` already bounded by S05 (≤ 1 M chars by
  default, ≤ 10 M by configuration); a longer text is a contract violation → excluded with an error.
- **Pathological Unicode**: no normalization in S06, so no NFC/NFKC amplification; hashing is linear
  (E8: 1 M combining-mark chars ≈ 2.9 ms).
- **Oversized input**: document-count limit (OD-8); S06 adds no copies of texts.
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

Budgets derived from E8 [REC, OD-8]: `MAX_DOCUMENTS = 2 000` (2.5 × the 800-hit job maximum, §2.2);
grouping ≤ 0.5 s and grouping + verification ≤ 5 s at that limit with S05-capped texts; peak RSS growth
≤ 32 MB. Input memory is the caller's (already held after S05).

## 14. Dependency decision

**No new dependency** [REC]. `hashlib`, `unicodedata`, existing `normalize_url` and pydantic cover the
design. Not added: `rapidfuzz` (ARCHITECTURE §9, entity matching — C-1), `datasketch`/`simhash` (L4
deferred), `xxhash`/`blake3` (SHA-256 verification costs ≈ 2.9 ms per 1 M chars, E8), URL libraries (S01
normalizer is the contract), Unicode libraries (no normalization in S06).

## 15. Test matrix (for implementation)

| Group | Cases |
|---|---|
| Normalization | S06 never changes `text`/title/metadata; NFC/NFD inputs (via S05) hash equal; NFKC-sensitive text (`mc²`, `①`, full-width) untouched; code fences/tables/lists untouched; empty text never keyed; Zalgo/ZWJ text hashed linearly |
| Identity | same URL; slash / fragment / `utm_*` / `ref` / host case / default port / percent-encoding variants (expectations = S01 normalizer, E1); redirect to a shared final URL; claimed canonical pointing elsewhere does **not** merge; invalid URL → `INVALID_SOURCE_URL`, document kept |
| Dedup | 2-way and N-way groups per level; L1 with different content (`SOURCE_CONTENT_DIFFERS`); L2 with different texts (`RAW_DUPLICATE_TEXT_DIFFERS`, E3); L3 across hosts (`CROSS_SOURCE_DUPLICATE`); chains (A~B at L2, B~C at L3) stay separate per level; mixed unique/duplicates; duplicates with different metadata, warnings, statuses, search provenance — all retained |
| Eligibility | `PARTIAL` truncated twins not grouped at L3 (E5); `EMPTY`/`NOT_FETCHED`/`FAILED` never L3; `NOT_FETCHED` still L1 |
| Provenance | every input document appears exactly once in `refs` with all fields equal to its source; groups list all members; `trust`/`kind` preserved; `search_source` is the same object; citation of a sentence resolves to all L3 member URLs |
| Determinism | same input × 50 runs → identical output; reversed/shuffled input → identical member sets, representatives follow order; no dependence on `PYTHONHASHSEED` (run in subprocesses with different seeds) |
| Security | provenance-spoof page (claimed canonical = other URL); shared interstitial across hosts; text longer than the S05 maximum; 2 001 documents → `DedupInputTooLarge`; tampered `text_sha256` → `TEXT_HASH_MISMATCH`, not grouped |
| Failure | one bad document among good ones (kept, excluded, error); all documents bad (empty groups, every document kept with errors — not an exception); injected exception inside grouping propagates (no empty result) |
| Performance | benchmark scenarios of §13 as a `benchmark`-marked test with the budgets of OD-8 |
| Real world | §17 |

## 16. Mutation matrix (for implementation)

| # | Mutant | Must be caught by |
|---|---|---|
| N1 | S06 rewrites/normalizes `text` (any transform) | normalization "unchanged" tests |
| N2 | NFC → NFKC anywhere | NFKC-sensitive text test |
| N3 | hash input changed (e.g. title included, `text.strip()`, other encoding) | verification + L3 grouping tests |
| N4 | exact dedup disabled (L3 or L2 returns no groups) | 2-way/N-way tests |
| N5 | wrong key (L3 on `content_sha256`, L1 on `requested_url`, L1 on claimed canonical) | E3, redirect and spoof tests |
| N6 | provenance lost (member dropped, field not copied, `search_source` dropped) | provenance tests |
| N7 | non-deterministic representative (e.g. `set`, `hash()`-ordered, random) | determinism tests (multiple seeds) |
| N8 | ordering reversed (groups or members) | ordering tests |
| N9 | duplicate group removed / members collapsed into one document | group-retention tests |
| N10 | normalization/key error swallowed (document dropped or silently grouped) | failure tests |
| N11 | input limit ignored | limit test |
| N12 | `PARTIAL` admitted to L3 | truncation twin test |
| N13 | empty text keyed (`text_sha256 None` grouped) | empty test |
| N14 | trust elevated / `kind` changed | provenance test |

## 17. Real-world validation plan

S04 → S05 → S06 on egress-allowed hosts only (no bypass), opt-in `live_network` test:
`https://pypi.org/project/httpx/`, `…/?utm_source=probe`, `…/#history` (expect one L1/L2/L3 group of 3),
`…/0.28.1/` (expect no group: near duplicate, L4 out of scope), `…/project/httpx` and `…/project/HTTPX/`
(expect an L2/L3 group of two challenge pages flagged as duplicates across source identities — recorded,
not "fixed"). Records: levels, keys (hash prefixes), members, warnings, exclusions. Nothing sent to an LLM.

## 18. Architecture conflicts

| Id | Description | Evidence | Impact | Proposed resolution |
|---|---|---|---|---|
| C-1 | ARCHITECTURE §8/§9/§21 define Sprint 06 as normalization of **extracted entity fields** (price, phone, currency, names) and **entity resolution** (weighted scores, `phonenumbers`, `rapidfuzz`, merge thresholds); this brief defines S06 as **document-level** normalization/dedup | ARCHITECTURE §8, §9, §21 row 06; no entity records exist (AI structured extraction unscheduled since Sprint 05 OD-1) | entity normalization/resolution cannot be built now; document-level S06 does not need those dependencies | S06 = document level; entity normalization and resolution move after AI extraction; roadmap update in the implementation commit (OD-1) |
| C-2 | Roadmap numbering: brief lists S08 = Synthesis/Report; ARCHITECTURE §21 has 08 Database, 09 UI, 10 Export, 11 Hardening; PERSISTENCE-BLOCKER-01 is tied to "Sprint 08" persistence | ARCHITECTURE §21; docs/sprint-03-research-api.md §22 | the blocker's deadline and the order of persistence vs. synthesis are ambiguous | founder confirms roadmap; blocker text follows (OD-1) |
| C-3 | ARCHITECTURE §10 verification counts independent domains and requires a document row for each cited URL | ARCHITECTURE §10 ("Source exists", `n_independent_domains`) | collapsing duplicates would lose URLs; counting URLs of an exact-copy group would inflate independence | S06 keeps every member and lists hosts per group; S07 requirement: count independent groups (recorded here, decided in S07) |
| C-4 | ARCHITECTURE §5/§12 persistent identity "one `documents` row per `(url, content_hash)`" — `content_hash` undefined (raw `content_sha256` or text `text_sha256`), `url` normalized or final | ARCHITECTURE §5, §12 | ambiguity only bites at persistence | S06 defines no persistent key; decide in the database sprint (OD-10) |
| C-5 | Provenance tables expect stable `document_id`s; S04/S05 ids are random per run | E7; ARCHITECTURE §11/§12 | no cross-run identity today | S06 references documents by input position + instance ids; stable ids with persistence |
| C-6 | Tracking-parameter policy exists (S01) but excludes `ref` (and other site-specific params) | E1 | some duplicates stay separate (false negatives) | keep S01 policy in S06; extension is an S01 change (OD-6) |
| C-7 | S05 has no interstitial/challenge-page detection; such pages are `SUCCESS` documents | E6 (PyPI "Client Challenge", HTTP 200) | non-content pages can form cross-source duplicate groups and reach S07 as evidence | S06 flags cross-source duplicates only; challenge detection is a separate S05/S07 decision (OD-11) |

## 19. Open decisions

| Id | Question | Recommendation |
|---|---|---|
| OD-1 | Accept document-level S06 and update ARCHITECTURE roadmap (C-1, C-2) | accept; confirm sprint order |
| OD-2 | L3 eligibility | `SUCCESS` only; `PARTIAL` excluded (E5) |
| OD-3 | Representative / winner | lowest input position (S01 precedent), no quality heuristic; order-invariance not required |
| OD-4 | Near duplicates (L4) | defer; separate design with a labelled sample |
| OD-5 | Extra normalized-text key (case-fold, Markdown-agnostic) | no |
| OD-6 | Extend tracking-parameter list (`ref`, …) | no change in S06 (S01 contract) |
| OD-7 | Verify `text_sha256` by default | yes (≈ 2.9 ms per 1 M chars) |
| OD-8 | Limits and deadline | `MAX_DOCUMENTS = 2 000`; budgets of §13; cooperative deadline optional, raising a typed timeout |
| OD-9 | Cross-level combined clusters (union-find over L1–L3) | no; levels reported separately |
| OD-10 | Persistent document identity `(url, content_hash)` | decide in the database sprint |
| OD-11 | Interstitial / challenge-page detection | separate follow-up outside S06 |
| OD-12 | Module location / name | `src/research_agent/dedup/` (`config`, `models`, `identity`, `grouping`) |

## 20. Explicit non-goals

LLM calls, embeddings, vector stores, semantic similarity, near-duplicate merging, entity normalization
and resolution (C-1), verification / fact checking, citation generation, synthesis, report/export,
crawling, link following, persistence/database, JobRunner or API integration, UI, Deep Search
orchestration, changes to S01–S05 (including `normalize_url` and the tracking list), new dependencies.

## 21. Acceptance criteria (implementation sprint)

1. `group_documents(documents) -> DocumentSet` is pure, synchronous, offline and deterministic (§10).
2. No input document is ever dropped or modified; `trust`/`kind` preserved; `text` untouched.
3. L1/L2/L3 exactly as §7, separate lists, representatives per OD-3, warnings and exclusions as listed.
4. Failure semantics of §11 (kept + excluded + error; typed errors; no empty result on exceptions).
5. Limits and budgets of OD-8 enforced and benchmarked.
6. Test matrix §15 implemented; mutation matrix §16 all killed or documented as equivalent with reason.
7. Real-world validation §17 recorded (opt-in); S01–S05 suites unchanged and green; ruff, mypy strict,
   bandit, pip-audit, secret scan, `git diff --check` clean; no new dependency.

## 22. Implementation plan (next sprint, after approval)

1. `src/research_agent/dedup/config.py` — limits (`MAX_DOCUMENTS`, verification switch).
2. `models.py` — `DocumentRef`, `DuplicateGroup`, `DocumentSet`, warning/exclusion/error enums.
3. `identity.py` — `source_identity()` over `normalize_url`; key builders with level prefixes.
4. `grouping.py` — single pass in input order, per-level dict grouping, eligibility, warnings, errors.
5. Tests: `tests/unit/test_dedup_identity.py`, `tests/unit/test_dedup_grouping.py`,
   `tests/integration/test_dedup.py` (with S05 fixtures), benchmark, live test.
6. Mutation run (§16); docs: as-built section here; ARCHITECTURE roadmap update only if OD-1 approved.
