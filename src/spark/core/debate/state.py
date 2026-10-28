"""Debate state machine. Pause is a runtime condition, never persisted."""

from __future__ import annotations

from enum import Enum


class DebateState(str, Enum):
    SETUP = "setup"
    OPENING = "opening"
    ROUND = "round"
    RULING = "ruling"
    QA = "qa"


class DebateStateError(Exception):
    """Raised on an illegal state transition."""


_TRANSITIONS: dict[DebateState, set[DebateState]] = {
    DebateState.SETUP: {DebateState.OPENING},
    DebateState.OPENING: {DebateState.ROUND},
    DebateState.ROUND: {DebateState.ROUND, DebateState.RULING},
    DebateState.RULING: {DebateState.QA},
    DebateState.QA: set(),
}


def assert_transition(current: DebateState, new: DebateState) -> None:
    """Raise DebateStateError unless current -> new is a legal transition."""
    if new not in _TRANSITIONS.get(current, set()):
        raise DebateStateError(f"Illegal debate transition: {current.value} -> {new.value}")
