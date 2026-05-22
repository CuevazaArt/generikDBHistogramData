"""Joint metric aggregators for walk-forward and multi-symbol runs.

The two helpers in this module take per-fold or per-symbol metric dicts as
input and emit a flat aggregated dict that downstream reports (Markdown +
CSV) can render directly. They have zero third-party dependencies; mean and
median come from the stdlib `statistics` module and Pearson correlation is
hand-rolled to avoid pulling in numpy/scipy on machines that are not running
the full analytics stack.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional


# Metric keys we expect from `backtest.metrics.summarize_metrics`.
_TOTAL_RETURN_KEY = "total_return"
_SHARPE_KEY = "sharpe"
_WIN_RATE_KEY = "win_rate"
_NUM_TRADES_KEY = "num_trades"


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(f) or math.isinf(f):
        return float(default)
    return f


def _pearson(xs: List[float], ys: List[float]) -> float:
    """Pearson correlation between two equal-length sequences.

    Returns 0.0 when either side has zero variance or fewer than two points,
    matching the convention used elsewhere in the codebase (no NaN leakage
    into the report layer).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = 0.0
    sx = 0.0
    sy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        num += dx * dy
        sx += dx * dx
        sy += dy * dy
    den = math.sqrt(sx * sy)
    if den <= 0.0:
        return 0.0
    return num / den


def _safe_mean(xs: Iterable[float]) -> float:
    arr = [float(x) for x in xs]
    if not arr:
        return 0.0
    return float(statistics.fmean(arr))


def _safe_median(xs: Iterable[float]) -> float:
    arr = [float(x) for x in xs]
    if not arr:
        return 0.0
    return float(statistics.median(arr))


def aggregate_walk_forward_metrics(fold_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-fold train + test metrics into a single dict.

    Each item in ``fold_results`` is expected to look like::

        {
            "fold_index": int,
            "train_metrics": {"total_return": float, "sharpe": float, ...},
            "test_metrics":  {"total_return": float, "sharpe": float, ...},
            ...                   # other keys (run_ids, params, ...) are ignored.
        }

    The returned dict matches the schema documented on
    :func:`backtest.walkforward_runner.run_walk_forward`.
    """
    fold_results = list(fold_results or [])
    n = len(fold_results)

    train_returns: List[float] = []
    test_returns: List[float] = []
    test_sharpes: List[float] = []
    per_fold_summary: List[Dict[str, Any]] = []

    for entry in fold_results:
        train = entry.get("train_metrics") or {}
        test = entry.get("test_metrics") or {}
        tr_ret = _coerce_float(train.get(_TOTAL_RETURN_KEY))
        te_ret = _coerce_float(test.get(_TOTAL_RETURN_KEY))
        te_shp = _coerce_float(test.get(_SHARPE_KEY))
        train_returns.append(tr_ret)
        test_returns.append(te_ret)
        test_sharpes.append(te_shp)
        per_fold_summary.append(
            {
                "fold_index": int(entry.get("fold_index", len(per_fold_summary))),
                "train_total_return": tr_ret,
                "test_total_return": te_ret,
                "test_sharpe": te_shp,
            }
        )

    train_mean = _safe_mean(train_returns)
    test_mean = _safe_mean(test_returns)
    if abs(train_mean) > 1e-12:
        decay_pct = (train_mean - test_mean) / train_mean * 100.0
    else:
        decay_pct = 0.0

    return {
        "n_folds": int(n),
        "train_mean_total_return": train_mean,
        "test_mean_total_return": test_mean,
        "train_test_correlation_total_return": _pearson(train_returns, test_returns),
        "test_mean_sharpe": _safe_mean(test_sharpes),
        "test_median_sharpe": _safe_median(test_sharpes),
        "test_worst_total_return": float(min(test_returns)) if test_returns else 0.0,
        "test_best_total_return": float(max(test_returns)) if test_returns else 0.0,
        "decay_test_vs_train_pct": float(decay_pct),
        "per_fold_summary": per_fold_summary,
    }


def aggregate_multi_symbol_metrics(per_symbol: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-symbol metrics into a portfolio-level summary.

    ``per_symbol`` maps a symbol (``"BTCUSDT"``) to a payload dict containing at
    least ``metrics`` (the dict returned by ``summarize_metrics``). Optional
    ``run_id`` / ``params`` keys are ignored here.
    """
    items = list((per_symbol or {}).items())
    n = len(items)

    returns: List[float] = []
    summary: List[Dict[str, Any]] = []
    best_symbol: Optional[str] = None
    worst_symbol: Optional[str] = None
    best_ret = -math.inf
    worst_ret = math.inf

    for symbol, payload in items:
        metrics = (payload or {}).get("metrics") or {}
        ret = _coerce_float(metrics.get(_TOTAL_RETURN_KEY))
        sharpe = _coerce_float(metrics.get(_SHARPE_KEY))
        wr = _coerce_float(metrics.get(_WIN_RATE_KEY))
        nt = _coerce_float(metrics.get(_NUM_TRADES_KEY))
        returns.append(ret)
        summary.append(
            {
                "symbol": str(symbol),
                "total_return": ret,
                "sharpe": sharpe,
                "win_rate": wr,
                "num_trades": nt,
            }
        )
        if ret > best_ret:
            best_ret = ret
            best_symbol = str(symbol)
        if ret < worst_ret:
            worst_ret = ret
            worst_symbol = str(symbol)

    if not returns:
        best_ret = 0.0
        worst_ret = 0.0

    dispersion = float(best_ret - worst_ret) if returns else 0.0

    return {
        "n_symbols": int(n),
        "mean_total_return": _safe_mean(returns),
        "median_total_return": _safe_median(returns),
        "worst_symbol": worst_symbol,
        "worst_symbol_total_return": float(worst_ret) if returns else 0.0,
        "best_symbol": best_symbol,
        "best_symbol_total_return": float(best_ret) if returns else 0.0,
        # Joint capital curve is reserved for the future joint-pool path.
        "joint_capital_curve": None,
        "dispersion_pct": dispersion,
        "per_symbol_summary": summary,
    }


__all__ = [
    "aggregate_walk_forward_metrics",
    "aggregate_multi_symbol_metrics",
]
