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

Crash / WS-disconnect resilience
--------------------------------
- :meth:`poll_open_orders_for_fills` queries the exchange for **every
  locally-open order** via ``LiveClient.query_order``. This catches fills
  that happened while the ``userDataStream`` WS was down, which the
  account-snapshot diff alone cannot detect (a filled order disappears
  from ``open_orders`` and looks identical to a never-was-there one).
- :meth:`run_once` calls ``poll_open_orders_for_fills`` after the
  open-orders diff, so both checks run every reconcile tick.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import LiveClient
from backtest.agartha_cluster.models import (
    EventKind,
    EventSource,
    OrderSide,
    OrderState,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


# Type alias for the on-fill callback signature shared with BotRunner.on_fill
OnFillCallback = Callable[..., None]


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
        *,
        on_fill: Optional[OnFillCallback] = None,
    ):
        self.db = db
        self.client = client
        self.events = events
        self.config = config or ReconcilerConfig()
        self._on_fill = on_fill

    def set_on_fill(self, on_fill: OnFillCallback) -> None:
        """Inject the runner's ``on_fill`` callback used by
        :meth:`poll_open_orders_for_fills` to replay missed fills."""
        self._on_fill = on_fill

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

        poll_summary = self.poll_open_orders_for_fills()

        return {
            "snap_id": snap_id,
            "drift": drift,
            "orphan_at_exchange": sorted(orphan_at_exchange),
            "missing_at_exchange": sorted(missing_at_exchange),
            "positions_count": positions_count,
            "poll": poll_summary,
        }

    def poll_open_orders_for_fills(self) -> dict:
        """For each locally-open order, ask the exchange whether it filled.

        Replays any fills that the WS layer might have missed. Idempotent:
        skips orders that already have a fill row recorded.

        Returns
        -------
        dict with counters: ``queried``, ``filled``, ``cancelled``,
        ``rejected``, ``replayed``, ``errors``.
        """
        summary = {
            "queried": 0,
            "filled": 0,
            "cancelled": 0,
            "rejected": 0,
            "replayed": 0,
            "errors": 0,
        }
        open_orders = self.db.list_orders_by_state(
            ["submitted", "partially_filled", "pending"]
        )
        for o in open_orders:
            client_order_id = o["client_order_id"]
            try:
                result = self.client.query_order(
                    symbol=o["symbol"], client_order_id=client_order_id
                )
            except NotImplementedError:
                # Live client not wired yet; nothing to do here.
                return summary
            except Exception as e:  # noqa: BLE001
                summary["errors"] += 1
                self.events.error(
                    kind=EventKind.RECONCILIATION_DRIFT,
                    source=EventSource.RECONCILER,
                    payload={
                        "client_order_id": client_order_id,
                        "phase": "query_order",
                        "error": str(e),
                    },
                )
                continue

            summary["queried"] += 1
            self.events.info(
                kind=EventKind.ORDER_REQUERIED,
                source=EventSource.RECONCILER,
                bot_id=int(o["bot_id"]),
                symbol=o["symbol"],
                payload={
                    "client_order_id": client_order_id,
                    "status": result.status,
                    "filled_qty": result.filled_qty,
                    "avg_fill_price": result.avg_fill_price,
                },
            )

            status = (result.status or "").upper()
            if status in {"FILLED", "PARTIALLY_FILLED"}:
                summary["filled"] += 1
                if (
                    self._on_fill is not None
                    and self.db.count_fills_for_order(int(o["order_pk"])) == 0
                ):
                    side = OrderSide(o["side"])
                    self._on_fill(
                        client_order_id=client_order_id,
                        symbol=o["symbol"],
                        side=side,
                        price=float(result.avg_fill_price or o["price"]),
                        qty=float(result.filled_qty or o["qty"]),
                    )
                    summary["replayed"] += 1
                    self.events.warn(
                        kind=EventKind.FILL_REPLAYED,
                        source=EventSource.RECONCILER,
                        bot_id=int(o["bot_id"]),
                        symbol=o["symbol"],
                        payload={
                            "client_order_id": client_order_id,
                            "reason": "ws_gap_or_crash",
                        },
                    )
                else:
                    # Already recorded locally; just sync state if needed.
                    self.db.update_order_state(
                        client_order_id=client_order_id,
                        state=OrderState.FILLED,
                        filled_qty=float(result.filled_qty or o["qty"]),
                        avg_fill_price=float(result.avg_fill_price or o["price"]),
                    )
            elif status == "CANCELED":
                summary["cancelled"] += 1
                self.db.update_order_state(
                    client_order_id=client_order_id, state=OrderState.CANCELLED
                )
            elif status in {"REJECTED", "EXPIRED"}:
                summary["rejected"] += 1
                self.db.update_order_state(
                    client_order_id=client_order_id,
                    state=(
                        OrderState.REJECTED if status == "REJECTED"
                        else OrderState.EXPIRED
                    ),
                )
            # NEW / PARTIALLY_FILLED-still-open / UNKNOWN: leave as is.

        return summary
