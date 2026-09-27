"""URL and address policy — pure functions, no I/O.

The authoritative SSRF decision is made on *resolved* addresses at connect time
(``crawler.network``); the URL checks here are defence in depth and fail fast.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
DENIED_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")

# Hosts made only of digits/hex/dots/'x' that are not a canonical dotted quad
# (2130706433, 0x7f000001, 017700000001, 127.1, 0x7f.0.0.1 …): the OS resolver
# accepts them as IPv4 (probe E1) — reject before any lookup.
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$", re.IGNORECASE)
_CANONICAL_V4 = re.compile(
    r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$"
)


class PolicyViolation(Exception):
    """URL refused by policy. ``ssrf`` distinguishes address-based refusals."""

    def __init__(self, reason: str, *, ssrf: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ssrf = ssrf


def is_blocked_address(address: str) -> bool:
    """True if connecting to ``address`` must be refused.

    ``ipaddress.is_global`` alone is not enough (probe E2: multicast and NAT64-embedded
    loopback report ``is_global=True``), so embedded IPv4 addresses are unwrapped and
    multicast/unspecified are refused explicitly.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            candidates.append(ip.sixtofour)
        if ip.teredo is not None:
            candidates.extend(ip.teredo)
        if any(ip in net for net in _NAT64):
            # NAT64 prefixes sit inside ::/8, which ``ipaddress`` flags as reserved; judge a
            # NAT64 address only by the IPv4 address it embeds.
            candidates = [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)]
        if ip in _SIX_TO_FOUR or ip in _TEREDO:
            return True
    return any(
        (not c.is_global) or c.is_multicast or c.is_unspecified or c.is_reserved for c in candidates
    )


def check_url(url: str, *, extra_ports: frozenset[int] = frozenset()) -> str:
    """Validate a URL for fetching and return its host (lower-case, no brackets).

    Raises ``PolicyViolation``. ``extra_ports`` exists only for the test infrastructure.
    """
    if len(url) > 2048:
        raise PolicyViolation("url too long")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise PolicyViolation("malformed url") from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise PolicyViolation("scheme not allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise PolicyViolation("credentials in url")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise PolicyViolation("missing host")
    if "%" in host:
        raise PolicyViolation("zone id or encoded host")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in ALLOWED_PORTS and effective_port not in extra_ports:
        raise PolicyViolation("port not allowed")
    if host in DENIED_HOSTNAMES or host.endswith(".localhost"):
        raise PolicyViolation("local hostname", ssrf=True)
    if _NUMERIC_HOST.fullmatch(host) and not _CANONICAL_V4.fullmatch(host):
        raise PolicyViolation("non-canonical numeric host", ssrf=True)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and is_blocked_address(str(literal)):
        raise PolicyViolation("address not allowed", ssrf=True)
    return host
