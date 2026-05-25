"""Live client interface + Stub.

The **interface** ``LiveClient`` is what the bot runner depends on. The
``StubLiveClient`` is a deterministic, no-network implementation used for
dry-runs, tests and demos; it simulates fills using a price-walk.

The real Binance Alpha client lives in ``BinanceAlphaClient`` and is
intentionally **not implemented yet**: every operation raises
``NotImplementedError`` with a precise pointer to which endpoint must be
wired. When the operator runs ``cli live up`` for the first time and
provides credentials, the next step is to fill in those endpoints.
"""
from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from backtest.agartha_cluster.models import OrderSide, OrderType


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_client_order_id(prefix: str = "agc") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:18]}"


@dataclass
class PlaceOrderResult:
    """Standardised response shape across all backends."""

    accepted: bool
    order_id: Optional[str]
    client_order_id: str
    status: str                        # 'NEW' | 'FILLED' | 'REJECTED' | ...
    avg_fill_price: float = 0.0
    filled_qty: float = 0.0
    raw_response: Optional[str] = None
    error: Optional[str] = None
    weight_used: int = 0
    latency_ms: int = 0


@dataclass
class AccountSnapshot:
    timestamp_ms: int
    balances: dict[str, float]
    open_orders: list[dict]
    raw: Optional[str] = None


class LiveClient(Protocol):
    """Contract used by :class:`BotRunner`. Sync version.

    All methods raise on any unrecoverable error; the runner decides
    whether to retry, backoff or escalate.
    """

    def get_filters(self, symbol: str) -> dict: ...
    def get_price(self, symbol: str) -> float: ...
    def place_limit(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        client_order_id: str,
    ) -> PlaceOrderResult: ...
    def cancel_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult: ...
    def query_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult: ...
    def get_account(self) -> AccountSnapshot: ...


# =============================================================
# Stub backend (dry-run, tests)
# =============================================================


@dataclass
class StubMarketState:
    price: float
    trend: float = 0.0           # drift per tick
    volatility: float = 0.005    # stddev per tick


class StubLiveClient:
    """In-memory implementation that simulates a Binance Alpha-like pair.

    - ``place_limit`` returns immediately with status ``NEW``.
    - ``query_order`` walks the price and stochastically fills when the
      price crosses the limit (BUY fills if last <= price; SELL fills if
      last >= price).
    - ``get_account`` returns a synthetic snapshot built from prior
      fills (in-process bookkeeping only).

    Useful for end-to-end smoke tests of the scheduler, throttle and
    event log without any network or credentials.
    """

    def __init__(
        self,
        *,
        seed: Optional[int] = None,
        markets: Optional[dict[str, StubMarketState]] = None,
        initial_quote_balance: float = 1000.0,
        auto_configure: bool = True,
        default_price: float = 1.0,
        default_volatility: float = 0.0,
    ):
        self._rng = random.Random(seed)
        self._markets: dict[str, StubMarketState] = dict(markets or {})
        self._orders: dict[str, dict] = {}
        self._fills: list[dict] = []
        self._balances: dict[str, float] = {"USDT": float(initial_quote_balance)}
        self._filters: dict[str, dict] = {}
        self._auto_configure = bool(auto_configure)
        self._default_price = float(default_price)
        self._default_volatility = float(default_volatility)

    def _ensure_market(self, symbol: str) -> StubMarketState:
        sym = symbol.upper()
        if sym not in self._markets and self._auto_configure:
            self._markets[sym] = StubMarketState(
                price=self._default_price,
                trend=0.0,
                volatility=self._default_volatility,
            )
        return self._markets.get(sym, StubMarketState(price=0.0))

    def configure_market(
        self,
        symbol: str,
        *,
        price: float,
        trend: float = 0.0,
        volatility: float = 0.005,
        filters: Optional[dict] = None,
    ) -> None:
        self._markets[symbol.upper()] = StubMarketState(
            price=float(price), trend=float(trend), volatility=float(volatility)
        )
        if filters:
            self._filters[symbol.upper()] = filters

    def _tick(self, symbol: str) -> float:
        m = self._ensure_market(symbol)
        if m.price <= 0:
            return 0.0
        shock = self._rng.gauss(m.trend, m.volatility)
        m.price = max(1e-12, m.price * (1.0 + shock))
        return m.price

    # ------------------------------------------------------------------
    # LiveClient impl
    # ------------------------------------------------------------------
    def get_filters(self, symbol: str) -> dict:
        return dict(
            self._filters.get(
                symbol.upper(),
                {
                    "tick_size": 1e-6,
                    "step_size": 1e-2,
                    "min_notional": 0.1,
                    "bid_multiplier_up": 5.0,
                    "bid_multiplier_down": 0.2,
                    "ask_multiplier_up": 5.0,
                    "ask_multiplier_down": 0.2,
                },
            )
        )

    def get_price(self, symbol: str) -> float:
        m = self._ensure_market(symbol)
        return float(m.price)

    def place_limit(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        client_order_id: str,
    ) -> PlaceOrderResult:
        order_id = f"stub-{uuid.uuid4().hex[:12]}"
        self._orders[client_order_id] = {
            "order_id": order_id,
            "symbol": symbol.upper(),
            "side": side.value,
            "price": float(price),
            "qty": float(qty),
            "filled_qty": 0.0,
            "avg_fill_price": 0.0,
            "status": "NEW",
            "ts": _now_ms(),
        }
        return PlaceOrderResult(
            accepted=True,
            order_id=order_id,
            client_order_id=client_order_id,
            status="NEW",
            raw_response="{stub: placed}",
            weight_used=1,
            latency_ms=1,
        )

    def cancel_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult:
        o = self._orders.get(client_order_id)
        if o is None:
            return PlaceOrderResult(
                accepted=False,
                order_id=None,
                client_order_id=client_order_id,
                status="UNKNOWN",
                error="order_not_found",
                weight_used=1,
            )
        o["status"] = "CANCELED"
        return PlaceOrderResult(
            accepted=True,
            order_id=o.get("order_id"),
            client_order_id=client_order_id,
            status="CANCELED",
            weight_used=1,
        )

    def query_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult:
        o = self._orders.get(client_order_id)
        if o is None:
            return PlaceOrderResult(
                accepted=False,
                order_id=None,
                client_order_id=client_order_id,
                status="UNKNOWN",
                error="not_found",
            )
        if o["status"] not in {"NEW", "PARTIALLY_FILLED"}:
            return PlaceOrderResult(
                accepted=True,
                order_id=o["order_id"],
                client_order_id=client_order_id,
                status=o["status"],
                avg_fill_price=o["avg_fill_price"],
                filled_qty=o["filled_qty"],
                weight_used=1,
            )
        current = self._tick(symbol)
        if o["side"] == OrderSide.BUY.value and current <= o["price"]:
            self._fill_full(client_order_id, current, OrderSide.BUY)
        elif o["side"] == OrderSide.SELL.value and current >= o["price"]:
            self._fill_full(client_order_id, current, OrderSide.SELL)
        return PlaceOrderResult(
            accepted=True,
            order_id=o["order_id"],
            client_order_id=client_order_id,
            status=o["status"],
            avg_fill_price=o["avg_fill_price"],
            filled_qty=o["filled_qty"],
            weight_used=1,
        )

    def _fill_full(self, client_order_id: str, price: float, side: OrderSide) -> None:
        o = self._orders[client_order_id]
        o["filled_qty"] = o["qty"]
        o["avg_fill_price"] = float(price)
        o["status"] = "FILLED"
        self._fills.append(
            {
                "ts_ms": _now_ms(),
                "symbol": o["symbol"],
                "side": side.value,
                "price": float(price),
                "qty": o["qty"],
                "client_order_id": client_order_id,
                "order_id": o["order_id"],
            }
        )
        notional = float(price) * o["qty"]
        base = o["symbol"].replace("USDT", "").replace("USDC", "") or "BASE"
        quote = "USDT" if o["symbol"].endswith("USDT") else "USDC"
        if side == OrderSide.BUY:
            self._balances[quote] = self._balances.get(quote, 0.0) - notional
            self._balances[base] = self._balances.get(base, 0.0) + o["qty"]
        else:
            self._balances[quote] = self._balances.get(quote, 0.0) + notional
            self._balances[base] = self._balances.get(base, 0.0) - o["qty"]

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            timestamp_ms=_now_ms(),
            balances=dict(self._balances),
            open_orders=[
                {
                    "client_order_id": cid,
                    **{k: v for k, v in o.items() if k != "raw"},
                }
                for cid, o in self._orders.items()
                if o["status"] in {"NEW", "PARTIALLY_FILLED"}
            ],
        )

    # ------------------------------------------------------------------
    # Test helpers (StubLiveClient only)
    # ------------------------------------------------------------------
    def force_fill(self, client_order_id: str) -> None:
        o = self._orders[client_order_id]
        side = OrderSide(o["side"])
        price = o["price"]
        self._fill_full(client_order_id, price, side)

    def list_fills(self) -> list[dict]:
        return list(self._fills)


# =============================================================
# Real Binance Alpha client (stub: requires credentials)
# =============================================================


class BinanceAlphaClient:
    """Real REST + WS client. **Not implemented** until live credentials.

    To wire this in (after ``cli live up`` provides credentials):

    1. ``get_filters``: REST ``GET /api/v3/exchangeInfo`` filtered by
       ``symbol`` (Alpha shares the spot endpoint). Cache 1 day.
    2. ``get_price``: REST ``GET /api/v3/ticker/price`` (weight 1).
    3. ``place_limit``: signed REST ``POST /api/v3/order`` with
       ``type=LIMIT``, ``timeInForce=GTC``, ``newClientOrderId``.
    4. ``cancel_order``: signed REST ``DELETE /api/v3/order``.
    5. ``query_order``: signed REST ``GET /api/v3/order``.
    6. ``get_account``: signed REST ``GET /api/v3/account``.
    7. WS ``userDataStream`` (``listenKey`` flow) for executionReport
       events; pushes go through :class:`event_logger.EventLogger`.

    All signed requests must update :class:`ApiThrottle` with the
    ``X-MBX-USED-WEIGHT-1M`` header from the response.
    """

    def __init__(self, *, api_key: str, api_secret: str, base_url: str = "https://api.binance.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def _not_yet(self, endpoint: str):  # noqa: ANN001
        raise NotImplementedError(
            f"BinanceAlphaClient.{endpoint} not wired yet. "
            "See docs/AGARTHA_CLUSTER.md section 'Pendientes hasta puesta en producción'."
        )

    def get_filters(self, symbol: str) -> dict:
        self._not_yet("get_filters")

    def get_price(self, symbol: str) -> float:
        self._not_yet("get_price")

    def place_limit(self, **kw):  # noqa: ANN003
        self._not_yet("place_limit")

    def cancel_order(self, **kw):  # noqa: ANN003
        self._not_yet("cancel_order")

    def query_order(self, **kw):  # noqa: ANN003
        self._not_yet("query_order")

    def get_account(self) -> AccountSnapshot:
        self._not_yet("get_account")
