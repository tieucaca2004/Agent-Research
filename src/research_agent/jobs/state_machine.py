"""Job state machine.

Allowed (for a job with configured ``stages`` s0 … sN):

    QUEUED      -> s0, CANCELLED, FAILED
    s_i (i < N) -> s_{i+1}, CANCELLED, FAILED
    sN          -> COMPLETED, PARTIAL, FAILED, CANCELLED
    terminal    -> (nothing)

Everything else — backwards, skipping a stage, re-entering QUEUED, self-transitions, leaving a
terminal state, QUEUED -> COMPLETED/PARTIAL — raises ``InvalidJobTransitionError``.
"""

from __future__ import annotations

from research_agent.jobs.errors import InvalidJobTransitionError
from research_agent.jobs.models import TERMINAL_STATES, JobState

_OUTCOMES = frozenset({JobState.COMPLETED, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED})


def allowed_targets(stages: tuple[JobState, ...], current: JobState) -> frozenset[JobState]:
    if current in TERMINAL_STATES:
        return frozenset()
    if current is JobState.QUEUED:
        return frozenset({stages[0], JobState.CANCELLED, JobState.FAILED})
    if current not in stages:
        return frozenset()
    index = stages.index(current)
    if index < len(stages) - 1:
        return frozenset({stages[index + 1], JobState.CANCELLED, JobState.FAILED})
    return _OUTCOMES


def check_transition(
    job_id: str, stages: tuple[JobState, ...], current: JobState, target: JobState
) -> None:
    allowed = allowed_targets(stages, current)
    if target not in allowed:
        raise InvalidJobTransitionError(
            job_id, current.value, target.value, sorted(s.value for s in allowed)
        )
