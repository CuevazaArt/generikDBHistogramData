"""Crash / shutdown / WS-disconnect recovery tests.

These tests simulate operator scenarios that the v0.1.2 hardening
addresses:

  R1. ``service_runs`` row left with ``stopped_at IS NULL`` after SIGKILL /
      power loss -> next ``start()`` marks it as ``crash_detected_on_restart``.
  R2. Order placed before crash but never updated to ``submitted`` ->
      ``recovery_boot()`` re-queries the exchange via ``query_order`` and
      transitions the local state to match.
  R3. Fill happened during downtime (WS disconnected, process restarting) ->
      ``Reconciler.poll_open_orders_for_fills`` replays the fill and
      transitions the bot to ``in_position`` / ``closed_*`` without
      duplicating the fill row.
  R4. SQLite ``synchronous = FULL`` is enforced on production-style
      construction (durability against power loss).
  R5. ``wal_checkpoint`` shrinks the WAL file when called.
  R6. ``purge_throttle_buckets_older_than`` deletes the right rows.
  R7. JSONL writer fsyncs on every event when ``fsync_jsonl=True``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
from backtest.agartha_cluster.bot_runner import BotRunner, RunnerConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.cluster_service import ClusterService, ServiceConfig
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import StubLiveClient
from backtest.agartha_cluster.models import (
    BotState,
    EventKind,
    OrderSide,
    OrderState,
    SymbolFilters,
    SymbolParams,
)
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig


def _build_service(tmp_path: Path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    db.upsert_universe([
        {"symbol": "FOOUSDT", "alpha_id": "alpha-foo", "quote_asset": "USDT"}
    ])
    db.upsert_symbol_params(SymbolParams(
        symbol="FOOUSDT",
        trailing_stop_pct=30.0,
        activation_profit_pct=0.0,
        breakeven_lock_pct=0.0,
        entry_limit_offset_pct=0.0,
        optimized_at="2026-05-25",
    ))
    db.upsert_symbol_filters(SymbolFilters(
        symbol="FOOUSDT",
        tick_size=1e-4,
        step_size=0.01,
        min_notional=0.5,
    ))
    client = StubLiveClient(seed=11, initial_quote_balance=1000.0)
    client.configure_market("FOOUSDT", price=1.0, trend=0.0, volatility=0.0)
    events = EventLogger(
        db,
        jsonl_dir=str(tmp_path / "logs"),
        echo_stdout=False,
        fsync_jsonl=False,            # speed up tests
    )
    throttle = ApiThrottle(db, ThrottleConfig(weight_limit_per_minute=10_000, orders_limit_per_10s=1000))
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=0))
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events,
                       config=RunnerConfig())
    recon = Reconciler(db, client, events)
    service = ClusterService(
        db=db, client=client, events=events, throttle=throttle,
        scheduler=sch, runner=runner, reconciler=recon,
        config=ServiceConfig(
            mode="dry-run",
            tick_seconds=0.0,
            reconcile_every_seconds=10_000,    # disable mid-tick reconcile
            wal_checkpoint_every_seconds=10_000,
            capital_usdt_per_bot=10.0,
        ),
    )
    return db, client, runner, service, sch


# ---------------------------------------------------------------------------
# R1: previous service_runs row left open is marked as crash
# ---------------------------------------------------------------------------


def test_R1_previous_run_marked_as_crash(tmp_path):
    db, client, runner, service, sch = _build_service(tmp_path)

    # Simulate a previous process that died without graceful stop.
    crashed_run_id = db.start_service_run(
        mode="live", pid=99999, host="prev-host", version="0.1.0"
    )

    # Boot a fresh service.
    service.start()

    # The previous run must now be stopped with the recovery reason.
    rows = list(db.connect().execute(
        "SELECT * FROM service_runs WHERE run_id = ?", (crashed_run_id,)
    ))
    assert len(rows) == 1
    assert rows[0]["stopped_at"] is not None
    assert rows[0]["stop_reason"] == "crash_detected_on_restart"

    # And the recovery emitted the critical event.
    crash_events = list(db.connect().execute(
        "SELECT * FROM event_log WHERE kind = ?",
        (EventKind.SERVICE_PREVIOUS_CRASH_DETECTED.value,),
    ))
    assert len(crash_events) == 1
    assert crash_events[0]["level"] == "critical"
    payload = json.loads(crash_events[0]["payload_json"])
    assert payload["prev_run_id"] == crashed_run_id
    assert payload["prev_pid"] == 99999

    db.close()


def test_R1_no_open_runs_no_recovery_noise(tmp_path):
    db, _, _, service, _ = _build_service(tmp_path)
    service.start()
    crash_events = list(db.connect().execute(
        "SELECT * FROM event_log WHERE kind = ?",
        (EventKind.SERVICE_PREVIOUS_CRASH_DETECTED.value,),
    ))
    assert crash_events == []
    started = list(db.connect().execute(
        "SELECT * FROM event_log WHERE kind = ?",
        (EventKind.SERVICE_RECOVERY_COMPLETED.value,),
    ))
    assert len(started) == 1
    db.close()


# ---------------------------------------------------------------------------
# R2: order placed but bot crashed before state advanced
# ---------------------------------------------------------------------------


def test_R2_recovery_resolves_pending_order_via_query(tmp_path):
    """A bot in awaiting_entry_fill whose order quietly filled while the
    process was down should transition to in_position on next start()."""
    db, client, runner, service, sch = _build_service(tmp_path)

    # Run once to place the entry (StubLiveClient does not auto-fill on place;
    # it fills on query_order when price <= limit).
    sch.enqueue_symbols(["FOOUSDT"], start_ts_ms=int(time.time() * 1000) - 60_000)
    service.start()                      # recovery_boot no-op (clean DB)
    service.tick_once()                  # deploys + places BUY LIMIT @ 1.0
    bot = db.list_bots()[0]
    assert bot.state == BotState.AWAITING_ENTRY_FILL
    assert bot.entry_client_order_id is not None

    # Simulate the process dying: leave the order pending, do NOT graceful stop.
    db.close()

    # New process starts; market still at 1.0 so query_order will fill.
    db2 = ClusterDB(str(tmp_path / "cluster.db"))
    db2.init_schema()
    events2 = EventLogger(db2, jsonl_dir=str(tmp_path / "logs"), echo_stdout=False, fsync_jsonl=False)
    throttle2 = ApiThrottle(db2, ThrottleConfig(weight_limit_per_minute=10_000, orders_limit_per_10s=1000))
    sch2 = DeployScheduler(db2, throttle2, events2, SchedulerConfig(slot_seconds=0))
    runner2 = BotRunner(db=db2, client=client, throttle=throttle2, events=events2, config=RunnerConfig())
    recon2 = Reconciler(db2, client, events2)
    service2 = ClusterService(
        db=db2, client=client, events=events2, throttle=throttle2,
        scheduler=sch2, runner=runner2, reconciler=recon2,
        config=ServiceConfig(
            mode="dry-run", tick_seconds=0.0,
            reconcile_every_seconds=10_000, wal_checkpoint_every_seconds=10_000,
            capital_usdt_per_bot=10.0,
        ),
    )
    service2.start()                     # recovery_boot polls open orders

    # Recovery should have detected:
    #   - 1 previous open run -> marked as crash.
    #   - 1 open BUY order -> stub fills it -> on_fill -> bot in_position.
    bot_after = db2.get_bot(bot.bot_id)
    assert bot_after.state == BotState.IN_POSITION
    assert bot_after.entry_filled_ts is not None

    # And the fill should be recorded exactly once.
    nfills = db2.count_fills_for_order(
        db2.get_order_by_client_id(bot.entry_client_order_id)["order_pk"]
    )
    assert nfills == 1

    # FILL_REPLAYED event should be present.
    replayed = list(db2.connect().execute(
        "SELECT * FROM event_log WHERE kind = ?",
        (EventKind.FILL_REPLAYED.value,),
    ))
    assert len(replayed) == 1

    db2.close()


# ---------------------------------------------------------------------------
# R3: WS gap during steady-state operation (mid-tick reconciler poll)
# ---------------------------------------------------------------------------


def test_R3_reconciler_polls_and_replays_missed_fill(tmp_path):
    db, client, runner, service, sch = _build_service(tmp_path)
    sch.enqueue_symbols(["FOOUSDT"], start_ts_ms=int(time.time() * 1000) - 60_000)
    service.start()
    service.tick_once()                  # places entry
    bot = db.list_bots()[0]
    assert bot.state == BotState.AWAITING_ENTRY_FILL

    # The fill happens at the exchange but the runner's WS missed it
    # (i.e. on_fill was never called by the WS layer). We simulate this
    # by forcing the stub's order into FILLED state directly.
    client.force_fill(bot.entry_client_order_id)

    # The reconciler poll detects the missed fill and replays it.
    summary = service.reconciler.poll_open_orders_for_fills()
    assert summary["queried"] >= 1
    assert summary["replayed"] == 1

    bot_after = db.get_bot(bot.bot_id)
    assert bot_after.state == BotState.IN_POSITION

    # Idempotency: a second poll does NOT replay again.
    summary2 = service.reconciler.poll_open_orders_for_fills()
    assert summary2["replayed"] == 0

    db.close()


# ---------------------------------------------------------------------------
# R4: synchronous=FULL by default for production-style construction
# ---------------------------------------------------------------------------


def test_R4_synchronous_full_by_default(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    conn = db.connect()
    row = conn.execute("PRAGMA synchronous").fetchone()
    # 2 = FULL in SQLite enum.
    assert int(row[0]) == 2
    db.close()


def test_R4_synchronous_normal_when_overridden(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"), synchronous="NORMAL")
    db.init_schema()
    row = db.connect().execute("PRAGMA synchronous").fetchone()
    assert int(row[0]) == 1  # NORMAL
    db.close()


def test_R4_synchronous_rejects_garbage(tmp_path):
    with pytest.raises(ValueError):
        ClusterDB(str(tmp_path / "cluster.db"), synchronous="banana")


# ---------------------------------------------------------------------------
# R5: wal_checkpoint helper
# ---------------------------------------------------------------------------


def test_R5_wal_checkpoint_returns_counters(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    # Trigger some writes so the WAL has pages.
    for i in range(5):
        db.start_service_run(mode="dry-run", pid=1000 + i, host="h", version="t")
    busy, log_pages, checkpointed = db.wal_checkpoint(mode="TRUNCATE")
    assert busy == 0
    assert log_pages >= 0
    assert checkpointed >= 0
    db.close()


def test_R5_wal_checkpoint_rejects_invalid_mode(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    with pytest.raises(ValueError):
        db.wal_checkpoint(mode="FOO")
    db.close()


# ---------------------------------------------------------------------------
# R6: throttle bucket purge
# ---------------------------------------------------------------------------


def test_R6_purge_throttle_buckets(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    db.bump_throttle(minute_bucket=100, weight=5, orders=1)
    db.bump_throttle(minute_bucket=200, weight=5, orders=1)
    db.bump_throttle(minute_bucket=300, weight=5, orders=1)
    deleted = db.purge_throttle_buckets_older_than(before_minute_bucket=250)
    assert deleted == 2
    remaining = list(db.connect().execute(
        "SELECT minute_bucket FROM api_throttle_buckets ORDER BY minute_bucket"
    ))
    assert [r["minute_bucket"] for r in remaining] == [300]
    db.close()


# ---------------------------------------------------------------------------
# R7: JSONL fsync
# ---------------------------------------------------------------------------


def test_R7_jsonl_fsync_flag(tmp_path):
    db = ClusterDB(str(tmp_path / "cluster.db"))
    db.init_schema()
    events = EventLogger(
        db, jsonl_dir=str(tmp_path / "logs"), echo_stdout=False, fsync_jsonl=True
    )
    events.info(
        kind=EventKind.SERVICE_START,
        source=__import__(
            "backtest.agartha_cluster.models", fromlist=["EventSource"]
        ).EventSource.SERVICE,
    )
    files = list(Path(tmp_path / "logs").glob("*.jsonl"))
    assert files, "JSONL file was not created"
    # File must be non-empty AND contain a valid JSON line right after the call.
    content = files[0].read_text(encoding="utf-8").splitlines()
    assert content, "fsync_jsonl=True should ensure data is on disk immediately"
    payload = json.loads(content[0])
    assert payload["kind"] == "service_start"
    db.close()


# ---------------------------------------------------------------------------
# R8: recovery_boot can be disabled via config
# ---------------------------------------------------------------------------


def test_R8_recovery_boot_disabled(tmp_path):
    db, _, _, _, _ = _build_service(tmp_path)
    # Build a fresh service with recovery disabled.
    db.start_service_run(mode="live", pid=42, host="x", version="x")  # would normally trigger crash event
    from backtest.agartha_cluster import cluster_service as svc_mod
    cfg = ServiceConfig(
        mode="dry-run", tick_seconds=0.0,
        reconcile_every_seconds=10_000, wal_checkpoint_every_seconds=10_000,
        capital_usdt_per_bot=10.0,
        enable_recovery_boot=False,
    )
    # Instantiate ClusterService manually with the same wiring.
    from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
    from backtest.agartha_cluster.event_logger import EventLogger
    from backtest.agartha_cluster.live_client import StubLiveClient
    from backtest.agartha_cluster.bot_runner import BotRunner, RunnerConfig
    from backtest.agartha_cluster.reconciler import Reconciler
    from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig

    client = StubLiveClient(seed=1)
    events = EventLogger(db, jsonl_dir=str(tmp_path / "logs"), echo_stdout=False, fsync_jsonl=False)
    throttle = ApiThrottle(db, ThrottleConfig())
    sch = DeployScheduler(db, throttle, events, SchedulerConfig(slot_seconds=0))
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events, config=RunnerConfig())
    recon = Reconciler(db, client, events)
    service = svc_mod.ClusterService(
        db=db, client=client, events=events, throttle=throttle,
        scheduler=sch, runner=runner, reconciler=recon, config=cfg,
    )
    service.start()
    # No crash event should have been logged.
    rows = list(db.connect().execute(
        "SELECT * FROM event_log WHERE kind = ?",
        (EventKind.SERVICE_PREVIOUS_CRASH_DETECTED.value,),
    ))
    assert rows == []
    db.close()
