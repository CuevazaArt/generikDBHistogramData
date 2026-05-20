"""Elphaba — Anti-Dorothy bearish short strategy runner — LIVE mode only.

Inverse of Dorothy's dual-gate TrendSignal logic:
  Gate 1 (Trend, 2h):  HA MA(1,open) < MA(2,open) on 1h → BEARISH
  Gate 2 (Entry, 5min): price > regular 1h candle open → CLEAR
  Elphaba shorts ONLY when BEARISH + CLEAR.

Execution via Binance Isolated Margin at 1x leverage:
  - SELL MARKET (auto-borrow) to open short
  - BUY LIMIT with AUTO_REPAY to take profit
  - No stop-loss (L0 symmetric hub doctrine)
  - Safety exit: close all shorts when TrendSignal flips BULLISH

Capital: 21 USDT per symbol (3 rungs × 7 USDT). Liquidation at +81.8%.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from runtime.bot._base_runner import BaseStrategyRunner
from runtime.bot._decimal_utils import dec as _dec, quantize as _q
from runtime.connectors.binance_gateway import normalize_binance_spot_symbol

# Core dependencies (moved from inline for performance/cleanliness)
from runtime.core.api_fuse import get_api_fuse
from runtime.core.exception_zoo import get_exception_zoo
from runtime.core.exchange_filters import get_exchange_filters
from runtime.core.market_cache import get_market_cache
from runtime.core.order_ledger import get_order_ledger
from runtime.core.symmetry_guard import get_symmetry_guard
from runtime.modules.trend_signal import get_trend_signal_service


@dataclass
class ElphabaConfig:
    """Configuration for Elphaba short-selling bot."""
    preset_id: str = "E1"
    symbol: str = "XRPUSDT"
    loop_interval_sec: int = 225
    # L0: 7 USDT per rung — matches Dorothy for symmetric hedge
    quote_order_qty: Decimal = Decimal("7")
    # Take-profit: buy back at entry × (1 - profit_factor)
    profit_factor: Decimal = Decimal("0.05")
    # DCA margin: short next rung when price rises this much above anchor
    margin_rise_factor: Decimal = Decimal("0.03")
    qty_decimals: int = 8
    price_decimals: int = 4
    note: str = ""
    # Drawdown guard DEPRECATED — symmetric hub edge absorbs drawdown.
    max_drawdown_pct: Decimal = Decimal("0")
    metrics_interval_cycles: int = 5
    # Max DCA rungs: 3 rungs × 7 USDT = 21 USDT max collateral per symbol
    max_rungs_per_symbol: int = 3
    # Isolated Margin settings
    margin_type: str = "ISOLATED"  # Only ISOLATED supported


    def normalize(self) -> None:
        self.symbol = normalize_binance_spot_symbol(self.symbol)
        self.loop_interval_sec = max(1, min(int(self.loop_interval_sec), 86_400))
        self.quote_order_qty = max(_dec(self.quote_order_qty, "7.0"), Decimal("7.0"))
        self.profit_factor = max(_dec(self.profit_factor), Decimal("0.03"))
        self.margin_rise_factor = max(_dec(self.margin_rise_factor), Decimal("0"))
        self.qty_decimals = max(0, min(int(self.qty_decimals), 18))
        self.price_decimals = max(0, min(int(self.price_decimals), 18))
        self.note = (self.note or "").strip()[:20]
        self.max_drawdown_pct = max(_dec(self.max_drawdown_pct), Decimal("0"))
        self.metrics_interval_cycles = max(1, min(int(self.metrics_interval_cycles), 10_000))
        self.max_rungs_per_symbol = max(1, min(int(self.max_rungs_per_symbol), 100))
        self.margin_type = "ISOLATED"

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["quote_order_qty"] = str(self.quote_order_qty)
        d["profit_factor"] = str(self.profit_factor)
        d["margin_rise_factor"] = str(self.margin_rise_factor)
        d["max_drawdown_pct"] = str(self.max_drawdown_pct)
        d["mode"] = "LIVE"
        d["execution_mode"] = "MARGIN_SHORT"
        return d


class ElphabaRunner(BaseStrategyRunner):
    """Anti-Dorothy: bearish trend-fading via Isolated Margin shorts."""

    BOT_TYPE = "elphaba"
    # Instant-pause: a single TP orphan feeds this many failures to
    # SymmetryGuard, immediately pausing the symbol.
    _TP_ORPHAN_PENALTY = 3

    def __init__(
        self,
        log: Callable[[str], None],
        event_log: Optional[Callable[[str, str, Optional[dict[str, Any]]], None]] = None,
    ) -> None:
        super().__init__(log, event_log)
        self.config = ElphabaConfig()
        self.config.normalize()
        self._precision_cache: dict[str, tuple[int, int]] = {}

    def apply_config(self, cfg: ElphabaConfig) -> None:
        cfg.normalize()
        self.config = cfg

    def _bot_key(self) -> str:
        return f"elphaba:{self.config.symbol}"

    def _loop_log_summary(self, report: dict[str, Any]) -> str:
        return f"elphaba:{report.get('decision')} symbol={report.get('symbol')}"

    # ── Margin helpers ───────────────────────────────────────────

    async def _ensure_collateral(
        self, client: Any, symbol: str, amount: Decimal, active_rungs: int,
    ) -> bool:
        """Transfer USDT from Spot to Isolated Margin wallet if needed."""
        required_equity = amount * (active_rungs + 1)
        try:
            iso_account = await client.get_isolated_margin_account(symbols=symbol)
            assets = iso_account.get("assets", [])
            if not assets:
                pass
            else:
                pair = assets[0] if isinstance(assets, list) else {}
                quote_asset = pair.get("quoteAsset", {})
                net_usdt = _dec(quote_asset.get("netAsset", "0"), "0")
                if net_usdt >= required_equity:
                    return True  # Already enough collateral
        except Exception as e:
            self._emit("WARNING", f"elphaba:collateral_check_failed: {e}")

        # Transfer from Spot
        try:
            result = await client.transfer_spot_to_isolated_margin(
                asset="USDT", symbol=symbol, amount=str(amount),
            )
            self._emit("INFO", "elphaba:collateral_transferred", {
                "symbol": symbol, "amount": str(amount), "response": result,
            })
            return True
        except Exception as e:
            self._emit("ERROR", f"elphaba:collateral_transfer_failed: {e}")
            return False

    async def _get_margin_open_orders(
        self, client: Any, symbol: str,
    ) -> list[dict[str, Any]]:
        """Get open margin orders for this symbol (isolated)."""
        try:
            orders = await client.get_open_margin_orders(symbol=symbol, isIsolated="TRUE")
            return orders if isinstance(orders, list) else []
        except Exception as e:
            # -11001 = Isolated margin account not yet created for this symbol.
            # This is expected on first run — no orders exist, return empty.
            if "-11001" in str(e):
                return []
            self._emit("WARNING", f"elphaba:get_margin_orders_failed: {e}")
            return []

    async def _close_position_at_market(
        self, client: Any, symbol: str, qty: Decimal, my_tag: str,
    ) -> Optional[dict[str, Any]]:
        """Buy-to-cover a short position at market price with auto-repay."""
        try:
            order = await client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=str(qty),
                isIsolated="TRUE",
                sideEffectType="AUTO_REPAY",
                newClientOrderId=f"{my_tag}-cover-{int(time.time())}",
            )
            self._emit("INFO", "elphaba:close_at_market", {
                "symbol": symbol, "qty": str(qty), "response": order,
            })
            self._capture_order_rate(client)
            return order
        except Exception as e:
            self._emit("ERROR", f"elphaba:close_at_market_failed: {e}")
            return None

    # ── Main cycle ───────────────────────────────────────────────

    async def run_once(self) -> dict[str, Any]:
        fuse = get_api_fuse()
        if fuse.is_tripped():
            remaining = fuse.remaining_cooldown_sec()
            self._emit("WARNING", f"API FUSE ACTIVO: ciclo omitido ({remaining:.0f}s restantes)")
            return {"decision": "FUSE_TRIPPED", "remaining_sec": remaining}
        c = self.config
        c.normalize()


        # ── SymmetryGuard: watchdog tick + hub pause check ──────────
        try:
            _guard = get_symmetry_guard()
            _tick = _guard.tick()
            if _tick.get("action") == "AUTO_RETRY":
                self._emit("INFO", "elphaba:GUARD_AUTO_RETRY", _tick)
            elif _tick.get("needs_rotation"):
                self._emit("CRITICAL", "elphaba:NEEDS_ROTATION", {
                    "action": "Symbol rotation required — exhausted recovery attempts",
                    "tick": _tick,
                })
                return {"decision": "NEEDS_ROTATION", "reason": "Recovery exhausted, awaiting symbol rotation"}
            if _guard.is_hub_paused():
                reason = _guard.get_pause_reason()
                self._emit("WARNING", "elphaba:HUB_PAUSED", {
                    "reason": reason,
                    "tick": _tick,
                })
                return {"decision": "HUB_PAUSED", "reason": reason}
            if _guard.is_symbol_paused(c.symbol):
                reason = f"Symbol {c.symbol} paused (recoverable failure)"
                self._emit("INFO", "elphaba:SYMBOL_PAUSED", {"symbol": c.symbol})
                return {"decision": "HUB_PAUSED", "reason": reason}
        except Exception:
            pass

        client = await self._ensure_client()
        symbol = c.symbol

        # ── Auto-resolve precision from ExchangeFilterCache ─────────
        try:
            _ef = get_exchange_filters()
            _sf = await _ef.ensure_loaded(symbol, client)
            c.qty_decimals = _sf.qty_decimals
            c.price_decimals = _sf.price_decimals
        except Exception as e:
            self._emit("WARNING", f"exchange_filters:fallback {symbol} — {e}")

        # ── Market price ──────────────────────────────────────────
        try:
            _cache = get_market_cache()
            ticker = await _cache.get_or_fetch(
                f"symbol_ticker:{symbol}",
                lambda: client.get_symbol_ticker(symbol=symbol),
            )
        except Exception:
            ticker = await client.get_symbol_ticker(symbol=symbol)
        market_price = _dec(ticker.get("price", "0"), "0")

        # ── Fetch my open margin orders ───────────────────────────
        open_orders = await self._get_margin_open_orders(client, symbol)
        my_tag = f"elphaba-{getattr(self, '_bot_id', 'elphaba')}"

        # My BUY LIMIT orders = take-profit anchors for short positions
        buy_limit = [
            o for o in open_orders
            if str(o.get("side")) == "BUY"
            and str(o.get("type")) == "LIMIT"
            and str(o.get("clientOrderId", "")).startswith(my_tag)
        ]
        highest_buy = None
        if buy_limit:
            highest_buy = max(buy_limit, key=lambda o: _dec(o.get("price", "0"), "0"))

        active_rungs = len(buy_limit)

        # ── DUAL-GATE: Inverse TrendSignal Check ─────────────────
        # Gate 1: HA MA crossover → BEARISH required (MA1 < MA2)
        # Gate 2: price > regular 1h candle open → shorting into a pump
        try:
            _trend_svc = get_trend_signal_service()

            if _trend_svc.needs_trend_refresh(symbol):
                try:
                    klines_1h = await client.get_klines(symbol=symbol, interval="1h", limit=10)
                    _trend_svc.update_trend(symbol, klines_1h)
                except Exception as kl_err:
                    self._emit("WARNING", f"elphaba:trend_refresh_failed: {kl_err}")

            if _trend_svc.needs_entry_refresh(symbol):
                try:
                    kline_now = await client.get_klines(symbol=symbol, interval="1h", limit=1)
                    candle_open_1h = float(kline_now[0][1]) if kline_now else 0.0
                    _trend_svc.update_entry_gate(symbol, float(market_price), candle_open_1h)
                except Exception as eg_err:
                    self._emit("WARNING", f"elphaba:entry_gate_refresh_failed: {eg_err}")

            _full = _trend_svc.get_full_state(symbol)


            trend_direction = _full.get("trend")
            # Elphaba requires BEARISH (inverse of Dorothy)
            trend_ok = trend_direction == "BEARISH"
            # Inverse entry gate: price > candle open = shorting into a pump
            entry_ok = _full.get("entry_gate") == "BLOCKED"  # Dorothy's BLOCKED is Elphaba's CLEAR

            self._emit("INFO", "elphaba:trend_gate", {
                "symbol": symbol,
                "trend": trend_direction,
                "entry_gate": _full.get("entry_gate"),
                "trend_ok_for_short": trend_ok,
                "entry_ok_for_short": entry_ok,
                "should_short": trend_ok and entry_ok,
            })

            if not trend_ok:
                return {
                    "preset_id": c.preset_id, "symbol": symbol,
                    "decision": "WAIT_TREND_BULLISH",
                    "trend": trend_direction,
                    "market_price": str(market_price),
                    "loop_interval_sec": c.loop_interval_sec,
                }

            if not entry_ok:
                return {
                    "preset_id": c.preset_id, "symbol": symbol,
                    "decision": "WAIT_ENTRY_BLOCKED",
                    "trend": trend_direction,
                    "entry_gate": _full.get("entry_gate"),
                    "market_price": str(market_price),
                    "loop_interval_sec": c.loop_interval_sec,
                }

        except ImportError:
            self._emit("WARNING", "elphaba:trend_signal_not_available")
        except Exception as ts_err:
            self._emit("ERROR", f"elphaba:trend_service_error: {ts_err}")
            return {
                "preset_id": c.preset_id, "symbol": symbol,
                "decision": "WAIT_TREND_ERROR",
                "error": str(ts_err),
                "market_price": str(market_price),
                "loop_interval_sec": c.loop_interval_sec,
            }

        # ── DCA short entry logic ─────────────────────────────────
        # Inverse of Dorothy: short more when price RISES above anchor
        should_short = False
        threshold = None
        if highest_buy is not None:
            # We have existing TP anchors — DCA up
            anchor_buy_price = _dec(highest_buy.get("price", "0"), "0")
            # Implied short entry = TP / (1 - pf)
            implied_short_entry = anchor_buy_price / (Decimal("1") - c.profit_factor)
            threshold = implied_short_entry * (Decimal("1") + c.profit_factor + c.margin_rise_factor)
            should_short = market_price >= threshold
        else:
            # No positions — first entry
            should_short = True

        report: dict[str, Any] = {
            "preset_id": c.preset_id,
            "symbol": symbol,

            "open_orders_count": len(open_orders),
            "has_buy_limit_anchor": highest_buy is not None,
            "market_price": str(market_price),
            "entry_threshold_price": str(threshold) if threshold else None,
            "decision": "SHORT_AND_COVER" if should_short else "WAIT",
            "loop_interval_sec": c.loop_interval_sec,
            "active_rungs": active_rungs,
            "max_rungs": c.max_rungs_per_symbol,
            "execution_mode": "MARGIN_SHORT",
        }

        # Rung ceiling
        if should_short and active_rungs >= c.max_rungs_per_symbol:
            report["decision"] = "BLOCKED_MAX_RUNGS"
            self._emit("WARNING", f"elphaba:max_rungs_reached {active_rungs}/{c.max_rungs_per_symbol}", {"report": report})
            self._maybe_emit_metrics()
            return report

        if not should_short:
            self._maybe_emit_metrics()
            return report



        # ── Ensure collateral in Isolated Margin wallet ───────────
        if not await self._ensure_collateral(client, symbol, c.quote_order_qty, active_rungs):
            report["decision"] = "BLOCKED_COLLATERAL"
            self._emit("ERROR", "elphaba:collateral_insufficient", {"report": report})
            self._maybe_emit_metrics()
            return report

        # ── EXECUTE: SELL SHORT (auto-borrow) ─────────────────────
        est_price = market_price if market_price > 0 else Decimal("1")
        est_qty = c.quote_order_qty / est_price
        sell_qty = _q(est_qty, c.qty_decimals)

        # Forensic ledger — record BEFORE sending
        _ledger_id = None
        try:
            _ledger_id = get_order_ledger().record(
                bot_id=getattr(self, '_bot_id', 'elphaba'),
                bot_type="elphaba", symbol=symbol, side="SELL",
                order_type="MARKET", qty=str(sell_qty),
                quote_order_qty=str(c.quote_order_qty), reason="SHORT_ENTRY",
                active_rungs=active_rungs, max_rungs=c.max_rungs_per_symbol,
                execution_mode="MARGIN_SHORT",
            )
        except Exception as e:
            self._emit("ERROR", f"order_ledger:record_failed:{e}")

        # ── Order Fuse gate ──────────────────────────────────────
        if not self._order_fuse_allows():
            report["decision"] = "ORDER_FUSE_TRIPPED"
            self._maybe_emit_metrics()
            return report

        try:
            short_order = await client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=str(sell_qty),
                isIsolated="TRUE",
                sideEffectType="MARGIN_BUY",
                newClientOrderId=f"{my_tag}-short-{int(time.time())}",
            )
        except Exception as order_err:
            # ── Alert: order failed → feed SymmetryGuard ──────────
            try:
                get_symmetry_guard().record_order_failure(
                    self._bot_key(), str(order_err)
                )
            except Exception:
                pass
            # ── Register in ExceptionZoo for forensic tracking ────
            try:
                get_exception_zoo().register(
                    order_err, module="elphaba:short_order",
                    context=f"symbol={symbol} qty={sell_qty}",
                )
            except Exception:
                pass
            self._emit("CRITICAL", "elphaba:SHORT_ORDER_FAILED", {
                "symbol": symbol, "error": str(order_err)[:300],
                "action": "Hub may auto-pause after 3 consecutive failures.",
            })
            report["decision"] = "ORDER_FAILED"
            report["error"] = str(order_err)[:300]
            self._maybe_emit_metrics()
            return report

        # ── Alert: order succeeded → reset failure counter ────────
        try:
            get_symmetry_guard().record_order_success(self._bot_key())
        except Exception:
            pass

        if _ledger_id:
            try:
                get_order_ledger().update_binance_response(
                    _ledger_id,
                    str(short_order.get("orderId", "")),
                    str(short_order.get("status", "")),
                )
            except Exception:
                pass

        self._emit("INFO", "elphaba:create_margin_order_sell_short", {
            "symbol": symbol, "response": short_order,
        })
        self._capture_order_rate(client)

        # Resolve actual fill price
        fills = short_order.get("fills") or []
        if fills:
            short_price = _dec(fills[0].get("price", "0"), "0")
        else:
            executed = _dec(short_order.get("executedQty", "0"), "0")
            quote = _dec(short_order.get("cummulativeQuoteQty", "0"), "0")
            short_price = quote / executed if executed > 0 else est_price
        filled_qty = _dec(short_order.get("executedQty", "0"), "0")

        # ── Place BUY LIMIT take-profit (with AUTO_REPAY) ─────────
        tp_price = _q(short_price * (Decimal("1") - c.profit_factor), c.price_decimals)
        tp_qty = _q(filled_qty, c.qty_decimals)

        _tp_ledger_id = None
        try:
            _tp_ledger_id = get_order_ledger().record(
                bot_id=getattr(self, '_bot_id', 'elphaba'),
                bot_type="elphaba", symbol=symbol, side="BUY",
                order_type="LIMIT", qty=str(tp_qty), price=str(tp_price),
                reason="TAKE_PROFIT_COVER", execution_mode="MARGIN_SHORT",
            )
        except Exception as e:
            self._emit("ERROR", f"order_ledger:record_failed:{e}")

        try:
            tp_order = await client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="LIMIT",
                timeInForce="GTC",
                quantity=str(tp_qty),
                price=str(tp_price),
                isIsolated="TRUE",
                sideEffectType="AUTO_REPAY",
                newClientOrderId=f"{my_tag}-tp-{int(time.time())}",
            )
        except Exception as tp_err:
            # ── CRITICAL: Short succeeded but COVER LIMIT failed ──
            # Position is now "orphaned" — short open without take-profit.
            # DRAIN LOOP PREVENTION: immediately pause THIS symbol.
            self._emit("CRITICAL", "elphaba:TP_COVER_FAILED_ORPHAN", {
                "symbol": symbol,
                "short_order_id": short_order.get("orderId"),
                "filled_qty": str(filled_qty),
                "filled_short_price": str(short_price),
                "intended_tp_price": str(tp_price),
                "error": str(tp_err)[:300],
                "action": "Symbol auto-paused to prevent drain loop. OrphanGuard will attempt recovery.",
            })
            # Feed SymmetryGuard with 3 failures at once to immediately
            # pause this symbol and prevent the drain loop.
            try:
                _guard = get_symmetry_guard()
                for _ in range(self.__class__._TP_ORPHAN_PENALTY):
                    _guard.record_order_failure(
                        self._bot_key(), f"TP_ORPHAN: {tp_err}"
                    )
            except Exception:
                pass
            report["decision"] = "TP_FAILED_ORPHAN"
            report["execution"] = "LIVE"
            report["short_order_id"] = short_order.get("orderId")
            report["filled_qty"] = str(filled_qty)
            report["filled_short_price"] = str(short_price)
            report["error"] = str(tp_err)[:300]
            self._maybe_emit_metrics()
            return report

        if _tp_ledger_id:
            try:
                get_order_ledger().update_binance_response(
                    _tp_ledger_id,
                    str(tp_order.get("orderId", "")),
                    str(tp_order.get("status", "")),
                )
            except Exception:
                pass

        self._emit("INFO", "elphaba:create_margin_order_buy_limit_tp", {
            "symbol": symbol, "response": tp_order,
        })
        self._capture_order_rate(client)

        report["execution"] = "LIVE"
        report["short_order_id"] = short_order.get("orderId")
        report["tp_order_id"] = tp_order.get("orderId")
        report["filled_qty"] = str(filled_qty)
        report["filled_short_price"] = str(short_price)
        report["placed_tp_price"] = str(tp_price)
        self._emit("INFO", "elphaba:decision", {"report": report})
        self._maybe_emit_metrics()
        return report
