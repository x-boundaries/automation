"""Explicit fail-closed job state transitions."""

from __future__ import annotations

from typing import Iterable

from .models import JobState


class InvalidTransition(RuntimeError):
    pass


ALLOWED_TRANSITIONS = {
    JobState.RECEIVED: frozenset({JobState.VALIDATED, JobState.REJECTED_VALIDATION}),
    JobState.VALIDATED: frozenset({JobState.QUEUED, JobState.REJECTED_VALIDATION}),
    JobState.QUEUED: frozenset({JobState.LEASED, JobState.RETRY_WAIT, JobState.DEAD_LETTER}),
    JobState.LEASED: frozenset({JobState.PRECHECKING, JobState.RETRY_WAIT, JobState.DEAD_LETTER}),
    JobState.PRECHECKING: frozenset({JobState.ALLOCATION_BOUND, JobState.REJECTED_VALIDATION, JobState.RETRY_WAIT, JobState.AMBIGUOUS_LOOKUP, JobState.MANUAL_REVIEW}),
    JobState.ALLOCATION_BOUND: frozenset({JobState.WRITE_INTENT_RECORDED, JobState.AMBIGUOUS_LOOKUP, JobState.MANUAL_REVIEW, JobState.RETRY_WAIT}),
    JobState.WRITE_INTENT_RECORDED: frozenset({JobState.WRITING, JobState.WRITE_OUTCOME_UNCERTAIN, JobState.MANUAL_REVIEW, JobState.RETRY_WAIT}),
    JobState.WRITING: frozenset({JobState.READBACK, JobState.WRITE_OUTCOME_UNCERTAIN, JobState.CONFIRMED_NOT_CREATED, JobState.CREATED_READBACK_MISMATCH}),
    JobState.READBACK: frozenset({JobState.CREATED_VERIFIED, JobState.CREATED_READBACK_MISMATCH, JobState.WRITE_OUTCOME_UNCERTAIN, JobState.CONFIRMED_NOT_CREATED}),
    JobState.RETRY_WAIT: frozenset({JobState.QUEUED, JobState.LEASED, JobState.DEAD_LETTER, JobState.MANUAL_REVIEW}),
    JobState.AMBIGUOUS_LOOKUP: frozenset({JobState.MANUAL_REVIEW}),
    JobState.WRITE_OUTCOME_UNCERTAIN: frozenset({JobState.CREATED_VERIFIED, JobState.CONFIRMED_NOT_CREATED, JobState.CREATED_READBACK_MISMATCH, JobState.MANUAL_REVIEW}),
    JobState.CONFIRMED_NOT_CREATED: frozenset({JobState.MANUAL_REVIEW}),
    JobState.CREATED_READBACK_MISMATCH: frozenset({JobState.MANUAL_REVIEW}),
    JobState.MANUAL_REVIEW: frozenset({JobState.DEAD_LETTER}),
    JobState.REJECTED_VALIDATION: frozenset({JobState.DEAD_LETTER}),
    JobState.DEAD_LETTER: frozenset(),
    JobState.CREATED_VERIFIED: frozenset(),
}

TERMINAL_STATES = frozenset({JobState.CREATED_VERIFIED, JobState.REJECTED_VALIDATION, JobState.CONFIRMED_NOT_CREATED, JobState.CREATED_READBACK_MISMATCH, JobState.MANUAL_REVIEW, JobState.DEAD_LETTER})
POST_DISPATCH_STATES = frozenset({JobState.WRITING, JobState.READBACK, JobState.CREATED_VERIFIED, JobState.WRITE_OUTCOME_UNCERTAIN, JobState.CONFIRMED_NOT_CREATED, JobState.CREATED_READBACK_MISMATCH, JobState.MANUAL_REVIEW, JobState.DEAD_LETTER})


def as_state(value: JobState | str) -> JobState:
    try:
        return value if isinstance(value, JobState) else JobState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTransition("unknown_state") from exc


def validate_transition(current: JobState | str, target: JobState | str, *, dispatch_fenced: bool = False) -> None:
    current_state, target_state = as_state(current), as_state(target)
    if dispatch_fenced and target_state not in POST_DISPATCH_STATES:
        raise InvalidTransition("post_dispatch_state_cannot_requeue")
    if target_state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        raise InvalidTransition(f"transition_not_allowed:{current_state.value}:{target_state.value}")


def next_state(current: JobState | str, target: JobState | str, *, dispatch_fenced: bool = False) -> JobState:
    validate_transition(current, target, dispatch_fenced=dispatch_fenced)
    return as_state(target)


def can_auto_retry(state: JobState | str, *, dispatch_fenced: bool) -> bool:
    return not dispatch_fenced and as_state(state) in {JobState.QUEUED, JobState.LEASED, JobState.PRECHECKING, JobState.ALLOCATION_BOUND, JobState.WRITE_INTENT_RECORDED, JobState.RETRY_WAIT}


def transition_values(state: JobState | str) -> Iterable[str]:
    return tuple(item.value for item in ALLOWED_TRANSITIONS[as_state(state)])
