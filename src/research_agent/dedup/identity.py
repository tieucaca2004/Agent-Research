"""Source identity and group keys (Sprint 06, P1/P8/P10).

Identity uses only crawler-observed URLs with the unchanged S01 normalizer: no tracking-parameter
policy of its own, and claimed canonical URLs / ``og:url`` / JSON-LD ids are never inputs.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from research_agent.core.errors import InvalidURLError
from research_agent.core.urls import normalize_url
from research_agent.extraction.models import ExtractedDocument

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def source_identity(document: ExtractedDocument) -> str | None:
    """``normalize_url(final_url or requested_url)``, or ``None`` if the URL is not usable.

    ``normalize_url`` raises ``InvalidURLError``, and ``ValueError`` from ``urlsplit`` for
    malformed hosts such as an unclosed IPv6 bracket; both mean "no identity"."""
    provenance = document.provenance
    url = provenance.final_url or provenance.requested_url
    try:
        return normalize_url(url)
    except (InvalidURLError, ValueError):
        return None


def host_of(identity: str | None) -> str | None:
    return urlsplit(identity).hostname if identity is not None else None


def is_sha256_hex(value: str) -> bool:
    return _SHA256_HEX.fullmatch(value) is not None


def l1_key(identity: str) -> str:
    return f"L1:url:{identity}"


def l2_key(content_sha256: str) -> str:
    return f"L2:sha256:{content_sha256}"


def l3_key(text_sha256: str) -> str:
    return f"L3:sha256:{text_sha256}"
