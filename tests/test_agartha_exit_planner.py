"""Tests for backtest.agartha_exit_planner."""
import pytest

from backtest.agartha_exit_planner import (
    ExitAction,
    SymbolFilters,
    compute_sell_band,
    compute_trail_floor,
    detect_crest,
    plan_exit,
)


def test_compute_trail_floor_basic():
    floor = compute_trail_floor(peak_price=2.0, entry_price=1.0, trailing_stop_pct=20.0)
    assert floor == pytest.approx(1.6)


def test_compute_trail_floor_breakeven_lock_protects_entry():
    # peak = 2.5x del entry, breakeven_lock = 100% (activo a 2x)
    floor = compute_trail_floor(
        peak_price=2.5, entry_price=1.0, trailing_stop_pct=50.0, breakeven_lock_pct=100.0
    )
    # raw trailing = 2.5 * 0.5 = 1.25; entry = 1.0 -> max(1.25, 1.0) = 1.25
    assert floor == pytest.approx(1.25)
    # Caso donde el trailing baja del entry y breakeven lo eleva
    floor = compute_trail_floor(
        peak_price=2.5, entry_price=1.5, trailing_stop_pct=50.0, breakeven_lock_pct=50.0
    )
    # raw trailing = 1.25; entry = 1.5; lock activo (peak 2.5 >= 1.5*1.5=2.25) -> max(1.25, 1.5) = 1.5
    assert floor == pytest.approx(1.5)


def test_compute_sell_band_typical():
    f = SymbolFilters(ask_multiplier_down=0.2, ask_multiplier_up=5.0)
    lo, hi = compute_sell_band(price_reference=1.0, filters=f)
    assert lo == pytest.approx(0.2)
    assert hi == pytest.approx(5.0)


def test_detect_crest_threshold():
    # peak 1.5, current 1.4 -> pullback 6.7% < 20% -> False
    assert detect_crest(recent_highs=[1.0, 1.2, 1.5], current_price=1.4, min_pullback_pct=20.0) is False
    # peak 1.5, current 1.1 -> pullback 26.7% >= 20% -> True
    assert detect_crest(recent_highs=[1.0, 1.2, 1.5], current_price=1.1, min_pullback_pct=20.0) is True
    # peak 1.5, current 1.0 -> pullback 33% >= 20% -> True
    assert detect_crest(recent_highs=[1.0, 1.2, 1.5], current_price=1.0, min_pullback_pct=20.0) is True


def test_plan_exit_hold_when_trail_not_triggered():
    plan = plan_exit(
        current_price=1.8, entry_price=1.0, peak_price=2.0, trailing_stop_pct=20.0,
    )
    assert plan.action == ExitAction.HOLD
    assert plan.trail_floor == pytest.approx(1.6)


def test_plan_exit_trail_limit_in_band():
    plan = plan_exit(
        current_price=1.5, entry_price=1.0, peak_price=2.0, trailing_stop_pct=20.0,
        filters=SymbolFilters(tick_size=0.01),
    )
    assert plan.action == ExitAction.TRAIL_LIMIT
    assert plan.limit_price is not None and plan.limit_price <= 1.5
    assert not plan.fallback_used


def test_plan_exit_recotes_to_band_border_when_target_below():
    # Forzamos banda muy estrecha por debajo: ask_multiplier_down=0.95 -> floor banda = 0.95
    # target ~ current_price = 0.5, esta por debajo -> recotiza a 0.95 * 0.5 = ... espera,
    # band se calcula sobre current_price. Para forzar fuera de banda, necesitamos un caso
    # donde el target quede por debajo del borde inferior. Lo simulo con ask_multiplier_down alto.
    plan = plan_exit(
        current_price=1.0, entry_price=1.0, peak_price=2.0, trailing_stop_pct=20.0,
        filters=SymbolFilters(tick_size=0.001, ask_multiplier_down=1.05, ask_multiplier_up=5.0),
        aggressive_limit_offset_ticks=10,
    )
    # band_lower = 1.0 * 1.05 = 1.05 > target ~0.99 -> fallback al borde
    assert plan.action == ExitAction.TRAIL_BORDER
    assert plan.limit_price is not None and plan.limit_price >= 1.05
    assert plan.fallback_used


def test_plan_exit_crest_alone_triggers_sell_even_without_trailing():
    # peak histórico = 2.0, current = 1.6 -> pullback 20% > min_pullback 15%
    plan = plan_exit(
        current_price=1.6, entry_price=1.0, peak_price=2.0, trailing_stop_pct=50.0,
        recent_highs=[1.5, 1.8, 2.0],
        crest_pullback_pct=15.0,
    )
    # trail_floor = 2.0 * 0.5 = 1.0 -> no triggered (current 1.6 > 1.0)
    # pero crest detectada (pullback 20%) -> CREST_CONFIRMED
    assert plan.action == ExitAction.CREST_CONFIRMED
    assert plan.crest_detected
