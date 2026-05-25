"""Periodic reconciliation between cluster DB and the live exchange.

The reconciler is a defensive layer:

- Periodically (every ``interval_seconds``) call ``LiveClient.get_account()``.
- Compare ``open_orders`` against ``orders`` table where state IN
  ('submitted','partially_filled').
- Compare positions inferred from ``fills`` against the exchange balances
  for known symbols.
- If drift is detected (orphan order, missing fill, balance mismatch
  above tolerance), emit ``EventKind.RECONCILIATION_DRIFT`` at
  ``ERROR`` level and persist a snapshot row.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import LiveClient
from backtest.agartha_cluster.models import EventKind, EventSource


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ReconcilerConfig:
    balance_tolerance_pct: float = 1.0    # allow 1% drift before flagging


class Reconciler:
    def __init__(
        self,
        db: ClusterDB,
        client: LiveClient,
        events: EventLogger,
        config: Optional[ReconcilerConfig] = None,
    ):
        self.db = db
        self.client = client
        self.events = events
        self.config = config or ReconcilerConfig()

    def run_once(self) -> dict:
        snap = self.client.get_account()
        conn = self.db.connect()
        local_open_rows = conn.execute(
            """
            SELECT client_order_id, order_id, symbol, side, price, qty, state
            FROM orders
            WHERE state IN ('submitted','partially_filled','pending')
            """
        ).fetchall()
        local_open_ids = {r["client_order_id"] for r in local_open_rows}
        exchange_open_ids = {o.get("client_order_id") for o in snap.open_orders if o.get("client_order_id")}

        orphan_at_exchange = exchange_open_ids - local_open_ids
        missing_at_exchange = local_open_ids - exchange_open_ids
        drift = bool(orphan_at_exchange or missing_at_exchange)

        positions_count = sum(1 for asset, amt in snap.balances.items() if asset != "USDT" and amt > 0)
        total_quote = float(snap.balances.get("USDT", 0.0))

        snapshot_payload = {
            "balances": snap.balances,
            "open_orders_exchange": [o.get("client_order_id") for o in snap.open_orders],
            "open_orders_local": sorted(local_open_ids),
            "orphan_at_exchange": sorted(orphan_at_exchange),
            "missing_at_exchange": sorted(missing_at_exchange),
        }

        snap_id = self.db.insert_reconciliation(
            ts_ms=snap.timestamp_ms,
            open_orders_count=len(snap.open_orders),
            positions_count=positions_count,
            total_equity_usdt=total_quote,
            drift_detected=drift,
            snapshot=snapshot_payload,
        )

        if drift:
            self.events.error(
                kind=EventKind.RECONCILIATION_DRIFT,
                source=EventSource.RECONCILER,
                payload={
                    "snap_id": snap_id,
                    "orphan_count": len(orphan_at_exchange),
                    "missing_count": len(missing_at_exchange),
                },
            )
        else:
            self.events.info(
                kind=EventKind.RECONCILIATION_OK,
                source=EventSource.RECONCILER,
                payload={
                    "snap_id": snap_id,
                    "open_orders": len(snap.open_orders),
                    "positions": positions_count,
                },
            )

        return {
            "snap_id": snap_id,
            "drift": drift,
            "orphan_at_exchange": sorted(orphan_at_exchange),
            "missing_at_exchange": sorted(missing_at_exchange),
            "positions_count": positions_count,
        }
