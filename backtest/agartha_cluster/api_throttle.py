"""API throttle: enforce Binance weight + order budgets.

Two rolling windows tracked locally:
    - **Weight** per 1 minute (default cap 600 of 1200 = 50% safety).
    - **Orders** per 10 seconds (default cap 20 of 50).

The implementation is intentionally conservative: it relies on **local
accounting** before sending a request and updates the DB bucket after.
This catches almost all bursts; the actual server-reported headers
(``X-MBX-USED-WEIGHT-1M``) are used by :meth:`reconcile_server_weight`
to correct drift.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from backtest.agartha_cluster.cluster_db import ClusterDB


def _now_ms() -> int:
    return int(time.time() * 1000)


def _minute_bucket(ts_ms: int) -> int:
    return int(ts_ms // 60_000)


@dataclass
class ThrottleConfig:
    weight_limit_per_minute: int = 600       # 50% of 1200 default
    orders_limit_per_10s: int = 20           # well under 50
    block_when_exceeded: bool = True


class ApiThrottle:
    """Per-process rate limiter backed by ``api_throttle_buckets``."""

    def __init__(self, db: ClusterDB, config: Optional[ThrottleConfig] = None):
        self.db = db
        self.config = config or ThrottleConfig()
        # Rolling 10s window for orders (in-memory; persisted to DB for forensics).
        self._order_ts: list[int] = []

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------
    def record(self, *, weight: int, orders: int = 0, ts_ms: Optional[int] = None) -> tuple[int, int]:
        """Persist used weight/orders into the current minute bucket and
        update the in-memory order window. Returns the post-update tuple
        ``(weight_used_in_minute, orders_in_last_10s)``."""
        ts_ms = int(ts_ms if ts_ms is not None else _now_ms())
        bucket = _minute_bucket(ts_ms)
        weight_used, _ = self.db.bump_throttle(
            minute_bucket=bucket, weight=int(weight), orders=int(orders)
        )
        for _ in range(int(orders)):
            self._order_ts.append(ts_ms)
        cutoff = ts_ms - 10_000
        self._order_ts = [t for t in self._order_ts if t >= cutoff]
        return (weight_used, len(self._order_ts))

    def used(self, *, ts_ms: Optional[int] = None) -> tuple[int, int]:
        ts_ms = int(ts_ms if ts_ms is not None else _now_ms())
        bucket = _minute_bucket(ts_ms)
        weight_used, _ = self.db.get_throttle_bucket(bucket)
        cutoff = ts_ms - 10_000
        order_count = sum(1 for t in self._order_ts if t >= cutoff)
        return (weight_used, order_count)

    def can_send(self, *, weight: int, orders: int = 0, ts_ms: Optional[int] = None) -> bool:
        weight_used, order_count = self.used(ts_ms=ts_ms)
        if weight_used + weight > self.config.weight_limit_per_minute:
            return False
        if order_count + orders > self.config.orders_limit_per_10s:
            return False
        return True

    # ------------------------------------------------------------------
    # Blocking waits (sync + async)
    # ------------------------------------------------------------------
    def wait_for_budget(
        self,
        *,
        weight: int,
        orders: int = 0,
        poll_seconds: float = 0.25,
        max_wait_seconds: float = 120.0,
    ) -> float:
        """Block until budget is available (or raise after ``max_wait_seconds``).

        Returns the actual time waited (seconds).
        """
        start = time.time()
        while True:
            if self.can_send(weight=weight, orders=orders):
                return time.time() - start
            if time.time() - start > max_wait_seconds:
                raise TimeoutError(
                    f"ApiThrottle wait exceeded {max_wait_seconds}s"
                    f" (weight={weight}, orders={orders})"
                )
            time.sleep(poll_seconds)

    async def await_budget(
        self,
        *,
        weight: int,
        orders: int = 0,
        poll_seconds: float = 0.25,
        max_wait_seconds: float = 120.0,
    ) -> float:
        start = time.time()
        while True:
            if self.can_send(weight=weight, orders=orders):
                return time.time() - start
            if time.time() - start > max_wait_seconds:
                raise TimeoutError(
                    f"ApiThrottle await exceeded {max_wait_seconds}s"
                )
            await asyncio.sleep(poll_seconds)

    # ------------------------------------------------------------------
    # Server reconciliation
    # ------------------------------------------------------------------
    def reconcile_server_weight(self, *, used_weight_1m: int, ts_ms: Optional[int] = None) -> None:
        """Snap the local minute bucket to the server's reported usage.

        Useful right after every signed REST response (header
        ``X-MBX-USED-WEIGHT-1M``).
        """
        ts_ms = int(ts_ms if ts_ms is not None else _now_ms())
        bucket = _minute_bucket(ts_ms)
        weight_used, orders_count = self.db.get_throttle_bucket(bucket)
        diff = int(used_weight_1m) - int(weight_used)
        if diff > 0:
            self.db.bump_throttle(minute_bucket=bucket, weight=diff, orders=0)
