# Sprint 05 — Content extraction (design)

Status: **DESIGN — not implemented.** No production code, no dependency added.
Evidence ids `P1…P12` refer to probes run for this design (§4.2); probe scripts were kept out of the repository.

## 1. Baseline

| Item | Value |
|---|---|
| HEAD = origin | `54164fe` `feat(research): implement secure crawler` (Sprint 04 FROZEN) |
| Working tree | clean (verified before this design: `git status`, `git diff`, `git diff --cached` empty) |
| Runtime | Python 3.11.15, httpx 0.28.1, pydantic 2, structlog |
| Runtime deps | fastapi, httpx, pydantic, pydantic-settings, structlog, uvicorn — **no HTML, charset or language library** (`charset-normalizer` is present only via the dev tool pip-audit → requests) |

## 2. Scope

Turn a validated Sprint 04 `FetchResult` (HTML / XHTML / plain text, already decoded) into an
`ExtractedDocument`: readable, structure-preserving text, title, declared language, links (not followed),
page-claimed metadata, hashes, warnings and the unchanged search + crawl provenance.

```text
SearchResult ─▶ Crawler (S04) ─▶ FetchResult ─▶ Extractor (S05) ─▶ ExtractedDocument
```

## 3. Non-goals

Search, crawling, SSRF, robots.txt, HTTP retry, DNS, re-fetching, charset *decoding* (owned by S04),
verification / fact checking, dedup across documents (S06), LLM calls of any kind, Deep Search orchestration,
answer synthesis, database / persistent storage, JavaScript rendering, PDF / images / OCR, link following,
JobRunner / API integration, statistical language detection (deferred, §13).

## 4. Existing architecture (read, not assumed)

### 4.1 Answers to brief §3

| # | Question | Fact (source) |
|---|---|---|
| 1 | How does `FetchResult` hold the body? | `content: str \| None` — already **decoded text** (`body.decode(charset, "replace")`), only when `status == OK`. Raw bytes are **not** retained. `content_length` = decoded-body bytes (after decompression), `wire_bytes` = bytes on the wire, `content_sha256` = SHA-256 of the decompressed body bytes (`crawler.py::_result`). |
| 2 | Content-Type | `FetchResult.content_type`: lower-case media type without parameters; for `OK` always one of `text/html`, `application/xhtml+xml`, `text/plain` (`ALLOWED_CONTENT_TYPES`). |
| 3 | Charset detection | `_choose_charset`: HTTP header charset → UTF-8 BOM → first `<meta …charset=…>` in the first 4096 bytes (the regex matches both `<meta charset>` and `http-equiv … content="…; charset=…"`) → `utf-8`; labels resolved with `codecs.lookup` (Python codec names, not the WHATWG label table). Result stored in `FetchResult.charset`. Gaps: §12, P1. |
| 4 | Final URL | `FetchResult.final_url`; plus `requested_url` and `redirect_chain: list[RedirectHop]`. |
| 5 | Provenance | `FetchResult.source: SearchResult \| None` (provider `source`, `query`, `rank`, `original_url`, `url`, `metadata`) carried unchanged from `FetchTarget`; crawl provenance: `crawl_id`, `fetched_at`, `resolved_ip`, `robots`, `redirect_chain`, `content_sha256`. |
| 6 | HTML parser available | stdlib `html.parser` only (S04 `_TitleParser`, private). No lxml / selectolax / bs4 / html5lib installed. |
| 7 | Text normalization utility | None for content. `core/urls.py` normalizes URLs only; S04 collapses title whitespace inline. |
| 8 | Reusable models | `SearchResult` (provenance, unchanged), `FetchResult` / `FetchStatus` (input, unchanged), category vocabulary of `core.errors`, `normalize_url` (link identity), `configure_logging`. S04 private helpers are **not** reused (frozen, private). |
| 9 | 5 MiB limit | S04 `BoundedDecoder` caps *decoded* bytes at `CRAWL_MAX_RESPONSE_BYTES` (5 MiB) with a Content-Length pre-check and a wire cap; robots.txt 512 KiB. So `len(content) ≤ ~5.25 M` code points. |
| 10 | Does the API need extraction results? | No. The crawler is not integrated into JobRunner/API (S04 option I1); S05 is standalone as well. |

### 4.2 Probe evidence

| Id | Probe | Result |
|---|---|---|
| P1 | S04 `_choose_charset` edge cases | UTF-8 (no header) ✓; `charset=utf-16` header ✓; `windows-1252` via `<meta charset>` and via `http-equiv` ✓; invalid header label → falls through to meta / UTF-8 ✓; invalid UTF-8 → U+FFFD ✓. **Gaps**: (a) UTF-16 with BOM but no header → decoded as UTF-8 → NUL-riddled mojibake (8×U+FFFD in a short sample); (b) header `iso-8859-1` + UTF-8 BOM → header wins (`ï»¿phá»` mojibake; WHATWG: BOM wins); (c) `iso-8859-1` decoded as Latin-1, not windows-1252 (WHATWG alias) → `\x93…\x94` C1 controls instead of quotes; (d) `<meta charset>` after 4096 bytes ignored (WHATWG prescan is 1024 bytes, so acceptable). |
| P2 | `html.parser` throughput, no tracing | linear: text 110 ms/MB, `<div>`-soup ~300 ms/MB, 1 M nesting `<div>` 409 ms/MB, `<`-storm (worst found) ~1.15 s/MB. 5 MB realistic text 0.58 s, 5 MB div-soup 1.44 s, fed in 64 KiB chunks; worst single chunk 73 ms. (A traced run showed 4.3–47 s for the same inputs: ~8× `tracemalloc` overhead, not the parser.) |
| P3 | Unclosed constructs at `close()` | `<!--x` → comment swallows to EOF (same as browsers); `<script>var x` → text emitted *inside* script; `<title>unterminated` → text; `<p>a<b>bold<i>it</p>tail` → all text kept; `<![CDATA[` → dropped. No crash. |
| P4 | Entities / attributes | `&#xFFFFFFFF; &#0; &#55296; &#x110000;` → U+FFFD; `&bogus;`, `&am` kept literally; `&nbsp;&copy` → `\xa0©`. Duplicate attributes are **all** reported in order (`href=/a href=javascript:x`) — the extractor must take the first (HTML rule). `</script>` inside a JS string ends the script (HTML-conformant). |
| P5 | Attribute-heavy start tag (one tag, 700 k attributes, 5 MB) | **29.6 s and +410 MB RSS** (the incomplete tag is re-scanned on every chunk). Other chunk-spanning constructs (open comment / doctype / PI / quoted attr / CDATA / 2 MB `<abbb…`) ≤ 0.22 s. With a pre-parse guard replacing any tag of ≥ 32 KiB (`<[A-Za-z/!?][^<>]{32768,}>?`, linear) → **0.07 s, +0 MB**. |
| P6 | Memory of a bounded element tree (slotted nodes, attribute whitelist) | 100 KB article 0.8 MB; 5 MB text page 41 MB traced; 5 MB div-soup (~300 k elements) 85 MB traced → element cap needed (§16). Python `str`: 1 B/char ASCII, 2 B/char Vietnamese, 4 B/char emoji (a 5 MiB body can be a ~21 MB `str`). |
| P7 | Real page via S04 (`https://pypi.org/project/httpx/`, 148 KB) | parse 19 ms; 1 `<main>`, 0 `<article>`, 8 `<nav>`, header/footer 1, 4 tables, 5 `<pre>`, 36 `<code>`, 283 links; `<html lang="en">`; `og:title`=`httpx`, `<title>`=`httpx · PyPI`; no JSON-LD. `/help/`: 1 `<main>`, 12 `<section>`, 8 `<ol>`. |
| P8 | "Hidden" markers on P7 | 0 `hidden` attributes, 0 inline `display:none`; **268 `aria-hidden="true"`** elements (icons) holding 128 visible characters; `class="… hidden"` used for responsive toggles. → `aria-hidden` and class names are **not** "invisible content" signals. |
| P9 | Unicode normalization | Vietnamese NFD `Phở bò, cá hồi, Nguyễn Trãi` 36 → 27 code points under NFC (needed for evidence matching). NFKC is lossy: `E=mc²`→`E=mc2`, `①`→`1`, `½`→`1⁄2`, `™`→`TM`, full-width → ASCII, half-width kana → full-width. Removing ZWJ breaks emoji (`👨‍👩‍👧`→`👨👩👧`); ZWNJ is orthographic in Persian. |
| P10 | Egress for real-web tests | reachable: `pypi.org`, `raw.githubusercontent.com`, `files.pythonhosted.org`; denied by the sandbox egress policy (not by sites): wikipedia, python.org, news sites, docs sites, httpbin … (not routed around). |
| P11 | Candidate packages (PyPI metadata, nothing installed) | see §26. |
| P12 | Extraction concurrency (derived from P2) | pure-Python parsing is CPU-bound and holds the GIL; running it on the event loop would block the S03 API for up to seconds (P2). |

## 5. Input contract

```python
class Extractor:
    def __init__(self, settings: ExtractionSettings | None = None) -> None: ...
    def extract(self, fetch: FetchResult, *, deadline: float | None = None,
                cancel: threading.Event | None = None) -> ExtractedDocument: ...        # sync, pure CPU
    async def aextract(self, fetch: FetchResult, context: CrawlContext | None = None) -> ExtractedDocument: ...
```

- Input is the S04 `FetchResult`, unchanged. The extractor **never** performs network, file or subprocess I/O and
  never re-fetches (enforced by a test that makes `socket.socket`/`getaddrinfo` raise during extraction).
- Dispatch:

| `fetch.status` | `fetch.content_type` | Handling |
|---|---|---|
| ≠ `OK` | any | `NOT_FETCHED` (fetch status copied into `fetch_status`); no parsing |
| `OK` | `text/html`, `application/xhtml+xml` | HTML pipeline (§8) — XHTML parsed by the same tolerant HTML parser |
| `OK` | `text/plain` | plain-text pipeline (normalization only, §11) |
| `OK` | anything else (defensive; S04 never returns it) | `UNSUPPORTED` |
| `OK` | `content is None` | `FAILED` (`INVALID_INPUT`) — contract violation, not an empty page |

- `aextract` runs `extract` in `asyncio.to_thread` under a semaphore (`EXTRACT_CONCURRENCY`), with
  `deadline = min(now + EXTRACT_TIMEOUT_S, context.deadline)`. On cancellation it sets `cancel`, which the parser
  checks between 64 KiB chunks, then re-raises `CancelledError` (threads cannot be killed; the thread stops at
  the next chunk, ≤ ~75 ms per P2).

## 6. Output contract

New models in `src/research_agent/extraction/models.py` (Pydantic, frozen, `extra="forbid"`):

```text
ExtractedDocument
  document_id        str (uuid4 hex)
  extractor_version  str  (constant, bumped on behaviour change → re-extraction traceable)
  trust              Literal["UNTRUSTED"]  — always; see §20/§21
  status             ExtractionStatus (§18)
  warnings           list[ExtractionWarning]   (enum codes, fixed texts; never page text)
  fetch_status       FetchStatus               (copied)
  content_source     MAIN | ARTICLE | ARTICLES_COMMON_ANCESTOR | ROLE_MAIN | BODY | PLAIN_TEXT | NONE
  title              str | None
  title_source       TITLE_TAG | OG_TITLE | H1 | NONE
  text               str        (normalized, structure-preserving; offsets for later evidence spans refer to it)
  char_count         int        (code points of text)
  word_count         int        (Unicode \w+ runs; CJK without spaces counts one per run — documented)
  text_sha256        str | None (hash of `text`, UTF-8)  — "normalized hash" for S06 exact-duplicate checks
  declared_language  str | None (page-declared, §13)  + language_source HTML_LANG | CONTENT_LANGUAGE | OG_LOCALE | NONE
  links              list[ExtractedLink(url, text, in_main_content)]   (§14)
  claimed_metadata   ClaimedMetadata (§15) — claimed by the page, never verified facts
  stats              ExtractionStats (input_chars, elements, removed_chars per reason, truncated flags, duration_ms)
  provenance         DocumentProvenance
  extracted_at       datetime (UTC)

DocumentProvenance  (copies of small FetchResult fields — nothing re-derived)
  crawl_id, requested_url, final_url, http_status, content_type, charset, fetched_at,
  redirect_chain: list[RedirectHop], robots: RobotsDecision | None, resolved_ip,
  content_sha256 (raw-body hash from S04), source: SearchResult | None (the same object, unchanged)
```

Crawler provenance is copied field-for-field (a mutation that drops it must fail tests, §24); the extractor only
**adds** metadata. `FetchResult.title` (S04's first-`<title>` value) stays in the FetchResult untouched;
`ExtractedDocument.title` is the downstream title.

## 7. Raw vs extracted content; parser strategy

- **Raw content** = `FetchResult.content` (decoded body). It is never modified or overwritten and is **not**
  copied into `ExtractedDocument` (would double memory). Pairing is by `crawl_id`. Raw content lives only in
  memory for as long as the caller keeps the FetchResult; no persistence in S05 (PERSISTENCE-BLOCKER-01 unchanged).
- **Extracted content** = `ExtractedDocument.text`.

Parser options:

| Option | Evidence | Verdict |
|---|---|---|
| A. stdlib `html.parser` + own bounded tree | installed; linear (P2); tolerant of malformed input (P3, P4); one pathological case fixed by a guard (P5); memory bounded by an element cap (P6); PSF licence; ships with CPython 3.11 | **selected** |
| B. existing dependency | none that parses HTML (§1) | n/a |
| C. `selectolax` 0.4.12 (MIT, Lexbor, C, 63 wheels, no deps) | not installed → speed/robustness **unverified here**; native code parsing hostile input adds a C attack surface; browser-grade tree | candidate if P2 speed proves insufficient in integration (OD-2) |
| D. `trafilatura` 2.2.0 (Apache-2.0) | pulls lxml, courlan, htmldate(+dateparser), justext, charset_normalizer, urllib3, certifi; ships its own fetching/URL tooling (overlaps S04; must never be used to fetch) | rejected for S05: large dependency surface, unverified quality on our targets, network-capable |

## 8. HTML extraction pipeline

```text
content (str)
 ↓ 1 guard: replace start/end/decl tags ≥ 32 KiB with a placeholder (P5); cap input at EXTRACT_MAX_INPUT_CHARS
 ↓ 2 parse: html.parser (convert_charrefs=True), fed in 64 KiB chunks; deadline + cancel checked per chunk
 ↓ 3 bounded tree: slotted nodes, attribute whitelist, first duplicate attribute wins, depth/element caps
 ↓ 4 drop non-content elements (always)       → counted in stats.removed_chars[reason]
 ↓ 5 drop invisible elements                  → counted
 ↓ 6 metadata, title, declared language, links (whole document, before boilerplate removal)
 ↓ 7 select main content (§9)
 ↓ 8 drop boilerplate inside the selection (§17), with the swallow guard
 ↓ 9 serialize blocks to structured text (§23 rules), normalize (§11)
 ↓ 10 limits (§16), status + warnings (§18), hashes → ExtractedDocument
```

Tree construction (simplified HTML rules, text-oriented): void elements never pushed; an end tag pops to the
nearest open element with that name, or is ignored if none is open; `<p>` is implicitly closed by block-level
start tags; `<li>`, `<dt>/<dd>`, `<tr>`, `<td>/<th>`, `<option>` implicitly close their open sibling; at EOF all
elements are closed. Text of elements beyond `EXTRACT_MAX_DEPTH` or `EXTRACT_MAX_ELEMENTS` is appended to the
deepest allowed ancestor (text kept, structure flattened, warning `STRUCTURE_LIMIT`).

Element handling:

| Elements | Decision | Reason |
|---|---|---|
| `script`, `style`, `template`, `svg`, `canvas`, `object`, `embed`, `iframe`, `frame`, `audio`, `video`, `picture`, `map` | always drop | code / drawing / fallback text, never page prose. `<svg><title>` must never become the page title. |
| comments, doctype, processing instructions, CDATA | dropped by the parser | not rendered (P3) |
| `noscript` | drop; if the final text is empty and noscript had text → warning `JS_REQUIRED_SUSPECTED` | usually "enable JavaScript" text; its presence explains an empty result |
| `input`, `select`, `option`, `textarea`, `button`, `datalist` | drop | UI controls, not content |
| `form` | **keep** (children still filtered) | whole pages are commonly wrapped in one `<form>` (ASP.NET WebForms pattern); dropping it would drop the page |
| `nav` | drop anywhere | navigation lists |
| `header`, `footer`, `aside`, `role=banner/contentinfo/complementary/navigation/search` | drop only when **not** inside the selected `article`/`main` | an article's own `<header>` holds its title/byline (HTML sectioning semantics) |
| `hidden` attribute (except `hidden="until-found"`), inline `display:none` / `visibility:hidden`, `<dialog>` without `open` | drop (invisible to readers), counted as `removed_chars.hidden` | reduces invisible prompt-injection text; `until-found` content is user-findable |
| `aria-hidden="true"`, CSS classes such as `hidden`, `sr-only` | **keep** | P8: aria-hidden text is visible; class semantics are site-specific |
| `details`/`summary` (closed) | keep | user-expandable content |
| `img` | no text (alt text deferred) | |
| `math` | keep text | formulas are content |

## 9. Main-content selection

Ordered, deterministic, first match wins; the chosen rule is recorded in `content_source`:

1. `<main>` (or `role="main"`), first visible one, if it holds ≥ `EXTRACT_MIN_MAIN_CHARS` (default 200)
   characters of text **or** ≥ 25 % of the body text → `MAIN` / `ROLE_MAIN` (P7: PyPI uses `<main>`).
2. Exactly one `<article>` meeting the same size test → `ARTICLE`.
3. Several `<article>` (forum threads, listings, comment lists) → their lowest common ancestor →
   `ARTICLES_COMMON_ANCESTOR` (keeps every post instead of the first).
4. Otherwise `<body>` after page-level boilerplate removal → `BODY` with warning
   `LOW_CONFIDENCE_MAIN_CONTENT`; inside this fallback only, blocks (`div`, `ul`, `ol`, `section`, `td`) whose
   link-text ratio ≥ 0.5 and that contain ≥ 3 links are pruned as navigation (OD-6), counted in
   `removed_chars.link_dense`.

Confidence is never hidden: a selected container holding < 25 % of the body text also gets
`LOW_CONFIDENCE_MAIN_CONTENT`; empty output is `EMPTY` with a reason warning, never a network error.
Page types covered by fixtures: blog (article + header/footer), news (article with header/byline, aside),
documentation (main + nav sidebar + sections), product (main, tables, lists), forum (many articles), simple
HTML (no semantic tags), div-soup, plain text. There is **no** class/id keyword heuristic (`cookie`, `ad`,
`social`…) in S05 (§17).

## 10. Title

Precedence (OD-7): first `<title>` that is **not** inside `svg`/`math` → `og:title` → first `<h1>` in the main
content → first `<h1>` anywhere → `None`.

Processing: entities decoded by the parser, NFC, control / zero-width stripped, whitespace collapsed, empty after
processing → next source; truncated to `EXTRACT_MAX_TITLE_CHARS` (300) with `TITLE_TRUNCATED`. Duplicate
`<title>` elements: the first wins. No site-suffix stripping (`httpx · PyPI` stays as is; `og:title` remains in
`claimed_metadata`). The title is data: it is never interpreted, and it is subject to the same untrusted-data
rules (§20).

## 11. Text normalization

Applied to serialized text (outside `<pre>` unless noted):

| Step | Rule | Evidence / reason |
|---|---|---|
| HTML entities | decoded once by the parser (`convert_charrefs=True`); invalid refs → U+FFFD, unknown names kept literally | P4 |
| Inline whitespace | runs of `[ \t\n\r\f]` → one space (HTML rendering rule); not in `<pre>` | |
| NBSP, U+202F, U+2007 | → space (outside `<pre>`) | reading whitespace; keeps `100 000 ₫` matchable |
| Line breaks | `\r\n`/`\r` → `\n` everywhere; block boundaries → blank line; `<br>` → `\n`; ≥ 3 newlines → 2; trailing spaces per line stripped | |
| Unicode | **NFC** everywhere, never NFKC | P9: NFC fixes Vietnamese NFD; NFKC changes meaning (`mc²`→`mc2`) |
| Removed characters | C0/C1 controls except `\n`, `\t`; U+200B, U+2060, U+FEFF, U+00AD | invisible, break matching |
| Kept characters | ZWJ U+200D, ZWNJ U+200C, bidi marks/controls, emoji, CJK, combining marks | P9: removing ZWJ breaks emoji; ZWNJ is orthographic |
| Never changed | case, digits, punctuation, quotes, URLs, dates, currency formats (`120.000đ`) | evidence must stay verbatim |

Plain text (`text/plain`) uses the same Unicode/control rules and line-ending normalization, keeps line
structure (only runs of ≥ 3 blank lines are reduced), and gets `content_source = PLAIN_TEXT`.

## 12. Charset

S05 does **not** decode bytes: S04 owns decoding and S05 cannot recover lost bytes (`FetchResult` holds only the
decoded text). The precedence required by the brief — HTTP charset → `<meta charset>` → `<meta http-equiv>` →
safe fallback (UTF-8) — is what S04 implements (P1), plus a UTF-8 BOM check.

S05 adds quality signals only:
- `DECODING_ERRORS` warning when U+FFFD exceeds 0.5 % of characters (or > 100);
- `CHARSET_SUSPECT` warning when NUL characters are present (UTF-16 decoded as UTF-8, P1a) or when C1 controls
  U+0080–U+009F appear with a Latin-1 charset (P1c).

**S04 findings (reported, not fixed; S04 frozen):** SF-1 BOM is not honoured for UTF-16 without a header; SF-2
header beats BOM; SF-3 `iso-8859-1`/`latin1` not mapped to windows-1252. These do not block S05 — S05 consumes
the decoded text and flags these cases. Fixing them is an S04 change and needs explicit approval (OD-8).

## 13. Language

S05 records the **declared** language only: `<html lang>` → `<meta http-equiv="content-language">` →
`og:locale` → `None`; validated against `^[a-z]{2,3}(-[a-z0-9]{2,8})*$` after lower-casing and `_`→`-`;
invalid values are dropped. It is a page claim, not a detection. Statistical detection is **deferred**: no
evidence it is needed before S06/S07, and the candidates are unsuitable now — `lingua-language-detector` 2.2.0
requires Python ≥ 3.12 and has 172 MB wheels; `py3langid` needs numpy; `fast-langdetect` downloads a model at
runtime (network from the extractor is forbidden); `langdetect` unmaintained since 2021.

## 14. Links

Extracted (not followed; depth stays 0) because they are cheap, bounded and ARCHITECTURE §6 lists them (OD-4):

- source: `<a href>` (first `href` if duplicated, P4); base = `<base href>` if it is absolute http(s), else
  `final_url`; resolved with `urljoin`;
- kept: `http`/`https` only, without credentials, ≤ 2048 characters; fragment removed; fragment-only links
  dropped; `javascript:`, `data:`, `mailto:`, `tel:`, `ftp:`, `file:`, `blob:`, `about:` and unparsable URLs
  dropped (counted per scheme in `stats`);
- dedup by `normalize_url` identity (first anchor text wins); anchor text normalized, ≤ 200 characters;
- `in_main_content` flag; main-content links first, then others; capped at `EXTRACT_MAX_LINKS` (500) →
  `LINKS_TRUNCATED`.
- No SSRF validation here: nothing is fetched. Any future follow goes through the S04 crawler, which validates.

## 15. Metadata (claimed, unverified)

`ClaimedMetadata` (every field `str | None`, normalized, ≤ `EXTRACT_MAX_METADATA_CHARS` = 1000, warning
`METADATA_TRUNCATED`): `description` (`meta name=description`), `og_title`, `og_description`, `og_type`,
`og_site_name`, `og_locale`, `canonical_url` (`link rel=canonical`, absolute http(s) only), `author`
(`meta name=author` / `article:author`), `published_time` (`article:published_time`), `modified_time`
(`article:modified_time` / `og:updated_time`) plus `published_time_parsed` / `modified_time_parsed` only when
`datetime.fromisoformat` accepts the raw value (no fuzzy date parsing). First occurrence wins; at most 200
`<meta>` elements examined. The model name and docstring state that these are **claims by the page**; S07
decides what is verified.

JSON-LD (`<script type="application/ld+json">`, listed in ARCHITECTURE §6): OD-5 — proposed: keep up to 5
blocks of ≤ 100 000 characters each as raw strings that parse with `json.loads` (invalid dropped, counted); no
interpretation in S05.

## 16. Limits and memory

| Setting (`EXTRACT_` prefix) | Default | Exceeded → |
|---|---|---|
| `MAX_INPUT_CHARS` | 6 000 000 | input truncated, `INPUT_TRUNCATED`, `PARTIAL` (S04 already caps at ~5.25 M) |
| `MAX_TAG_CHARS` (guard) | 32 768 | tag replaced by a placeholder (P5), counted |
| `MAX_ELEMENTS` | 100 000 | structure flattened, `STRUCTURE_LIMIT` (text kept) |
| `MAX_DEPTH` | 256 | as above |
| `MAX_TEXT_CHARS` | 1 000 000 | cut at the last block boundary, `TEXT_TRUNCATED`, `PARTIAL` |
| `MAX_TITLE_CHARS` | 300 | `TITLE_TRUNCATED` |
| `MAX_LINKS` | 500 | `LINKS_TRUNCATED` |
| `MAX_METADATA_CHARS` | 1 000 per field | `METADATA_TRUNCATED` |
| `TIMEOUT_S` | 10 | parsing stops at the chunk boundary: text so far, `TIME_BUDGET_EXCEEDED`, `PARTIAL` |
| `CONCURRENCY` | 2 | bounds simultaneous extractions (memory) |

Memory model per extraction (worst case): input `str` already owned by the caller (≤ ~21 MB for 5 MiB of emoji,
P6); guard copy only when a tag is replaced; tree ≤ ~30 MB at 100 000 elements (P6: ~280 B/element); text
fragments ≤ input size; output text ≤ 1 M characters (≤ 4 MB). Budget: peak traced allocation < 128 MB on every
5 MiB benchmark input, asserted by tests. Attribute values are capped (2 KiB, `href` 8 KiB) and only
whitelisted attributes are stored (`id`, `class`, `role`, `hidden`, `style`, `href`, `lang`, `name`,
`property`, `content`, `http-equiv`, `rel`, `type`, `datetime`, `open`).

## 17. Boilerplate / noise

Goal: high-precision readable content **without aggressive deletion**. S05 removes only structurally identified
noise (§8 table: `nav`, page-level `header`/`footer`/`aside`, landmark roles, invisible elements, UI controls)
plus link-dense blocks in the `BODY` fallback (§9). Cookie banners, ads and social widgets are removed only when
they are structurally marked (`<dialog>`, `aside`, `hidden`, landmark roles); keyword heuristics on class/id are
deferred until a labelled sample exists (P8 shows class names are unreliable).

**Swallow guard**: an element selected for boilerplate removal is kept (warning `BOILERPLATE_GUARD`) when it holds
≥ 50 % of the body text or contains `<main>`, `<article>` or the first `<h1>` — this protects against unclosed
`<header>`/`<nav>` tags that swallow the page (P3 behaviour of implicit closing at EOF).

## 18. Empty / partial semantics

`ExtractionStatus`:

| Status | Meaning |
|---|---|
| `SUCCESS` | text produced within all limits (may carry warnings such as `LOW_CONFIDENCE_MAIN_CONTENT`, `THIN_CONTENT` < 200 chars) |
| `PARTIAL` | text produced but incomplete: `TEXT_TRUNCATED`, `INPUT_TRUNCATED`, `TIME_BUDGET_EXCEEDED` |
| `EMPTY` | fetch OK (e.g. HTTP 200), supported type, but no text after extraction; reason warnings e.g. `JS_REQUIRED_SUSPECTED`, `ONLY_BOILERPLATE` |
| `NOT_FETCHED` | `FetchResult.status ≠ OK` (e.g. HTTP 404 → `fetch_status = HTTP_ERROR`); nothing parsed |
| `UNSUPPORTED` | content type outside the extraction scope |
| `FAILED` | internal extractor error (caught; fixed message, no page text, no traceback in the model); `CancelledError` always propagates |

So "HTTP 200 but empty text" (`EMPTY`) is distinct from "HTTP 404" (`NOT_FETCHED` + `fetch_status`), and an empty
extraction is never reported as a network error.

## 19. Malformed HTML

Must not crash or hang: unclosed tags, broken nesting, invalid entities, binary bytes / NULs / U+FFFD in HTML,
huge comments (5 MB), 1 M-deep nesting, duplicate attributes, invalid UTF-8 (arrives as U+FFFD), unterminated
`<script>`/`<title>`/attribute quotes, `<`-storms, attribute-heavy tags. Guarantees: linear time plus the
chunk-level deadline (P2, P5); bounded memory (§16); every exception inside the pipeline → `FAILED`, never
propagated (except `CancelledError`).

## 20. Security — web content is untrusted

```text
Crawler (S04) ──▶ untrusted web data (FetchResult.content)
                        │
Extractor (S05) ──▶ untrusted extracted data (ExtractedDocument, trust = "UNTRUSTED")
                        │
future LLM stages ──▶ receive it only as delimited evidence, never as instructions
```

The extractor never executes JavaScript or HTML (`html.parser` is a tokenizer), never follows instructions in
content, calls no tools, reads no configuration from content, performs no I/O, and never sends anything
anywhere. Output is plain text: markup cannot survive into `text` (tags are consumed by the parser). Logs
(`extract.document`) contain only `crawl_id`, `document_id`, host, status, warnings codes, sizes and duration —
never text, title, metadata, link URLs or query strings.

## 21. Prompt-injection boundary

- S05 does **not** rewrite or censor instructions found in pages: doing so would corrupt evidence. Text such as
  "Ignore previous instructions…" is kept verbatim as data (tested).
- Invisible text (hidden elements, §8) is removed and counted — a common vector for text humans never see.
- Every document carries `trust = "UNTRUSTED"`; extracted strings are control-character-free.
- Representation for later LLM stages (implemented with the AI-extraction sprint, not S05):

```text
<source_document id="{document_id}" boundary="{random per-call token}">
  <meta>final_url, title, declared_language, fetched_at — all data</meta>
  <content>…ExtractedDocument.text…</content>
</source_document boundary="{same token}">
```

  The boundary token is random per call, so content cannot forge the closing delimiter; the system prompt
  declares everything inside as data (ARCHITECTURE §7). S05 tests assert that a page containing a literal
  `</source_document>` is preserved as text and changes nothing else.

## 22. Performance

Benchmark (pytest, marker `benchmark`, synthetic inputs generated in the test, no network):

| Input | Budget (duration, peak traced memory) |
|---|---|
| small HTML (2 KB) | < 20 ms |
| medium article (100 KB) | < 300 ms |
| large text HTML (5 MiB) | < 3 s, < 128 MB |
| large div-soup (5 MiB) | < 5 s, < 128 MB |
| malformed / `<`-storm (5 MiB) | ≤ `TIMEOUT_S` (then `PARTIAL`), < 128 MB |
| attribute-heavy tag (5 MiB) | < 1 s (guard), < 128 MB |
| 5 MiB emoji / Vietnamese text | < 3 s, < 128 MB |

Duration is measured without `tracemalloc` (tracing inflates time ~8×, P2); memory is measured in a
separate traced run. Budgets are a safety baseline, not optimization targets; results are printed in the sprint
report.

## 23. Test matrix

Fixtures are hand-written synthetic HTML files under `tests/fixtures/extraction/` (no copied third-party pages).

| Group | Cases |
|---|---|
| Basic | simple HTML, plain text, empty body (`EMPTY`), title only (`EMPTY` + title), `NOT_FETCHED` (404, SSRF), `UNSUPPORTED`, `content=None` → `FAILED` |
| HTML structure | `<main>`, single `<article>` with header/byline kept, many articles (forum), nested divs, news (aside, related), documentation (nav sidebar, sections), product (tables, lists), ASP.NET-style page-wrapping `<form>`, div-soup fallback with link-dense nav pruned + low-confidence warning, tiny `<main>` (app shell) falls through |
| Noise | script, style, noscript (+ `JS_REQUIRED_SUSPECTED`), nav, page header/footer, aside, form controls, iframe, svg (`<svg><title>` never page title), comments, `hidden` / `display:none` / closed `<dialog>` removed, `hidden="until-found"` / `aria-hidden` kept, swallow guard (unclosed `<header>`) |
| Encoding | UTF-8, Vietnamese NFC + NFD input → NFC, Chinese, Japanese, emoji + ZWJ sequences, Persian ZWNJ, U+FFFD-heavy input (`DECODING_ERRORS`), NUL-riddled UTF-16-as-UTF-8 (`CHARSET_SUSPECT`), C1 with Latin-1 charset, windows-1252 decoded text; charset/`FetchResult` fields copied |
| Normalization | NBSP, zero-width, soft hyphen, controls, `mc²`/`①`/full-width unchanged, URLs, numbers, dates, `120.000đ` unchanged, blank-line collapsing |
| Malformed | broken nesting, unclosed tags, invalid entities, duplicate attributes (first wins), huge comment, 1 M-deep nesting, `<`-storm, attribute-heavy tag, binary junk, unterminated script/title/quotes |
| Structured | table (header row + separator, cell pipes escaped, nested table flattened), `ul`/`ol` (nested indentation, numbering), `pre` verbatim (whitespace + tabs, fence longer than inner backticks), inline `code`, `blockquote` (`> `), headings (`#`…), `dl` |
| Links | relative/absolute/`<base>`, fragment stripped, fragment-only dropped, `javascript:`/`data:`/`mailto:`/`tel:` dropped, credentials dropped, dedup, `in_main_content`, cap |
| Title / metadata / language | precedence chain, empty/whitespace/entity/very long/duplicate titles, og/description/canonical/author/dates (ISO parsed, non-ISO raw only), invalid lang dropped, first meta wins |
| Security | injection text kept verbatim, `</source_document>` in content, hidden injection removed, no network during extraction (socket patched), no page text in logs, `trust == "UNTRUSTED"` |
| Limits | huge title, huge metadata, 10 000 links, text > `MAX_TEXT_CHARS` (`PARTIAL`, cut on block boundary), element/depth caps, time budget (`PARTIAL`), cancellation of `aextract` propagates and stops the thread |
| Provenance | every `DocumentProvenance` field equals the FetchResult's, `source` is the same `SearchResult`, `FetchResult` unchanged after extraction (raw content not modified) |
| Regression | Sprint 01–04 suites unchanged, no diff in their files |

## 24. Mutation strategy

Each mutant applied alone, full S05 suite run, restored; a surviving mutant gets a test or a written equivalence
argument before PASS:

| # | Mutant |
|---|---|
| X1 | script removal disabled |
| X2 | style removal disabled |
| X3 | charset quality signals disabled (no `CHARSET_SUSPECT`/`DECODING_ERRORS`) |
| X4 | NFC removed / X4b NFKC instead of NFC / X4c ZWJ stripped |
| X5 | output limits removed (text, links, title, metadata) |
| X6 | `javascript:` (and `data:`) links allowed |
| X7 | title precedence changed (og:title first; svg title allowed) |
| X8 | raw HTML returned as `text` |
| X9 | provenance dropped / one field not copied |
| X10 | hidden-element removal disabled |
| X11 | long-tag guard removed (benchmark budget must fail) |
| X12 | deadline/cancel check removed |
| X13 | article-level header kept → removed (byline test) / swallow guard removed |
| X14 | `<pre>` whitespace collapsed; table cells flattened into one line |
| X15 | last duplicate attribute wins |
| X16 | `EMPTY` reported as `FAILED`/`NOT_FETCHED` |

## 25. Real-web strategy

Opt-in test (`live_network` marker, `RUN_LIVE_NETWORK=1`, otherwise skipped `REQUIRES_NETWORK`), S04 crawler →
extractor, hosts proven reachable here (P10), egress policy never bypassed:

| Case | URL | Checks |
|---|---|---|
| HTML page with `<main>`, tables, `pre` | `https://pypi.org/project/httpx/` | `SUCCESS`, `content_source=MAIN`, title `httpx · PyPI`, `og_title` `httpx`, text contains the project description and no "Skip to main content", charset `utf-8`, provenance == FetchResult, links bounded |
| documentation-style page | `https://pypi.org/help/` | `SUCCESS`, headings/lists preserved |
| plain text | `https://raw.githubusercontent.com/python/cpython/main/LICENSE` | `PLAIN_TEXT`, line structure kept |
| redirect | `http://pypi.org/` | `final_url` `https://pypi.org/`, redirect chain in provenance |
| robots-approved | all above (`robots.outcome` `ALLOWED`/`UNAVAILABLE_ALLOW` recorded) | |

Extra URLs can be supplied via `LIVE_EXTRACT_URLS` where egress allows (e.g. news/blog pages elsewhere); results
record URL, status, content_source, title, char count, warnings, duration. No real web data is sent to any LLM.
Limitation: this sandbox reaches no news/blog/forum site, so heuristics for those page types are verified on
fixtures only (§29).

## 26. Dependency decisions

**No dependency is added in S05** (stdlib + existing packages). Candidates evaluated (PyPI metadata, P11; none
installed):

| Package | Version / date | Licence | Python | Deps | Why it would help | Why not now |
|---|---|---|---|---|---|---|
| selectolax | 0.4.12 / 2026-09-18 | MIT | 3.9–3.14 | none (C/Lexbor wheels) | fast browser-grade DOM, CSS selectors | speed not needed yet (P2); native code on hostile input; unverified here |
| trafilatura | 2.2.0 / 2026-07-31 | Apache-2.0 | ≥ 3.10 | lxml, courlan, htmldate→dateparser, justext, charset_normalizer, urllib3, certifi | main-content + metadata extraction | big surface, own network helpers, quality unverified on our targets |
| lxml | 6.1.3 / 2026-09-02 | BSD-3 | ≥ 3.8 | none (libxml2 wheels) | fast tree | libxml2 C surface; not needed for text-oriented extraction |
| beautifulsoup4 | 4.15.0 / 2026-06-07 | MIT | ≥ 3.7 | soupsieve, typing-extensions | convenient API | slower than html.parser it wraps; no capability we need |
| readability-lxml | 0.9 / 2026-08-27 | Apache-2.0 | 3.8–3.14 | chardet, cssselect, lxml, lxml-html-clean | readability heuristics | pulls lxml + chardet; unverified quality |
| html5lib | 1.1 / 2020-06-22 | MIT | any | six, webencodings | spec tree building | unmaintained since 2020, slow |
| language detectors | see §13 | | | | | deferred |

## 27. S04 compatibility

The extractor consumes `FetchResult` exactly as frozen at `54164fe`; no S04 field is required that does not exist,
and no S04 file changes. Requirements recorded for a possible future S04 delta (approval needed, OD-8): WHATWG
charset precedence (SF-1…SF-3); optionally exposing raw bytes would allow re-decoding. **No blocking conflict.**

## 28. S06 / S07 boundary

| Stage | Owns | S05 provides |
|---|---|---|
| S05 | HTML/text → readable `ExtractedDocument` | — |
| S06 normalize/dedup | cross-document dedup, near/semantic duplicates, entity resolution, value normalization | `text_sha256` (exact duplicate of normalized text), `content_sha256` (raw), `normalize_url`-deduped links, `declared_language` |
| S07 verification | truth of claims, dates, authorship, conflicts | `claimed_metadata` explicitly unverified; stable `text` for evidence offsets |

No semantic dedup, similarity or verification logic enters S05.

## 29. Risks

- Main-content heuristics are validated on fixtures and PyPI only (egress, P10); news/blog/forum quality on real
  sites is unmeasured → warnings are explicit; a labelled evaluation is deferred.
- `html.parser` is not a spec tree builder; unusual nesting may differ from browsers (text is still kept).
- Pure-Python speed: 5 MiB worst cases take seconds (P2); bounded by `TIMEOUT_S` and threads; a native parser
  remains an option (OD-2).
- A thread cannot be killed; cancellation is cooperative per chunk.
- S04 charset gaps (SF-1…SF-3) produce flagged but unrecoverable mojibake.
- Markdown-like serialization markers (`#`, `-`, `|`, fences) are extractor-added; the text is a readable
  rendering, not the original markup (documented for evidence offset consumers).

## 30. Deferred

Statistical language detection; class/id keyword boilerplate heuristics; image alt text; JSON-LD interpretation
(raw capture is OD-5); job/API integration (CRAWLING → EXTRACTING stage); persistence of documents; chunking for
LLMs (ARCHITECTURE §6, belongs with AI extraction); LLM envelope implementation; S04 charset delta; labelled
quality evaluation; native parser.

## 31. Architecture Decision Record

| # | Decision | Options | Selected | Evidence | Trade-off |
|---|---|---|---|---|---|
| 1 | Parser | html.parser / selectolax / lxml / trafilatura | stdlib `html.parser` + bounded tree | P2, P3, P4, P5, P6, §26 | slower than native; own tree rules to maintain |
| 2 | Main content | trafilatura / readability / semantic rules + fallback | `main` → `article` → articles' ancestor → body with link-density pruning | P7; spec semantics | weaker on div-soup sites; flagged low confidence |
| 3 | Title precedence | title / og:title / h1 orders | `<title>` (not in svg) → og:title → h1 | P7 (both present; `<title>` universal) | keeps site suffixes |
| 4 | Charset | re-decode in S05 / trust S04 + signals / change S04 | trust S04 + `CHARSET_SUSPECT`/`DECODING_ERRORS` | P1; bytes not retained | SF-1…3 mojibake remains until an approved S04 delta |
| 5 | Normalization | none / NFC / NFKC; strip all zero-width | NFC; strip ZWSP/WJ/BOM/SHY/controls; keep ZWJ/ZWNJ | P9 | NBSP → space loses a typographic hint |
| 6 | Links | defer / extract bounded | extract http(s), no follow, cap 500 | ARCHITECTURE §6 | small cost; unused until link following |
| 7 | Metadata | none / claimed fields / full JSON-LD | claimed fields (+ raw JSON-LD, OD-5) | brief §14 | not verified by design |
| 8 | Language | detector / declared only | declared only | §13 package facts | pages without `lang` → `None` |
| 9 | Limits | crawler limit only / own limits | own limits §16 | P5, P6 | truncation → `PARTIAL` |
| 10 | Malformed HTML | trust parser / guard + caps + deadline | guard + caps + chunk deadline | P2, P3, P5 | placeholder replaces giant tags |
| 11 | Tables/code/lists | flatten / rich model / Markdown-like text | Markdown-like text in one string | brief §23 | markers are synthetic |
| 12 | Injection boundary | sanitize text / mark + delimit | mark `UNTRUSTED`, remove invisible text, random-boundary envelope later | ARCHITECTURE §7 | relies on later stages honouring it |
| 13 | Dependency | add / none | none | §26 | own code |
| 14 | Performance | event loop / thread / process | `to_thread` + semaphore + cooperative deadline | P2, P12 | GIL-bound; no hard kill |
| 15 | Empty/partial | reuse FetchStatus / own enum | `ExtractionStatus` (6 values) + fetch_status copy | brief §20 | two enums to read |

## 32. Conflicts and open decisions

Conflicts (none with the S04 frozen code):

| Id | Class | Conflict | Proposal |
|---|---|---|---|
| C-1 | roadmap/doc | ARCHITECTURE §21 names Sprint 05 "Extraction" = AI structured extraction (AIProvider, evidence check); this brief defines S05 = content extraction (ARCHITECTURE §6 "Content parser", listed under Sprint 04 there) | follow the brief; AI extraction moves to a later sprint; update ARCHITECTURE §21 in the implementation commit (docs only) |
| C-2 | doc/deps | ARCHITECTURE §1/§6 name selectolax + trafilatura | stdlib (ADR 1), as already decided for S04 (OD-4 there) |
| C-3 | doc | ARCHITECTURE §6 "whitespace collapsed" and unconditional `nav/footer` stripping | structure-preserving serialization (§11, §23); header/footer/aside only at page level (§8) |
| C-4 | S04 behaviour | charset gaps SF-1…SF-3 (P1) | not blocking; S05 flags them; S04 change only with approval (OD-8) |

Open decisions:

| Id | Question | Proposal |
|---|---|---|
| OD-1 | Roadmap renumbering (C-1) | accept |
| OD-2 | Parser | stdlib `html.parser`; revisit selectolax only with measured need |
| OD-3 | Text representation | Markdown-like structured plain text (§23 rules) |
| OD-4 | Links | extract bounded, never follow |
| OD-5 | JSON-LD | capture raw, validated JSON strings (≤ 5 × 100 000 chars); no interpretation |
| OD-6 | Body fallback | link-density pruning (≥ 0.5 link-text ratio and ≥ 3 links) only in the fallback |
| OD-7 | Title precedence | `<title>` → `og:title` → `h1` |
| OD-8 | S04 charset findings | accept + warn in S05 now; separate approved S04 delta later if wanted |
| OD-9 | Limits | defaults in §16 |
| OD-10 | Execution | sync `extract` + `aextract` via `to_thread`, `EXTRACT_CONCURRENCY=2`, `EXTRACT_TIMEOUT_S=10` |
| OD-11 | `noscript` | drop, with `JS_REQUIRED_SUSPECTED` on empty output |

## 33. As built (Sprint 05 implementation)

Code: `src/research_agent/extraction/` (`config`, `models`, `text`, `tree`, `content`, `extractor`). Standalone:
no change to Sprint 01–04 code (S04 frozen at `54164fe`), JobRunner or API. No dependency added.

Refinements made during implementation (all within the approved design):

- **Field names**: the page-declared language is `claimed_language` (+ `language_source`), to make "claimed, not
  verified" explicit; `ExtractedDocument.kind = "source_document"` and `trust = "UNTRUSTED"` are constants.
  `requested_url`, `final_url`, `content_type`, `charset`, `content_sha256` are read-only properties over
  `provenance` (one provenance record, no duplication). The raw body is **not** copied into the document.
- **Hashes**: `content_sha256` = crawler's SHA-256 of the fetched (decompressed) body bytes (raw);
  `text_sha256` = SHA-256 of `ExtractedDocument.text` in UTF-8 (normalized extracted text). No similarity.
- **html.parser treats `<title>` as RCDATA** (Python 3.11.15, like browsers): markup inside a title is literal
  title text; it is still plain data.
- **Text events are joined** per element (html.parser emits one event per `<` in a `<<<<` run): the 5 MiB
  `<`-storm went from 8.4 s / +130 MB to ~5.5 s / +47 MB.
- **Links** are examined main-content first and only until 500 are kept (or 20 × `max_links` candidates were
  examined); identical `href` values are resolved once; unexamined candidates → `LINKS_TRUNCATED`
  (`links_dropped.not_examined`). Dropped links are counted per reason (`javascript`, `data`, `mailto`, `tel`,
  `credentials`, `fragment`, `duplicate`, `too_long`, `invalid`, `other_scheme`, …).
- **Deadline**: checked before every 64 KiB parser chunk and every 512 rendered elements. If parsing times out,
  the (bounded) partial tree is still rendered with cancel-only checks — that text is the `PARTIAL` result.
  Cancellation (`aextract`) sets an event checked at the same points; the semaphore slot is held until the
  worker thread has stopped, then `CancelledError` propagates.
- **Tables**: a table whose cells contain sectioning/heading/form elements is a layout table (cells rendered as
  ordinary blocks); nested data tables are flattened into their cell (pipes escaped).
- **Swallow guard** applies to `nav`/page-level boilerplate and to link-dense pruning; boilerplate removal runs
  inside the selected container (page-level blocks outside the selection are never rendered anyway).
- **Oversized-tag guard**: `<[A-Za-z/!?][^<>]{32767,}>?` removed before parsing, counted in
  `stats.oversized_tags_removed` with warning `OVERSIZED_TAG_REMOVED` (text after the tag survives).
- **S04 charset findings SF-1…SF-3: NOT MODIFIED.** S05 reads the decoded text as-is, never re-decodes, and only
  reports `CHARSET_SUSPECT` / `DECODING_ERRORS`; `claimed_metadata.declared_charset` records the page's claim.

### Tests

`tests/unit/test_extraction_text.py`, `tests/unit/test_extraction_tree.py`, `tests/integration/test_extraction.py`
(fixtures: hand-written synthetic pages in `tests/fixtures/extraction/`), `tests/integration/test_extraction_benchmark.py`
(marker `benchmark`), `tests/live/test_extraction_live.py` (marker `live_network`, opt-in).

### Mutation testing (each mutant alone; S05 unit + integration suite; restored afterwards)

All killed: X1 script removal, X2 style removal, X3 charset signals, X4 NFC removed, X4b NFKC, X4c ZWJ stripped,
X5a–d text/link/title-metadata/input limits, X6/X6b javascript scheme checks, X7 og:title first, X7b svg title,
X8 raw HTML as text, X9/X9b provenance, X10 `hidden`, X10b `display:none`, X10c `aria-hidden` treated as hidden
(with the attribute whitelisted), X11 long-tag guard, X12a/b parse/render deadline, X12c cancel check, X13 article
header as boilerplate, X13b swallow guard, X13c `<form>` removed, X13d link-dense pruning everywhere, X14 `<pre>`
collapsed, X14b table flattened, X15 last duplicate attribute wins, X16 `EMPTY` → `FAILED`.
Equivalent: X10c without whitelisting `aria-hidden` — the tree never stores that attribute, so a check on it can
never fire.

### Benchmark (this sandbox, subprocess per scenario; peak RSS growth via VmHWM reset)

| Scenario | Input chars | Duration | Peak RSS growth | Output chars | Status |
|---|---|---|---|---|---|
| small_html | 1 465 | 0.002 s | 0.1 MB | 1 003 | SUCCESS |
| medium_article | 76 056 | 0.05 s | 1.3 MB | 53 598 | SUCCESS |
| large_text (5 MiB) | 5 242 876 | 2.1 s | 73 MB | 971 029 | SUCCESS (`STRUCTURE_LIMIT`) |
| large_html (5 MiB div-soup) | 5 242 901 | 3.4 s | 51 MB | 258 873 | SUCCESS (`STRUCTURE_LIMIT`) |
| pathological_start_tag (600 k attributes) | 5 888 914 | 0.05 s | 0.0 MB | 8 | SUCCESS (`OVERSIZED_TAG_REMOVED`) |
| deep_nesting (1 M `<div>`) | 5 242 884 | 2.6 s | 8 MB | 4 | SUCCESS (`STRUCTURE_LIMIT`) |
| large_metadata (600 × 8 KB meta) | 4 821 650 | 0.06 s | 0.3 MB | 4 | SUCCESS (`METADATA_TRUNCATED`) |
| lt_storm (5 MiB `<`) | 5 242 880 | 5.3 s | 47 MB | 1 000 000 | PARTIAL (`TEXT_TRUNCATED`) |
| emoji + Vietnamese text/plain | 2 708 811 | 0.24 s | 21 MB | 999 997 | PARTIAL (`TEXT_TRUNCATED`) |

Machine noise of ±2 s was observed between runs on the same input; the benchmark test uses the best of two runs.

### Real-web (S04 crawler → S05, hosts allowed by the sandbox egress policy)

| Case | URL | Result |
|---|---|---|
| HTML page with metadata and links | `https://pypi.org/project/httpx/` | SUCCESS, MAIN, title `httpx · PyPI`, lang `en`, og:title `httpx`, 9 154 chars, 158 links, robots ALLOWED |
| documentation page | `https://pypi.org/help/` | SUCCESS, MAIN, 36 090 chars, 120 links |
| plain text | `https://raw.githubusercontent.com/python/cpython/main/LICENSE` | SUCCESS, PLAIN_TEXT, 13 803 chars |
| redirect | `http://pypi.org/` | SUCCESS, final `https://pypi.org/`, redirect 301, `LOW_CONFIDENCE_MAIN_CONTENT` (small `<main>`) |
