"""Outermost ASGI middleware for the Research API.

- ``http_request_id``: taken from ``X-Request-ID`` if it matches ``^[A-Za-z0-9._-]{1,64}$``
  (prevents log injection), otherwise generated; bound to logs as ``http_request_id`` (never
  ``request_id``, which means *search execution id* in Sprint 01/02 logs); returned in the
  ``X-Request-ID`` header and the envelope's ``meta.request_id``.
- request body limit, enforced on ``Content-Length`` and on the streamed body (the body is
  buffered here, at most ``max_body_bytes``, then replayed to the application).
- catch-all: unexpected exceptions become a sanitized 500 envelope (type name logged, never
  the message or stack trace).
- one ``http.request`` log line per request (method, route template, status, duration).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_agent.logging import get_logger

log = get_logger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class _BodyTooLarge(Exception):
    pass


def _error_body(request_id: str, code: str, message: str, reason: str | None = None) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if reason is not None:
        error["reason"] = reason
    return json.dumps({"data": None, "error": error, "meta": {"request_id": request_id}}).encode()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        supplied = headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        scope.setdefault("state", {})["http_request_id"] = request_id

        started = time.monotonic()
        status_holder = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                message = dict(message)
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        async def send_error(status: int, body: bytes) -> None:
            if status_holder["started"]:
                return
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": body})

        async def buffered_body() -> list[Message] | None:
            """Read the whole request body up to the limit (FastAPI would turn an exception
            raised while it reads the body into a 400, so the limit is enforced here)."""
            messages: list[Message] = []
            total = 0
            while True:
                message = await receive()
                messages.append(message)
                if message["type"] != "http.request":
                    return messages
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    return None
                if not message.get("more_body", False):
                    return messages

        with structlog.contextvars.bound_contextvars(http_request_id=request_id):
            try:
                declared = headers.get("content-length")
                if (
                    declared is not None
                    and declared.isdigit()
                    and int(declared) > self.max_body_bytes
                ):
                    raise _BodyTooLarge
                messages = await buffered_body()
                if messages is None:
                    raise _BodyTooLarge

                async def replay() -> Message:
                    return messages.pop(0) if messages else await receive()

                await self.app(scope, replay, send_wrapper)
            except _BodyTooLarge:
                await send_error(
                    413,
                    _error_body(
                        request_id,
                        "VALIDATION_ERROR",
                        "request body too large",
                        "PAYLOAD_TOO_LARGE",
                    ),
                )
            except Exception as exc:
                log.error("http.unhandled_error", error_type=type(exc).__name__)
                await send_error(
                    500, _error_body(request_id, "INTERNAL_ERROR", "internal server error")
                )
            finally:
                route = scope.get("route")
                log.info(
                    "http.request",
                    method=scope.get("method"),
                    route=getattr(route, "path", None),
                    status=status_holder["status"],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    job_id=scope.get("path_params", {}).get("job_id"),
                )
