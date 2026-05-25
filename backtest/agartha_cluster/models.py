"""Models, enums and dataclasses for the Agartha cluster.

Kept pure (no I/O, no DB) so they can be shared across the DAO, the state
machine, the live client and the tests without circular deps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BotState(str, Enum):
    """Lifecycle states of a single Agartha bot instance.

    Transitions are validated by ``state_machine.transition``.
    """

    CREATED = "created"
    OPTIMIZING = "optimizing"
    OPTIMIZED = "optimized"
    QUEUED = "queued"
    PLACING_ENTRY = "placing_entry"
    AWAITING_ENTRY_FILL = "awaiting_entry_fill"
    IN_POSITION = "in_position"
    PLACING_EXIT = "placing_exit"
    AWAITING_EXIT_FILL = "awaiting_exit_fill"
    STALE_EXIT = "stale_exit"
    CLOSED_WIN = "closed_win"
    CLOSED_LOSS = "closed_loss"
    CANCELLED_ENTRY = "cancelled_entry"
    MANUAL_CLOSED = "manual_closed"
    FAILED_OPTIMIZATION = "failed_optimization"
    FAILED_DEPLOY = "failed_deploy"
    BLACKLISTED = "blacklisted"


TERMINAL_STATES: frozenset[BotState] = frozenset(
    {
        BotState.CLOSED_WIN,
        BotState.CLOSED_LOSS,
        BotState.CANCELLED_ENTRY,
        BotState.MANUAL_CLOSED,
        BotState.FAILED_OPTIMIZATION,
        BotState.FAILED_DEPLOY,
        BotState.BLACKLISTED,
    }
)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"


class OrderState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"


class EventSource(str, Enum):
    SERVICE = "service"
    BINANCE_REST = "binance_rest"
    BINANCE_WS = "binance_ws"
    SUPERVISOR = "supervisor"
    SCHEDULER = "scheduler"
    OPTIMIZER = "optimizer"
    RECONCILER = "reconciler"


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventKind(str, Enum):
    SERVICE_START = "service_start"
    SERVICE_STOP = "service_stop"

    UNIVERSE_REFRESHED = "universe_refreshed"
    SYMBOL_SCHEDULED = "symbol_scheduled"
    SYMBOL_BLACKLISTED = "symbol_blacklisted"

    OPTIMIZATION_STARTED = "optimization_started"
    OPTIMIZATION_COMPLETED = "optimization_completed"
    OPTIMIZATION_FAILED = "optimization_failed"

    BOT_CREATED = "bot_created"
    BOT_STATE_CHANGED = "bot_state_changed"
    BOT_DEPLOYED = "bot_deployed"

    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    ORDER_EXPIRED = "order_expired"
    ORDER_STALE = "order_stale"
    EXIT_REORDER = "exit_reorder"
    EXIT_BORDER = "exit_border"
    EXIT_OUT_OF_BAND = "exit_out_of_band"
    NEEDS_MANUAL_ACTION = "needs_manual_action"
    MANUAL_CLOSE = "manual_close"

    API_CALL = "api_call"
    API_RATE_LIMIT = "api_rate_limit"
    API_ERROR = "api_error"
    API_THROTTLE_WAIT = "api_throttle_wait"

    WS_CONNECTED = "ws_connected"
    WS_DISCONNECTED = "ws_disconnected"
    WS_RECONNECTED = "ws_reconnected"

    RECONCILIATION_OK = "reconciliation_ok"
    RECONCILIATION_DRIFT = "reconciliation_drift"
    HEALTH_CHECK = "health_check"

    SERVICE_RECOVERY_STARTED = "service_recovery_started"
    SERVICE_RECOVERY_COMPLETED = "service_recovery_completed"
    SERVICE_PREVIOUS_CRASH_DETECTED = "service_previous_crash_detected"
    ORDER_REQUERIED = "order_requeried"
    FILL_REPLAYED = "fill_replayed"


@dataclass(frozen=True)
class SymbolParams:
    """Best parameters produced by the per-symbol Optuna study."""

    symbol: str
    trailing_stop_pct: float
    activation_profit_pct: float
    breakeven_lock_pct: float
    entry_limit_offset_pct: float = 0.0
    partial_tp_pct: float = 0.0
    partial_tp_size_pct: float = 0.0
    max_holding_bars: int = 0
    study_equity_pct: Optional[float] = None
    study_max_dd_pct: Optional[float] = None
    study_trial_id: Optional[str] = None
    optimized_at: Optional[str] = None
    optuna_db_path: Optional[str] = None

    def as_runtime_dict(self) -> dict[str, Any]:
        return {
            "trailing_stop_pct": float(self.trailing_stop_pct),
            "activation_profit_pct": float(self.activation_profit_pct),
            "breakeven_lock_pct": float(self.breakeven_lock_pct),
            "entry_limit_offset_pct": float(self.entry_limit_offset_pct),
            "partial_tp_pct": float(self.partial_tp_pct),
            "partial_tp_size_pct": float(self.partial_tp_size_pct),
            "max_holding_bars": int(self.max_holding_bars),
        }


@dataclass(frozen=True)
class SymbolFilters:
    """Cached exchange filters per symbol (refreshed via REST)."""

    symbol: str
    tick_size: float = 1e-8
    step_size: float = 1e-8
    min_notional: float = 0.1
    bid_multiplier_up: float = 5.0
    bid_multiplier_down: float = 0.2
    ask_multiplier_up: float = 5.0
    ask_multiplier_down: float = 0.2
    refreshed_at: Optional[str] = None


@dataclass
class BotRecord:
    """In-memory view of a row of ``cluster_bots``."""

    bot_id: int
    symbol: str
    state: BotState
    capital_usdt: float
    params_snapshot_json: str
    correlation_id: str
    entry_order_id: Optional[str] = None
    entry_client_order_id: Optional[str] = None
    entry_price: Optional[float] = None
    entry_qty: Optional[float] = None
    entry_filled_ts: Optional[int] = None
    peak_price: Optional[float] = None
    trail_floor: Optional[float] = None
    exit_order_id: Optional[str] = None
    exit_client_order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_qty: Optional[float] = None
    exit_filled_ts: Optional[int] = None
    realized_pnl_usdt: Optional[float] = None
    deployed_at: Optional[str] = None
    closed_at: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class OrderRecord:
    order_id: Optional[str]
    client_order_id: str
    bot_id: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    state: OrderState
    price: float
    qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    submitted_ts: Optional[int] = None
    last_update_ts: Optional[int] = None
    correlation_id: str = ""
    raw_response: Optional[str] = None


@dataclass
class Event:
    ts_ms: int
    source: EventSource
    level: EventLevel
    kind: EventKind
    bot_id: Optional[int] = None
    symbol: Optional[str] = None
    correlation_id: Optional[str] = None
    payload: dict = field(default_factory=dict)
