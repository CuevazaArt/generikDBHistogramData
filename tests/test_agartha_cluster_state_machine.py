"""Tests for the bot state machine."""
from __future__ import annotations

import pytest

from backtest.agartha_cluster.models import BotState, TERMINAL_STATES
from backtest.agartha_cluster.state_machine import (
    InvalidTransition,
    can_transition,
    is_terminal,
    successors,
    transition,
)


def test_happy_path_full_lifecycle():
    sequence = [
        BotState.CREATED,
        BotState.OPTIMIZING,
        BotState.OPTIMIZED,
        BotState.QUEUED,
        BotState.PLACING_ENTRY,
        BotState.AWAITING_ENTRY_FILL,
        BotState.IN_POSITION,
        BotState.PLACING_EXIT,
        BotState.AWAITING_EXIT_FILL,
        BotState.CLOSED_WIN,
    ]
    for cur, nxt in zip(sequence, sequence[1:]):
        assert can_transition(cur, nxt)
        assert transition(cur, nxt) == nxt


def test_idempotent_same_state_transition():
    assert transition(BotState.IN_POSITION, BotState.IN_POSITION) == BotState.IN_POSITION


def test_invalid_transitions_raise():
    with pytest.raises(InvalidTransition):
        transition(BotState.CREATED, BotState.IN_POSITION)
    with pytest.raises(InvalidTransition):
        transition(BotState.CLOSED_WIN, BotState.QUEUED)
    with pytest.raises(InvalidTransition):
        transition(BotState.IN_POSITION, BotState.OPTIMIZING)


def test_stale_exit_recovery_paths():
    assert can_transition(BotState.AWAITING_EXIT_FILL, BotState.STALE_EXIT)
    assert can_transition(BotState.STALE_EXIT, BotState.PLACING_EXIT)
    assert can_transition(BotState.STALE_EXIT, BotState.MANUAL_CLOSED)


def test_terminal_states_have_no_successors():
    for s in TERMINAL_STATES:
        assert is_terminal(s)
        assert successors(s) == frozenset()


def test_failed_optimization_is_terminal():
    assert can_transition(BotState.OPTIMIZING, BotState.FAILED_OPTIMIZATION)
    assert is_terminal(BotState.FAILED_OPTIMIZATION)


def test_manual_close_from_position():
    assert can_transition(BotState.IN_POSITION, BotState.MANUAL_CLOSED)
    assert is_terminal(BotState.MANUAL_CLOSED)
