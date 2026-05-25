"""Agartha cluster: live deployment + supervision of N Agartha bots.

Public surface (stable):
    - ClusterDB          (DAO over SQLite WAL)
    - BotStateMachine    (state transitions)
    - ApiThrottle        (rate limit guard)
    - DeployScheduler    (1 bot every N minutes, FIFO)
    - EventLogger        (DB + JSONL structured events)
    - Credentials        (OS keyring; prompted on `live up`)
    - LiveClient         (interface) / StubLiveClient (no network)
    - BotRunner          (single bot lifecycle async)
    - ClusterService     (main loop)

Design doc: docs/AGARTHA_CLUSTER.md
"""
from backtest.agartha_cluster.models import (
    BotState,
    EventKind,
    EventLevel,
    EventSource,
    OrderSide,
    OrderState,
    OrderType,
)

__all__ = [
    "BotState",
    "EventKind",
    "EventLevel",
    "EventSource",
    "OrderSide",
    "OrderState",
    "OrderType",
]
