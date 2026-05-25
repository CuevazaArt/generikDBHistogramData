"""Tests for the Agartha cluster DB layer.

Covers: migration idempotency, universe upsert, params upsert,
bots CRUD, state log, deploy queue uniqueness, throttle buckets and
event log.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.models import (
    BotState,
    Event,
    EventKind,
    EventLevel,
    EventSource,
    OrderRecord,
    OrderSide,
    OrderState,
    OrderType,
    SymbolFilters,
    SymbolParams,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cluster.db"
    db = ClusterDB(str(path))
    db.init_schema()
    try:
        yield db
    finally:
        db.close()


def test_schema_init_idempotent(db: ClusterDB):
    db.init_schema()
    db.init_schema()
    assert db.schema_version() == "1"


def test_universe_upsert(db: ClusterDB):
    n = db.upsert_universe(
        [
            {"symbol": "FOOUSDT", "alpha_id": "alpha-1", "quote_asset": "USDT", "holders": 100},
            {"symbol": "BARUSDT", "alpha_id": "alpha-2", "quote_asset": "USDT"},
        ]
    )
    assert n == 2
    rows = db.list_universe()
    assert {r["symbol"] for r in rows} == {"FOOUSDT", "BARUSDT"}

    db.upsert_universe([{"symbol": "FOOUSDT", "holders": 999, "quote_asset": "USDT"}])
    foo = next(r for r in db.list_universe() if r["symbol"] == "FOOUSDT")
    assert foo["holders"] == 999

    db.set_symbol_status("FOOUSDT", "deployed")
    foo = next(r for r in db.list_universe() if r["symbol"] == "FOOUSDT")
    assert foo["status"] == "deployed"


def test_symbol_params_roundtrip(db: ClusterDB):
    params = SymbolParams(
        symbol="FOOUSDT",
        trailing_stop_pct=30.0,
        activation_profit_pct=10.0,
        breakeven_lock_pct=50.0,
        entry_limit_offset_pct=2.0,
        partial_tp_pct=0.0,
        partial_tp_size_pct=0.0,
        max_holding_bars=0,
        study_equity_pct=120.0,
        optimized_at="2026-05-25 10:00:00",
    )
    db.upsert_universe([{"symbol": "FOOUSDT"}])
    db.upsert_symbol_params(params, raw_params={"k": "v"})
    got = db.get_symbol_params("FOOUSDT")
    assert got is not None
    assert got.trailing_stop_pct == 30.0
    assert got.entry_limit_offset_pct == 2.0
    assert got.study_equity_pct == 120.0


def test_create_bot_appends_state_log(db: ClusterDB):
    db.upsert_universe([{"symbol": "FOOUSDT"}])
    bot_id = db.create_bot(
        symbol="FOOUSDT",
        capital_usdt=10.0,
        params_snapshot={"trailing_stop_pct": 30, "activation_profit_pct": 0, "breakeven_lock_pct": 0},
        correlation_id="corr-1",
    )
    bot = db.get_bot(bot_id)
    assert bot is not None
    assert bot.state == BotState.CREATED
    assert bot.capital_usdt == 10.0
    conn = db.connect()
    logs = list(conn.execute("SELECT * FROM bot_state_log WHERE bot_id = ?", (bot_id,)))
    assert len(logs) == 1
    assert logs[0]["to_state"] == "created"


def test_deploy_queue_uniqueness_active_symbol(db: ClusterDB):
    db.upsert_universe([{"symbol": "FOOUSDT"}])
    qid1 = db.enqueue_deploy(symbol="FOOUSDT", planned_deploy_ts=1_000)
    qid2 = db.enqueue_deploy(symbol="FOOUSDT", planned_deploy_ts=2_000)
    assert qid1 is not None
    assert qid2 is None  # blocked by unique partial index
    db.mark_queue_status(qid1, "deployed", actual_deploy_ts=3_000, bot_id=42)
    qid3 = db.enqueue_deploy(symbol="FOOUSDT", planned_deploy_ts=4_000)
    assert qid3 is not None


def test_next_due_deploy_respects_priority_and_time(db: ClusterDB):
    db.upsert_universe([{"symbol": "AUSDT"}, {"symbol": "BUSDT"}])
    qa = db.enqueue_deploy(symbol="AUSDT", planned_deploy_ts=1_000, priority=200)
    qb = db.enqueue_deploy(symbol="BUSDT", planned_deploy_ts=500, priority=100)
    row = db.next_due_deploy(now_ms=10_000)
    assert row["queue_id"] == qb  # higher priority (lower number) first


def test_orders_and_fills(db: ClusterDB):
    db.upsert_universe([{"symbol": "FOOUSDT"}])
    bot_id = db.create_bot(
        symbol="FOOUSDT", capital_usdt=10.0,
        params_snapshot={"trailing_stop_pct": 30, "activation_profit_pct": 0, "breakeven_lock_pct": 0},
        correlation_id="corr-x",
    )
    order = OrderRecord(
        order_id=None,
        client_order_id="cid-1",
        bot_id=bot_id,
        symbol="FOOUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        state=OrderState.PENDING,
        price=0.5,
        qty=20.0,
        submitted_ts=1,
        correlation_id="corr-x",
    )
    pk = db.insert_order(order)
    assert pk > 0
    db.update_order_state(client_order_id="cid-1", state=OrderState.SUBMITTED, order_id="bx-1")
    row = db.get_order_by_client_id("cid-1")
    assert row["state"] == "submitted"
    assert row["order_id"] == "bx-1"

    fill_id = db.insert_fill(
        bot_id=bot_id,
        order_pk=pk,
        exchange_fill_id="fx-1",
        symbol="FOOUSDT",
        side=OrderSide.BUY,
        price=0.5,
        qty=20.0,
        fee=0.001,
        fee_asset="USDT",
        ts_ms=2,
        is_maker=False,
        correlation_id="corr-x",
        raw_payload=None,
    )
    assert fill_id > 0


def test_event_log_query(db: ClusterDB):
    db.log_event(
        Event(
            ts_ms=1_000,
            source=EventSource.SERVICE,
            level=EventLevel.INFO,
            kind=EventKind.SERVICE_START,
            payload={"foo": 1},
        )
    )
    db.log_event(
        Event(
            ts_ms=2_000,
            source=EventSource.BINANCE_REST,
            level=EventLevel.ERROR,
            kind=EventKind.ORDER_REJECTED,
            bot_id=1,
            symbol="FOOUSDT",
            correlation_id="corr",
            payload={"error": "test"},
        )
    )
    err = db.query_events(level=EventLevel.ERROR)
    assert len(err) == 1
    assert err[0]["kind"] == "order_rejected"

    by_symbol = db.query_events(symbol="FOOUSDT")
    assert len(by_symbol) == 1


def test_throttle_bucket_atomic(db: ClusterDB):
    bucket = 12_345
    w, o = db.bump_throttle(minute_bucket=bucket, weight=10, orders=1)
    assert (w, o) == (10, 1)
    w, o = db.bump_throttle(minute_bucket=bucket, weight=5, orders=0)
    assert (w, o) == (15, 1)
    assert db.get_throttle_bucket(bucket) == (15, 1)
    assert db.get_throttle_bucket(9_999) == (0, 0)


def test_symbol_filters_roundtrip(db: ClusterDB):
    db.upsert_symbol_filters(
        SymbolFilters(symbol="FOOUSDT", tick_size=1e-4, step_size=0.01, min_notional=0.5),
        raw={"raw": True},
    )
    got = db.get_symbol_filters("FOOUSDT")
    assert got is not None
    assert got.tick_size == 1e-4
    assert got.min_notional == 0.5


def test_service_runs_lifecycle(db: ClusterDB):
    run_id = db.start_service_run(mode="dry-run", pid=os.getpid(), host="host", version="0.0")
    assert run_id > 0
    db.stop_service_run(run_id, reason="done")
    conn = db.connect()
    row = conn.execute("SELECT * FROM service_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["mode"] == "dry-run"
    assert row["stop_reason"] == "done"
    assert row["stopped_at"] is not None


def test_credentials_meta_no_secret_persisted(db: ClusterDB):
    db.upsert_credentials_meta(
        profile="default",
        service_name="binance_alpha",
        username="default",
        storage_method="os_keyring",
    )
    row = db.get_credentials_meta("default")
    assert row is not None
    assert row["service_name"] == "binance_alpha"
    # Sanity: schema has no column that could leak a secret.
    conn = db.connect()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(credentials_meta)")]
    forbidden = {"api_key", "api_secret", "secret", "password", "token"}
    assert not (forbidden & set(cols))
