from __future__ import annotations

import itertools

import pytest

from contentshare.jobs.states import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransitionError,
    RequestState,
    check_transition,
)

S = RequestState


def test_every_state_has_a_rule() -> None:
    assert set(TRANSITIONS) == set(RequestState)


def test_terminal_states() -> None:
    assert {S.REPLIED, S.SKIPPED, S.REJECTED} == TERMINAL


def test_happy_path() -> None:
    path = [S.NEW, S.READY, S.APPROVED, S.COPYING, S.VERIFIED, S.REPLIED]
    for a, b in itertools.pairwise(path):
        check_transition(a, b)


def test_failed_copy_can_retry_but_never_reply() -> None:
    check_transition(S.COPYING, S.FAILED)
    check_transition(S.FAILED, S.APPROVED)
    for target in (S.VERIFIED, S.REPLIED, S.COPYING):
        with pytest.raises(InvalidTransitionError):
            check_transition(S.FAILED, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.NEW, S.COPYING),  # cannot skip approval
        (S.READY, S.COPYING),  # cannot skip approval
        (S.NEEDS_REVIEW, S.APPROVED),  # must be marked READY by the user first
        (S.APPROVED, S.VERIFIED),
        (S.VERIFIED, S.FAILED),
        (S.REPLIED, S.READY),
        (S.REJECTED, S.READY),
        (S.SKIPPED, S.APPROVED),
    ],
)
def test_forbidden(current: RequestState, target: RequestState) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(current, target)


def test_only_approved_requests_can_start_copying() -> None:
    sources = {s for s, nxt in TRANSITIONS.items() if S.COPYING in nxt}
    assert sources == {S.APPROVED}
