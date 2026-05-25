"""Bot lifecycle state machine.

Pure logic (no DB, no I/O). The caller (BotRunner / supervisor) drives
transitions; this module just validates them and emits the canonical
reason text.

Invalid transitions raise :class:`InvalidTransition` so silent corruption
of a bot's state is impossible.
"""
from __future__ import annotations

from typing import Optional

from backtest.agartha_cluster.models import BotState, TERMINAL_STATES


class InvalidTransition(ValueError):
    """Raised when a requested transition is not allowed."""


# Directed graph of allowed transitions.
_ALLOWED: dict[BotState, frozenset[BotState]] = {
    BotState.CREATED: frozenset({
        BotState.OPTIMIZING,
        BotState.FAILED_DEPLOY,
        BotState.BLACKLISTED,
    }),
    BotState.OPTIMIZING: frozenset({
        BotState.OPTIMIZED,
        BotState.FAILED_OPTIMIZATION,
    }),
    BotState.OPTIMIZED: frozenset({
        BotState.QUEUED,
        BotState.BLACKLISTED,
    }),
    BotState.QUEUED: frozenset({
        BotState.PLACING_ENTRY,
        BotState.FAILED_DEPLOY,
        BotState.BLACKLISTED,
    }),
    BotState.PLACING_ENTRY: frozenset({
        BotState.AWAITING_ENTRY_FILL,
        BotState.FAILED_DEPLOY,
        BotState.CANCELLED_ENTRY,
    }),
    BotState.AWAITING_ENTRY_FILL: frozenset({
        BotState.IN_POSITION,
        BotState.CANCELLED_ENTRY,
        BotState.FAILED_DEPLOY,
    }),
    BotState.IN_POSITION: frozenset({
        BotState.PLACING_EXIT,
        BotState.MANUAL_CLOSED,
    }),
    BotState.PLACING_EXIT: frozenset({
        BotState.AWAITING_EXIT_FILL,
        BotState.IN_POSITION,  # rejected exit goes back
        BotState.MANUAL_CLOSED,
    }),
    BotState.AWAITING_EXIT_FILL: frozenset({
        BotState.CLOSED_WIN,
        BotState.CLOSED_LOSS,
        BotState.STALE_EXIT,
        BotState.MANUAL_CLOSED,
    }),
    BotState.STALE_EXIT: frozenset({
        BotState.PLACING_EXIT,           # supervisor / autorecovery reorders
        BotState.MANUAL_CLOSED,
        BotState.AWAITING_EXIT_FILL,
    }),
    # Terminal nodes (no outgoing edges):
    BotState.CLOSED_WIN: frozenset(),
    BotState.CLOSED_LOSS: frozenset(),
    BotState.CANCELLED_ENTRY: frozenset(),
    BotState.MANUAL_CLOSED: frozenset(),
    BotState.FAILED_OPTIMIZATION: frozenset(),
    BotState.FAILED_DEPLOY: frozenset(),
    BotState.BLACKLISTED: frozenset(),
}


def is_terminal(state: BotState) -> bool:
    return state in TERMINAL_STATES


def can_transition(current: BotState, target: BotState) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def transition(current: BotState, target: BotState, *, reason: Optional[str] = None) -> BotState:
    """Validate and return the target state (no side effects).

    Parameters
    ----------
    current: BotState
        The bot's current state.
    target: BotState
        Desired next state.
    reason: Optional[str]
        Diagnostic text for the error message and the audit log.

    Raises
    ------
    InvalidTransition
        If ``target`` is not reachable from ``current``.
    """
    if current == target:
        # Treat idempotent transitions as no-ops (safe for retries).
        return target
    if not can_transition(current, target):
        msg = (
            f"Invalid bot transition {current.value} -> {target.value}"
            + (f" (reason: {reason})" if reason else "")
        )
        raise InvalidTransition(msg)
    return target


def successors(current: BotState) -> frozenset[BotState]:
    """Return the set of allowed next states from ``current``."""
    return _ALLOWED.get(current, frozenset())
