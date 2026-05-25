"""Tests for the scheduler and the API throttle."""
from __future__ import annotations

import time

import pytest

from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cluster.db"
    d = ClusterDB(str(path))
    d.init_schema()
    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def events(db, tmp_path):
    return EventLogger(db, jsonl_dir=str(tmp_path / "logs"), echo_stdout=False)


def test_throttle_budget_block(db: ClusterDB):
    cfg = ThrottleConfig(weight_limit_per_minute=10, orders_limit_per_10s=2)
    t = ApiThrottle(db, cfg)
    assert t.can_send(weight=5, orders=1)
    t.record(weight=5, orders=1)
    assert t.can_send(weight=5, orders=1)
    t.record(weight=5, orders=1)
    assert not t.can_send(weight=1, orders=0)


def test_throttle_orders_window_rolling(db: ClusterDB):
    cfg = ThrottleConfig(weight_limit_per_minute=10_000, orders_limit_per_10s=2)
    t = ApiThrottle(db, cfg)
    now = 1_000_000
    t.record(weight=1, orders=1, ts_ms=now)
    t.record(weight=1, orders=1, ts_ms=now + 1000)
    assert not t.can_send(weight=1, orders=1, ts_ms=now + 2000)
    # 10s later the first one rolls off
    assert t.can_send(weight=1, orders=1, ts_ms=now + 11_000)


def test_throttle_reconcile_server_weight(db: ClusterDB):
    cfg = ThrottleConfig(weight_limit_per_minute=1000)
    t = ApiThrottle(db, cfg)
    t.record(weight=10, orders=0)
    t.reconcile_server_weight(used_weight_1m=80)
    w, _ = t.used()
    assert w == 80


def test_scheduler_enqueue_spacing(db, events):
    db.upsert_universe(
        [{"symbol": s} for s in ["A", "B", "C", "D"]]
    )
    throttle = ApiThrottle(db, ThrottleConfig())
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=600))
    ids = sch.enqueue_symbols(["A", "B", "C", "D"], start_ts_ms=1_000)
    assert len(ids) == 4
    rows = list(db.connect().execute("SELECT symbol, planned_deploy_ts FROM deploy_queue ORDER BY queue_id"))
    assert rows[0]["symbol"] == "A"
    assert rows[1]["planned_deploy_ts"] - rows[0]["planned_deploy_ts"] == 600_000


def test_scheduler_slot_gating(db, events):
    db.upsert_universe([{"symbol": "A"}, {"symbol": "B"}])
    throttle = ApiThrottle(db, ThrottleConfig())
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=600))

    now_ms = int(time.time() * 1000)
    qa = db.enqueue_deploy(symbol="A", planned_deploy_ts=now_ms - 1000)
    qb = db.enqueue_deploy(symbol="B", planned_deploy_ts=now_ms - 1000)

    assert sch.slot_open(now_ms=now_ms) is True
    pick = sch.pick_next(now_ms=now_ms)
    assert pick is not None
    assert pick["queue_id"] == qa

    db.mark_queue_status(qa, "deployed", actual_deploy_ts=now_ms, bot_id=1)
    assert sch.slot_open(now_ms=now_ms + 100_000) is False
    assert sch.pick_next(now_ms=now_ms + 100_000) is None
    assert sch.slot_open(now_ms=now_ms + 700_000) is True
