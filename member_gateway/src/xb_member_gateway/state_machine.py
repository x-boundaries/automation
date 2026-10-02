"""Explicit fail-closed v2 job state transitions (W-G2-149 section 3).

Only the edges below can be entered. Legacy v1 states (PRECHECKING,
ALLOCATION_BOUND, WRITE_INTENT_RECORDED, WRITING, READBACK, AMBIGUOUS_LOOKUP,
WRITE_OUTCOME_UNCERTAIN, WRITER_TERMINATION_UNCONFIRMED,
CONFIRMED_NOT_CREATED, CREATED_READBACK_MISMATCH, DEAD_LETTER) remain valid
stored values that can be read, but no edge enters or leaves them.
"""

from __future__ import annotations

from typing import Iterable

from .models import JobState


class InvalidTransition(RuntimeError):
    pass


LIVE_STATES = frozenset({JobState.RECEIVED, JobState.VALIDATED, JobState.QUEUED, JobState.LEASED, JobState.RETRY_WAIT})
V2_TERMINAL_STATES = frozenset({JobState.CREATED_VERIFIED, JobState.LINKED_EXISTING, JobState.REJECTED_VALIDATION, JobState.RESOLVED})
HUMAN_QUEUE_STATES = frozenset({JobState.MANUAL_REVIEW})
LEGACY_STATES = frozenset(
    {
        JobState.PRECHECKING, JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED, JobState.WRITING,
        JobState.READBACK, JobState.AMBIGUOUS_LOOKUP, JobState.WRITE_OUTCOME_UNCERTAIN,
        JobState.WRITER_TERMINATION_UNCONFIRMED, JobState.CONFIRMED_NOT_CREATED,
        JobState.CREATED_READBACK_MISMATCH, JobState.DEAD_LETTER,
    }
)

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.RECEIVED: frozenset({JobState.VALIDATED, JobState.REJECTED_VALIDATION}),
    JobState.VALIDATED: frozenset({JobState.QUEUED, JobState.REJECTED_VALIDATION}),
    JobState.QUEUED: frozenset({JobState.LEASED}),
    JobState.RETRY_WAIT: frozenset({JobState.LEASED}),
    JobState.LEASED: frozenset(
        {
            JobState.CREATED_VERIFIED, JobState.LINKED_EXISTING, JobState.REJECTED_VALIDATION,
            JobState.MANUAL_REVIEW, JobState.RETRY_WAIT,
        }
    ),
    JobState.MANUAL_REVIEW: frozenset({JobState.RESOLVED, JobState.QUEUED}),
    JobState.CREATED_VERIFIED: frozenset(),
    JobState.LINKED_EXISTING: frozenset(),
    JobState.REJECTED_VALIDATION: frozenset(),
    JobState.RESOLVED: frozenset(),
    **{state: frozenset() for state in LEGACY_STATES},
}

# A job in one of these states can never be claimed again and holds no lease.
TERMINAL_STATES = V2_TERMINAL_STATES | HUMAN_QUEUE_STATES | LEGACY_STATES
CLAIMABLE_STATES = frozenset({JobState.QUEUED, JobState.RETRY_WAIT})


def as_state(value: JobState | str) -> JobState:
    try:
        return value if isinstance(value, JobState) else JobState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTransition("unknown_state") from exc


def validate_transition(current: JobState | str, target: JobState | str) -> None:
    current_state, target_state = as_state(current), as_state(target)
    if target_state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        raise InvalidTransition(f"transition_not_allowed:{current_state.value}:{target_state.value}")


def next_state(current: JobState | str, target: JobState | str) -> JobState:
    validate_transition(current, target)
    return as_state(target)


def transition_values(state: JobState | str) -> Iterable[str]:
    return tuple(item.value for item in ALLOWED_TRANSITIONS[as_state(state)])
