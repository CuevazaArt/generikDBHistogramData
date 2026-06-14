"""Deployment scheduler.

Rules:
    1. At most **one bot deployed every ``slot_seconds``** (default 600 = 10 min).
    2. FIFO over ``deploy_queue`` ordered by ``(priority, planned_deploy_ts, queue_id)``.
    3. Throttle-aware: if API budget cannot place an entry order, defer.

The scheduler is purely a **planner**. It hands off to the
:class:`BotRunner` (caller) the queue item that is due. The runner is
responsible for actually placing the order and updating bot state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from backtest.agartha_cluster.api_throttle import ApiThrottle
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.models import EventKind, EventSource


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class SchedulerConfig:
    slot_seconds: int = 600                  # 10 min between deployments
    entry_weight_cost: int = 2               # signed REST place + book check
    entry_orders_cost: int = 1
    enqueue_spacing_seconds: int | None = None  # defaults to ``slot_seconds``


class DeployScheduler:
    """Decide whether and which queue item is due."""

    def __init__(
        self,
        db: ClusterDB,
        throttle: ApiThrottle,
        events: EventLogger,
        config: SchedulerConfig | None = None,
    ):
        self.db = db
        self.throttle = throttle
        self.events = events
        self.config = config or SchedulerConfig()

    # ------------------------------------------------------------------
    # Bulk planning
    # ------------------------------------------------------------------
    def enqueue_symbols(
        self,
        symbols: list[str],
        *,
        start_ts_ms: int | None = None,
        priority: int = 100,
    ) -> list[int]:
        """Plan deployments spaced by ``slot_seconds`` starting at ``start_ts_ms``
        (or now+slot if omitted). Returns the queue_ids created.

        Symbols already in an active queue entry are silently skipped (the
        unique partial index on ``deploy_queue`` enforces this).
        """
        spacing_s = (
            self.config.enqueue_spacing_seconds
            if self.config.enqueue_spacing_seconds is not None
            else self.config.slot_seconds
        )
        spacing = max(int(spacing_s), 1) * 1000
        start = int(start_ts_ms if start_ts_ms is not None else _now_ms())
        created: list[int] = []
        for i, symbol in enumerate(symbols):
            ts = start + i * spacing
            qid = self.db.enqueue_deploy(
                symbol=symbol,
                planned_deploy_ts=ts,
                priority=priority,
                reason="bulk_enqueue",
            )
            if qid is not None:
                created.append(qid)
                self.events.info(
                    kind=EventKind.SYMBOL_SCHEDULED,
                    source=EventSource.SCHEDULER,
                    symbol=symbol,
                    payload={"queue_id": qid, "planned_deploy_ts": ts, "priority": priority},
                )
        return created

    # ------------------------------------------------------------------
    # Per-tick decision
    # ------------------------------------------------------------------
    def slot_open(self, *, now_ms: int | None = None) -> bool:
        """True if at least ``slot_seconds`` have passed since the last deploy."""
        last = self.db.last_deploy_ts()
        if last is None:
            return True
        now_ms = int(now_ms if now_ms is not None else _now_ms())
        return (now_ms - last) >= self.config.slot_seconds * 1000

    def next_deploy_in_seconds(self, *, now_ms: int | None = None) -> int:
        """Seconds until the next slot opens. 0 if open now."""
        last = self.db.last_deploy_ts()
        if last is None:
            return 0
        now_ms = int(now_ms if now_ms is not None else _now_ms())
        elapsed = (now_ms - last) // 1000
        wait = self.config.slot_seconds - int(elapsed)
        return max(0, wait)

    def pick_next(self, *, now_ms: int | None = None):
        """Return a queue Row when ready, else None.

        Three gating checks (in order):
            1. Slot interval respected.
            2. API throttle budget available for an entry.
            3. There is a due queue item.
        """
        now_ms = int(now_ms if now_ms is not None else _now_ms())
        if not self.slot_open(now_ms=now_ms):
            return None
        if not self.throttle.can_send(
            weight=self.config.entry_weight_cost,
            orders=self.config.entry_orders_cost,
            ts_ms=now_ms,
        ):
            self.events.warn(
                kind=EventKind.API_THROTTLE_WAIT,
                source=EventSource.SCHEDULER,
                payload={"reason": "no_budget_for_entry"},
            )
            return None
        row = self.db.next_due_deploy(now_ms=now_ms)
        return row
