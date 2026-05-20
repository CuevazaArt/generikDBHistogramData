"""Tests for the strategy registry and CLI parameter helpers."""
import argparse

import pytest

from backtest.registry import get_strategy, list_strategy_names, params_from_cli
from backtest.strategies import (
    AntiLouiseLuckyStrategy,
    AntiLouiseStrategy,
    LouiseLuckyStrategy,
    LouiseStrategy,
    MashaStrategy,
)


def test_listed_strategies_contain_core_bots():
    available = list_strategy_names(include_aliases=False)
    for name in (
        "dorothy",
        "elphaba",
        "ha_trend",
        "sma_cross",
        "masha",
        "louise",
        "louise_lucky",
        "anti_louise",
        "anti_louise_lucky",
        "thusnelda",
    ):
        assert name in available, f"missing strategy {name} in registry"


def test_get_strategy_returns_correct_classes():
    assert get_strategy("masha") is MashaStrategy
    assert get_strategy("louise") is LouiseStrategy
    assert get_strategy("louise_lucky") is LouiseLuckyStrategy
    assert get_strategy("anti_louise") is AntiLouiseStrategy
    assert get_strategy("anti_louise_lucky") is AntiLouiseLuckyStrategy


def test_get_strategy_rejects_unknown():
    with pytest.raises(ValueError):
        get_strategy("does_not_exist")


def test_params_from_cli_masha_contains_required_keys():
    ns = argparse.Namespace(
        fast=10, slow=30, quote_order_qty_usdt=8.0,
        take_profit_pct=1.5, stop_loss_pct=4.0, pullback_factor=0.006,
    )
    out = params_from_cli(ns, "masha")
    expected = {"fast", "slow", "quote_order_qty_usdt", "take_profit_pct", "stop_loss_pct", "pullback_factor"}
    assert expected.issubset(out.keys())


def test_params_from_cli_louise_lucky_includes_window():
    ns = argparse.Namespace(
        target_profit_pct=1.5, margin_drop_factor=0.004,
        quote_order_qty_usdt=8.0, lucky_window=24,
    )
    out = params_from_cli(ns, "louise_lucky")
    assert "lucky_window" in out
    assert out["lucky_window"] == 24
