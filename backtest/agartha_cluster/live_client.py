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
import sys
import time
import uuid
import hmac
import hashlib
import urllib.parse
import json
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

import requests
import websockets

from backtest.agartha_cluster.models import OrderSide, OrderType

# Retriable HTTP status codes for _rest_with_retry
_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


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
    def start_user_data_stream(self, on_fill: Callable[..., None]) -> None: ...
    def stop_user_data_stream(self) -> None: ...


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

    def start_user_data_stream(self, on_fill: Callable[..., None]) -> None:
        pass

    def stop_user_data_stream(self) -> None:
        pass

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
    """Real REST + WS client for Binance spot / Alpha trading."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.binance.com",
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.throttle = None
        self._on_fill_callback = None
        self._ws_running = False
        self._ws_thread = None
        self._ws_loop = None
        self._listen_key = None
        self._keepalive_task = None

    def set_throttle(self, throttle) -> None:
        self.throttle = throttle

    def _reconcile_weight(self, response: requests.Response) -> None:
        if self.throttle is not None:
            header = response.headers.get("X-MBX-USED-WEIGHT-1M")
            if header:
                try:
                    self.throttle.reconcile_server_weight(used_weight_1m=int(header))
                except Exception:
                    pass

    def _rest_with_retry(
        self,
        fn: Callable[[], requests.Response],
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 15.0,
        label: str = "REST",
    ) -> requests.Response:
        """Execute *fn* with retry + exponential backoff on transient errors.

        Retries on:
          - ``requests.Timeout``
          - ``requests.ConnectionError``
          - HTTP 429, 500, 502, 503, 504

        Does NOT retry on 400, 401, 403 (client/auth errors).
        Respects ``Retry-After`` header when present.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                r = fn()
                if r.status_code not in _RETRIABLE_STATUS_CODES:
                    return r
                # Retriable HTTP status — treat as transient.
                if attempt >= max_retries:
                    return r  # return the last response as-is
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), max_delay)
                    except ValueError:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                else:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                print(
                    f"[agartha][{label}] HTTP {r.status_code}, retry "
                    f"{attempt + 1}/{max_retries} in {delay:.1f}s",
                    file=sys.stderr, flush=True,
                )
                time.sleep(delay)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt >= max_retries:
                    raise
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(
                    f"[agartha][{label}] {type(e).__name__}, retry "
                    f"{attempt + 1}/{max_retries} in {delay:.1f}s",
                    file=sys.stderr, flush=True,
                )
                time.sleep(delay)
        # Should not reach here, but satisfy the type checker.
        raise last_exc  # type: ignore[misc]

    def _send_signed_request(
        self, method: str, path: str, params: dict
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        params = dict(params)
        if "timestamp" not in params:
            params["timestamp"] = int(time.time() * 1000)

        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature

        headers = {"X-MBX-APIKEY": self.api_key}

        if method.upper() == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        elif method.upper() == "POST":
            r = requests.post(url, data=params, headers=headers, timeout=10)
        elif method.upper() == "DELETE":
            r = requests.delete(url, params=params, headers=headers, timeout=10)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        self._reconcile_weight(r)
        return r

    def get_filters(self, symbol: str) -> dict:
        url = f"{self.base_url}/api/v3/exchangeInfo"
        r = self._rest_with_retry(
            lambda: requests.get(url, params={"symbol": symbol}, timeout=10),
            label="get_filters",
        )
        self._reconcile_weight(r)
        r.raise_for_status()
        data = r.json()
        sym_info = data["symbols"][0]
        filters = sym_info["filters"]

        tick_size = 1e-8
        step_size = 1e-8
        min_notional = 0.1
        bid_mu = 5.0
        bid_md = 0.2
        ask_mu = 5.0
        ask_md = 0.2

        for f in filters:
            ft = f["filterType"]
            if ft == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif ft == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif ft == "NOTIONAL":
                min_notional = float(f["minNotional"])
            elif ft == "PERCENT_PRICE_BY_SIDE":
                bid_mu = float(f["bidMultiplierUp"])
                bid_md = float(f["bidMultiplierDown"])
                ask_mu = float(f["askMultiplierUp"])
                ask_md = float(f["askMultiplierDown"])

        return {
            "tick_size": tick_size,
            "step_size": step_size,
            "min_notional": min_notional,
            "bid_multiplier_up": bid_mu,
            "bid_multiplier_down": bid_md,
            "ask_multiplier_up": ask_mu,
            "ask_multiplier_down": ask_md,
        }

    def get_price(self, symbol: str) -> float:
        url = f"{self.base_url}/api/v3/ticker/price"
        r = self._rest_with_retry(
            lambda: requests.get(url, params={"symbol": symbol}, timeout=10),
            label="get_price",
        )
        self._reconcile_weight(r)
        r.raise_for_status()
        return float(r.json()["price"])

    def place_limit(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        client_order_id: str,
    ) -> PlaceOrderResult:
        params = {
            "symbol": symbol,
            "side": side.value,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
            "price": f"{price:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        start_time = time.time()
        try:
            r = self._send_signed_request("POST", "/api/v3/order", params)
            latency = int((time.time() - start_time) * 1000)
            if r.status_code == 200:
                data = r.json()
                exec_qty = float(data["executedQty"])
                cum_quote = float(data["cummulativeQuoteQty"])
                avg_price = cum_quote / exec_qty if exec_qty > 0 else 0.0
                return PlaceOrderResult(
                    accepted=True,
                    order_id=str(data["orderId"]),
                    client_order_id=data["clientOrderId"],
                    status=data["status"],
                    avg_fill_price=avg_price,
                    filled_qty=exec_qty,
                    raw_response=r.text,
                    latency_ms=latency,
                )
            else:
                try:
                    err_msg = r.json().get("msg") or r.text
                except Exception:
                    err_msg = r.text
                return PlaceOrderResult(
                    accepted=False,
                    order_id=None,
                    client_order_id=client_order_id,
                    status="REJECTED",
                    error=err_msg,
                    raw_response=r.text,
                    latency_ms=latency,
                )
        except Exception as e:
            latency = int((time.time() - start_time) * 1000)
            return PlaceOrderResult(
                accepted=False,
                order_id=None,
                client_order_id=client_order_id,
                status="REJECTED",
                error=str(e),
                latency_ms=latency,
            )

    def cancel_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult:
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
        }
        start_time = time.time()
        try:
            r = self._send_signed_request("DELETE", "/api/v3/order", params)
            latency = int((time.time() - start_time) * 1000)
            if r.status_code == 200:
                data = r.json()
                exec_qty = float(data.get("executedQty", 0.0))
                cum_quote = float(data.get("cummulativeQuoteQty", 0.0))
                avg_price = (
                    cum_quote / exec_qty if exec_qty > 0 else 0.0
                )
                return PlaceOrderResult(
                    accepted=True,
                    order_id=str(data["orderId"]),
                    client_order_id=data["origClientOrderId"],
                    status=data["status"],
                    avg_fill_price=avg_price,
                    filled_qty=exec_qty,
                    raw_response=r.text,
                    latency_ms=latency,
                )
            else:
                try:
                    err_msg = r.json().get("msg") or r.text
                except Exception:
                    err_msg = r.text
                return PlaceOrderResult(
                    accepted=False,
                    order_id=None,
                    client_order_id=client_order_id,
                    status="UNKNOWN",
                    error=err_msg,
                    raw_response=r.text,
                    latency_ms=latency,
                )
        except Exception as e:
            latency = int((time.time() - start_time) * 1000)
            return PlaceOrderResult(
                accepted=False,
                order_id=None,
                client_order_id=client_order_id,
                status="UNKNOWN",
                error=str(e),
                latency_ms=latency,
            )

    def query_order(self, *, symbol: str, client_order_id: str) -> PlaceOrderResult:
        params = {
            "symbol": symbol,
            "origClientOrderId": client_order_id,
        }
        start_time = time.time()
        try:
            r = self._rest_with_retry(
                lambda: self._send_signed_request("GET", "/api/v3/order", params),
                label="query_order",
            )
            latency = int((time.time() - start_time) * 1000)
            if r.status_code == 200:
                data = r.json()
                exec_qty = float(data.get("executedQty", 0.0))
                cum_quote = float(data.get("cummulativeQuoteQty", 0.0))
                avg_price = (
                    cum_quote / exec_qty if exec_qty > 0 else 0.0
                )
                return PlaceOrderResult(
                    accepted=True,
                    order_id=str(data["orderId"]),
                    client_order_id=data["clientOrderId"],
                    status=data["status"],
                    avg_fill_price=avg_price,
                    filled_qty=exec_qty,
                    raw_response=r.text,
                    latency_ms=latency,
                )
            else:
                try:
                    err_msg = r.json().get("msg") or r.text
                except Exception:
                    err_msg = r.text
                return PlaceOrderResult(
                    accepted=False,
                    order_id=None,
                    client_order_id=client_order_id,
                    status="UNKNOWN",
                    error=err_msg,
                    raw_response=r.text,
                    latency_ms=latency,
                )
        except Exception as e:
            latency = int((time.time() - start_time) * 1000)
            return PlaceOrderResult(
                accepted=False,
                order_id=None,
                client_order_id=client_order_id,
                status="UNKNOWN",
                error=str(e),
                latency_ms=latency,
            )

    def get_account(self) -> AccountSnapshot:
        try:
            r_acct = self._rest_with_retry(
                lambda: self._send_signed_request("GET", "/api/v3/account", {}),
                label="get_account",
            )
            r_acct.raise_for_status()
            acct_data = r_acct.json()

            balances = {}
            for b in acct_data.get("balances", []):
                free = float(b.get("free", 0.0))
                locked = float(b.get("locked", 0.0))
                if free > 0 or locked > 0:
                    balances[b["asset"]] = free + locked

            r_orders = self._send_signed_request("GET", "/api/v3/openOrders", {})
            r_orders.raise_for_status()
            orders_data = r_orders.json()

            open_orders = []
            for o in orders_data:
                open_orders.append(
                    {
                        "client_order_id": o["clientOrderId"],
                        "order_id": str(o["orderId"]),
                        "symbol": o["symbol"],
                        "side": o["side"],
                        "price": float(o["price"]),
                        "qty": float(o["origQty"]),
                        "status": o["status"],
                    }
                )

            return AccountSnapshot(
                timestamp_ms=acct_data.get("updateTime")
                or int(time.time() * 1000),
                balances=balances,
                open_orders=open_orders,
                raw=r_acct.text,
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to fetch live account snapshot: {e}"
            ) from e

    def start_user_data_stream(self, on_fill: Callable[..., None]) -> None:
        self._on_fill_callback = on_fill
        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._run_ws_loop, daemon=True
        )
        self._ws_thread.start()

    def stop_user_data_stream(self) -> None:
        self._ws_running = False
        if self._ws_loop and self._ws_loop.is_running():
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        if self._ws_thread:
            self._ws_thread.join(timeout=5.0)
            self._ws_thread = None
        if self._listen_key:
            try:
                headers = {"X-MBX-APIKEY": self.api_key}
                requests.delete(
                    f"{self.base_url}/api/v3/userDataStream",
                    params={"listenKey": self._listen_key},
                    headers=headers,
                    timeout=5,
                )
            except Exception:
                pass
            self._listen_key = None

    def _run_ws_loop(self) -> None:
        self._ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._ws_loop)
        self._ws_loop.run_until_complete(self._ws_listener_main())
        self._ws_loop.close()

    async def _ws_listener_main(self) -> None:
        try:
            headers = {"X-MBX-APIKEY": self.api_key}
            r = requests.post(
                f"{self.base_url}/api/v3/userDataStream",
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            self._listen_key = r.json()["listenKey"]
        except Exception as e:
            print(
                f"[agartha][WS] Failed to obtain listen key: {e}",
                file=sys.stderr, flush=True,
            )
            return

        self._keepalive_task = self._ws_loop.create_task(
            self._keepalive_loop()
        )

        ws_url = f"wss://stream.binance.com:9443/ws/{self._listen_key}"
        backoff = 5.0  # initial reconnect delay (seconds)
        _MAX_BACKOFF = 60.0
        _BASE_BACKOFF = 5.0
        while self._ws_running:
            try:
                async with websockets.connect(ws_url) as websocket:
                    backoff = _BASE_BACKOFF  # reset on successful connect
                    print(
                        "[agartha][WS] userDataStream connected",
                        file=sys.stderr, flush=True,
                    )
                    while self._ws_running:
                        msg_str = await websocket.recv()
                        msg = json.loads(msg_str)
                        self._handle_ws_message(msg)
            except websockets.exceptions.ConnectionClosed as e:
                if self._ws_running:
                    print(
                        f"[agartha][WS] ConnectionClosed ({e}), "
                        f"reconnecting in {backoff:.0f}s",
                        file=sys.stderr, flush=True,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
            except Exception as e:
                if self._ws_running:
                    print(
                        f"[agartha][WS] Error ({type(e).__name__}: {e}), "
                        f"reconnecting in {backoff:.0f}s",
                        file=sys.stderr, flush=True,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)

        if self._keepalive_task:
            self._keepalive_task.cancel()

    async def _keepalive_loop(self) -> None:
        headers = {"X-MBX-APIKEY": self.api_key}
        while self._ws_running:
            await asyncio.sleep(30 * 60)
            try:
                requests.put(
                    f"{self.base_url}/api/v3/userDataStream",
                    params={"listenKey": self._listen_key},
                    headers=headers,
                    timeout=10,
                )
            except Exception as e:
                print(
                    f"[agartha][WS] listen key keepalive failed: {e}",
                    file=sys.stderr, flush=True,
                )

    def _handle_ws_message(self, msg: dict) -> None:
        if msg.get("e") == "executionReport" and msg.get("x") == "TRADE":
            client_order_id = msg.get("c")
            symbol = msg.get("s")
            side_str = msg.get("S")
            try:
                side = OrderSide(side_str)
            except Exception:
                side = (
                    OrderSide.BUY
                    if side_str == "BUY"
                    else OrderSide.SELL
                )

            price = float(msg.get("L", 0.0))
            qty = float(msg.get("l", 0.0))
            fee = float(msg.get("n", 0.0))
            fee_asset = msg.get("N")
            ts_ms = int(msg.get("E", int(time.time() * 1000)))
            exchange_fill_id = str(msg.get("t", ""))

            if self._on_fill_callback:
                try:
                    self._on_fill_callback(
                        client_order_id=client_order_id,
                        symbol=symbol,
                        side=side,
                        price=price,
                        qty=qty,
                        fee=fee,
                        fee_asset=fee_asset,
                        ts_ms=ts_ms,
                        exchange_fill_id=exchange_fill_id,
                        raw_payload=json.dumps(msg),
                    )
                except Exception as e:
                    # CRITICAL: Never silently swallow a fill callback error.
                    # The reconciler's periodic poll will catch missed fills,
                    # but we must at least log the failure for forensics.
                    print(
                        f"[agartha][WS] FILL CALLBACK ERROR: {type(e).__name__}: {e} "
                        f"| order={client_order_id} symbol={symbol} side={side_str} "
                        f"price={price} qty={qty}",
                        file=sys.stderr, flush=True,
                    )
