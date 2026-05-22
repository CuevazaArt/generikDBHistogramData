"""Tests for Fase 2 checkpoint / resume support.

The fixtures here piggy-back on the deterministic sine-wave candle series
already used by the Rust-vs-Python parity harness. That gives us a workload
where `SmaCrossStrategy` produces enough trades that the broker state at
mid-run differs meaningfully from the start state — without it the resume
test would be vacuous.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Any, Dict, List

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.checkpoint import (  # noqa: E402
    Checkpoint,
    latest_checkpoint_path,
    read_checkpoint,
    write_checkpoint,
)
from backtest.engine import EngineConfig, run_backtest  # noqa: E402
from backtest.strategies import SmaCrossStrategy  # noqa: E402
from tests.test_engine_rs_parity import _build_candles  # noqa: E402


_CANONICAL_METRICS = (
    "initial_cash",
    "final_equity",
    "total_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "ulcer_index",
    "win_rate",
    "profit_factor",
    "num_trades",
)


def _base_config(**overrides: Any) -> EngineConfig:
    cfg = EngineConfig(
        db_path=":memory:",
        symbol="TESTUSDT",
        interval="1s",
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        sma_fast=10,
        sma_slow=30,
        ema_period=20,
        rsi_period=14,
        atr_period=14,
        events_mode="minimal",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_checkpoint_roundtrip_json(tmp_path: "os.PathLike[str]") -> None:
    """JSON read/write must preserve every field, including the tuple."""
    cp_path = os.path.join(str(tmp_path), "cp_42.json")
    original = Checkpoint(
        run_id=7,
        sim_ts=1_700_000_000_000,
        candle_offset=249,
        broker_state={"cash": 5_000.0, "position_qty": 0.123, "avg_entry": 100.0},
        strategy_state={"active_sell_limits": [101.5, 102.0]},
        seq=12,
        last_exec_ts=1_700_000_000_000,
        last_snapshot_ts=None,
        last_trade_entry=(99.5, 0.5),
        engine_kind="python",
        engine_version="0.2.0",
    )
    write_checkpoint(cp_path, original)
    assert os.path.exists(cp_path)

    loaded = read_checkpoint(cp_path)
    assert loaded.run_id == original.run_id
    assert loaded.sim_ts == original.sim_ts
    assert loaded.candle_offset == original.candle_offset
    assert loaded.broker_state == original.broker_state
    assert loaded.strategy_state == original.strategy_state
    assert loaded.seq == original.seq
    assert loaded.last_exec_ts == original.last_exec_ts
    assert loaded.last_snapshot_ts == original.last_snapshot_ts
    assert loaded.last_trade_entry == original.last_trade_entry
    assert loaded.engine_kind == original.engine_kind
    assert loaded.engine_version == original.engine_version


def test_latest_checkpoint_path_picks_highest_sim_ts(tmp_path: "os.PathLike[str]") -> None:
    base = str(tmp_path)
    # Intentionally out-of-order writes to verify ordering is by parsed int,
    # not by file mtime or lexicographic name.
    for ts in (200, 50, 1000):
        cp = Checkpoint(
            run_id=1,
            sim_ts=ts,
            candle_offset=ts - 1,
            broker_state={"cash": 1.0, "position_qty": 0.0, "avg_entry": 0.0},
            strategy_state={},
            seq=ts,
            last_exec_ts=ts,
            last_snapshot_ts=None,
            last_trade_entry=None,
        )
        write_checkpoint(os.path.join(base, f"cp_{ts}.json"), cp)
    # Add a stray file that must be ignored.
    with open(os.path.join(base, "not_a_checkpoint.txt"), "w", encoding="utf-8") as fh:
        fh.write("noise")
    picked = latest_checkpoint_path(base)
    assert picked is not None
    assert picked.endswith("cp_1000.json")


def test_latest_checkpoint_path_handles_missing_dir() -> None:
    assert latest_checkpoint_path("") is None
    assert latest_checkpoint_path(os.path.join(tempfile.gettempdir(), "non_existent_dir_xyz")) is None


def test_engine_writes_checkpoint(tmp_path: "os.PathLike[str]") -> None:
    """Bar-threshold trigger fires at the expected cadence."""
    candles = _build_candles(500)
    cp_dir = str(tmp_path)
    cfg = _base_config(
        checkpoint_every_bars=100,
        checkpoints_dir=cp_dir,
    )
    run_backtest(
        config=cfg,
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles,
    )
    files = sorted(f for f in os.listdir(cp_dir) if f.startswith("cp_") and f.endswith(".json"))
    assert len(files) >= 4, f"expected >= 4 checkpoint files, got {files}"
    latest = latest_checkpoint_path(cp_dir)
    assert latest is not None
    loaded = read_checkpoint(latest)
    assert loaded.candle_offset >= 400, f"latest checkpoint offset too low: {loaded.candle_offset}"


def test_engine_resume_continues(tmp_path: "os.PathLike[str]") -> None:
    """Resuming a 500-bar run from a mid-run checkpoint reproduces final equity."""
    candles_full = _build_candles(500)

    cp_dir = str(tmp_path)
    cfg_with_cp = _base_config(checkpoint_every_bars=250, checkpoints_dir=cp_dir)
    # First pass writes checkpoints so we have something to resume from.
    first_pass = run_backtest(
        config=cfg_with_cp,
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_full],
    )

    # Single-shot reference (no checkpointing) on a fresh copy.
    reference = run_backtest(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_full],
    )

    # Pick the FIRST checkpoint (the one written near bar 250) and resume.
    files = sorted(
        f for f in os.listdir(cp_dir) if f.startswith("cp_") and f.endswith(".json")
    )
    assert files, "no checkpoint files were written"
    # Sort by embedded sim_ts to pick the earliest mid-run snapshot.
    def _ts_of(name: str) -> int:
        return int(name[len("cp_"):-len(".json")])
    files.sort(key=_ts_of)
    chosen_path = os.path.join(cp_dir, files[0])
    cp_payload = read_checkpoint(chosen_path)
    # Sanity: the checkpoint should be near the 250-bar mark.
    assert 200 <= cp_payload.candle_offset <= 300

    cfg_resume = _base_config(resume_from_checkpoint=chosen_path)
    resumed = run_backtest(
        config=cfg_resume,
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_full],
    )

    # final_equity / total_return are state-based and must match exactly.
    assert resumed.metrics["final_equity"] == pytest.approx(
        reference.metrics["final_equity"], abs=1e-9
    )
    assert resumed.metrics["total_return"] == pytest.approx(
        reference.metrics["total_return"], abs=1e-9
    )

    # The first-pass run already matched the reference (it was the same
    # workload with checkpointing enabled); pin that too so we know the
    # checkpoint side-channel did not corrupt the run.
    assert first_pass.metrics["final_equity"] == pytest.approx(
        reference.metrics["final_equity"], abs=1e-9
    )


def test_engine_no_regression_when_disabled() -> None:
    """All checkpoint fields = None must give bit-identical metrics."""
    candles_a = _build_candles(1500)
    candles_b = [dict(c) for c in candles_a]

    reference = run_backtest(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_a,
    )
    # Explicitly construct a config with the Fase-2 fields present but set
    # to their defaults; ensures the new fields do not change the fast path.
    cfg_explicit = _base_config(
        checkpoint_every_bars=None,
        checkpoint_every_sim_seconds=None,
        checkpoints_dir=None,
        resume_from_checkpoint=None,
    )
    actual = run_backtest(
        config=cfg_explicit,
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=candles_b,
    )
    # IDENTITY, not approx: the no-regression gate.
    for key in _CANONICAL_METRICS:
        assert reference.metrics[key] == actual.metrics[key], (
            f"metric drift on {key}: ref={reference.metrics[key]!r} actual={actual.metrics[key]!r}"
        )
    assert reference.events == actual.events
    assert reference.equity_curve == actual.equity_curve
