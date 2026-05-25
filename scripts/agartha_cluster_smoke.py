"""End-to-end smoke of the Agartha cluster (no network, no credentials).

Exercises the full pipeline with five bots that take different paths:

    A. FOOUSDT - happy path: entry fills, price rallies then drops,
                 trailing triggers, SELL LIMIT fills -> CLOSED_WIN.
    B. BARUSDT - loss path: entry fills, price drops, trailing triggers,
                 SELL LIMIT fills below entry -> CLOSED_LOSS.
    C. BAZUSDT - entry never fills: limit price below the market that
                 keeps walking away -> stays AWAITING_ENTRY_FILL forever.
    D. QUXUSDT - exit never fills: entry fills, trailing triggers, but
                 the SELL LIMIT is unreachable; the supervisor steps in
                 with manual_close -> MANUAL_CLOSED.
    E. ZOOUSDT - mid-flight: entry fills and the price is still climbing,
                 no trailing trigger -> stays IN_POSITION.

Capital is assumed abundant (no symbol fails on min_notional). The output
mirrors what an operator would see in production via ``cli status``:
final state per bot, orders placed, fills observed, events emitted and
API-call accounting.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
from backtest.agartha_cluster.bot_runner import BotRunner, RunnerConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.cluster_service import ClusterService, ServiceConfig
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import StubLiveClient
from backtest.agartha_cluster.models import (
    BotState,
    OrderSide,
    SymbolFilters,
    SymbolParams,
)
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig


SCENARIOS = [
    {
        "name": "A_happy",
        "symbol": "FOOUSDT",
        "start_price": 1.0,
        "params": dict(trailing_stop_pct=30.0, activation_profit_pct=0.0,
                       breakeven_lock_pct=0.0, entry_limit_offset_pct=0.0),
        "expected_state": BotState.CLOSED_WIN,
    },
    {
        "name": "B_loss",
        "symbol": "BARUSDT",
        "start_price": 1.0,
        "params": dict(trailing_stop_pct=20.0, activation_profit_pct=0.0,
                       breakeven_lock_pct=0.0, entry_limit_offset_pct=0.0),
        "expected_state": BotState.CLOSED_LOSS,
    },
    {
        "name": "C_no_entry_fill",
        "symbol": "BAZUSDT",
        "start_price": 1.0,
        "params": dict(trailing_stop_pct=25.0, activation_profit_pct=0.0,
                       breakeven_lock_pct=0.0, entry_limit_offset_pct=2.0),
        "expected_state": BotState.AWAITING_ENTRY_FILL,
    },
    {
        "name": "D_no_exit_fill_then_manual",
        "symbol": "QUXUSDT",
        "start_price": 1.0,
        "params": dict(trailing_stop_pct=25.0, activation_profit_pct=0.0,
                       breakeven_lock_pct=0.0, entry_limit_offset_pct=0.0),
        "expected_state": BotState.MANUAL_CLOSED,
    },
    {
        "name": "E_mid_flight",
        "symbol": "ZOOUSDT",
        "start_price": 1.0,
        "params": dict(trailing_stop_pct=30.0, activation_profit_pct=0.0,
                       breakeven_lock_pct=0.0, entry_limit_offset_pct=0.0),
        "expected_state": BotState.IN_POSITION,
    },
]


def _filters(symbol: str) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=1e-4,
        step_size=0.01,
        min_notional=0.5,
        bid_multiplier_up=5.0,
        bid_multiplier_down=0.2,
        ask_multiplier_up=5.0,
        ask_multiplier_down=0.2,
    )


def _seed_db(db: ClusterDB) -> None:
    universe = [
        {"symbol": s["symbol"], "alpha_id": f"alpha-{s['symbol'].lower()}", "quote_asset": "USDT"}
        for s in SCENARIOS
    ]
    db.upsert_universe(universe)
    for s in SCENARIOS:
        db.upsert_symbol_params(
            SymbolParams(
                symbol=s["symbol"],
                optimized_at="2026-05-25 12:00:00",
                **s["params"],
            )
        )
        db.upsert_symbol_filters(_filters(s["symbol"]))


def _configure_markets(client: StubLiveClient) -> None:
    for s in SCENARIOS:
        client.configure_market(
            s["symbol"], price=s["start_price"], trend=0.0, volatility=0.0
        )


def _fill_via_ws(runner: BotRunner, db: ClusterDB, *, symbol: str,
                 client_order_id: str, side: OrderSide, price: float) -> None:
    order = db.get_order_by_client_id(client_order_id)
    if order is None:
        return
    runner.on_fill(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        price=float(price),
        qty=float(order["qty"]),
    )


def _hr(title: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n{title}\n{bar}")


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="agartha_smoke_"))
    db_path = tmpdir / "cluster.db"
    log_dir = tmpdir / "logs"
    print(f"smoke workspace: {tmpdir}")

    db = ClusterDB(str(db_path))
    db.init_schema()
    _seed_db(db)

    client = StubLiveClient(seed=42, initial_quote_balance=10_000.0)
    _configure_markets(client)

    events = EventLogger(db, jsonl_dir=str(log_dir), echo_stdout=False)
    throttle = ApiThrottle(
        db,
        ThrottleConfig(weight_limit_per_minute=600, orders_limit_per_10s=20),
    )
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=0))
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events,
                       config=RunnerConfig())
    recon = Reconciler(db, client, events)

    service = ClusterService(
        db=db, client=client, events=events, throttle=throttle,
        scheduler=sch, runner=runner, reconciler=recon,
        config=ServiceConfig(
            mode="dry-run", tick_seconds=0.0, reconcile_every_seconds=10_000,
            capital_usdt_per_bot=10.0,
        ),
    )
    service.start()

    _hr("STEP 1 - schedule 5 symbols (FIFO, slot=0s for smoke)")
    qids = sch.enqueue_symbols(
        [s["symbol"] for s in SCENARIOS],
        start_ts_ms=int(time.time() * 1000) - 60_000,  # all already due
    )
    print(f"queue_ids: {qids}")
    for s in SCENARIOS:
        print(f"  - {s['name']:<28} symbol={s['symbol']:<8} "
              f"start_price={s['start_price']}")

    _hr("STEP 2 - deploy all 5 bots (one per tick)")
    for _ in range(len(SCENARIOS)):
        service.tick_once()

    bots_by_symbol = {b.symbol: b for b in db.list_bots()}
    for s in SCENARIOS:
        b = bots_by_symbol[s["symbol"]]
        print(f"  bot {b.bot_id:>2} {s['name']:<28} state={b.state.value:<22} "
              f"entry_coid={b.entry_client_order_id}")

    _hr("STEP 3 - market scenarios + fills")

    # ---- A: entry fills at 1.0, then price up to 2.0, then crash to 1.2 ----
    bot_a = bots_by_symbol["FOOUSDT"]
    _fill_via_ws(runner, db, symbol="FOOUSDT",
                 client_order_id=bot_a.entry_client_order_id,
                 side=OrderSide.BUY, price=1.0)
    client.configure_market("FOOUSDT", price=2.0, trend=0.0, volatility=0.0)
    service.tick_once()  # peak updated, HOLD
    client.configure_market("FOOUSDT", price=1.2, trend=0.0, volatility=0.0)
    service.tick_once()  # places SELL LIMIT
    bot_a = db.get_bot(bot_a.bot_id)
    _fill_via_ws(runner, db, symbol="FOOUSDT",
                 client_order_id=bot_a.exit_client_order_id,
                 side=OrderSide.SELL, price=float(
                     db.get_order_by_client_id(bot_a.exit_client_order_id)["price"]))

    # ---- B: entry fills, price drops, trailing places exit, exit fills ----
    bot_b = bots_by_symbol["BARUSDT"]
    _fill_via_ws(runner, db, symbol="BARUSDT",
                 client_order_id=bot_b.entry_client_order_id,
                 side=OrderSide.BUY, price=1.0)
    client.configure_market("BARUSDT", price=1.1, trend=0.0, volatility=0.0)
    service.tick_once()  # peak=1.1
    client.configure_market("BARUSDT", price=0.85, trend=0.0, volatility=0.0)
    service.tick_once()  # crash below floor (0.88) -> places SELL
    bot_b = db.get_bot(bot_b.bot_id)
    _fill_via_ws(runner, db, symbol="BARUSDT",
                 client_order_id=bot_b.exit_client_order_id,
                 side=OrderSide.SELL, price=float(
                     db.get_order_by_client_id(bot_b.exit_client_order_id)["price"]))

    # ---- C: entry never fills (limit @ 0.98, price runs up) ----
    bot_c = bots_by_symbol["BAZUSDT"]
    client.configure_market("BAZUSDT", price=1.10, trend=0.0, volatility=0.0)
    service.tick_once()  # nothing changes; entry still NEW, no fill emitted

    # ---- D: entry fills, trailing exit placed but never fills, supervisor closes ----
    bot_d = bots_by_symbol["QUXUSDT"]
    _fill_via_ws(runner, db, symbol="QUXUSDT",
                 client_order_id=bot_d.entry_client_order_id,
                 side=OrderSide.BUY, price=1.0)
    client.configure_market("QUXUSDT", price=1.3, trend=0.0, volatility=0.0)
    service.tick_once()  # peak=1.3, HOLD
    client.configure_market("QUXUSDT", price=0.90, trend=0.0, volatility=0.0)
    service.tick_once()  # places SELL LIMIT (floor=0.975)
    bot_d = db.get_bot(bot_d.bot_id)
    # Exit limit hovers above market; price keeps walking down -> never fills.
    client.configure_market("QUXUSDT", price=0.60, trend=0.0, volatility=0.0)
    service.tick_once()  # still AWAITING_EXIT_FILL
    bot_d_pre = db.get_bot(bot_d.bot_id)
    runner.manual_close(bot_d_pre, reason="smoke: exit_unreachable_3+x")

    # ---- E: entry fills, price climbing, no trailing trigger ----
    bot_e = bots_by_symbol["ZOOUSDT"]
    _fill_via_ws(runner, db, symbol="ZOOUSDT",
                 client_order_id=bot_e.entry_client_order_id,
                 side=OrderSide.BUY, price=1.0)
    client.configure_market("ZOOUSDT", price=1.4, trend=0.0, volatility=0.0)
    service.tick_once()  # peak=1.4, still HOLD

    _hr("STEP 4 - final per-bot state")
    print(f"{'bot':<4} {'scenario':<28} {'symbol':<8} {'state':<22} "
          f"{'entry':>7} {'exit':>7} {'pnl':>8} expected")
    print("-" * 110)
    failures: list[str] = []
    for s in SCENARIOS:
        b = db.get_bot(bots_by_symbol[s["symbol"]].bot_id)
        ok = "PASS" if b.state == s["expected_state"] else "FAIL"
        if ok == "FAIL":
            failures.append(
                f"{s['name']}: expected {s['expected_state'].value} "
                f"got {b.state.value}"
            )
        entry = f"{b.entry_price:.4f}" if b.entry_price else "-"
        exit_p = f"{b.exit_price:.4f}" if b.exit_price else "-"
        pnl = f"{b.realized_pnl_usdt:+.4f}" if b.realized_pnl_usdt is not None else "-"
        print(f"{b.bot_id:<4} {s['name']:<28} {s['symbol']:<8} "
              f"{b.state.value:<22} {entry:>7} {exit_p:>7} {pnl:>8} "
              f"[{ok}] expected={s['expected_state'].value}")

    _hr("STEP 5 - order ledger (all symbols)")
    rows = db.connect().execute(
        """SELECT bot_id, symbol, side, state, price, qty, filled_qty, avg_fill_price
           FROM orders ORDER BY order_pk"""
    ).fetchall()
    print(f"{'bot':<4} {'symbol':<8} {'side':<4} {'state':<10} "
          f"{'price':>8} {'qty':>8} {'filled':>8} {'avg_fill':>8}")
    print("-" * 80)
    for r in rows:
        print(f"{r['bot_id']:<4} {r['symbol']:<8} {r['side']:<4} {r['state']:<10} "
              f"{float(r['price']):>8.4f} {float(r['qty']):>8.4f} "
              f"{float(r['filled_qty'] or 0):>8.4f} "
              f"{float(r['avg_fill_price'] or 0):>8.4f}")

    _hr("STEP 6 - event_log breakdown (kind, level)")
    rows = db.connect().execute(
        """SELECT kind, level, COUNT(*) AS n FROM event_log
           GROUP BY kind, level ORDER BY n DESC"""
    ).fetchall()
    for r in rows:
        print(f"  {r['level']:<8} {r['kind']:<30} count={r['n']}")

    _hr("STEP 7 - API call accounting (REST)")
    total = db.connect().execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(weight),0) AS w, "
        "COALESCE(AVG(latency_ms),0) AS lat FROM api_calls"
    ).fetchone()
    print(f"  rest_calls={total['n']}  weight_sum={total['w']}  avg_latency_ms={total['lat']:.2f}")
    per_endpoint = db.connect().execute(
        "SELECT endpoint, method, COUNT(*) AS n, SUM(weight) AS w "
        "FROM api_calls GROUP BY endpoint, method"
    ).fetchall()
    for r in per_endpoint:
        print(f"  {r['method']} {r['endpoint']:<20} calls={r['n']} weight={r['w']}")

    _hr("STEP 8 - bot state transitions log")
    rows = db.connect().execute(
        """SELECT bot_id, from_state, to_state, reason
           FROM bot_state_log ORDER BY bot_id, log_id"""
    ).fetchall()
    for r in rows:
        frm = r["from_state"] or "(none)"
        to = r["to_state"] or "(none)"
        reason = r["reason"] or ""
        print(f"  bot {r['bot_id']:<3} {frm:<22} -> {to:<22} {reason}")

    _hr("STEP 9 - throttle usage snapshot")
    weight_min, orders_10s = throttle.used()
    print(f"  weight_used_in_last_minute = {weight_min} / {throttle.config.weight_limit_per_minute} budget")
    print(f"  orders_used_in_last_10s    = {orders_10s} / {throttle.config.orders_limit_per_10s} budget")

    _hr("STEP 10 - JSONL telemetry sample")
    jsonl_files = sorted(log_dir.glob("*.jsonl"))
    for p in jsonl_files:
        n = sum(1 for _ in p.open(encoding="utf-8"))
        print(f"  {p.name}: {n} lines")
        with p.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 3:
                    break
                row = json.loads(line)
                print(f"    {row['ts_ms']} {row['level']:<8} {row['kind']:<22} "
                      f"bot={row.get('bot_id')} sym={row.get('symbol')}")

    _hr("SMOKE RESULT")
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL EXPECTATIONS MET")
    print(f"artifacts: {tmpdir}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
