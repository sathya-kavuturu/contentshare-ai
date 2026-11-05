"""Request state machine. Pure logic, no I/O."""

from __future__ import annotations

from enum import StrEnum


class RequestState(StrEnum):
    NEW = "NEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"
    APPROVED = "APPROVED"
    COPYING = "COPYING"
    VERIFIED = "VERIFIED"
    REPLIED = "REPLIED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


S = RequestState

TRANSITIONS: dict[RequestState, frozenset[RequestState]] = {
    S.NEW: frozenset({S.NEEDS_REVIEW, S.READY, S.SKIPPED, S.REJECTED}),
    S.NEEDS_REVIEW: frozenset({S.READY, S.SKIPPED, S.REJECTED}),
    S.READY: frozenset({S.APPROVED, S.NEEDS_REVIEW, S.SKIPPED, S.REJECTED}),
    S.APPROVED: frozenset({S.COPYING, S.READY, S.SKIPPED}),
    # COPYING -> APPROVED is crash recovery: the job resumes at file level.
    S.COPYING: frozenset({S.VERIFIED, S.FAILED, S.APPROVED}),
    S.FAILED: frozenset({S.APPROVED, S.SKIPPED}),
    S.VERIFIED: frozenset({S.REPLIED}),
    S.REPLIED: frozenset(),
    # A skipped request can be put back by the person (History -> Put back in Requests).
    S.SKIPPED: frozenset({S.NEEDS_REVIEW, S.READY}),
    S.REJECTED: frozenset(),
}

# Skipped is final as far as editing goes: it is only put back as a whole, never changed in place.
TERMINAL = frozenset(state for state, nxt in TRANSITIONS.items() if not nxt) | {S.SKIPPED}


class InvalidTransitionError(ValueError):
    def __init__(self, current: RequestState, target: RequestState) -> None:
        super().__init__(f"Cannot move a request from {current} to {target}")
        self.current = current
        self.target = target


def check_transition(current: RequestState, target: RequestState) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)
