"""Job-layer errors raised to callers (never stored as job failures).

Job *failures* are recorded on the job as ``JobError`` (see ``jobs.models``); these exceptions
signal misuse of the job API: unknown job, illegal transition, double execution, key reuse.
"""

from __future__ import annotations

from research_agent.core.errors import ResearchAgentError


class JobNotFoundError(ResearchAgentError):
    code = "JOB_NOT_FOUND"
    category = "INVALID_INPUT"

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"job {job_id} not found")


class InvalidJobTransitionError(ResearchAgentError):
    """A state change that the job state machine forbids. Always a programming error."""

    code = "INVALID_JOB_TRANSITION"
    category = "INTERNAL_ERROR"

    def __init__(self, job_id: str, current: str, target: str, allowed: list[str]) -> None:
        self.job_id = job_id
        self.current = current
        self.target = target
        self.allowed = allowed
        super().__init__(
            f"job {job_id}: transition {current} -> {target} not allowed "
            f"(allowed: {', '.join(allowed) or 'none, terminal state'})"
        )


class JobNotClaimableError(ResearchAgentError):
    """``run`` was called for a job that is not QUEUED (already running, finished or
    cancelled). Guarantees a job executes at most once; there is no job-level retry."""

    code = "JOB_NOT_CLAIMABLE"
    category = "INVALID_INPUT"

    def __init__(self, job_id: str, status: str) -> None:
        self.job_id = job_id
        self.status = status
        super().__init__(f"job {job_id} cannot be started from status {status}")


class JobVersionConflictError(ResearchAgentError):
    """Optimistic-concurrency check failed: the job changed since it was read."""

    code = "JOB_VERSION_CONFLICT"
    category = "INTERNAL_ERROR"

    def __init__(self, job_id: str, expected: int, actual: int) -> None:
        super().__init__(f"job {job_id}: expected version {expected}, found {actual}")


class IdempotencyConflictError(ResearchAgentError):
    code = "IDEMPOTENCY_CONFLICT"
    category = "INVALID_INPUT"

    def __init__(self, key_hash: str) -> None:
        super().__init__(f"idempotency key {key_hash} was already used with a different request")
