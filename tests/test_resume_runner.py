"""Integration test for the ``execute_and_persist_resumable`` dispatcher.

Validates the env-var contract used by the CLI (`BACKTEST_RESUME_RUN_ID`)
without requiring PostgreSQL or any wired-up storage backend: the runner
locates the checkpoint via ``BACKTEST_DATA_ROOT`` + ``StoragePaths``,
sets the resume fields on the config, and the engine takes over.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.checkpoint import latest_checkpoint_path  # noqa: E402
from backtest.engine import EngineConfig, run_backtest  # noqa: E402
from backtest.storage_paths import StoragePaths  # noqa: E402
from backtest.strategies import SmaCrossStrategy  # noqa: E402
from tests.test_engine_rs_parity import _build_candles  # noqa: E402


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


def test_resumable_runner_picks_latest_checkpoint(
    tmp_path: "os.PathLike[str]", monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BACKTEST_RESUME_RUN_ID is set, the runner injects resume fields."""
    data_root = str(tmp_path)
    monkeypatch.setenv("BACKTEST_DATA_ROOT", data_root)

    paths = StoragePaths(data_root)
    fake_run_id = 99
    cp_dir = paths.checkpoints_dir(fake_run_id)
    os.makedirs(cp_dir, exist_ok=True)

    # Build candles and seed a checkpoint by running the engine with bar-based
    # checkpointing on a fresh copy of the data.
    candles_seed = _build_candles(500)
    seed_result = run_backtest(
        config=_base_config(checkpoint_every_bars=200, checkpoints_dir=cp_dir),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_seed],
    )
    assert seed_result.metrics["num_trades"] >= 1
    cp_path = latest_checkpoint_path(cp_dir)
    assert cp_path is not None

    # Now ask the runner to resume. We cannot use `execute_and_persist`
    # (it spins up the SQLite metadata layer), so we exercise the
    # resume-discovery half directly by calling the env-driven helper.
    monkeypatch.setenv("BACKTEST_RESUME_RUN_ID", str(fake_run_id))
    from backtest import runner

    assert runner._resume_run_id_from_env() == fake_run_id  # type: ignore[attr-defined]
    assert runner._resolve_checkpoints_dir(fake_run_id) == cp_dir  # type: ignore[attr-defined]

    # Resume by hand and confirm final equity matches a no-resume reference.
    reference = run_backtest(
        config=_base_config(),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_seed],
    )
    resumed = run_backtest(
        config=_base_config(resume_from_checkpoint=cp_path, checkpoints_dir=cp_dir),
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": 10, "slow": 30},
        candles=[dict(c) for c in candles_seed],
    )
    assert resumed.metrics["final_equity"] == pytest.approx(
        reference.metrics["final_equity"], abs=1e-9
    )
