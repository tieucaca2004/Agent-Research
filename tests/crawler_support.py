"""Test infrastructure for the crawler: a tiny local HTTP/1.1 (+TLS) server with host routing,
request recording and concurrency tracking, plus throw-away certificates made with openssl.

Everything binds to 127.0.0.1. Test hostnames (``*.test``) resolve to 127.0.0.1 only through the
test resolver, and the crawler accepts them only via ``UnsafeTestOverrides``.
"""

from __future__ import annotations

import asyncio
import gzip
import shutil
import ssl
import subprocess
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_agent.crawler import Crawler, CrawlerSettings, UnsafeTestOverrides
from research_agent.crawler.network import system_resolver


@dataclass
class Req:
    method: str
    path: str
    host: str
    headers: dict[str, str]


Handler = Callable[[Req, asyncio.StreamWriter], Awaitable[None]]


async def send(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    *,
    content_length: bool = True,
) -> None:
    lines = [f"HTTP/1.1 {status} X"]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    if content_length:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


def page(
    body: bytes | str = b"<html><head><title>T</title></head><body>hi</body></html>",
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
    content_length: bool = True,
) -> Handler:
    data = body.encode() if isinstance(body, str) else body

    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        h = dict(headers or {})
        if content_type is not None:
            h["Content-Type"] = content_type
        await send(writer, status, h, data, content_length=content_length)

    return handler


def redirect(location: str, status: int = 302) -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        await send(writer, status, {"Location": location})

    return handler


def chunked(parts: list[bytes], content_type: str = "text/html") -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        head = (
            f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
            "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode())
        for part in parts:
            writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    return handler


def stall_headers(seconds: float) -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(seconds)
        await send(writer, 200, {"Content-Type": "text/html"}, b"late")

    return handler


def stall_body(seconds: float, started: asyncio.Event | None = None) -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100\r\n\r\npart"
        )
        await writer.drain()
        if started is not None:
            started.set()
        await asyncio.sleep(seconds)

    return handler


def drip(total_s: float, interval_s: float) -> Handler:
    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100000\r\n\r\n"
        )
        loop = asyncio.get_running_loop()
        end = loop.time() + total_s
        while loop.time() < end:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(interval_s)

    return handler


def sequence(*handlers: Handler) -> Handler:
    """Different handler per call; the last repeats."""
    calls = {"n": 0}

    async def handler(req: Req, writer: asyncio.StreamWriter) -> None:
        index = min(calls["n"], len(handlers) - 1)
        calls["n"] += 1
        await handlers[index](req, writer)

    return handler


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data)


def zlib_deflate(data: bytes) -> bytes:
    return zlib.compress(data)


def raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


@dataclass
class LocalServer:
    ssl_context: ssl.SSLContext | None = None
    routes: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[Req] = field(default_factory=list)
    in_flight: dict[str, int] = field(default_factory=dict)
    max_in_flight: dict[str, int] = field(default_factory=dict)
    total_in_flight: int = 0
    max_total_in_flight: int = 0
    closed_early: int = 0
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    def route(self, host: str, path: str, handler: Handler) -> None:
        self.routes[(host, path)] = handler

    def paths(self, host: str | None = None) -> list[str]:
        return [r.path for r in self.requests if host is None or r.host == host]

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            method, target, _ = line.decode("latin-1").split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                raw = await reader.readline()
                if raw in (b"\r\n", b"\n", b""):
                    break
                k, _, v = raw.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            host = headers.get("host", "").split(":")[0]
            req = Req(method, target, host, headers)
            self.requests.append(req)
            self.in_flight[host] = self.in_flight.get(host, 0) + 1
            self.max_in_flight[host] = max(self.max_in_flight.get(host, 0), self.in_flight[host])
            self.total_in_flight += 1
            self.max_total_in_flight = max(self.max_total_in_flight, self.total_in_flight)
            try:
                handler = self.routes.get((host, target)) or self.routes.get(("*", target))
                if handler is None:
                    await send(writer, 404, {"Content-Type": "text/plain"}, b"not found")
                else:
                    await handler(req, writer)
            finally:
                self.in_flight[host] -= 1
                self.total_in_flight -= 1
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            self.closed_early += 1
        except asyncio.CancelledError:
            self.closed_early += 1
            raise
        finally:
            writer.close()


def make_certificates(directory: Path) -> tuple[Path, Path, Path] | None:
    """Create a throw-away CA and a server certificate for ``tls.test``/127.0.0.1.
    Returns (ca_cert, server_cert, server_key) or None when openssl is unavailable."""
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    ca_key, ca_crt = directory / "ca.key", directory / "ca.crt"
    key, csr, crt = directory / "srv.key", directory / "srv.csr", directory / "srv.crt"
    ext = directory / "ext.cnf"
    ext.write_text("subjectAltName=DNS:tls.test,DNS:tls2.test,IP:127.0.0.1\n")
    run = [openssl]
    cmds = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_crt),
            "-days",
            "2",
            "-subj",
            "/CN=crawler-test-ca",
        ],
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            "/CN=tls.test",
        ],
        [
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(crt),
            "-days",
            "2",
            "-extfile",
            str(ext),
        ],
    ]
    for cmd in cmds:
        subprocess.run(run + cmd, check=True, capture_output=True)  # noqa: S603 — fixed args
    return ca_crt, crt, key


TEST_HOSTS = frozenset(
    {
        "a.test",
        "b.test",
        "c.test",
        "d.test",
        "e.test",
        "f.test",
        "g.test",
        "h.test",
        "tls.test",
        "tls2.test",
        "robots.test",
        "rebind.test",
    }
)


async def fake_resolver(host: str, port: int) -> list[str]:
    if host.endswith(".test"):
        return ["127.0.0.1"]
    return await system_resolver(host, port)


def make_crawler(
    ports: set[int],
    *,
    resolver: Any = fake_resolver,
    ssl_context: ssl.SSLContext | None = None,
    sleep: Any = None,
    loopback_hosts: frozenset[str] = TEST_HOSTS,
    **settings: Any,
) -> Crawler:
    base: dict[str, Any] = {
        "per_host_min_interval_s": 0.0,
        "retry_backoff_s": 0.0,
        "connect_timeout_s": 2.0,
        "read_timeout_s": 2.0,
        "timeout_s": 5.0,
    }
    base.update(settings)
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return Crawler(
        CrawlerSettings(_env_file=None, **base),
        resolver=resolver,
        test_overrides=UnsafeTestOverrides(
            loopback_hosts=loopback_hosts, extra_ports=frozenset(ports), ssl_context=ssl_context
        ),
        **kwargs,
    )
