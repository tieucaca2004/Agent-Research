# Sprint 06 — Normalization / Dedup (design)

Status: **DESIGN approved at `15afa1e`; IMPLEMENTED** — as-built record and evidence in §25 (design text
below unchanged). No new dependency. Decision classes:
**[EXISTING CONTRACT]** = fixed by existing code or an approved design; **[APPROVED DECISION]** = stated
or explicitly closed by the founder in the Sprint 06 reviews (design review of `3a10e30`; decision closure
of `e6557da`); **[RECOMMENDATION]** = proposed, not approved; **[OPEN]** = undecided, with an owner.
A recommendation is never treated as an approval; no item is both APPROVED and OPEN.
§0 is the authoritative register; where older wording below differs, §0 prevails.
Evidence ids `E1…E11` refer to probes run for this design (scratch scripts, not committed).

## 0. Decision register (after the decision closure of `e6557da`)

| # | Policy | Class | Basis |
|---|---|---|---|
| P1 | **L1** groups documents by `source_identity = normalize_url(final_url or requested_url)` with the S01 normalizer unchanged | APPROVED DECISION (grouping) on EXISTING CONTRACT (normalizer semantics) | §6; E1, E6 |
| P2 | **L2** groups by raw `content_sha256` exactly as produced by S04 | APPROVED DECISION | §7; E3 (may group differently-decoded texts → flagged) |
| P3 | **L3** groups by `text_sha256`, **only `status == SUCCESS`** documents with non-empty text | APPROVED DECISION | §7; E5 (`PARTIAL` truncation twins) |
| P4 | **L4** near-duplicate detection is **not implemented in S06** | APPROVED DECISION | §7; a later design is OD-4 |
| P5 | **Provenance**: no document is ever removed or modified; no source reference is lost; no synthetic merged document | APPROVED DECISION | §9 |
| P6 | **Representative** = member with the lowest position **in the sequence the caller passes** | APPROVED DECISION | §8. S06 makes no claim that this sequence is the S01 rank order; that end-to-end property is OD-13 (integration sprint) |
| P7 | **No transitive merging** across L1/L2/L3; each level is its own group list | APPROVED DECISION | §7 |
| P8 | **Hash verification uses each hash's own input semantics**: `text_sha256` = SHA-256 of `text` encoded UTF-8 (S05 `extractor.py`: `hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None`) → recomputable; `content_sha256` = SHA-256 of decompressed body bytes (S04) → **not verifiable in S06** (no raw bytes; re-hashing text is the wrong input, E3) → used as given, format-checked only | APPROVED DECISION (principle) on EXISTING CONTRACT (hash definitions) | §5; E11 |
| P8a | **`text_sha256` verification is always on** (no switch): every L3 candidate's hash is recomputed from its `text` before it can be grouped; any mismatch, missing hash, hash on empty text or unencodable text excludes the document from L3 (kept, error recorded) | APPROVED DECISION (OD-7 closed) | §5.1; E8, E11 |
| P9 | **No second text normalization** in S06 | APPROVED DECISION | §4; E4 |
| P10 | **No tracking-parameter policy of S06's own**; only the S01 list applies; `ref` stays distinguishing | APPROVED DECISION on EXISTING CONTRACT | §6; C-6 |
| P11 | **Bot-challenge / interstitial detection is out of S06** | APPROVED DECISION | §23; C-7 |
| P12 | **No S06-specific document-count limit and no S06 deadline**; only input-contract checks (type, text length ≤ S05 maximum, hash format) | APPROVED DECISION (OD-8 and OD-8b closed) | §11.1 — including what the benchmark does **not** show |
| P13 | Claimed canonical URL, `og:url`, JSON-LD ids never feed identity keys | EXISTING CONTRACT (S05: claimed metadata is unverified) | §6 |
| P14 | `trust="UNTRUSTED"` and `kind` preserved, never elevated | EXISTING CONTRACT (S05) | §9 |
| P15 | **Deterministic for the exact input sequence passed by the caller**: same sequence → same output; keys never from `uuid4`, time, `hash()` or set order | APPROVED DECISION | §10; E7 |
| P15b | Pure, synchronous function with no I/O (no network, files, LLM) | RECOMMENDATION (no-LLM / no-fetch are approved scope limits; "synchronous, no I/O" is the recommendation) | §10, §20 |
| P16 | General failure semantics: kept + excluded + error code; `TypeError` for non-document input; exceptions propagate, never an empty result | RECOMMENDATION (the hash-verification part is approved under P8a) | §11 |
| P17 | **Module location `src/research_agent/dedup/`** | APPROVED DECISION (OD-12 closed) | §22; repository precedent: stages are top-level packages (`crawler/`, `extraction/`); no `dedup` module exists. Naming note: ARCHITECTURE's planned layout lists `pipeline/dedup.py` for *entity* resolution — that future module must get a different name (§24) |
| P18 | Names of group warnings, exclusion reasons and error codes (§7, §11); output model shape (§9) | RECOMMENDATION | §7, §9, §11 |

Open items (owners): OD-1, OD-1b (founder, roadmap/architecture text), OD-4 (future L4 design), OD-6 (S01
change request), OD-10 (database sprint), OD-11 (future S05/S07), OD-13, OD-14 (integration sprint) — §19.

## 1. Scope and baseline

| Item | Value |
|---|---|
| Baseline | design written on `55b858d` `fix(research): prevent writes after terminal job state`; reviewed at `3a10e30` (design review) and `e6557da` (decision closure); docs only — no source change since `55b858d` |
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
| E8 | Prototype grouping benchmark (§13) — superseded for the implementation by the as-built measurements in §25 | grouping ≤ 135 ms for 10 000 documents, peak RSS growth ≤ 4.6 MB (prototype); hash re-verification ≈ 2.9 ms per 1 M chars; input texts dominate memory |
| E10 | Upstream ordering guarantees | S01: `SearchRun.to_response` orders by (query order, provider priority, rank, index) — `tests/unit/test_search_response.py::test_ordering_is_deterministic_regardless_of_completion_order`; S04: `fetch_many` returns results in input order — `tests/integration/test_crawler.py:837`; S05: one document per call (order = caller's). No component defines the order of the S06 input end to end (no integration yet) |
| E11 | `text_sha256` recomputation (OD-7) | recomputing `sha256(text.encode("utf-8"))` reproduced S05's `text_sha256` for 13/13 documents (9 fixtures, app shell `EMPTY` → `None`, emoji/ZWJ, NFD source, `PARTIAL` 1 M chars); ≈ 2.1 ms per 1 M Vietnamese chars; a lone surrogate (impossible from S05 — decoding uses `replace`, invalid char refs become U+FFFD — but possible in a hand-built document) makes `encode("utf-8")` raise `UnicodeEncodeError` |
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
| `text_sha256` [EXISTING CONTRACT S05] | `ExtractedDocument.text` as UTF-8 | S05 extraction + normalization (E4) | L3 exact text duplicate | yes (pure function of FetchResult; E2) | negligible | **truncated `PARTIAL` documents** (E5); identical non-content pages (E6) | stored; S06 **always verifies** it with the same input semantics before L3 grouping [APPROVED DECISION P8a] |
| `normalized_text_sha256` | — | — | — | — | — | — | **not created** [APPROVED DECISION P9 → no extra normalization]: `text_sha256` already is the normalized-text hash defined by S05 |
| `source_identity` [APPROVED DECISION P1] | `final_url`, else `requested_url` | `normalize_url` (S01, unchanged) | L1 same source | yes (pure string function) | n/a (string key, no hash) | `ref`-style non-tracking params keep URLs apart (false negative); a shared final URL for different requests merges (correct) | recomputed (cheap); kept in the output reference |
| `document_identity` [RECOMMENDATION] | — | — | instance reference inside one run | — | — | — | no new hash: a document is referenced by its **input position** and `document_id` (instance id). A persistent cross-run id belongs to the database sprint (C-4, C-5) |

### 5.1 `text_sha256` verification (OD-7 closed → P8a)

| Aspect | Decision |
|---|---|
| Input | exactly S05's: `ExtractedDocument.text` encoded UTF-8 (strict), SHA-256, lower-case hex; `None` when `text == ""` |
| When | for every L3 candidate (`status == SUCCESS`, non-empty text), before its key is used; L1/L2 do not depend on it |
| Outcomes | equal → eligible for L3; `text_sha256` missing on non-empty text → `MISSING_TEXT_HASH`; different → `TEXT_HASH_MISMATCH`; `UnicodeEncodeError` → `TEXT_HASH_UNVERIFIABLE`; hash present on empty text → `TEXT_HASH_MISMATCH`. In every case the document is **kept**, excluded from L3 only, and the error recorded (fail closed for merging) |
| Cost | ≈ 2.1–2.9 ms per 1 M chars (E8, E11); linear; one text encoded at a time (transient ≤ 4 bytes/char) |
| Switch | none: an off switch would allow merging on unverified keys; the measured cost does not justify one |
| Not covered | `content_sha256` is never "verified" (no raw bytes; P8); hashes never prove semantic equivalence |

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
| Input | all documents | documents whose `content_sha256` is present and well-formed (64 lower-case hex), used as given — never verified (P8). S06 does not check `fetch_status`: in the pipeline S04 sets the hash only for `OK` fetches with a body (§25, F-3) | `status == SUCCESS`, non-empty `text`, `text_sha256` verified against `text` (P8a) | — |
| Key | `source_identity` | `content_sha256` | `text_sha256` | — |
| Rule | equal key, ≥ 2 members | equal key, ≥ 2 members | equal key, ≥ 2 members | **not implemented in S06** [APPROVED DECISION P4] |
| Representative | lowest input position [APPROVED DECISION P6] | lowest input position | lowest input position | — |
| Provenance | every member reference (§9) | every member | every member | — |
| Group warnings [RECOMMENDATION] | `SOURCE_CONTENT_DIFFERS` (members' `content_sha256` differ, e.g. page changed between fetches), `MIXED_FETCH_STATUS` | `RAW_DUPLICATE_TEXT_DIFFERS` (members' `text_sha256` differ — E3), `CROSS_SOURCE_DUPLICATE` | `CROSS_SOURCE_DUPLICATE` (members from > 1 source identity / host — E6) | — |
| Exclusions [RECOMMENDATION] | `INVALID_SOURCE_URL` | `NO_CONTENT_HASH`, `INVALID_CONTENT_HASH` | `NOT_SUCCESS` (incl. `PARTIAL` — E5), `EMPTY_TEXT`, `TEXT_HASH_MISMATCH`, `MISSING_TEXT_HASH`, `TEXT_HASH_UNVERIFIABLE` | — |
| False positive risk | none beyond the normalizer's contract (same normalized URL = same resource) | identical non-content responses served as `OK` (interstitials / bot challenges, soft error pages with HTTP 200) | identical non-content pages; truncation (excluded) | — |
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

[APPROVED DECISION P6] **No quality-based winner.** Each group has a *representative* = the member with the
lowest position in the sequence the caller passes, mirroring the S01 precedent (first occurrence in a
deterministic order). All members stay in the group with full provenance; consumers may use any member.

- **Function contract vs. end-to-end order.** S06 only promises P6/P15 *for the sequence it receives*. It
  does not check, and the design does not claim, that this sequence is the S01 rank order. Today each
  component preserves order (E10), but no integration composes them; whether the representative is the
  "best-ranked" search hit is therefore unproven until the integration sprint guarantees and tests it (OD-13).

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

- Output is **deterministic for the exact input sequence** [APPROVED DECISION P15]: `refs` and `documents` keep input order;
  groups are ordered by representative position; members ascending; group keys derived from content/URL,
  never from `uuid4` or time (E7).
- Python `dict` insertion order (defined behaviour) may be used; set iteration order and `hash()` must
  not influence output.
- Reordered input: the same groups as member *sets* (keys are content-derived); representatives and group
  order follow the new order — intended, since the representative is defined by upstream order (P6).
  Order-invariant representatives are not part of S06.
- No parallelism: grouping is O(n) and sub-second at 10 000 documents (E8).
- End-to-end order (search rank → fetch → extraction → S06) is **not** an S06 property and is not claimed
  (OD-13); S06 tests only cover order relative to the input sequence.

## 11. Failure semantics

| Situation | Behaviour [RECOMMENDATION P16] |
|---|---|
| A document's key cannot be derived (invalid URL, missing / mismatching / unverifiable `text_sha256` (§5.1), malformed `content_sha256`, text longer than S05's configurable maximum of 10 M chars) | document **kept** in `documents`/`refs`; excluded from that level with a reason; error code recorded; never merged on a doubtful key (fail closed for merging) |
| `NOT_FETCHED` / `UNSUPPORTED` / `FAILED` / `EMPTY` / `PARTIAL` documents | kept; take part in L1, and in L2 when they carry a well-formed `content_sha256` (from S04: fetched `OK` with a body — `UNSUPPORTED` / `FAILED` / `EMPTY` / `PARTIAL` can; `NOT_FETCHED` never does); excluded from L3 with a reason (P3) |
| Input is not a sequence of `ExtractedDocument` | `TypeError` before processing (programming error, not data) |
| Unexpected exception | propagates; never an empty or "no duplicates" result. When integrated, the caller records a stage error (e.g. `DEDUP_FAILED`) — JobRunner contract unchanged in S06 |
| Deadline / cancellation | **no S06 deadline** [APPROVED DECISION P12]; synchronous function (P15b). Measured: verification of 1 G chars ≈ 2.9 s (E8); 800 documents at the S05 *default* cap (800 M chars) extrapolates to ≈ 2.3 s. Time bounds for the whole pipeline stage belong to the integration (OD-14), not to S06 |
| Memory | references only; peak growth ≤ 4.6 MB at 10 000 documents (E8, prototype; as-built ≈ 24 MB per call at 10 000 documents — §25); verification encodes one text at a time (≤ 4 bytes/char) |

### 11.1 Document limit and deadline (OD-8 / OD-8b closed → P12)

| Question | Answer (evidence) |
|---|---|
| Upstream bound today | ≤ 800 search hits per job (8 queries × 50 results × 2 providers, §2.2), already deduplicated by URL in S01; one ExtractedDocument per fetched URL |
| S06's own cost | grouping is O(n): 10 000 documents in ≤ 135 ms with ≤ 4.6 MB peak growth (E8, prototype; as-built figures in §25). The only text-proportional cost is `text_sha256` verification (≈ 2.1–2.9 ms per 1 M chars, E8/E11) |
| Does a count limit bound the real cost? | no — verification cost follows total characters, not document count; memory for texts is held by the caller before S06 runs |
| Decision | **no S06 document-count limit, no S06 deadline** [APPROVED DECISION P12]; input-contract checks only (type, text length ≤ S05's 10 M-char maximum, hash format). No truncation, no partial results |
| What the benchmark does **not** show | the 10 000-document result measures **grouping overhead** only. It does **not** show that every text volume is safe: verification is linear in total characters and was measured up to **1 G chars** (≈ 2.9 s, ≈ 2 GB of input held by the caller). Larger totals — e.g. 800 documents at the 10 M-char S05 *configurable maximum* = 8 G chars — were **not** measured and are **not** claimed safe |
| Where total volume is bounded | upstream: S05 per-document cap (1 M default) and job sizing. Whether the integration needs a total-text bound or a stage time limit is OD-14 (integration sprint) — not a new S06 limit |

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
  limit or deadline (P12, §11.1); S06 adds no copies of texts. Total-volume bounds: OD-14.
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

_Historical baseline: the table and figures in this section are **prototype** measurements (E8), taken
before implementation; they are kept as the design basis. The as-built measurements in §25 supersede
them for the implementation._

Peak RSS growth of the grouping step: ≤ 4.6 MB in every scenario (prototype). 10 000 × 1 M-char pathological
documents (≈ 20 GB of input) was not run: the *inputs* exceed this machine, S06 is not the bottleneck.

Performance budgets for the implementation's benchmark test, derived from E8 [RECOMMENDATION]: at 2 000
documents (2.5 × the 800-hit job maximum — a **test size, not a runtime limit**, P12) grouping ≤ 0.5 s,
grouping + verification ≤ 5 s for texts up to 100 k chars each, peak RSS growth ≤ 32 MB. These budgets
regress the measured behaviour; they do not certify larger text volumes (§11.1). Input memory is the
caller's.

## 14. Dependency decision

**No new dependency** [RECOMMENDATION]. `hashlib`, `unicodedata`, existing `normalize_url` and pydantic cover the
design. Not added: `rapidfuzz` (ARCHITECTURE §9, entity matching — C-1), `datasketch`/`simhash` (L4
deferred), `xxhash`/`blake3` (SHA-256 verification costs ≈ 2.9 ms per 1 M chars, E8), URL libraries (S01
normalizer is the contract), Unicode libraries (no normalization in S06).

## 15. Test matrix (for implementation)

Only S06's own contract (the function over a given input sequence). Integration-level properties are listed
separately in §15.1 and are **not** S06 tests.

| Group | Cases |
|---|---|
| Normalization (P9) | S06 never changes `text`/title/metadata (object identity and equality); NFC/NFD sources (via S05) hash equal; NFKC-sensitive text (`mc²`, `①`, full-width) untouched; code fences/tables/lists untouched; empty text never keyed; Zalgo/ZWJ text hashed linearly |
| Identity (P1, P10, P13) | same URL; slash / fragment / `utm_*` / `ref` / host case / default port / percent-encoding variants (expectations = S01 normalizer, E1); `ref` stays distinguishing; redirect to a shared final URL groups at L1; `final_url` missing → `requested_url`; claimed canonical / `og:url` pointing elsewhere does **not** merge; invalid URL → `INVALID_SOURCE_URL`, document kept |
| L1/L2/L3 (P1–P3) | 2-way and N-way groups per level; L1 with different content (`SOURCE_CONTENT_DIFFERS`), mixed fetch statuses (`MIXED_FETCH_STATUS`); L2 with different texts (`RAW_DUPLICATE_TEXT_DIFFERS`, E3); L3 across hosts (`CROSS_SOURCE_DUPLICATE`); mixed unique/duplicates; duplicates with different metadata, warnings, statuses, search provenance — all retained |
| No transitive merging (P7) | A~B only at L2, B~C only at L3 → no group contains A and C; each level's groups computed from its own key only |
| Eligibility (P3) | `PARTIAL` truncation twins (E5) not grouped at L3 but still L1/L2; `EMPTY`/`NOT_FETCHED`/`FAILED`/`UNSUPPORTED` never L3; `NOT_FETCHED` still L1 |
| Hash verification (P8, P8a) | recomputed `sha256(text UTF-8)` equals S05's for real S05 output; tampered hash → `TEXT_HASH_MISMATCH`; missing hash on non-empty text → `MISSING_TEXT_HASH`; hash on empty text → `TEXT_HASH_MISMATCH`; hand-built document with a lone surrogate → `TEXT_HASH_UNVERIFIABLE` (no crash); in all cases document kept and excluded from L3 only; there is no way to disable verification; `content_sha256` never recomputed from text (same bytes / different charsets stay one L2 group, E3); malformed `content_sha256` → `INVALID_CONTENT_HASH`, excluded from L2 |
| Provenance (P5, P14) | every input document appears exactly once in `refs`, fields equal to its source; `documents` are the same objects in the same order; groups list all members, `source_identities` and `hosts`; `trust`/`kind` preserved; `search_source` is the same object; for any L3 member the group exposes all member URLs and their search provenance |
| Representative / ordering (P6) | representative = lowest position in every group; groups ordered by representative; members ascending; reversed/shuffled input → identical member sets, representatives follow the new order |
| Determinism (P15) | same input sequence × 50 runs → identical output; subprocess runs with different `PYTHONHASHSEED` → identical output; no `uuid4`/time in keys |
| Security | provenance-spoof page (claimed canonical = another URL); shared interstitial across hosts → one L2/L3 group with `CROSS_SOURCE_DUPLICATE`, all members kept; text longer than 10 M chars → excluded with error |
| Failure (P16) | one bad document among good ones (kept, excluded, error); all documents bad (no groups, every document kept with errors — not an exception); non-`ExtractedDocument` input → `TypeError`; injected exception inside grouping propagates (no empty result) |
| No limit / no deadline (P12) | 10 000 documents processed completely (no truncation, no error); no deadline parameter exists |
| Performance | §13 scenarios as a `benchmark`-marked test with the §13 budgets |
| Real world | §17 (opt-in, standalone S04 → S05 → S06; not a JobRunner integration) |

### 15.1 Deferred to the integration sprint (not S06 tests; do not exist yet)

- End-to-end order: documents reach S06 in S01 result order through fetch and extraction, so the
  representative is the best-ranked hit (OD-13).
- Total text volume / stage time behaviour of the integrated pipeline (OD-14).
- Mapping of S06 exceptions to a job stage error (e.g. `DEDUP_FAILED`) without changing JobRunner semantics.

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
| N11 | `text_sha256` verification skipped (L3 groups on the stored hash without recomputing) | tampered-hash test |
| N12 | `PARTIAL` admitted to L3 | truncation twin test |
| N13 | empty text keyed (`text_sha256 None` grouped) | empty test |
| N14 | trust elevated / `kind` changed | provenance test |
| N15 | `content_sha256` "verified" by re-hashing `text` (wrong input semantics) | E3 test (one L2 group must survive) |
| N16 | transitive merging across levels (union of L1/L2/L3) | no-transitive-merging test |
| N17 | tracking parameters stripped beyond the S01 list (e.g. `ref`) | `ref` identity test |
| N18 | representative = highest position / last member | representative test |
| N19 | `UnicodeEncodeError` during verification propagates (crash) instead of `TEXT_HASH_UNVERIFIABLE` | lone-surrogate test |
| N20 | a hidden limit or truncation (e.g. only the first N documents grouped) | 10 000-document completeness test |

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
| C-2 | Roadmap: brief maps S07 = Verification, S08 = Synthesis/Report; ARCHITECTURE §21 has 07 Verification, 08 Database (must resolve PERSISTENCE-BLOCKER-01), 09 UI, 10 Export, 11 Hardening; no Synthesis sprint exists there | ARCHITECTURE §21; docs/sprint-03-research-api.md §22 | S07 maps consistently; S08 does not; the persistence blocker is tied to "the database sprint" | **OPEN (OD-1b), roadmap only**: not a technical dependency of the S06 module — S06 consumes S05 output and produces groups for whatever stage follows. Founder chooses (a) insert Synthesis/Report as 08 and shift Database → 09, or (b) keep Database 08 and schedule Synthesis later. ARCHITECTURE numbering is not changed by this design (§24, not applied) |
| C-3 | ARCHITECTURE §10 verification counts independent domains and requires a document row per cited URL | ARCHITECTURE §10 | collapsing duplicates would lose URLs; counting URLs of an exact-copy group would inflate independence (E6) | **Resolved in S06 design** (P5): every member kept; groups expose `source_identities` and `hosts`. **Requirement handed to S07** (not decided here): S07 must distinguish *source URLs* from *independent evidence groups* |
| C-4 | ARCHITECTURE §5/§12: one `documents` row per `(url, content_hash)`; `content_hash` undefined (raw `content_sha256` vs text `text_sha256`), `url` undefined (requested / final / normalized) | ARCHITECTURE §5, §12 | ambiguity only matters for persistence | **Deferred to the database sprint (OD-10)**. Recommendation for that sprint: store both hashes under their explicit names and the normalized final URL; never an unqualified `content_hash` |
| C-5 | Provenance tables expect stable `document_id`s; S04/S05 ids are random per run | E7; ARCHITECTURE §11/§12 | no cross-run identity today | **Accepted limitation**: S06 references documents by input position + instance ids, keys from content/URL only; stable ids with persistence. No change to S04/S05 |
| C-6 | Tracking-parameter policy exists (S01) but excludes `ref` and other site-specific parameters | E1 | some duplicates stay separate (false negatives, never false merges) | **Resolved**: S06 uses the S01 normalizer as-is (P10); any extension is a separate S01 change request (OD-6) |
| C-7 | S05 has no interstitial/bot-challenge detection; such pages are `SUCCESS` documents (HTTP 200) | E6 (PyPI "Client Challenge") | non-content pages can form cross-source duplicate groups and reach S07 as evidence | **Out of S06 scope** (P11, §23): S06 groups and flags only; detection is a future S05/S07 decision (OD-11) |

## 19. Open decisions

Closed (now in §0): OD-2 → P3, OD-3 → P6, OD-5 → P9, OD-9 → P7 (design review of `3a10e30`);
**OD-7 → P8a, OD-8 and OD-8b → P12, OD-12 → P17** (decision closure of `e6557da`).

Still open — none blocks implementing the S06 module:

| Id | Question | Owner | Recommendation |
|---|---|---|---|
| OD-1 | Update ARCHITECTURE §8/§9/§21 for document-level S06 (C-1) | founder | approve the §24 wording |
| OD-1b | Roadmap order after S07: Synthesis/Report vs Database as Sprint 08 (C-2) — roadmap only, no technical dependency | founder | founder choice |
| OD-4 | Design of near-duplicate detection (L4) | later sprint | separate design with a labelled sample |
| OD-6 | Extend the S01 tracking-parameter list (`ref`, …) | S01 change request | not in S06 |
| OD-10 | Persistent document identity `(url, content_hash)` | database sprint | store both hashes by name and the normalized final URL |
| OD-11 | Interstitial / bot-challenge detection | future S05/S07 | — |
| OD-13 | End-to-end input order (S01 rank order preserved through fetch/extraction into S06) | integration sprint | guarantee it and test it (§15.1) |
| OD-14 | Total text volume / stage time bounds for the integrated pipeline | integration sprint | decide from measurements at integration; not an S06 limit (P12) |

## 20. Explicit non-goals

LLM calls, embeddings, vector stores, semantic similarity, near-duplicate merging, entity normalization
and resolution (C-1), verification / fact checking, citation generation, synthesis, report/export,
crawling, link following, persistence/database, JobRunner or API integration, UI, Deep Search
orchestration, changes to S01–S05 (including `normalize_url` and the tracking list), new dependencies.

## 21. Acceptance criteria (implementation sprint)

1. `group_documents(documents) -> DocumentSet` is deterministic for the exact input sequence (P15); synchronous with no I/O if P15b is approved.
2. No input document is ever dropped or modified; `trust`/`kind` preserved; `text` untouched (P5, P9, P14).
3. L1/L2/L3 exactly as P1–P3 and §7, separate lists (P7), representative per P6, warnings and exclusions as listed (P18); L4 absent (P4).
4. Hash handling per P8/P8a: `text_sha256` always verified with S05's input semantics (§5.1); `content_sha256` used as given and format-checked, never recomputed from text.
5. Failure semantics of §11 (kept + excluded + error; no empty result on exceptions); no document-count limit and no deadline (P12).
6. Test matrix §15 implemented (§15.1 excluded); mutation matrix §16 all killed or documented as equivalent with reason.
7. Real-world validation §17 recorded (opt-in); S01–S05 suites unchanged and green; ruff, mypy strict,
   bandit, pip-audit, secret scan, `git diff --check` clean; no new dependency.

## 22. Implementation plan (next sprint, after approval)

Module: `src/research_agent/dedup/` (P17). No configuration module: verification has no switch (P8a) and
there is no limit or deadline (P12).

1. `__init__.py` — public API (`group_documents`, models).
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
5. §2 Module layout — note that the planned `pipeline/dedup.py` ("entity resolution") must get a distinct
   name when built (e.g. `entities/`), since document-level dedup lives in `src/research_agent/dedup/` (P17).

Impact: documentation only; no code, test or dependency change; `phonenumbers`/`rapidfuzz` remain future
mentions; PERSISTENCE-BLOCKER-01 unchanged (still PARTIALLY RESOLVED).

## 25. Implementation status and evidence (as built)

Implemented per §22 against the approved design (`15afa1e`); no decision in §0 was changed.

**Files.** `src/research_agent/dedup/{__init__,models,identity,grouping}.py` (no config module);
tests `tests/unit/test_dedup_identity.py`, `tests/unit/test_dedup_grouping.py`,
`tests/integration/test_dedup.py`, `tests/integration/test_dedup_benchmark.py` (marker `benchmark`),
`tests/live/test_dedup_live.py` (marker `live_network`), helper `tests/dedup_support.py`. No change to
S01–S05 source/tests, SearchService, crawler, extraction, JobRunner, API, repository, schema,
dependencies or ARCHITECTURE.md.

**API.** `group_documents(documents: Sequence[ExtractedDocument]) -> DocumentSet` — the only parameter;
no switch, limit or deadline. `DocumentSet`: `s06_version="s06.1"`, `documents` (the input objects, same
order), `refs` (one `DocumentRef` per position), `groups` (`L1`/`L2`/`L3` → `DuplicateGroup` lists),
`exclusions`, `errors`, `stats`. Keys: `L1:url:<identity>`, `L2:sha256:<content_sha256>`,
`L3:sha256:<recomputed text digest>`.

**As-built details (within the design).**
- Verification-by-construction (P8): the L3 key *is* the digest recomputed from `text.encode("utf-8")`;
  the stored `text_sha256` is only compared, never used as a key, so no code path groups at L3 without
  verification. Check order: not `SUCCESS` → `NOT_SUCCESS`; empty text → `EMPTY_TEXT` (or
  `TEXT_HASH_MISMATCH` if a hash is present); length > 10 000 000 (S05 `max_text_chars` upper bound,
  asserted equal in a test) → `TEXT_TOO_LONG`; no hash → `MISSING_TEXT_HASH`; `UnicodeEncodeError` →
  `TEXT_HASH_UNVERIFIABLE`; digest differs → `TEXT_HASH_MISMATCH`.
- `errors` = exclusions whose reason is a contract violation (`INVALID_SOURCE_URL`,
  `INVALID_CONTENT_HASH`, `TEXT_TOO_LONG`, `MISSING_TEXT_HASH`, `TEXT_HASH_MISMATCH`,
  `TEXT_HASH_UNVERIFIABLE`); ineligibility (`NOT_SUCCESS`, `EMPTY_TEXT`, `NO_CONTENT_HASH`) is not an error.
- Identity: S01 `normalize_url` raises plain `ValueError` (from `urlsplit`) for malformed hosts such as
  `http://[::1`, besides `InvalidURLError`; S06 treats both as "no identity" (`INVALID_SOURCE_URL`,
  document kept). S01 is **not modified** (known S01 behaviour, recorded here only).
- Input validation: `str`/`bytes`, non-sequences (incl. generators) and non-`ExtractedDocument` items →
  `TypeError` before any processing. Unexpected exceptions propagate (no empty/partial result).
- Log `dedup.grouped` carries counts only (no URLs, no text).

**Tests (this sprint).** 86 passed + 1 live (opt-in): identity 19, grouping 48, integration with real S05
extraction 16, benchmark 3. Covers every §15 group, including: tampered / forged-copy / missing / empty /
lone-surrogate / over-long hashes; tampered hash at text sizes 1 … 10 000 000 (no size fast path); a
recording test proving each L3 member was hashed from its own text exactly once; E3 same-bytes /
different-charset L2 group; PARTIAL twins; no transitive merging; provenance identity and unchanged
`model_dump`; representative / ordering / reversed input; 50 identical runs and 4 `PYTHONHASHSEED`
subprocesses; 10 000 documents complete.

**Mutation (§16).** 42 mutants over N1–N20 (variants per row), run on a scratch copy against the dedup
tests: **42 KILLED, 0 SURVIVED, 0 EQUIVALENT**. Variants: N1 (output rewritten, NFC before verify), N2
(NFKC), N3 (strip / UTF-16 / title in hash input), N4 (L2, L3 disabled), N5 (L3 on content hash, L1 on
requested URL, L1 on claimed canonical), N6 (member, `search_source`, `hosts`, `crawl_id`, `warnings`
lost), N7 (representative by `document_id`, groups by key, groups in set order), N8 (members / groups
reversed), N9 (2-member groups dropped, documents collapsed), N10 (identity exception swallowed, invalid
URL grouped, bad content hash grouped, L3 errors hidden), N11 (stored hash used, size fast path, cache on
stored hash, skip when raw hash seen), N12, N13, N14 (trust, kind), N15, N16, N17, N18, N19, N20 (document
limit, member cap).

**Benchmark — as-built measurement (supersedes the E8 prototype figures of §13 for the implementation).**
Method: `tests/dedup_support.py` / `test_dedup_benchmark.py`, one subprocess per scenario, inputs built
first, VmHWM reset, then `group_documents` called 3 times (best time kept; peak RSS over the 3 calls).
Machine: the shared Linux sandbox of this sprint, Python 3.11; synthetic `ExtractedDocument`s with
distinct hosts and valid hashes (`short_2000`: short texts in groups of 5; `large_2000`: unique
≈ 100 k-char Vietnamese texts, every text hash re-verified; `groups_10000`: short texts in groups of 5).

| Scenario | Docs | Input chars | Time, best of 3 (run 1 / review re-run) | Peak RSS growth over 3 calls (run 1 / re-run) | Single call peak RSS growth | Budget (§13) |
|---|---|---|---|---|---|---|
| short_2000 | 2 000 | 77 450 | 0.044 s / 0.044 s | 9.8 MB / 9.8 MB | 5.0 MB | ≤ 0.5 s, ≤ 32 MB — met |
| large_2000 | 2 000 | 197 406 890 | 0.403 s / 0.392 s | 7.7 MB / 7.8 MB | 4.3 MB | ≤ 5 s, ≤ 32 MB — met |
| groups_10000 | 10 000 | 154 450 | 0.239 s / 0.248 s | 45.5 MB / 45.5 MB | 24.2 MB | completeness only (no budget) — complete |

- **Prototype vs as-built.** E8 (prototype: keys in dicts, no output models) measured ≤ 135 ms and
  ≤ 4.6 MB at 10 000 documents. The implementation is ≈ 2× slower and holds ≈ 2.1 KB per document
  (`tracemalloc`: 20.5 MiB retained at 10 000 documents, 84 % in pydantic model instances — one
  `DocumentRef` per document plus groups). These as-built figures are the confirmed ones.
- **Why two RSS columns.** The benchmark keeps the previous `DocumentSet` alive while the next call
  builds its own, so its peak covers two results (45.5 MB); one call alone grows by 24.2 MB at 10 000
  documents. The assertion uses the conservative 3-call figure; the harness is unchanged.
- **Limits.** Machine-dependent; the budgets are regression assertions at 2 000 documents, not a runtime
  limit (P12). Text volume beyond the measured inputs (≈ 197 M chars here, ≈ 1 G chars in E8) is not
  claimed safe (§11.1); total-volume bounds remain OD-14. Input memory is the caller's.

**Real web (§17).** `RUN_LIVE_NETWORK=1`, TLS verification on (proxy CA via `CRAWL_CA_BUNDLE`), egress
policy not bypassed; PASS. PyPI `…/httpx/`, `?utm_source=probe`, `#history` → one group of 3 at L1, L2
and L3; `…/0.28.1/` → in no group with them; `…/project/httpx` and `…/project/HTTPX/` → L2 and L3 group
of two with `CROSS_SOURCE_DUPLICATE` (challenge pages with `LOW_CONFIDENCE_MAIN_CONTENT`; recorded, not
"fixed", §23); no exclusions, no errors.

**Post-implementation review of `ae7218f` (findings and disposition).**

| Id | Severity | Finding | Disposition |
|---|---|---|---|
| F-1 | MEDIUM | `tests/live/test_dedup_live.py` passed with every fetch failed (6× `TLS_ERROR` without `CRAWL_CA_BUNDLE`): `final_url` is set on failure and the remaining assertions were vacuous | **fixed (test only)**: every URL must be fetched `OK` with a body and extracted `SUCCESS` before any dedup assertion; L1, L2 and L3 must each contain the group `[0, 1, 2]`; the version page is in no group with it. Assertions live in `check_live_dedup`, proven offline by `tests/unit/test_dedup_live_checks.py` (16 tests: TLS / HTTP / connection failure, missing body or hash, wrong grouping all fail; a §17-conforming run passes). Live re-run after the fix: FAIL without `CRAWL_CA_BUNDLE` (`TLS_ERROR`), PASS with it (6/6 fetched `OK`, extracted `SUCCESS`) |
| F-2 | LOW | `RAW_DUPLICATE_TEXT_DIFFERS` (L2 warning) compares the members' stored, unverified `text_sha256` (§7 wording); a forged hash can add or hide the warning (only for documents not produced by S05) | **open — founder decision**; production semantics unchanged |
| F-3 | LOW | §7 said L2 input is "fetch `OK`", §11 said "when bytes were fetched"; code checks hash presence and format only | **documentation clarified** (§7, §11): S06 uses the well-formed hash as given and does not check `fetch_status`; S04 sets it only for `OK` fetches with a body; L2 never verifies raw bytes; `OK` interstitials / soft error pages remain an L2 false-positive risk |
| F-4 | LOW | §11/§11.1/§13 carried prototype figures (E8) without reconciliation with the as-built benchmark | **documentation clarified**: §13/E8 marked as historical prototype baseline; as-built figures above are the confirmed ones |

**Not done / unverified (unchanged, §15.1).** End-to-end order S01 → crawler → extraction → S06 and the
claim "representative = best-ranked hit" (OD-13); total-volume / stage-time bounds of the integrated
pipeline (OD-14); mapping of S06 exceptions to a job stage error. These belong to the integration sprint;
no S06 test claims them. The `benchmark` marker description in `pyproject.toml` still says "extraction"
(not edited: outside the allowed file scope).
