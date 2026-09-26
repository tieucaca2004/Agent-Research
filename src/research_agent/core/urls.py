"""URL normalization shared by search, discovery and crawler.

Normalized form:
- scheme and host lower-cased; host IDNA-encoded; only http/https accepted
- userinfo (``user:pass@``) dropped — credentials never propagate
- default ports (80/443) removed
- empty path becomes ``/``; dot segments resolved
- fragment removed
- tracking query parameters removed; remaining parameters sorted (stable)
"""

from __future__ import annotations

import posixpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from research_agent.core.errors import InvalidURLError

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "srsltid",
    }
)
TRACKING_PREFIXES = ("utm_",)

MAX_URL_LENGTH = 2048


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _normalize_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL has no host")
    if host.startswith("["):  # IPv6 literal, keep as-is
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidURLError(f"Invalid host: {host!r}") from exc


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    trailing = path.endswith("/")
    normalized = posixpath.normpath(path)
    if normalized.startswith("//"):  # posixpath keeps a leading '//' pair
        normalized = "/" + normalized.lstrip("/")
    if normalized == ".":
        normalized = "/"
    if trailing and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def normalize_url(url: str, base: str | None = None) -> str:
    """Return the canonical form of ``url`` (resolved against ``base`` if relative).

    Raises ``InvalidURLError`` for non-http(s) schemes, missing hosts, bad ports
    or overly long URLs.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidURLError("URL is empty")
    raw = url.strip()
    if base is not None:
        raw = urljoin(base, raw)
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidURLError(f"URL longer than {MAX_URL_LENGTH} characters")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError(f"Unsupported URL scheme: {scheme or '(none)'}")

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("Invalid port") from exc

    host = _normalize_host(parts.hostname or "")
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]
    query = urlencode(sorted(query_pairs))

    return urlunsplit((scheme, netloc, _normalize_path(parts.path), query, ""))


def url_domain(normalized_url: str) -> str:
    """Host of an already-normalized URL, without a leading ``www.``."""
    host = urlsplit(normalized_url).hostname or ""
    return host.removeprefix("www.")
