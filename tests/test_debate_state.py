"""Tests for the debate state machine."""

from __future__ import annotations

import pytest

from spark.core.debate.state import DebateState, DebateStateError, assert_transition


class TestTransitions:
    @pytest.mark.parametrize(
        "current,new",
        [
            (DebateState.SETUP, DebateState.OPENING),
            (DebateState.OPENING, DebateState.ROUND),
            (DebateState.ROUND, DebateState.ROUND),
            (DebateState.ROUND, DebateState.RULING),
            (DebateState.RULING, DebateState.QA),
        ],
    )
    def test_legal(self, current, new) -> None:
        assert_transition(current, new)  # must not raise

    @pytest.mark.parametrize(
        "current,new",
        [
            (DebateState.SETUP, DebateState.ROUND),
            (DebateState.SETUP, DebateState.RULING),
            (DebateState.OPENING, DebateState.RULING),
            (DebateState.RULING, DebateState.ROUND),
            (DebateState.QA, DebateState.ROUND),
            (DebateState.QA, DebateState.RULING),
        ],
    )
    def test_illegal(self, current, new) -> None:
        with pytest.raises(DebateStateError):
            assert_transition(current, new)
