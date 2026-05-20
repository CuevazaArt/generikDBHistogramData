"""ExampleJV-inspired rule set — LIVE mode only (simulated mode removed).

Dorothy activation is governed by the EVI (Electric Volatility Index) gate:
  EVI = NATR × AvgSpeed × FreqExtreme × (Choppiness/50)
  Gate: EVI must be ≥ evi_min_threshold (default 0.02 = Grade D).
  Symbols below threshold are considered dead markets for DCA.
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
from runtime.core.exchange_filters import get_exchange_filters
from runtime.core.hub_state import get_hub_state
from runtime.core.market_cache import get_market_cache, MarketCache
from runtime.core.order_ledger import get_order_ledger
from runtime.core.orphan_guard import get_orphan_guard
from runtime.core.symmetry_guard import get_symmetry_guard
from runtime.modules.trend_signal import get_trend_signal_service


# _dec and _q imported from runtime.bot._decimal_utils


@dataclass
class DorothyConfig:
    # Preset B with safe mode defaults.
    preset_id: str = "B"
    symbol: str = "XRPUSDT"
    loop_interval_sec: int = 225
    quote_order_qty: Decimal = Decimal("7")  # 7 USDT per rung (above MIN_NOTIONAL)
    profit_factor: Decimal = Decimal("0.05")
    margin_drop_factor: Decimal = Decimal("0.03")  # L0: 3% between DCA steps
    qty_decimals: int = 8
    price_decimals: int = 4
    note: str = ""
    # Drawdown guard DEPRECATED — symmetric hub edge absorbs drawdown.
    # Kept as telemetry-only field (0 = disabled / no blocking).
    max_drawdown_pct: Decimal = Decimal("0")
    stop_loss_pct: Decimal = Decimal("0")  # Disabled — symmetric hub replaces SL
    metrics_interval_cycles: int = 5
    # T0.1: Hard ceiling on DCA rungs per symbol.
    # Each BUY_AND_SELL creates a SELL LIMIT anchor = 1 rung.
    # When open SELL LIMITs >= max_rungs, new buys are BLOCKED.
    max_rungs_per_symbol: int = 3  # Conservative: 3 rungs × 7 USDT = 21 USDT max exposure
    # T0.4: Force-liquidate worst position when drawdown exceeds this.
    # 0 = disabled (only blocks buys). Example: 0.40 = liquidate at 40% DD.
    drawdown_liquidate_pct: Decimal = Decimal("0")
    # EVI gate: minimum Electric Volatility Index to allow new buys.
    # Symbols with EVI < threshold are considered "dead markets" for DCA.
    # Grade scale: S≥0.50, A≥0.20, B≥0.10, C≥0.05, D≥0.02, F<0.02
    evi_min_threshold: Decimal = Decimal("0.02")  # Default: Grade D minimum


    def normalize(self) -> None:
        self.symbol = normalize_binance_spot_symbol(self.symbol)
        self.loop_interval_sec = max(1, min(int(self.loop_interval_sec), 86_400))
        self.quote_order_qty = max(_dec(self.quote_order_qty, "7.0"), Decimal("7.0"))
        # L0 floor: 3% minimum profit to ensure viability after commissions
        self.profit_factor = max(_dec(self.profit_factor), Decimal("0.03"))
        # L0 floor: 1% minimum DCA spread to prevent runaway buying on every small dip
        self.margin_drop_factor = max(_dec(self.margin_drop_factor), Decimal("0.01"))
        self.qty_decimals = max(0, min(int(self.qty_decimals), 18))
        self.price_decimals = max(0, min(int(self.price_decimals), 18))
        self.note = (self.note or "").strip()[:20]
        self.max_drawdown_pct = max(_dec(self.max_drawdown_pct), Decimal("0"))
        self.stop_loss_pct = max(_dec(self.stop_loss_pct), Decimal("0"))
        self.metrics_interval_cycles = max(1, min(int(self.metrics_interval_cycles), 10_000))
        self.max_rungs_per_symbol = max(1, min(int(self.max_rungs_per_symbol), 100))
        self.drawdown_liquidate_pct = max(_dec(self.drawdown_liquidate_pct), Decimal("0"))
        self.evi_min_threshold = max(_dec(self.evi_min_threshold), Decimal("0"))

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["quote_order_qty"] = str(self.quote_order_qty)
        d["profit_factor"] = str(self.profit_factor)
        d["margin_drop_factor"] = str(self.margin_drop_factor)
        d["max_drawdown_pct"] = str(self.max_drawdown_pct)
        d["stop_loss_pct"] = str(self.stop_loss_pct)
        d["evi_min_threshold"] = str(self.evi_min_threshold)
        d["mode"] = "LIVE"
        return d


class DorothyRunner(BaseStrategyRunner):

    BOT_TYPE = "dorothy"
    # Instant-pause: a single TP orphan feeds this many failures to
    # SymmetryGuard, immediately hitting MAX_FAILURE_STREAK and pausing
    # the symbol.  This prevents the drain loop where the bot keeps
    # buying without ever placing a sell.
    _TP_ORPHAN_PENALTY = 3

    def __init__(
        self,
        log: Callable[[str], None],
        event_log: Optional[Callable[[str, str, Optional[dict[str, Any]]], None]] = None,
    ) -> None:
        super().__init__(log, event_log)
        self.config = DorothyConfig()
        self.config.normalize()
        self._precision_cache: dict[str, tuple[int, int]] = {}

    def apply_config(self, cfg: DorothyConfig) -> None:
        cfg.normalize()
        self.config = cfg

    def _bot_key(self) -> str:
        return f"dorothy:{self.config.symbol}"

    def _loop_log_summary(self, report: dict[str, Any]) -> str:
        return f"bot:{report.get('decision')} symbol={report.get('symbol')}"

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
            # Tick the watchdog — handles auto-retry cooldowns
            _tick = _guard.tick()
            if _tick.get("action") == "AUTO_RETRY":
                self._emit("INFO", "dorothy:GUARD_AUTO_RETRY", _tick)
            elif _tick.get("needs_rotation"):
                self._emit("CRITICAL", "dorothy:NEEDS_ROTATION", {
                    "action": "Symbol rotation required — exhausted recovery attempts",
                    "tick": _tick,
                })
                return {"decision": "NEEDS_ROTATION", "reason": "Recovery exhausted, awaiting symbol rotation"}
            if _guard.is_hub_paused():
                reason = _guard.get_pause_reason()
                self._emit("WARNING", "dorothy:HUB_PAUSED", {
                    "reason": reason,
                    "tick": _tick,
                })
                return {"decision": "HUB_PAUSED", "reason": reason}
            if _guard.is_symbol_paused(c.symbol):
                reason = f"Symbol {c.symbol} paused (recoverable failure)"
                self._emit("INFO", "dorothy:SYMBOL_PAUSED", {"symbol": c.symbol})
                return {"decision": "HUB_PAUSED", "reason": reason}
        except Exception:
            pass

        client = await self._ensure_client()
        symbol = c.symbol

        # ── Auto-resolve precision from ExchangeFilterCache ──────────
        # Universal solution: fetches LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL
        # once per symbol and caches. Prevents TP failures from precision.
        try:
            _ef = get_exchange_filters()
            _sf = await _ef.ensure_loaded(symbol, client)
            c.qty_decimals = _sf.qty_decimals
            c.price_decimals = _sf.price_decimals
        except Exception as e:
            self._emit("WARNING", f"exchange_filters:fallback {symbol} — {e}")

        prev_equity = self._last_equity_usdt
        equity, capital = await self._compute_equity_usdt(client, symbol=symbol)
        drawdown, trading_blocked = self._register_equity(equity)
        self._record_return(prev_equity, equity)
        self._last_equity_usdt = equity
        self._emit(
            "SYSTEM",
            "bot:equity_snapshot",
            {
                "equity_usdt": str(equity),
                "capital_usdt": str(capital),
                "peak_equity_usdt": str(self._peak_equity_usdt or equity),
                "drawdown_pct": str(drawdown),
                "trading_blocked": trading_blocked,
            },
        )

        try:
            _cache = get_market_cache()
            open_orders = await _cache.get_or_fetch(
                MarketCache.scoped_key(f"open_orders:{symbol}", self._api_key),
                lambda: client.get_open_orders(symbol=symbol),
            )
        except Exception:
            open_orders = await client.get_open_orders(symbol=symbol)
        if not isinstance(open_orders, list):
            open_orders = []
        self._emit(
            "INFO",
            "binance:get_open_orders",
            {"symbol": symbol, "response": open_orders},
        )
        my_tag = f"dorothy-{getattr(self, '_bot_id', 'dorothy')}"
        # Filter to only MY tagged sell limits for DCA logic
        sell_limit = [
            o
            for o in (open_orders if isinstance(open_orders, list) else [])
            if str(o.get("side")) == "SELL" and str(o.get("type")) == "LIMIT" and str(o.get("clientOrderId", "")).startswith(my_tag)
        ]
        # SAFETY: Also detect ANY sell limits on this symbol (including manual/foreign)
        all_sell_limits = [
            o for o in (open_orders if isinstance(open_orders, list) else [])
            if str(o.get("side")) == "SELL" and str(o.get("type")) == "LIMIT"
        ]
        has_foreign_sells = len(all_sell_limits) > len(sell_limit)
        lowest_sell = None
        if sell_limit:
            lowest_sell = min(sell_limit, key=lambda o: _dec(o.get("price", "0"), "0"))

        try:
            _cache = get_market_cache()
            ticker = await _cache.get_or_fetch(
                f"symbol_ticker:{symbol}",
                lambda: client.get_symbol_ticker(symbol=symbol),
            )
        except Exception:
            ticker = await client.get_symbol_ticker(symbol=symbol)
        self._emit(
            "INFO",
            "binance:get_symbol_ticker",
            {"symbol": symbol, "response": ticker},
        )
        market_price = _dec(ticker.get("price", "0"), "0")

        # ── ORPHAN GUARD: periodic scan + auto-repair ──────────────
        try:
            _oguard = get_orphan_guard()
            if _oguard.needs_scan():
                _scan = await _oguard.scan_dorothy_orphans(
                    client, symbol, my_tag, c,
                )
                if _scan:
                    for _orphan in _scan:
                        self._emit("CRITICAL", "dorothy:ORPHAN_DETECTED", _orphan)
                        _recovery = await _oguard.recover_dorothy_orphan(
                            client, _orphan, c, my_tag,
                        )
                        self._emit(
                            "CRITICAL" if _recovery.get("action") != "RECOVERED" else "INFO",
                            f"dorothy:orphan_recovery:{_recovery.get('action')}",
                            _recovery,
                        )
                        self._capture_order_rate(client)
                _oguard._last_scan_mono = time.monotonic()
        except Exception as _og_err:
            self._emit("WARNING", f"orphan_guard:scan_failed: {_og_err}")

        # ── TREND GATE: HA crossover + price vs candle open ────────
        # Gate 1: HA MA(1) > MA(2) → BULLISH required for Dorothy
        # Gate 2: price < 1h candle open → buying into a dip
        try:
            _trend_svc = get_trend_signal_service()

            if _trend_svc.needs_trend_refresh(symbol):
                try:
                    klines_1h = await client.get_klines(symbol=symbol, interval="1h", limit=10)
                    _trend_svc.update_trend(symbol, klines_1h)
                except Exception as kl_err:
                    self._emit("WARNING", f"dorothy:trend_refresh_failed: {kl_err}")

            if _trend_svc.needs_entry_refresh(symbol):
                try:
                    kline_now = await client.get_klines(symbol=symbol, interval="1h", limit=1)
                    candle_open_1h = float(kline_now[0][1]) if kline_now else 0.0
                    _trend_svc.update_entry_gate(symbol, float(market_price), candle_open_1h)
                except Exception as eg_err:
                    self._emit("WARNING", f"dorothy:entry_gate_refresh_failed: {eg_err}")

            _full = _trend_svc.get_full_state(symbol)
            trend_direction = _full.get("trend")
            # Dorothy requires BULLISH trend
            trend_ok = trend_direction == "BULLISH"
            # Dorothy entry gate: price < candle open (BLOCKED = buying the dip)
            entry_ok = _full.get("entry_gate") == "BLOCKED"

            self._emit("INFO", "dorothy:trend_gate", {
                "symbol": symbol,
                "trend": trend_direction,
                "entry_gate": _full.get("entry_gate"),
                "trend_ok_for_long": trend_ok,
                "entry_ok_for_long": entry_ok,
                "should_buy": trend_ok and entry_ok,
            })

            if not trend_ok:
                return {
                    "preset_id": c.preset_id, "symbol": symbol,
                    "decision": "WAIT_TREND_BEARISH",
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
            self._emit("WARNING", "dorothy:trend_signal_not_available")
        except Exception as ts_err:
            self._emit("ERROR", f"dorothy:trend_service_error: {ts_err}")
            return {
                "preset_id": c.preset_id, "symbol": symbol,
                "decision": "WAIT_TREND_ERROR",
                "error": str(ts_err),
                "market_price": str(market_price),
                "loop_interval_sec": c.loop_interval_sec,
            }

        # ── DCA entry logic ────────────────────────────────────────
        should_buy = False
        threshold = None
        if lowest_sell is not None:
            trigger = _dec(lowest_sell.get("price", "0"), "0")
            threshold = trigger * (Decimal("1") - (c.profit_factor + c.margin_drop_factor))
            should_buy = market_price <= threshold
        elif has_foreign_sells:
            # Foreign/manual sell limits exist — do NOT buy, do NOT interfere
            should_buy = False
            self._emit("INFO", f"dorothy:foreign_sells_detected on {symbol}, skipping buy")
        else:
            should_buy = True

        # T0.1: Count active rungs (open SELL LIMITs = active DCA positions)
        active_rungs = len(sell_limit)

        report: dict[str, Any] = {
            "preset_id": c.preset_id,
            "symbol": symbol,
            "open_orders_count": len(open_orders),
            "has_sell_limit_anchor": lowest_sell is not None,
            "market_price": str(market_price),
            "entry_threshold_price": str(threshold) if threshold is not None else None,
            "decision": "BUY_AND_SELL" if should_buy else "WAIT",
            "sell_anchor_price": str(_dec(lowest_sell.get("price", "0"), "0")) if lowest_sell else None,
            "loop_interval_sec": c.loop_interval_sec,
            "active_rungs": active_rungs,
            "max_rungs": c.max_rungs_per_symbol,
        }
        # Drawdown guard DEPRECATED — symmetric hub absorbs drawdown.
        # trading_blocked is always False when max_drawdown_pct=0.
        # Kept as telemetry field, never blocks trading.
        if trading_blocked:
            # Only log as telemetry, do NOT block.
            self._emit("INFO", "bot:drawdown_telemetry", {
                "symbol": symbol,
                "drawdown_pct": str(drawdown),
                "note": "Symmetric hub — drawdown guard deprecated, telemetry only.",
            })
        # T0.1: Block new buys when rung ceiling is reached
        if should_buy and active_rungs >= c.max_rungs_per_symbol:
            report["decision"] = "BLOCKED_MAX_RUNGS"
            self._emit(
                "WARNING",
                f"bot:max_rungs_reached {active_rungs}/{c.max_rungs_per_symbol}",
                {"report": report},
            )
            self._maybe_emit_metrics()
            return report
        if not should_buy:
            self._maybe_emit_metrics()
            return report
        # Trend + entry gates already checked above — both are OPEN at this point



        est_buy = market_price if market_price > 0 else Decimal("1")
        # T1.6: Model fees + slippage for honest paper trading
        _FEE_BPS = Decimal("10")      # 0.10% taker fee
        _SLIPPAGE_BPS = Decimal("5")   # 0.05% average slippage
        _FRICTION = (Decimal("1") + (_FEE_BPS + _SLIPPAGE_BPS) / Decimal("10000"))
        est_buy_adj = est_buy * _FRICTION  # effective buy price (higher)
        est_qty = c.quote_order_qty / est_buy_adj if est_buy_adj > 0 else Decimal("0")
        est_sell = est_buy_adj * (Decimal("1") + c.profit_factor) / _FRICTION  # TP net of fees
        report["planned_quote_order_qty"] = str(c.quote_order_qty)
        report["planned_buy_price"] = str(est_buy_adj)
        report["planned_buy_price_raw"] = str(est_buy)
        report["planned_sell_price"] = str(est_sell)
        report["planned_qty"] = str(_q(est_qty, c.qty_decimals))
        report["fee_slippage_bps"] = str(_FEE_BPS + _SLIPPAGE_BPS)

        # T0.3: Record LIVE order in forensic ledger BEFORE sending
        _ledger_id = None
        try:
            _ledger_id = get_order_ledger().record(
                bot_id=getattr(self, '_bot_id', 'dorothy'),
                bot_type="dorothy", symbol=symbol, side="BUY",
                order_type="MARKET", qty=str(_q(est_qty, c.qty_decimals)),
                quote_order_qty=str(c.quote_order_qty), reason="BUY_AND_SELL",
                drawdown_pct=str(drawdown), active_rungs=active_rungs,
                max_rungs=c.max_rungs_per_symbol, execution_mode="LIVE",
            )
        except Exception as e:
            self._emit("ERROR", f"order_ledger:record_failed:{e}")

        # ── Order Fuse gate ──────────────────────────────────────
        if not self._order_fuse_allows():
            report["decision"] = "ORDER_FUSE_TRIPPED"
            self._maybe_emit_metrics()
            return report

        try:
            buy = await client.create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=str(c.quote_order_qty),
                newClientOrderId=f"{my_tag}-buy-{int(time.time())}"
            )
        except Exception as buy_err:
            # ── Alert: BUY order failed → feed SymmetryGuard ──────
            try:
                get_symmetry_guard().record_order_failure(
                    self._bot_key(), str(buy_err)
                )
            except Exception:
                pass
            self._emit("CRITICAL", "dorothy:BUY_ORDER_FAILED", {
                "symbol": symbol, "error": str(buy_err)[:300],
                "action": "Hub may auto-pause after 3 consecutive failures.",
            })
            report["decision"] = "ORDER_FAILED"
            report["error"] = str(buy_err)[:300]
            self._maybe_emit_metrics()
            return report

        # ── Alert: BUY succeeded → reset failure counter ──────────
        try:
            get_symmetry_guard().record_order_success(self._bot_key())
        except Exception:
            pass
        # Capture order rate from response headers
        self._capture_order_rate(client)

        # T0.3: Update ledger with Binance response
        if _ledger_id:
            try:
                get_order_ledger().update_binance_response(
                    _ledger_id,
                    str(buy.get("orderId", "")),
                    str(buy.get("status", "")),
                )
            except Exception as e:
                self._emit("ERROR", f"order_ledger:update_failed:{e}")

        self._emit(
            "INFO",
            "binance:create_order_buy_market",
            {"symbol": symbol, "response": buy},
        )
        fills = buy.get("fills") or []
        net_qty = Decimal("0")
        if fills:
            buy_price = _dec(fills[0].get("price", "0"), "0")
            for f in fills:
                fill_qty = _dec(f.get("qty", "0"), "0")
                comm = _dec(f.get("commission", "0"), "0")
                comm_asset = f.get("commissionAsset", "")
                # If commission is paid in the base asset (e.g., BTC in BTCUSDT), subtract it.
                if symbol.startswith(comm_asset) and comm_asset != "":
                    fill_qty -= comm
                net_qty += fill_qty
        else:
            executed = _dec(buy.get("executedQty", "0"), "0")
            quote = _dec(buy.get("cummulativeQuoteQty", "0"), "0")
            buy_price = quote / executed if executed > 0 else est_buy
            # Fallback 0.1% worst-case fee deduction if fills are missing
            net_qty = executed * Decimal("0.999")
            
        qty = net_qty
        sell_price = _q(buy_price * (Decimal("1") + c.profit_factor), c.price_decimals)
        sell_qty = _q(qty, c.qty_decimals)

        # ── Universal filter validation before SELL LIMIT ──────────
        try:
            _sf = get_exchange_filters().get(symbol)
            if _sf:
                sell_price = _sf.quantize_price(sell_price)
                sell_qty = _sf.quantize_qty(sell_qty)
                _ok, _reason = _sf.validate_order(sell_qty, sell_price)
                if not _ok:
                    self._emit("WARNING", f"dorothy:pre_flight_fail: {_reason}", {
                        "symbol": symbol, "sell_qty": str(sell_qty),
                        "sell_price": str(sell_price), "reason": _reason,
                    })
                    # Auto-adjust: if notional too low, use all available qty
                    if "minNotional" in _reason and sell_price > 0:
                        sell_qty = _sf.quantize_qty(
                            (_sf.min_notional / sell_price) * Decimal("1.05")
                        )
                        sell_qty = max(sell_qty, _sf.min_qty)
                        _ok2, _r2 = _sf.validate_order(sell_qty, sell_price)
                        if not _ok2:
                            # HARD ABORT: TP will fail on Binance → orphan loop
                            self._emit("CRITICAL", f"dorothy:FILTER_ABORT: {_r2}", {
                                "symbol": symbol, "sell_qty": str(sell_qty),
                                "sell_price": str(sell_price),
                                "action": "Aborting TP placement — would create orphan.",
                            })
                            try:
                                get_symmetry_guard().record_order_failure(
                                    self._bot_key(), f"FILTER_ABORT: {_r2}"
                                )
                            except Exception:
                                pass
                            report["decision"] = "TP_FILTER_REJECTED"
                            report["error"] = _r2
                            report["execution"] = "LIVE"
                            report["buy_order_id"] = buy.get("orderId")
                            report["filled_qty"] = str(qty)
                            report["filled_buy_price"] = str(buy_price)
                            self._maybe_emit_metrics()
                            return report
                    else:
                        # Non-notional filter failure — also abort
                        self._emit("CRITICAL", f"dorothy:FILTER_ABORT: {_reason}", {
                            "symbol": symbol, "sell_qty": str(sell_qty),
                            "sell_price": str(sell_price),
                        })
                        try:
                            get_symmetry_guard().record_order_failure(
                                self._bot_key(), f"FILTER_ABORT: {_reason}"
                            )
                        except Exception:
                            pass
                        report["decision"] = "TP_FILTER_REJECTED"
                        report["error"] = _reason
                        report["execution"] = "LIVE"
                        report["buy_order_id"] = buy.get("orderId")
                        report["filled_qty"] = str(qty)
                        report["filled_buy_price"] = str(buy_price)
                        self._maybe_emit_metrics()
                        return report
        except Exception as _ef_err:
            self._emit("WARNING", f"exchange_filters:validate_failed: {_ef_err}")

        # T0.4: Record OPEN RUNG in local hub state
        _hub_rung_id = 0
        try:
            _hub_rung_id = get_hub_state().open_rung(
                bot_id=getattr(self, '_bot_id', 'dorothy'),
                symbol=symbol,
                buy_order_id=str(buy.get("orderId", "")),
                buy_price=str(buy_price),
                qty=str(qty)
            )
        except Exception as e:
            self._emit("ERROR", f"hub_state:open_rung_failed:{e}")

        # T0.3: Record SELL LIMIT in ledger
        _sell_ledger_id = None
        try:
            _sell_ledger_id = get_order_ledger().record(
                bot_id=getattr(self, '_bot_id', 'dorothy'),
                bot_type="dorothy", symbol=symbol, side="SELL",
                order_type="LIMIT", qty=str(sell_qty), price=str(sell_price),
                reason="TAKE_PROFIT", execution_mode="LIVE",
            )
        except Exception as e:
            self._emit("ERROR", f"order_ledger:record_failed:{e}")

        try:
            sell = await client.create_order(
                symbol=symbol,
                side="SELL",
                type="LIMIT",
                timeInForce="GTC",
                quantity=str(sell_qty),
                price=str(sell_price),
                newClientOrderId=f"{my_tag}-sell-{int(time.time())}"
            )
        except Exception as tp_err:
            # ── CRITICAL: BUY succeeded but SELL LIMIT failed ─────
            # Position is now "orphaned" — asset bought but no TP set.
            # DRAIN LOOP PREVENTION: immediately pause THIS symbol so
            # the bot does not buy again on the next cycle without a TP.
            self._emit("CRITICAL", "dorothy:TP_LIMIT_FAILED_ORPHAN", {
                "symbol": symbol,
                "buy_order_id": buy.get("orderId"),
                "filled_qty": str(qty),
                "filled_buy_price": str(buy_price),
                "intended_sell_price": str(sell_price),
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
                
            # Mark rung as ORPHANED in local state
            if _hub_rung_id > 0:
                try:
                    get_hub_state().close_rung(_hub_rung_id, status="ORPHANED")
                except Exception:
                    pass

            report["decision"] = "TP_FAILED_ORPHAN"
            report["execution"] = "LIVE"
            report["buy_order_id"] = buy.get("orderId")
            report["filled_qty"] = str(qty)
            report["filled_buy_price"] = str(buy_price)
            report["error"] = str(tp_err)[:300]
            self._maybe_emit_metrics()
            return report

        # T0.3: Update sell ledger
        if _sell_ledger_id:
            try:
                get_order_ledger().update_binance_response(
                    _sell_ledger_id,
                    str(sell.get("orderId", "")),
                    str(sell.get("status", "")),
                )
            except Exception as e:
                self._emit("ERROR", f"order_ledger:update_failed:{e}")
        # Capture order rate from response headers
        self._capture_order_rate(client)

        # T0.4: Link SELL LIMIT to local hub rung
        if _hub_rung_id > 0:
            try:
                get_hub_state().link_sell_to_rung(
                    rung_id=_hub_rung_id,
                    sell_order_id=str(sell.get("orderId", "")),
                    sell_price=str(sell_price)
                )
            except Exception as e:
                self._emit("ERROR", f"hub_state:link_sell_failed:{e}")

        self._emit(
            "INFO",
            "binance:create_order_sell_limit",
            {"symbol": symbol, "response": sell},
        )
        report["execution"] = "LIVE"
        report["buy_order_id"] = buy.get("orderId")
        report["sell_order_id"] = sell.get("orderId")
        report["filled_qty"] = str(qty)
        report["filled_buy_price"] = str(buy_price)
        report["placed_sell_price"] = str(sell_price)
        self._emit("INFO", "bot:decision", {"report": report})
        self._maybe_emit_metrics()
        return report
