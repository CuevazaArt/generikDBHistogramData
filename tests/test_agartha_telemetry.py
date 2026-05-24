"""Tests for backtest.agartha_telemetry."""
import json
import os

import pytest

from backtest.agartha_exit_planner import ExitAction, ExitPlan
from backtest.agartha_telemetry import AgarthaJsonlLogger, append_alert_log, build_record


def test_build_record_computes_distance():
    plan = ExitPlan(
        action=ExitAction.HOLD,
        peak_price=2.0,
        trail_floor=1.6,
        band_lower=0.3,
        band_upper=7.5,
        reason="trail_not_triggered",
    )
    rec = build_record(
        symbol="ALPHA_953USDT",
        cycle_index=2,
        current_price=1.8,
        entry_price=1.0,
        plan=plan,
        cash=80.0,
        position_qty=120.0,
        equity=200.0,
        ts_ms=1_700_000_000_000,
    )
    assert rec.symbol == "ALPHA_953USDT"
    assert rec.cycle_index == 2
    assert rec.current_price == pytest.approx(1.8)
    assert rec.trail_floor == pytest.approx(1.6)
    # distance = (1.8 - 1.6) / 1.8 * 100 ≈ 11.11%
    assert rec.distance_to_floor_pct == pytest.approx(11.111, abs=0.01)
    assert rec.action == "hold"
    assert rec.cash == 80.0


def test_jsonl_logger_appends_records_per_symbol_and_day(tmp_path):
    plan = ExitPlan(action=ExitAction.TRAIL_LIMIT, limit_price=1.5, peak_price=2.0, trail_floor=1.6)
    rec = build_record(
        symbol="BILL", cycle_index=0, current_price=1.5, entry_price=1.0, plan=plan,
        ts_ms=1_700_000_000_000,
    )
    with AgarthaJsonlLogger(str(tmp_path)) as logger:
        logger.write(rec)
        logger.write(rec)
    # 2 lineas en el archivo del dia
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 2
    payload = json.loads(content[0])
    assert payload["symbol"] == "BILL"
    assert payload["action"] == "trail_limit"
    assert payload["limit_price"] == 1.5


def test_append_alert_log_creates_file(tmp_path):
    path = append_alert_log(str(tmp_path), {"event": "OUT_OF_BAND", "symbol": "ALPHA_953USDT"})
    assert os.path.exists(path)
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "OUT_OF_BAND"
    assert payload["symbol"] == "ALPHA_953USDT"
