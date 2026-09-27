"""Crawler: fetch one URL safely and return a validated ``FetchResult`` with provenance.

Pipeline per URL (every redirect hop repeats steps 1-3):
    1. URL policy (scheme, credentials, port 80/443, numeric/local hosts)      → POLICY/SSRF_BLOCKED
    2. robots.txt for the hop's origin (fetched through the same guarded path) → ROBOTS_BLOCKED
    3. request through the SSRF-guarded transport (resolve → validate → pin)   → SSRF_BLOCKED …
    4. redirects: manual, ≤ max_redirects, loop detection, no https→http downgrade
    5. response: 2xx, allowed content type, bounded raw read + bounded decompression
Retries (at most one) only for transient failures; total time ≤ min(timeout_s, caller deadline).
``asyncio.CancelledError`` always propagates — cancellation is never turned into a result.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import importlib.metadata
import random
import re
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookiePolicy
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import Request as UrllibRequest

import httpx

from research_agent.core.errors import InvalidURLError
from research_agent.core.urls import normalize_url
from research_agent.crawler.config import CrawlerSettings
from research_agent.crawler.decoding import (
    BodyDecodeError,
    BodyTooLarge,
    BoundedDecoder,
    UnsupportedEncoding,
)
from research_agent.crawler.models import (
    CATEGORY,
    CrawlContext,
    FetchError,
    FetchResult,
    FetchStatus,
    FetchTarget,
    RedirectHop,
    RobotsDecision,
)
from research_agent.crawler.network import (
    CONNECTIONS,
    DNSResolutionError,
    GuardedNetworkBackend,
    Resolver,
    SSRFBlockedError,
    build_guarded_transport,
    system_resolver,
)
from research_agent.crawler.policy import PolicyViolation, check_url
from research_agent.crawler.robots import RobotsRules
from research_agent.logging import get_logger

log = get_logger(__name__)

ROBOTS_AGENT = "ResearchAgentCrawler"
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
RETRYABLE_HTTP = frozenset({429, 502, 503, 504})
RETRYABLE_STATUS = frozenset({FetchStatus.CONNECTION_ERROR, FetchStatus.CONNECT_TIMEOUT})
_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_.:-]+)""", re.IGNORECASE)


def _version() -> str:
    try:
        return importlib.metadata.version("research-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0"


USER_AGENT = f"{ROBOTS_AGENT}/{_version()}"  # no contact URL until the owner provides one
_JITTER = random.SystemRandom()


@dataclass(frozen=True)
class UnsafeTestOverrides:
    """Test infrastructure only (local servers). Never built from settings or environment.

    ``loopback_hosts``: these exact hostnames may resolve to 127.0.0.1/::1.
    ``extra_ports``: additional ports allowed besides 80/443.
    ``ssl_context``: trust a test CA.
    """

    loopback_hosts: frozenset[str] = frozenset()
    extra_ports: frozenset[int] = frozenset()
    ssl_context: ssl.SSLContext | None = None


class _RejectAllCookies(CookiePolicy):
    """No cookie is ever stored or sent: no session state between hosts or requests."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie: Cookie, request: UrllibRequest) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: UrllibRequest) -> bool:
        return False

    def domain_return_ok(self, domain: str, request: UrllibRequest) -> bool:
        return False

    def path_return_ok(self, path: str, request: UrllibRequest) -> bool:
        return False


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self.title: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and self.title is None:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = " ".join("".join(self._parts).split())[:500] or None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._parts.append(data)


@dataclass
class _Outcome:
    status: FetchStatus
    message: str
    http_status: int | None = None
    retry_after_s: float | None = None
    final_url: str | None = None
    content_type: str | None = None
    charset: str | None = None
    body: bytes | None = None
    wire_bytes: int | None = None
    cause: FetchStatus | None = None


@dataclass
class _Redirect:
    location: str
    http_status: int


@dataclass
class _State:
    deadline: float
    chain: list[RedirectHop] = field(default_factory=list)
    robots: RobotsDecision | None = None
    resolved_ip: str | None = None
    attempts: int = 1


@dataclass
class _RobotsEntry:
    expires: float
    outcome: str
    rules: RobotsRules | None
    network_status: FetchStatus | None = None
    """Set when robots.txt could not be fetched because the host itself is unreachable."""


# robots.txt failures at connection level are reported as the network failure itself (the page
# would fail the same way); failures after a connection was made are ROBOTS_BLOCKED.
_ROBOTS_NETWORK_FAILURES = frozenset(
    {
        FetchStatus.DNS_ERROR,
        FetchStatus.CONNECTION_ERROR,
        FetchStatus.CONNECT_TIMEOUT,
        FetchStatus.TLS_ERROR,
    }
)


def _map_exception(exc: BaseException) -> tuple[FetchStatus, str]:
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, SSRFBlockedError):
            return FetchStatus.SSRF_BLOCKED, "resolved address not allowed"
        if isinstance(node, DNSResolutionError):
            return FetchStatus.DNS_ERROR, "dns resolution failed"
        if isinstance(node, ssl.SSLError | ssl.CertificateError):
            return FetchStatus.TLS_ERROR, "tls handshake or certificate verification failed"
        node = node.__cause__ or node.__context__
    if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
        return FetchStatus.CONNECT_TIMEOUT, "connect timed out"
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout):
        return FetchStatus.READ_TIMEOUT, "read timed out"
    if isinstance(exc, httpx.RemoteProtocolError | httpx.LocalProtocolError | httpx.DecodingError):
        return FetchStatus.INVALID_RESPONSE, "protocol error"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return FetchStatus.POLICY_BLOCKED, "unsupported protocol"
    return FetchStatus.CONNECTION_ERROR, "connection failed"


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _media_type(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [p.strip() for p in value.split(";")]
    media = parts[0].lower()
    if not _MEDIA_TYPE.fullmatch(media):
        return None, None
    charset = None
    for param in parts[1:]:
        if "=" in param:
            name, _, val = param.partition("=")
            if name.strip().lower() == "charset":
                charset = val.strip().strip("\"'").lower() or None
    return media, charset


def _choose_charset(declared: str | None, body: bytes) -> str:
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    if body.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    match = _META_CHARSET.search(body[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


def _loop_key(url: str) -> str:
    try:
        return normalize_url(url)
    except InvalidURLError:
        return url


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower().rsplit('@', 1)[-1]}"


class Crawler:
    def __init__(
        self,
        settings: CrawlerSettings | None = None,
        *,
        resolver: Resolver = system_resolver,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        test_overrides: UnsafeTestOverrides | None = None,
    ) -> None:
        self._settings = settings or CrawlerSettings()
        overrides = test_overrides or UnsafeTestOverrides()
        self._extra_ports = overrides.extra_ports
        if overrides.ssl_context is not None:
            ssl_context = overrides.ssl_context
        elif self._settings.ca_bundle is not None:
            ssl_context = ssl.create_default_context(cafile=str(self._settings.ca_bundle))
        else:
            ssl_context = httpx.create_ssl_context(trust_env=False)
        backend = GuardedNetworkBackend(resolver=resolver, loopback_hosts=overrides.loopback_hosts)
        transport = build_guarded_transport(backend, ssl_context)
        s = self._settings
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=s.connect_timeout_s,
                read=s.read_timeout_s,
                write=s.connect_timeout_s,
                pool=s.connect_timeout_s,
            ),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        self._client.cookies.jar.set_policy(_RejectAllCookies())
        self._sleep = sleep
        self._global = asyncio.Semaphore(s.concurrency)
        self._host_slots: dict[str, asyncio.Semaphore] = {}
        self._host_next_start: dict[str, float] = {}
        self._robots: dict[str, _RobotsEntry] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Crawler:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- public API ---------------------------------------------------------------------

    async def fetch_many(
        self, targets: Sequence[FetchTarget], context: CrawlContext | None = None
    ) -> list[FetchResult]:
        """Fetch several targets; concurrency is bounded by the crawler's semaphores."""
        return list(await asyncio.gather(*(self.fetch(t, context) for t in targets)))

    async def fetch(self, target: FetchTarget, context: CrawlContext | None = None) -> FetchResult:
        ctx = context or CrawlContext()
        loop = asyncio.get_running_loop()
        crawl_id = uuid.uuid4().hex
        fetched_at = datetime.now(UTC)
        started = time.monotonic()
        budget = self._settings.timeout_s
        if ctx.deadline is not None:
            budget = min(budget, ctx.deadline - loop.time())
        state = _State(deadline=loop.time() + max(budget, 0.0))
        if budget <= 0:
            outcome = _Outcome(FetchStatus.FETCH_TIMEOUT, "no time budget left")
        else:
            try:
                async with asyncio.timeout(budget) as scope:
                    async with self._global:
                        outcome = await self._fetch_with_retry(target.url, state)
            except TimeoutError:
                if not scope.expired():
                    raise
                outcome = _Outcome(FetchStatus.FETCH_TIMEOUT, "total fetch time exceeded")
        result = self._result(crawl_id, target, fetched_at, started, state, outcome)
        log.info(
            "crawl.fetch",
            crawl_id=crawl_id,
            job_id=ctx.job_id,
            request_id=ctx.request_id,
            host=urlsplit(target.url).hostname,
            status=result.status.value,
            http_status=result.http_status,
            duration_ms=result.duration_ms,
            bytes=result.content_length,
            redirects=len(result.redirect_chain),
            attempts=result.attempts,
        )
        return result

    # -- orchestration --------------------------------------------------------------------

    async def _fetch_with_retry(self, url: str, state: _State) -> _Outcome:
        loop = asyncio.get_running_loop()
        attempts = 1 + self._settings.retries
        outcome = _Outcome(FetchStatus.CONNECTION_ERROR, "not attempted")
        for attempt in range(1, attempts + 1):
            state.attempts = attempt
            state.chain = []
            outcome = await self._fetch_once(url, state)
            retryable = outcome.status in RETRYABLE_STATUS or (
                outcome.status is FetchStatus.HTTP_ERROR and outcome.http_status in RETRYABLE_HTTP
            )
            if not retryable:
                return outcome
            if attempt == attempts:
                break
            delay = outcome.retry_after_s
            if delay is None:
                delay = self._settings.retry_backoff_s * _JITTER.uniform(0.5, 1.0)
            if loop.time() + delay >= state.deadline:
                break  # waiting would exceed the budget
            await self._sleep(delay)
        if state.attempts == 1:
            return outcome  # no retry happened (disabled or no budget): report the real failure
        return _Outcome(
            FetchStatus.RETRY_EXHAUSTED,
            "transient failure persisted after retry",
            http_status=outcome.http_status,
            final_url=outcome.final_url,
            cause=outcome.status,
        )

    async def _fetch_once(self, url: str, state: _State) -> _Outcome:
        current = url
        seen: set[str] = set()
        for hop in range(self._settings.max_redirects + 1):
            try:
                host = check_url(current, extra_ports=self._extra_ports)
            except PolicyViolation as violation:
                status = FetchStatus.SSRF_BLOCKED if violation.ssrf else FetchStatus.POLICY_BLOCKED
                return _Outcome(status, violation.reason, final_url=current)
            key = _loop_key(current)
            if key in seen:
                return _Outcome(FetchStatus.REDIRECT_LOOP, "redirect loop", final_url=current)
            seen.add(key)

            blocked = await self._check_robots(current, state)
            if blocked is not None:
                return blocked

            response = await self._request(
                current, host, state, max_bytes=self._settings.max_response_bytes, page=True
            )
            if isinstance(response, _Outcome):
                return response
            state.chain.append(
                RedirectHop(
                    url=current,
                    http_status=response.http_status,
                    location=response.location,
                    connected_ip=state.resolved_ip,
                )
            )
            if hop == self._settings.max_redirects:
                return _Outcome(FetchStatus.REDIRECT_LIMIT, "too many redirects", final_url=current)
            target = urljoin(current, response.location)
            if (
                urlsplit(current).scheme.lower() == "https"
                and urlsplit(target).scheme.lower() == "http"
            ):
                return _Outcome(
                    FetchStatus.POLICY_BLOCKED, "https to http redirect", final_url=target
                )
            current = target
        return _Outcome(FetchStatus.REDIRECT_LIMIT, "too many redirects", final_url=current)

    # -- robots.txt -----------------------------------------------------------------------

    async def _check_robots(self, url: str, state: _State) -> _Outcome | None:
        origin = _origin(url)
        robots_url = f"{origin}/robots.txt"
        entry = await self._robots_entry(origin, robots_url, state)
        if entry.outcome == "SSRF_BLOCKED":
            return _Outcome(FetchStatus.SSRF_BLOCKED, "resolved address not allowed", final_url=url)
        if entry.network_status is not None:
            state.robots = RobotsDecision(
                robots_url=robots_url, outcome="UNREACHABLE_DISALLOW", allowed=False
            )
            return _Outcome(entry.network_status, "host unreachable", final_url=url)
        if entry.outcome == "PARSED":
            allowed = entry.rules is not None and entry.rules.is_allowed(ROBOTS_AGENT, url)
            outcome = "ALLOWED" if allowed else "DISALLOWED"
        elif entry.outcome == "UNAVAILABLE_ALLOW":
            allowed, outcome = True, "UNAVAILABLE_ALLOW"
        else:
            allowed, outcome = False, "UNREACHABLE_DISALLOW"
        state.robots = RobotsDecision(robots_url=robots_url, outcome=outcome, allowed=allowed)
        if not allowed:
            return _Outcome(FetchStatus.ROBOTS_BLOCKED, "disallowed by robots.txt", final_url=url)
        return None

    async def _robots_entry(self, origin: str, robots_url: str, state: _State) -> _RobotsEntry:
        loop = asyncio.get_running_loop()
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._robots.get(origin)
            if cached is not None and cached.expires > loop.time():
                return cached
            entry = await self._fetch_robots(robots_url, state)
            if entry.outcome in ("PARSED", "UNAVAILABLE_ALLOW"):
                self._robots[origin] = entry  # errors are re-evaluated next time
            return entry

    async def _fetch_robots(self, robots_url: str, state: _State) -> _RobotsEntry:
        loop = asyncio.get_running_loop()
        ttl = loop.time() + self._settings.robots_cache_ttl_s
        budget = min(self._settings.robots_timeout_s, state.deadline - loop.time())
        robots_state = _State(deadline=state.deadline)
        current = robots_url
        try:
            async with asyncio.timeout(max(budget, 0.0)) as scope:
                for _ in range(self._settings.max_redirects + 1):
                    try:
                        host = check_url(current, extra_ports=self._extra_ports)
                    except PolicyViolation as violation:
                        outcome = "SSRF_BLOCKED" if violation.ssrf else "UNREACHABLE_DISALLOW"
                        return _RobotsEntry(ttl, outcome, None)
                    response = await self._request(
                        current,
                        host,
                        robots_state,
                        max_bytes=self._settings.robots_max_bytes,
                        page=False,
                    )
                    if isinstance(response, _Redirect):
                        target = urljoin(current, response.location)
                        if (
                            urlsplit(current).scheme.lower() == "https"
                            and urlsplit(target).scheme.lower() == "http"
                        ):
                            return _RobotsEntry(ttl, "UNREACHABLE_DISALLOW", None)
                        current = target
                        continue
                    return self._robots_from_outcome(response, ttl)
                return _RobotsEntry(ttl, "UNREACHABLE_DISALLOW", None)
        except TimeoutError:
            if not scope.expired():
                raise
            return _RobotsEntry(ttl, "UNREACHABLE_DISALLOW", None)

    @staticmethod
    def _robots_from_outcome(outcome: _Outcome, ttl: float) -> _RobotsEntry:
        if outcome.status is FetchStatus.SSRF_BLOCKED:
            return _RobotsEntry(ttl, "SSRF_BLOCKED", None)
        if outcome.status in _ROBOTS_NETWORK_FAILURES:
            return _RobotsEntry(ttl, "UNREACHABLE_DISALLOW", None, network_status=outcome.status)
        if outcome.status is FetchStatus.OK and outcome.body is not None:
            text = outcome.body.decode(_choose_charset(outcome.charset, outcome.body), "replace")
            return _RobotsEntry(ttl, "PARSED", RobotsRules.parse(text))
        if (
            outcome.status is FetchStatus.HTTP_ERROR
            and outcome.http_status is not None
            and 400 <= outcome.http_status < 500
        ):
            return _RobotsEntry(ttl, "UNAVAILABLE_ALLOW", RobotsRules.allow_all())
        return _RobotsEntry(ttl, "UNREACHABLE_DISALLOW", None)

    # -- one HTTP request -----------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _host_slot(self, host: str) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        slot = self._host_slots.setdefault(
            host, asyncio.Semaphore(self._settings.per_host_concurrency)
        )
        async with slot:
            wait = self._host_next_start.get(host, 0.0) - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_next_start[host] = loop.time() + self._settings.per_host_min_interval_s
            yield

    async def _request(
        self, url: str, host: str, state: _State, *, max_bytes: int, page: bool
    ) -> _Outcome | _Redirect:
        async with self._host_slot(host):
            record: list[tuple[str, str]] = []
            token = CONNECTIONS.set(record)
            try:
                response = await self._client.send(
                    self._client.build_request("GET", url), stream=True
                )
            except httpx.HTTPError as exc:
                status, message = _map_exception(exc)
                return _Outcome(status, message, final_url=url)
            finally:
                CONNECTIONS.reset(token)
            try:
                if not record:
                    # Fail closed: a response that did not pass the guarded connect is refused.
                    return _Outcome(
                        FetchStatus.SSRF_BLOCKED, "connection was not validated", final_url=url
                    )
                state.resolved_ip = record[-1][1]
                return await self._read(response, url, max_bytes=max_bytes, page=page)
            except httpx.HTTPError as exc:
                status, message = _map_exception(exc)
                return _Outcome(status, message, final_url=url)
            finally:
                await response.aclose()

    async def _read(
        self, response: httpx.Response, url: str, *, max_bytes: int, page: bool
    ) -> _Outcome | _Redirect:
        status_code = response.status_code
        if status_code in REDIRECT_CODES:
            location = response.headers.get("location")
            if not location:
                return _Outcome(
                    FetchStatus.INVALID_RESPONSE,
                    "redirect without location",
                    http_status=status_code,
                    final_url=url,
                )
            return _Redirect(location=location, http_status=status_code)
        if not 200 <= status_code < 300:
            return _Outcome(
                FetchStatus.HTTP_ERROR,
                f"http status {status_code}",
                http_status=status_code,
                retry_after_s=_parse_retry_after(response.headers.get("retry-after")),
                final_url=url,
            )
        media, charset = _media_type(response.headers.get("content-type"))
        if page and media not in ALLOWED_CONTENT_TYPES:
            return _Outcome(
                FetchStatus.UNSUPPORTED_CONTENT_TYPE,
                "content type not supported",
                http_status=status_code,
                final_url=url,
                content_type=media,
            )
        wire_cap = max_bytes + max_bytes // 10 + 65_536
        try:
            decoder = BoundedDecoder(response.headers.get("content-encoding"), max_bytes=max_bytes)
        except UnsupportedEncoding:
            return _Outcome(
                FetchStatus.INVALID_RESPONSE,
                "unsupported content encoding",
                http_status=status_code,
                final_url=url,
            )
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit():
            limit = max_bytes if decoder.encoding == "identity" else wire_cap
            if int(declared) > limit:
                return _Outcome(
                    FetchStatus.RESPONSE_TOO_LARGE,
                    "declared content length over limit",
                    http_status=status_code,
                    final_url=url,
                )
        wire = 0
        try:
            async for chunk in response.aiter_raw():
                wire += len(chunk)
                if wire > wire_cap:
                    raise BodyTooLarge
                decoder.feed(chunk)
            body = decoder.finish()
        except BodyTooLarge:
            return _Outcome(
                FetchStatus.RESPONSE_TOO_LARGE,
                "response body over limit",
                http_status=status_code,
                final_url=url,
            )
        except BodyDecodeError:
            return _Outcome(
                FetchStatus.INVALID_RESPONSE,
                "body could not be decompressed",
                http_status=status_code,
                final_url=url,
            )
        return _Outcome(
            FetchStatus.OK,
            "ok",
            http_status=status_code,
            final_url=url,
            content_type=media,
            charset=charset,
            body=body,
            wire_bytes=wire,
        )

    # -- result -----------------------------------------------------------------------------

    def _result(
        self,
        crawl_id: str,
        target: FetchTarget,
        fetched_at: datetime,
        started: float,
        state: _State,
        outcome: _Outcome,
    ) -> FetchResult:
        duration_ms = int((time.monotonic() - started) * 1000)
        common: dict[str, Any] = {
            "crawl_id": crawl_id,
            "requested_url": target.url,
            "final_url": outcome.final_url,
            "status": outcome.status,
            "http_status": outcome.http_status,
            "content_type": outcome.content_type,
            "redirect_chain": list(state.chain),
            "resolved_ip": state.resolved_ip if outcome.http_status is not None else None,
            "robots": state.robots,
            "attempts": state.attempts,
            "fetched_at": fetched_at,
            "duration_ms": duration_ms,
            "source": target.source,
        }
        if outcome.status is not FetchStatus.OK or outcome.body is None:
            category = CATEGORY[outcome.status] or "INTERNAL_ERROR"
            common["error"] = FetchError(
                code=outcome.status, category=category, message=outcome.message, cause=outcome.cause
            )
            return FetchResult(**common)
        charset = _choose_charset(outcome.charset, outcome.body)
        text = outcome.body.decode(charset, "replace")
        title = None
        if outcome.content_type in ("text/html", "application/xhtml+xml"):
            parser = _TitleParser()
            with contextlib.suppress(Exception):
                parser.feed(text[:65_536])
            title = parser.title
        return FetchResult(
            **common,
            charset=charset,
            content_length=len(outcome.body),
            wire_bytes=outcome.wire_bytes,
            content=text,
            title=title,
            content_sha256=hashlib.sha256(outcome.body).hexdigest(),
        )
