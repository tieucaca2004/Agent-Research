"""Sprint 04: crawler integration + security tests against local servers (127.0.0.1 only)."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
import structlog

from research_agent.core.models import SearchResult
from research_agent.crawler import (
    CrawlContext,
    Crawler,
    CrawlerSettings,
    CrawlerSetupError,
    FetchStatus,
    FetchTarget,
)
from research_agent.crawler.network import DNSResolutionError
from research_agent.logging import configure_logging
from tests.crawler_support import (
    LocalServer,
    Req,
    chunked,
    drip,
    fake_resolver,
    gzip_bytes,
    make_certificates,
    make_crawler,
    page,
    raw_deflate,
    redirect,
    send,
    sequence,
    stall_body,
    stall_headers,
    zlib_deflate,
)

MIB = 1024 * 1024
S = FetchStatus


@pytest.fixture
async def server() -> AsyncIterator[LocalServer]:
    srv = LocalServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    made = make_certificates(tmp_path_factory.mktemp("certs"))
    if made is None:
        pytest.skip("REQUIRES_OPENSSL: openssl binary not available for TLS test certificates")
    return made


@pytest.fixture
async def tls_server(certs: tuple[Path, Path, Path]) -> AsyncIterator[LocalServer]:
    _, crt, key = certs
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(crt), str(key))
    srv = LocalServer(ssl_context=ctx)
    await srv.start()
    yield srv
    await srv.stop()


def client_ctx(certs: tuple[Path, Path, Path]) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(certs[0]))


def u(srv: LocalServer, host: str, path: str = "/", scheme: str = "http") -> str:
    return f"{scheme}://{host}:{srv.port}{path}"


async def fetch(crawler: Crawler, url: str, context: CrawlContext | None = None) -> Any:
    async with crawler:
        return await crawler.fetch(FetchTarget(url=url), context)


# --- success + provenance ----------------------------------------------------------------


async def test_fetch_html_with_provenance(server: LocalServer) -> None:
    server.route("a.test", "/menu", page("<html><title> Sushi  Menu </title><p>cá hồi</p></html>"))
    source = SearchResult(
        title="t",
        url="http://a.test/menu",
        original_url=u(server, "a.test", "/menu"),
        source="perplexity",
        rank=3,
        query="món Nhật",
        metadata={"provider_request_id": "r1"},
    )
    async with make_crawler({server.port}) as crawler:
        result = await crawler.fetch(FetchTarget.from_search_result(source))
    assert result.status is S.OK
    assert result.error is None
    assert result.source == source  # search provenance carried through unchanged
    assert result.requested_url == source.original_url
    assert result.final_url == source.original_url
    assert (result.http_status, result.content_type, result.charset) == (200, "text/html", "utf-8")
    assert result.title == "Sushi Menu"
    assert "cá hồi" in (result.content or "")
    assert result.content_length == len(result.content.encode()) if result.content else False
    assert result.resolved_ip == "127.0.0.1"
    assert result.robots is not None and result.robots.outcome == "UNAVAILABLE_ALLOW"
    assert result.content_sha256 and len(result.content_sha256) == 64
    assert result.fetched_at.tzinfo is not None and result.duration_ms >= 0
    assert result.redirect_chain == [] and result.attempts == 1
    ua = next(r for r in server.requests if r.path == "/menu").headers["user-agent"]
    assert ua.startswith("ResearchAgentCrawler/") and "Mozilla" not in ua and "http" not in ua


async def test_text_plain_and_charset(server: LocalServer) -> None:
    server.route(
        "a.test",
        "/t",
        page("Xin chào".encode("cp1258"), content_type="text/plain; charset=windows-1258"),
    )
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/t"))
    assert result.status is S.OK and result.content == "Xin chào" and result.charset == "cp1258"
    assert result.title is None


# --- SSRF --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f000001",
        "017700000001",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[64:ff9b::7f00:1]",
        "[fe80::1]",
    ],
)
async def test_ssrf_blocked_hosts_never_reach_network(server: LocalServer, host: str) -> None:
    result = await fetch(make_crawler({server.port}), f"http://{host}:{server.port}/")
    assert result.status is S.SSRF_BLOCKED, result
    assert result.error is not None and result.error.category == "POLICY_BLOCKED"
    assert server.requests == []


async def test_hostname_resolving_to_private_address_blocked_at_connect(
    server: LocalServer,
) -> None:
    calls: list[str] = []

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        return ["10.0.0.5"] if host == "evil.test" else await fake_resolver(host, port)

    result = await fetch(make_crawler({server.port}, resolver=resolver), u(server, "evil.test"))
    assert result.status is S.SSRF_BLOCKED
    assert server.requests == []
    assert result.attempts == 1  # never retried


async def test_mixed_public_private_answer_blocks_host(server: LocalServer) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1", "10.0.0.9"]  # a.test may be loopback, 10.x never

    result = await fetch(make_crawler({server.port}, resolver=resolver), u(server, "a.test"))
    assert result.status is S.SSRF_BLOCKED
    assert server.requests == []


async def test_dns_rebinding_connects_only_to_validated_address(server: LocalServer) -> None:
    server.route("rebind.test", "/", page())
    answers = iter([["127.0.0.1"], ["127.0.0.1"], ["10.0.0.1"], ["10.0.0.1"]])

    async def resolver(host: str, port: int) -> list[str]:
        return next(answers)

    async with make_crawler({server.port}, resolver=resolver) as crawler:
        first = await crawler.fetch(FetchTarget(url=u(server, "rebind.test")))
        crawler._robots.clear()
        second = await crawler.fetch(FetchTarget(url=u(server, "rebind.test")))
    assert first.status is S.OK and first.resolved_ip == "127.0.0.1"
    assert second.status is S.SSRF_BLOCKED  # rebound answer rejected at connect time
    assert server.paths("rebind.test") == ["/robots.txt", "/"]


async def test_proxy_environment_is_ignored(
    server: LocalServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = LocalServer()
    await proxy.start()
    try:
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.setenv(var, f"http://127.0.0.1:{proxy.port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        server.route("a.test", "/", page())
        async with make_crawler({server.port}) as crawler:
            ok = await crawler.fetch(FetchTarget(url=u(server, "a.test")))
            blocked = await crawler.fetch(FetchTarget(url=f"http://localhost:{server.port}/"))
        assert ok.status is S.OK
        assert blocked.status is S.SSRF_BLOCKED
        assert proxy.requests == []  # never used
    finally:
        await proxy.stop()


async def test_fail_closed_when_httpx_internals_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.AsyncHTTPTransport.__init__

    def broken_init(self: httpx.AsyncHTTPTransport, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        del self._pool

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", broken_init)
    with pytest.raises(CrawlerSetupError):
        Crawler(CrawlerSettings(_env_file=None))


async def test_unguarded_connection_is_refused(server: LocalServer) -> None:
    """Fail closed: if the pool were ever swapped for an unguarded one, a response whose
    connection was not validated (no connect record) is refused instead of returned."""
    from research_agent.crawler.crawler import _Outcome, _State

    server.route("*", "/", page())
    crawler = make_crawler({server.port})
    transport = crawler._client._transport
    transport._pool = httpcore.AsyncConnectionPool(max_keepalive_connections=0)  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    outcome = await crawler._request(
        f"http://127.0.0.1:{server.port}/",
        "127.0.0.1",
        _State(deadline=loop.time() + 5),
        max_bytes=1024,
        page=True,
    )
    await crawler.aclose()
    assert isinstance(outcome, _Outcome)
    assert outcome.status is S.SSRF_BLOCKED
    assert server.paths() == ["/"]  # the connection really happened, the response was refused


# --- redirects ---------------------------------------------------------------------------


async def test_redirect_public_to_public_revalidates_each_hop(server: LocalServer) -> None:
    server.route("a.test", "/start", redirect(u(server, "b.test", "/end"), 301))
    server.route("b.test", "/end", page())
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/start"))
    assert result.status is S.OK
    assert result.final_url == u(server, "b.test", "/end")
    assert [(h.url, h.http_status, h.connected_ip) for h in result.redirect_chain] == [
        (u(server, "a.test", "/start"), 301, "127.0.0.1")
    ]
    assert server.paths("b.test") == ["/robots.txt", "/end"]  # robots checked for the new host


@pytest.mark.parametrize(
    "location",
    [
        "http://10.0.0.1/",
        "http://localhost:{port}/",
        "http://2130706433:{port}/",
        "http://127.0.0.1:{port}/",
        "http://[::1]:{port}/",
        "http://169.254.169.254/",
    ],
)
async def test_redirect_to_private_blocked(server: LocalServer, location: str) -> None:
    server.route("a.test", "/r", redirect(location.format(port=server.port)))
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/r"))
    assert result.status is S.SSRF_BLOCKED
    assert server.paths() == ["/robots.txt", "/r"]


async def test_redirect_to_hostname_resolving_private_blocked(server: LocalServer) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["192.168.0.10"] if host == "evil.test" else ["127.0.0.1"]

    server.route("a.test", "/r", redirect(u(server, "evil.test", "/")))
    result = await fetch(make_crawler({server.port}, resolver=resolver), u(server, "a.test", "/r"))
    assert result.status is S.SSRF_BLOCKED


async def test_redirect_to_disallowed_port_and_scheme(server: LocalServer) -> None:
    server.route("a.test", "/p", redirect("http://b.test:8080/"))
    server.route("a.test", "/f", redirect("file:///etc/passwd"))
    async with make_crawler({server.port}) as crawler:
        port = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/p")))
        scheme = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/f")))
    assert port.status is S.POLICY_BLOCKED
    assert scheme.status is S.POLICY_BLOCKED


async def test_https_to_http_downgrade_blocked(
    tls_server: LocalServer, server: LocalServer, certs: tuple[Path, Path, Path]
) -> None:
    tls_server.route("tls.test", "/", redirect(u(server, "a.test", "/")))
    server.route("a.test", "/", page())
    crawler = make_crawler({tls_server.port, server.port}, ssl_context=client_ctx(certs))
    result = await fetch(crawler, u(tls_server, "tls.test", "/", "https"))
    assert result.status is S.POLICY_BLOCKED
    assert server.paths("a.test") == []


async def test_robots_redirect_https_to_http_downgrade_denies(
    tls_server: LocalServer, server: LocalServer, certs: tuple[Path, Path, Path]
) -> None:
    tls_server.route("tls.test", "/robots.txt", redirect(u(server, "a.test", "/robots.txt")))
    tls_server.route("tls.test", "/", page())
    crawler = make_crawler({tls_server.port, server.port}, ssl_context=client_ctx(certs))
    result = await fetch(crawler, u(tls_server, "tls.test", "/", "https"))
    assert result.status is S.ROBOTS_BLOCKED
    assert result.robots is not None and result.robots.outcome == "UNREACHABLE_DISALLOW"
    assert server.paths("a.test") == []  # the http robots.txt was never requested
    assert tls_server.paths("tls.test") == ["/robots.txt"]


async def test_http_to_https_upgrade_allowed(
    tls_server: LocalServer, server: LocalServer, certs: tuple[Path, Path, Path]
) -> None:
    server.route("a.test", "/", redirect(u(tls_server, "tls.test", "/secure", "https"), 308))
    tls_server.route("tls.test", "/secure", page())
    crawler = make_crawler({tls_server.port, server.port}, ssl_context=client_ctx(certs))
    result = await fetch(crawler, u(server, "a.test", "/"))
    assert result.status is S.OK
    assert result.final_url == u(tls_server, "tls.test", "/secure", "https")


async def test_redirect_loop_and_limit(server: LocalServer) -> None:
    server.route("a.test", "/x", redirect(u(server, "b.test", "/y")))
    server.route("b.test", "/y", redirect(u(server, "a.test", "/x")))
    for i in range(7):
        server.route("c.test", f"/{i}", redirect(u(server, "c.test", f"/{i + 1}")))
    server.route("d.test", "/0", redirect("/1"))
    for i in range(1, 5):
        server.route("d.test", f"/{i}", redirect(f"/{i + 1}"))
    server.route("d.test", "/5", page())
    async with make_crawler({server.port}) as crawler:
        loop = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/x")))
        limit = await crawler.fetch(FetchTarget(url=u(server, "c.test", "/0")))
        five = await crawler.fetch(FetchTarget(url=u(server, "d.test", "/0")))
    assert loop.status is S.REDIRECT_LOOP
    assert limit.status is S.REDIRECT_LIMIT and len(limit.redirect_chain) == 6
    assert five.status is S.OK and len(five.redirect_chain) == 5  # exactly 5 redirects allowed


async def test_redirect_with_credentials_blocked(server: LocalServer) -> None:
    server.route("a.test", "/r", redirect(f"http://user:pw@b.test:{server.port}/"))
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/r"))
    assert result.status is S.POLICY_BLOCKED
    assert server.paths("b.test") == []


# --- network errors ----------------------------------------------------------------------


async def test_dns_failure() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        raise DNSResolutionError("nxdomain")

    result = await fetch(
        make_crawler(set(), resolver=resolver, loopback_hosts=frozenset()),
        "http://nowhere.example/",
    )
    assert result.status is S.DNS_ERROR and result.attempts == 1
    real = await fetch(Crawler(CrawlerSettings(_env_file=None)), "http://no-such-host.invalid/")
    assert real.status is S.DNS_ERROR


async def test_connect_timeout() -> None:
    async def slow(host: str, port: int) -> list[str]:
        await asyncio.sleep(5)
        return ["127.0.0.1"]

    result = await fetch(
        make_crawler({1}, resolver=slow, connect_timeout_s=0.2, retries=0), "http://a.test:1/"
    )
    assert result.status is S.CONNECT_TIMEOUT
    assert result.error is not None and result.error.category == "TIMEOUT"


async def test_read_timeout(server: LocalServer) -> None:
    server.route("a.test", "/slow", stall_headers(3))
    result = await fetch(
        make_crawler({server.port}, read_timeout_s=0.2), u(server, "a.test", "/slow")
    )
    assert result.status is S.READ_TIMEOUT and result.attempts == 1


async def test_connection_refused(server: LocalServer) -> None:
    probe = LocalServer()
    port = await probe.start()
    await probe.stop()  # now nothing listens on this port
    result = await fetch(make_crawler({port}, retries=0), f"http://a.test:{port}/")
    assert result.status is S.CONNECTION_ERROR


async def test_tls_failure_untrusted_certificate(tls_server: LocalServer) -> None:
    tls_server.route("tls.test", "/", page())
    result = await fetch(make_crawler({tls_server.port}), u(tls_server, "tls.test", "/", "https"))
    assert result.status is S.TLS_ERROR
    assert tls_server.paths("tls.test") == []


async def test_tls_hostname_verified_against_certificate(
    tls_server: LocalServer, certs: tuple[Path, Path, Path]
) -> None:
    """IP pinning must not weaken verification: a name not in the certificate fails."""

    async def resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    crawler = make_crawler({tls_server.port}, ssl_context=client_ctx(certs), resolver=resolver)
    result = await fetch(crawler, u(tls_server, "a.test", "/", "https"))  # cert is for tls.test
    assert result.status is S.TLS_ERROR


# --- response validation ------------------------------------------------------------------


async def test_response_size_exact_and_over(server: LocalServer) -> None:
    exact = b"a" * (5 * MIB)
    server.route("a.test", "/exact", page(exact, content_type="text/plain"))
    server.route("a.test", "/over", page(exact + b"a", content_type="text/plain"))
    server.route(
        "a.test", "/over-nolen", page(exact + b"a", content_type="text/plain", content_length=False)
    )
    server.route(
        "a.test", "/exact-nolen", page(exact, content_type="text/plain", content_length=False)
    )
    async with make_crawler({server.port}) as crawler:
        ok = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/exact")))
        over = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/over")))
        over_nolen = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/over-nolen")))
        ok_nolen = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/exact-nolen")))
    assert ok.status is S.OK and ok.content_length == 5 * MIB
    assert ok_nolen.status is S.OK and ok_nolen.content_length == 5 * MIB
    assert over.status is S.RESPONSE_TOO_LARGE
    assert over_nolen.status is S.RESPONSE_TOO_LARGE


async def test_chunked_responses(server: LocalServer) -> None:
    server.route("a.test", "/c", chunked([b"<html>", b"hello", b"</html>"]))
    server.route("a.test", "/big", chunked([b"x" * 65_536] * 20))
    async with make_crawler({server.port}, max_response_bytes=1 * MIB) as crawler:
        ok = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/c")))
        big = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/big")))
    assert ok.status is S.OK and ok.content == "<html>hello</html>"
    assert big.status is S.RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip_bytes), ("deflate", zlib_deflate), ("deflate", raw_deflate)],
)
async def test_compressed_responses(server: LocalServer, encoding: str, compress: Any) -> None:
    body = b"<html><title>Z</title>" + b"y" * 10_000 + b"</html>"
    server.route("a.test", "/z", page(compress(body), headers={"Content-Encoding": encoding}))
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/z"))
    assert result.status is S.OK and result.content_length == len(body) and result.title == "Z"
    assert result.wire_bytes is not None and result.wire_bytes < len(body)


@pytest.mark.parametrize(
    ("encoding", "compress"), [("gzip", gzip_bytes), ("deflate", zlib_deflate)]
)
async def test_decompression_bomb(server: LocalServer, encoding: str, compress: Any) -> None:
    bomb = compress(b"\0" * (100 * MIB))
    server.route("a.test", "/bomb", page(bomb, headers={"Content-Encoding": encoding}))
    crawler = make_crawler({server.port}, max_response_bytes=1 * MIB)
    tracemalloc.start()
    try:
        result = await fetch(crawler, u(server, "a.test", "/bomb"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.status is S.RESPONSE_TOO_LARGE
    assert peak < 16 * MIB, f"peak {peak // MIB} MiB"
    assert crawler._client._transport._pool.connections == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "image/png",
        "video/mp4",
        "audio/mpeg",
        "application/octet-stream",
        "application/json",
        None,
        "garbage",
        "text/",
    ],
)
async def test_unsupported_or_missing_content_type(
    server: LocalServer, content_type: str | None
) -> None:
    server.route("a.test", "/f", page(b"%PDF-1.7 binary", content_type=content_type))
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/f"))
    assert result.status is S.UNSUPPORTED_CONTENT_TYPE
    assert result.content is None


async def test_unsupported_content_encoding(server: LocalServer) -> None:
    server.route("a.test", "/br", page(b"xx", headers={"Content-Encoding": "br"}))
    result = await fetch(make_crawler({server.port}), u(server, "a.test", "/br"))
    assert result.status is S.INVALID_RESPONSE


async def test_http_errors(server: LocalServer) -> None:
    server.route("a.test", "/404", page(status=404))
    server.route("a.test", "/403", page(status=403))
    async with make_crawler({server.port}) as crawler:
        nf = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/404")))
        fb = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/403")))
    assert (nf.status, nf.http_status, nf.attempts) == (S.HTTP_ERROR, 404, 1)
    assert (fb.status, fb.http_status, fb.attempts) == (S.HTTP_ERROR, 403, 1)
    assert server.paths("a.test").count("/404") == 1


# --- robots.txt ----------------------------------------------------------------------------


async def test_robots_rules_applied(server: LocalServer) -> None:
    # /public/*.pdf$ (15 octets) beats Allow: /public (7); a tie would go to Allow (RFC 9309).
    rules = "User-agent: *\nDisallow: /\nAllow: /public\nDisallow: /public/*.pdf$\n"
    server.route("robots.test", "/robots.txt", page(rules, content_type="text/plain"))
    server.route("robots.test", "/public/page", page())
    async with make_crawler({server.port}) as crawler:
        allowed = await crawler.fetch(FetchTarget(url=u(server, "robots.test", "/public/page")))
        denied = await crawler.fetch(FetchTarget(url=u(server, "robots.test", "/private")))
        pdf = await crawler.fetch(FetchTarget(url=u(server, "robots.test", "/public/x.pdf")))
    assert (
        allowed.status is S.OK
        and allowed.robots is not None
        and allowed.robots.outcome == "ALLOWED"
    )
    assert (
        denied.status is S.ROBOTS_BLOCKED
        and denied.robots is not None
        and denied.robots.outcome == "DISALLOWED"
    )
    assert pdf.status is S.ROBOTS_BLOCKED
    assert server.paths("robots.test") == [
        "/robots.txt",
        "/public/page",
    ]  # cached, blocked pages not fetched


async def test_robots_user_agent_group(server: LocalServer) -> None:
    rules = "User-agent: *\nDisallow: /\n\nUser-agent: ResearchAgentCrawler\nAllow: /\n"
    server.route("robots.test", "/robots.txt", page(rules, content_type="text/plain"))
    server.route("robots.test", "/x", page())
    result = await fetch(make_crawler({server.port}), u(server, "robots.test", "/x"))
    assert result.status is S.OK


@pytest.mark.parametrize(
    ("handler", "expected_status", "expected_outcome"),
    [
        (page(status=404, content_type="text/plain"), S.OK, "UNAVAILABLE_ALLOW"),
        (page(status=403, content_type="text/plain"), S.OK, "UNAVAILABLE_ALLOW"),
        (page(status=500, content_type="text/plain"), S.ROBOTS_BLOCKED, "UNREACHABLE_DISALLOW"),
        (page(status=503, content_type="text/plain"), S.ROBOTS_BLOCKED, "UNREACHABLE_DISALLOW"),
        (stall_headers(3), S.ROBOTS_BLOCKED, "UNREACHABLE_DISALLOW"),
    ],
)
async def test_robots_status_mapping(
    server: LocalServer, handler: Any, expected_status: FetchStatus, expected_outcome: str
) -> None:
    server.route("robots.test", "/robots.txt", handler)
    server.route("robots.test", "/x", page())
    crawler = make_crawler({server.port}, robots_timeout_s=0.3)
    result = await fetch(crawler, u(server, "robots.test", "/x"))
    assert result.status is expected_status
    assert result.robots is not None and result.robots.outcome == expected_outcome
    if expected_status is S.ROBOTS_BLOCKED:
        assert "/x" not in server.paths("robots.test")


async def test_robots_network_error_denies(server: LocalServer) -> None:
    async def closer(req: Req, writer: asyncio.StreamWriter) -> None:
        writer.transport.abort()

    server.route("robots.test", "/robots.txt", closer)
    server.route("robots.test", "/x", page())
    result = await fetch(
        make_crawler({server.port}, sleep=Sleeps()), u(server, "robots.test", "/x")
    )
    # connection established then dropped while serving robots.txt → deny the page
    assert result.status is S.ROBOTS_BLOCKED
    assert result.robots is not None and result.robots.outcome == "UNREACHABLE_DISALLOW"
    assert "/x" not in server.paths("robots.test")


async def test_robots_unreachable_host_reports_network_failure() -> None:
    probe = LocalServer()
    port = await probe.start()
    await probe.stop()
    result = await fetch(make_crawler({port}, retries=0), f"http://robots.test:{port}/x")
    assert result.status is S.CONNECTION_ERROR  # not hidden behind ROBOTS_BLOCKED
    assert result.robots is not None and result.robots.outcome == "UNREACHABLE_DISALLOW"


async def test_robots_redirect_followed_and_revalidated(server: LocalServer) -> None:
    server.route("robots.test", "/robots.txt", redirect("/real-robots.txt", 301))
    server.route(
        "robots.test",
        "/real-robots.txt",
        page("User-agent: *\nDisallow: /no\n", content_type="text/plain"),
    )
    server.route("robots.test", "/yes", page())
    server.route("b.test", "/robots.txt", redirect("http://10.0.0.1/robots.txt"))
    server.route("b.test", "/x", page())
    async with make_crawler({server.port}) as crawler:
        no = await crawler.fetch(FetchTarget(url=u(server, "robots.test", "/no")))
        yes = await crawler.fetch(FetchTarget(url=u(server, "robots.test", "/yes")))
        private = await crawler.fetch(FetchTarget(url=u(server, "b.test", "/x")))
    assert no.status is S.ROBOTS_BLOCKED and yes.status is S.OK
    assert private.status is S.SSRF_BLOCKED  # robots redirect to a private address
    assert "/x" not in server.paths("b.test")


async def test_robots_body_size_limited(server: LocalServer) -> None:
    server.route(
        "robots.test",
        "/robots.txt",
        page(b"User-agent: *\n" + b"#" * 10_000, content_type="text/plain"),
    )
    server.route("robots.test", "/x", page())
    result = await fetch(
        make_crawler({server.port}, robots_max_bytes=2048), u(server, "robots.test", "/x")
    )
    assert result.status is S.ROBOTS_BLOCKED  # oversized robots.txt → unreachable → deny


# --- retry ---------------------------------------------------------------------------------


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_retry_503_then_200(server: LocalServer) -> None:
    server.route("a.test", "/r", sequence(page(status=503), page()))
    sleeps = Sleeps()
    result = await fetch(make_crawler({server.port}, sleep=sleeps), u(server, "a.test", "/r"))
    assert result.status is S.OK and result.attempts == 2
    assert len(sleeps.delays) == 1


async def test_retry_503_twice_exhausted(server: LocalServer) -> None:
    server.route("a.test", "/r", page(status=503))
    result = await fetch(make_crawler({server.port}, sleep=Sleeps()), u(server, "a.test", "/r"))
    assert result.status is S.RETRY_EXHAUSTED and result.attempts == 2
    assert result.error is not None and result.error.cause is S.HTTP_ERROR
    assert result.http_status == 503
    assert server.paths("a.test").count("/r") == 2  # exactly one retry


async def test_retry_after_honoured_but_bounded_by_budget(server: LocalServer) -> None:
    server.route("a.test", "/ok", sequence(page(status=429, headers={"Retry-After": "2"}), page()))
    server.route("a.test", "/long", page(status=429, headers={"Retry-After": "600"}))
    sleeps = Sleeps()
    async with make_crawler({server.port}, sleep=sleeps) as crawler:
        ok = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/ok")))
        long = await crawler.fetch(FetchTarget(url=u(server, "a.test", "/long")))
    assert ok.status is S.OK and sleeps.delays == [2.0]
    # waiting 600 s would exceed the budget: no retry, the real failure is reported
    assert (long.status, long.http_status, long.attempts) == (S.HTTP_ERROR, 429, 1)


@pytest.mark.parametrize("path", ["/404", "/ssrf"])
async def test_no_retry_for_permanent_failures(server: LocalServer, path: str) -> None:
    server.route("a.test", "/404", page(status=404))
    server.route("a.test", "/ssrf", redirect("http://10.0.0.1/"))
    sleeps = Sleeps()
    result = await fetch(make_crawler({server.port}, sleep=sleeps), u(server, "a.test", path))
    assert result.attempts == 1 and sleeps.delays == []


async def test_connection_error_retried_once() -> None:
    probe = LocalServer()
    port = await probe.start()
    await probe.stop()
    sleeps = Sleeps()
    result = await fetch(make_crawler({port}, sleep=sleeps), f"http://a.test:{port}/")
    assert result.status is S.RETRY_EXHAUSTED and result.error is not None
    assert result.error.cause is S.CONNECTION_ERROR and result.attempts == 2


# --- cancellation ----------------------------------------------------------------------------


async def test_cancel_during_connection_propagates_without_retry() -> None:
    started = asyncio.Event()
    calls: list[str] = []

    async def hanging(host: str, port: int) -> list[str]:
        calls.append(host)
        started.set()
        await asyncio.sleep(3600)
        return ["127.0.0.1"]

    crawler = make_crawler({1}, resolver=hanging, connect_timeout_s=60, timeout_s=60)
    task = asyncio.create_task(crawler.fetch(FetchTarget(url="http://a.test:1/")))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert calls == ["a.test"]  # robots connect attempt only; no retry after cancellation
    await crawler.aclose()


async def test_cancel_during_read_propagates_and_cleans_up(server: LocalServer) -> None:
    started = asyncio.Event()
    server.route("a.test", "/stall", stall_body(30, started))
    crawler = make_crawler({server.port}, read_timeout_s=60, timeout_s=60)
    before = {t for t in asyncio.all_tasks()}
    task = asyncio.create_task(crawler.fetch(FetchTarget(url=u(server, "a.test", "/stall"))))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert crawler._client._transport._pool.connections == []  # type: ignore[attr-defined]
    leaked = {t for t in asyncio.all_tasks() if t not in before and not t.done()}
    assert not [t for t in leaked if "crawl" in repr(t.get_coro())]
    assert server.paths("a.test").count("/stall") == 1
    await crawler.aclose()


# --- timeouts / deadlines ----------------------------------------------------------------------


async def test_total_fetch_timeout_against_slow_drip(server: LocalServer) -> None:
    server.route("a.test", "/drip", drip(5, 0.05))
    t = time.monotonic()
    result = await fetch(make_crawler({server.port}, timeout_s=0.5), u(server, "a.test", "/drip"))
    assert result.status is S.FETCH_TIMEOUT and time.monotonic() - t < 2


async def test_parent_deadline_bounds_the_crawler(server: LocalServer) -> None:
    server.route("a.test", "/slow", stall_headers(10))
    crawler = make_crawler({server.port}, timeout_s=15, read_timeout_s=15)
    loop = asyncio.get_running_loop()
    t = time.monotonic()
    result = await fetch(
        crawler, u(server, "a.test", "/slow"), CrawlContext(deadline=loop.time() + 0.3)
    )
    assert result.status is S.FETCH_TIMEOUT and time.monotonic() - t < 1.5


async def test_no_budget_left_means_no_request(server: LocalServer) -> None:
    loop = asyncio.get_running_loop()
    result = await fetch(
        make_crawler({server.port}), u(server, "a.test"), CrawlContext(deadline=loop.time() - 1)
    )
    assert result.status is S.FETCH_TIMEOUT and server.requests == []


# --- cookies ----------------------------------------------------------------------------------


async def test_cookies_never_stored_or_sent(server: LocalServer) -> None:
    server.route("a.test", "/set", page(headers={"Set-Cookie": "sid=SECRETCOOKIE; Path=/"}))
    server.route("a.test", "/again", page())
    server.route("b.test", "/other", page())
    async with make_crawler({server.port}) as crawler:
        await crawler.fetch(FetchTarget(url=u(server, "a.test", "/set")))
        await crawler.fetch(FetchTarget(url=u(server, "b.test", "/other")))
        await crawler.fetch(FetchTarget(url=u(server, "a.test", "/again")))
    assert all("cookie" not in r.headers for r in server.requests)
    assert len(crawler._client.cookies.jar) == 0


# --- concurrency --------------------------------------------------------------------------------


def slow_page(seconds: float) -> Any:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(seconds)
        await send(writer, 200, {"Content-Type": "text/html"}, b"ok")

    return handler


async def test_per_host_and_global_concurrency_bounded(server: LocalServer) -> None:
    for host in "abcdefgh":
        server.route(f"{host}.test", "/p", slow_page(0.05))
    same_host = [FetchTarget(url=u(server, "a.test", f"/p?i={i}")) for i in range(6)]
    server.route("a.test", "/p?i=0", slow_page(0.05))
    for i in range(6):
        server.route("a.test", f"/p?i={i}", slow_page(0.05))
    async with make_crawler({server.port}, concurrency=3, per_host_concurrency=1) as crawler:
        results = await crawler.fetch_many(same_host)
        assert all(r.status is S.OK for r in results)
        assert server.max_in_flight["a.test"] == 1
        server.max_total_in_flight = 0
        many = [FetchTarget(url=u(server, f"{h}.test", "/p")) for h in "bcdefgh"]
        results = await crawler.fetch_many(many)
    assert all(r.status is S.OK for r in results)
    assert [r.requested_url for r in results] == [t.url for t in many]  # order preserved
    assert 1 < server.max_total_in_flight <= 3


async def test_per_host_min_interval(server: LocalServer) -> None:
    server.route("a.test", "/1", page())
    server.route("a.test", "/2", page())
    async with make_crawler({server.port}, per_host_min_interval_s=0.2) as crawler:
        t = time.monotonic()
        await crawler.fetch(FetchTarget(url=u(server, "a.test", "/1")))  # robots + page
        await crawler.fetch(FetchTarget(url=u(server, "a.test", "/2")))
    assert time.monotonic() - t >= 0.4  # three requests to one host, 0.2 s apart


# --- logging ------------------------------------------------------------------------------------


async def test_logs_are_safe(server: LocalServer, capsys: pytest.CaptureFixture[str]) -> None:
    server.route(
        "a.test",
        "/p?token=SECRETQUERY",
        page("<html>BODYSECRET</html>", headers={"Set-Cookie": "sid=COOKIESECRET"}),
    )
    configure_logging("INFO", "json")
    try:
        crawler = make_crawler({server.port})
        await fetch(
            crawler,
            u(server, "a.test", "/p?token=SECRETQUERY"),
            CrawlContext(job_id="job-1", request_id="req-1"),
        )
        await fetch(
            make_crawler({server.port}), f"http://user:PASSWORDSECRET@a.test:{server.port}/"
        )
        out = capsys.readouterr().out
    finally:
        structlog.reset_defaults()
    for secret in ("SECRETQUERY", "BODYSECRET", "COOKIESECRET", "PASSWORDSECRET"):
        assert secret not in out
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    fetch_logs = [r for r in records if r["event"] == "crawl.fetch"]
    assert fetch_logs[0]["job_id"] == "job-1" and fetch_logs[0]["request_id"] == "req-1"
    assert fetch_logs[0]["host"] == "a.test" and fetch_logs[0]["status"] == "OK"
    assert {"crawl_id", "duration_ms", "bytes"} <= set(fetch_logs[0])
    assert os.environ.get("CRAWL_TIMEOUT_S") is None  # tests do not depend on crawler env


def test_client_never_trusts_environment() -> None:
    crawler = Crawler(CrawlerSettings(_env_file=None))
    assert crawler._client.trust_env is False


async def test_ssl_cert_file_env_does_not_change_trust(
    tls_server: LocalServer, certs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment CA variables must not widen trust (only CRAWL_CA_BUNDLE does, explicitly)."""
    monkeypatch.setenv("SSL_CERT_FILE", str(certs[0]))
    tls_server.route("tls.test", "/", page())
    result = await fetch(make_crawler({tls_server.port}), u(tls_server, "tls.test", "/", "https"))
    assert result.status is S.TLS_ERROR


async def test_ca_bundle_setting_is_the_explicit_trust_switch(
    tls_server: LocalServer, certs: tuple[Path, Path, Path]
) -> None:
    tls_server.route("tls.test", "/", page())
    crawler = make_crawler({tls_server.port}, ca_bundle=certs[0])
    result = await fetch(crawler, u(tls_server, "tls.test", "/", "https"))
    assert result.status is S.OK  # verification on, against the configured CA
