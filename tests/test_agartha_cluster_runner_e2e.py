"""End-to-end smoke test of the cluster using StubLiveClient.

Exercises: init -> universe -> params -> schedule -> service tick ->
entry placement -> fill -> exit placement -> fill -> closed.
"""
from __future__ import annotations

import json
import time

import pytest

from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
from backtest.agartha_cluster.bot_runner import BotRunner, RunnerConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.cluster_service import ClusterService, ServiceConfig
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import StubLiveClient
from backtest.agartha_cluster.models import BotState, SymbolFilters, SymbolParams
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig


@pytest.fixture
def setup(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    db.upsert_universe([{"symbol": "FOOUSDT", "alpha_id": "alpha-foo", "quote_asset": "USDT"}])
    db.upsert_symbol_params(
        SymbolParams(
            symbol="FOOUSDT",
            trailing_stop_pct=30.0,
            activation_profit_pct=0.0,
            breakeven_lock_pct=0.0,
            entry_limit_offset_pct=0.0,
            optimized_at="2026-05-25 10:00:00",
        )
    )
    db.upsert_symbol_filters(
        SymbolFilters(
            symbol="FOOUSDT",
            tick_size=1e-4,
            step_size=0.01,
            min_notional=0.5,
            bid_multiplier_up=5.0,
            bid_multiplier_down=0.2,
            ask_multiplier_up=5.0,
            ask_multiplier_down=0.2,
        )
    )

    client = StubLiveClient(seed=7, initial_quote_balance=1000.0)
    client.configure_market("FOOUSDT", price=1.0, trend=0.0, volatility=0.0)

    events = EventLogger(db, jsonl_dir=str(tmp_path / "logs"), echo_stdout=False)
    throttle = ApiThrottle(db, ThrottleConfig(weight_limit_per_minute=100_000, orders_limit_per_10s=1000))
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=0))
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events, config=RunnerConfig())
    recon = Reconciler(db, client, events)

    service = ClusterService(
        db=db,
        client=client,
        events=events,
        throttle=throttle,
        scheduler=sch,
        runner=runner,
        reconciler=recon,
        config=ServiceConfig(mode="dry-run", tick_seconds=0.0, capital_usdt_per_bot=10.0),
    )
    yield db, client, runner, service, sch
    db.close()


def test_deploy_entry_fill_to_in_position(setup):
    db, client, runner, service, sch = setup
    sch.enqueue_symbols(["FOOUSDT"], start_ts_ms=int(time.time() * 1000) - 1000)
    service.start()
    service.tick_once()

    bots = db.list_bots()
    assert len(bots) == 1
    bot = bots[0]
    assert bot.state == BotState.AWAITING_ENTRY_FILL
    assert bot.entry_client_order_id is not None

    runner.on_fill(
        client_order_id=bot.entry_client_order_id,
        symbol="FOOUSDT",
        side=__import__("backtest.agartha_cluster.models", fromlist=["OrderSide"]).OrderSide.BUY,
        price=float(bot.entry_price),
        qty=float(bot.entry_qty),
        ts_ms=int(time.time() * 1000),
    )
    bot = db.get_bot(bot.bot_id)
    assert bot.state == BotState.IN_POSITION


def test_full_cycle_to_closed_win(setup):
    db, client, runner, service, sch = setup
    sch.enqueue_symbols(["FOOUSDT"], start_ts_ms=int(time.time() * 1000) - 1000)
    service.start()
    service.tick_once()

    bots = db.list_bots()
    bot = bots[0]
    from backtest.agartha_cluster.models import OrderSide

    # 1. Fill entry at 1.0
    runner.on_fill(
        client_order_id=bot.entry_client_order_id,
        symbol="FOOUSDT",
        side=OrderSide.BUY,
        price=1.0,
        qty=float(bot.entry_qty),
    )
    bot = db.get_bot(bot.bot_id)
    assert bot.state == BotState.IN_POSITION

    # 2. Move the market up then down to trigger trailing.
    client.configure_market("FOOUSDT", price=2.0, trend=0.0, volatility=0.0)
    service.tick_once()  # updates peak (above floor; HOLD expected)
    bot = db.get_bot(bot.bot_id)
    assert bot.peak_price is not None and bot.peak_price >= 2.0
    assert bot.state == BotState.IN_POSITION

    # 3. Crash below trailing floor (trailing=30% => floor=1.4); set price 1.2
    client.configure_market("FOOUSDT", price=1.2, trend=0.0, volatility=0.0)
    service.tick_once()  # should place SELL LIMIT
    bot = db.get_bot(bot.bot_id)
    assert bot.state == BotState.AWAITING_EXIT_FILL
    assert bot.exit_client_order_id is not None

    # 4. Simulate exit fill at the limit price (>1.0 entry => CLOSED_WIN).
    exit_row = db.get_order_by_client_id(bot.exit_client_order_id)
    runner.on_fill(
        client_order_id=bot.exit_client_order_id,
        symbol="FOOUSDT",
        side=OrderSide.SELL,
        price=float(exit_row["price"]),
        qty=float(exit_row["qty"]),
    )
    bot = db.get_bot(bot.bot_id)
    assert bot.state == BotState.CLOSED_WIN
    assert bot.realized_pnl_usdt is not None
    assert bot.realized_pnl_usdt > 0


def test_manual_close_after_stall(setup):
    db, client, runner, service, sch = setup
    sch.enqueue_symbols(["FOOUSDT"], start_ts_ms=int(time.time() * 1000) - 1000)
    service.start()
    service.tick_once()
    bot = db.list_bots()[0]
    from backtest.agartha_cluster.models import OrderSide

    runner.on_fill(
        client_order_id=bot.entry_client_order_id,
        symbol="FOOUSDT",
        side=OrderSide.BUY,
        price=1.0,
        qty=float(bot.entry_qty),
    )
    bot = db.get_bot(bot.bot_id)
    closed = runner.manual_close(bot, reason="supervisor_smoke")
    assert closed.state == BotState.MANUAL_CLOSED
    assert closed.notes == "supervisor_smoke"


def test_resource_monitoring_logging_in_service(setup):
    db, client, runner, service, sch = setup
    service.config.resource_log_interval_seconds = 0.0

    service.tick_once()

    metrics = db.get_resource_metrics()
    assert len(metrics) >= 1
    m = metrics[0]
    assert m["ts_ms"] > 0
    assert m["proc_cpu_pct"] >= 0.0
    assert m["proc_ram_mb"] >= 0.0
    assert m["host_cpu_pct"] >= 0.0
    assert m["host_ram_pct"] >= 0.0
    assert m["disk_used_gb"] >= 0.0
    assert m["disk_pct"] >= 0.0

