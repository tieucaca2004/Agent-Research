"""SSRF-guarded network layer.

``GuardedNetworkBackend.connect_tcp`` resolves the host, validates *every* returned address and
connects to one of those validated addresses inside the same call — the HTTP client never
resolves the name again, so there is no validate/connect TOCTOU window (DNS rebinding).
TLS still verifies the certificate against the hostname: httpcore passes the origin host as
``server_hostname`` to ``start_tls`` independently of the address we connect to.

The backend is installed by replacing the connection pool of an ``httpx.AsyncHTTPTransport``,
keeping httpx's own httpcore→httpx exception mapping. This relies on httpx internals
(``_pool``); ``build_guarded_transport`` verifies them and FAILS CLOSED (raises) if they differ.
Keep-alive is disabled so every request goes through ``connect_tcp`` and is recorded.
"""

from __future__ import annotations

import asyncio
import contextvars
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio
import httpcore
import httpx

from research_agent.crawler.policy import is_blocked_address

Resolver = Callable[[str, int], Awaitable[list[str]]]


class CrawlerSetupError(RuntimeError):
    """The guarded transport could not be installed; the crawler refuses to run."""


class SSRFBlockedError(httpcore.ConnectError):
    pass


class DNSResolutionError(httpcore.ConnectError):
    pass


# Connections made for the current fetch: (host, connected address). Set per request by the
# crawler; the backend appends to it. Keep-alive is off, so one request ⇒ one entry.
CONNECTIONS: contextvars.ContextVar[list[tuple[str, str]] | None] = contextvars.ContextVar(
    "crawler_connections", default=None
)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise DNSResolutionError(f"dns resolution failed ({type(exc).__name__})") from None
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        loopback_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._resolver = resolver
        # Test infrastructure only: these exact hostnames may resolve to loopback addresses.
        self._loopback_hosts = loopback_hosts

    def _blocked(self, host: str, address: str) -> bool:
        if not is_blocked_address(address):
            return False
        if host.lower() in self._loopback_hosts:
            return address not in ("127.0.0.1", "::1")
        return True

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout):
                addresses = await self._resolver(host, port)
                if not addresses:
                    raise DNSResolutionError("dns returned no address")
                if any(self._blocked(host, a) for a in addresses):
                    # Any disallowed address blocks the host (mixed public/private answers).
                    raise SSRFBlockedError("resolved address not allowed")
                last_error: Exception | None = None
                for address in addresses:
                    try:
                        stream = await self._inner.connect_tcp(
                            address,
                            port,
                            timeout=timeout,
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except httpcore.ConnectError as exc:
                        last_error = exc
                        continue
                    record = CONNECTIONS.get()
                    if record is not None:
                        record.append((host, address))
                    return stream
                raise last_error or httpcore.ConnectError("connection failed")
        except TimeoutError:
            raise httpcore.ConnectTimeout("connect timed out") from None

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — httpcore backend interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFBlockedError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def build_guarded_transport(
    backend: GuardedNetworkBackend, ssl_context: ssl.SSLContext
) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, verify=ssl_context, proxy=None)
    current = getattr(transport, "_pool", None)
    if not isinstance(current, httpcore.AsyncConnectionPool):
        raise CrawlerSetupError("unsupported httpx transport internals; refusing to run unguarded")
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_keepalive_connections=0,
        network_backend=backend,
    )
    transport._pool = pool
    if (
        getattr(transport, "_pool", None) is not pool
        or getattr(pool, "_network_backend", None) is not backend
    ):
        raise CrawlerSetupError("guarded network backend not installed; refusing to run")
    return transport
