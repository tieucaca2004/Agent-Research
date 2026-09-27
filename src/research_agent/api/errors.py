"""API error contract.

``code`` is one of ARCHITECTURE §14's codes; ``reason`` (additive) gives the precise cause.
Messages are fixed strings built here — never exception text, stack traces or input values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.reason = reason
        self.details = dict(details or {})
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason is not None:
            error["reason"] = self.reason
        if self.details:
            error["details"] = self.details
        return error


def job_not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "research job not found", reason="JOB_NOT_FOUND")


def idempotency_conflict() -> ApiError:
    return ApiError(
        409,
        "CONFLICT",
        "Idempotency-Key was already used with a different request",
        reason="IDEMPOTENCY_KEY_REUSED",
    )


def job_not_finished(status: str) -> ApiError:
    return ApiError(
        409,
        "CONFLICT",
        "results are available once the job has finished",
        reason="JOB_NOT_FINISHED",
        details={"status": status},
    )


def job_already_finished(status: str) -> ApiError:
    return ApiError(
        409,
        "CONFLICT",
        "the job has already finished and cannot be cancelled",
        reason="JOB_ALREADY_FINISHED",
        details={"status": status},
    )


def job_failed_without_results(job_error: Mapping[str, Any] | None) -> ApiError:
    return ApiError(
        409,
        "CONFLICT",
        "the job failed before producing any result",
        reason="JOB_FAILED",
        details={"status": "FAILED", "job_error": dict(job_error) if job_error else None},
    )


def capacity_exhausted(retry_after_s: int) -> ApiError:
    return ApiError(
        503,
        "CAPACITY_EXHAUSTED",
        "too many research jobs are running or queued; retry later",
        reason="CAPACITY_EXHAUSTED",
        headers={"Retry-After": str(retry_after_s)},
    )


def requires_configuration(missing: list[str]) -> ApiError:
    return ApiError(
        503,
        "REQUIRES_CONFIGURATION",
        "no search provider is configured",
        details={"missing": missing},
    )
