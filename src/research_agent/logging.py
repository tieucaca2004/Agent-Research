"""Structured logging with secret redaction.

Every event passes through ``redact_secrets``:
- values under sensitive keys (``*key*``, ``*token*``, ``*secret*``, ``authorization``…)
  are replaced with ``[REDACTED]``
- ``key=`` / ``api_key=`` / ``token=`` query parameters inside any string are masked
- ``Bearer <token>`` inside any string is masked
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|^key$|token|secret|password|authorization|cookie)", re.IGNORECASE
)
_QUERY_SECRET = re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token|token)=)[^&#\s'\"]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_text(text: str) -> str:
    text = _QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    return _BEARER.sub(rf"\1{REDACTED}", text)


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(key, v) for v in value]
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        event_dict[key] = _redact_value(key, event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )
    # httpx logs full request URLs at INFO, which would include query-string API keys.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
