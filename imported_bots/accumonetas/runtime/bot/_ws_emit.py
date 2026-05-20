"""Shared WS emission helpers for bot runtime events.

Bots call these after recording state in the DB to push real-time updates to
the console (Flutter UI subscribes to the broadcaster's event stream).
Failures here NEVER raise — broadcaster issues must not break trading logic.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

_LOG = logging.getLogger("pecunator.bot.ws_emit")


def publish_pnl_snapshot(
    bot_id: str,
    bot_type: str,
    epoch: dict[str, Any],
    current_price: Decimal,
    avg_entry_price: Decimal,
    total_committed_usdt: Decimal,
    unrealized_pnl_usdt: Decimal,
    unrealized_pnl_pct: Decimal,
    cumulative_realized_pnl_usdt: float,
) -> None:
    """Publish a PNL_SNAPSHOT event over the WS broadcaster.

    Topic: ``PNL_SNAPSHOT``. The console subscribes to this and updates the
    P&L time-series chart and dual-state header in real time without polling.
    """
    try:
        from runtime.core.ws_broadcaster import get_broadcaster
        bc = get_broadcaster()
        net_position = float(cumulative_realized_pnl_usdt) + float(unrealized_pnl_usdt)
        net_pct = (
            (net_position / float(total_committed_usdt) * 100.0)
            if total_committed_usdt > Decimal("0") else 0.0
        )
        bc.publish_sync("PNL_SNAPSHOT", {
            "bot_id": bot_id,
            "bot_type": bot_type,
            "epoch_id": epoch.get("epoch_id"),
            "snapshot_at": int(time.time()),
            "current_price": float(current_price),
            "avg_entry_price_usdt": float(avg_entry_price),
            "num_entries": int(epoch.get("num_purchases", 0)),
            "total_committed_usdt": float(total_committed_usdt),
            "unrealized_pnl_usdt": float(unrealized_pnl_usdt),
            "unrealized_pnl_pct": float(unrealized_pnl_pct),
            "cumulative_realized_pnl_usdt": float(cumulative_realized_pnl_usdt),
            "net_position_usdt": net_position,
            "net_position_pct": net_pct,
        })
    except Exception as e:
        _LOG.debug("WS publish failed (bot=%s): %s", bot_id, e)
