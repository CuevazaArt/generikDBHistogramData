//! `genericbt-core` — Rust core for the backtesting framework.
//!
//! Public surface (pyo3):
//!
//! * `run_backtest(config, strategy, candles, run_id=None, trial_id=None)`
//!   – Streaming bar loop, calls `strategy.on_bar(ctx)` once per candle.
//!     Returns a Python dict with the same keys as
//!     `backtest.engine.BacktestResult.to_dict()`:
//!     `{metrics, events, equity_curve, final_state, run_id, trial_id}`.
//!
//! * `SpotBrokerRs` – pyclass mirroring `backtest.broker.SpotBroker` 1:1.
//!
//! * `sma(values, period)` – `_rolling_sma` for Python tests.
//!
//! * `apply_indicators(candles, sma_period, ema_period, rsi_period, atr_period, price_key)`
//!   – Mutates candle dicts in place with `sma`, `ema`, `rsi`, `atr` columns.
//!
//! * `apply_heikin_ashi(candles)` – Returns a fresh list with Heikin-Ashi
//!   columns layered on top.
//!
//! * `apply_candle_source(candles, source)` – Writes `price_source` into
//!   each candle dict.
//!
//! Indicator and broker math live in `indicators.rs`, `broker.rs`,
//! `transforms.rs`. The Python <-> Rust dance is concentrated here so the
//! pure modules can be unit-tested with `cargo test` independently of pyo3.

#![allow(deprecated)]

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

mod broker;
mod checkpoint;
mod engine;
mod indicators;
mod transforms;
mod types;

use crate::broker::SpotBroker;
use crate::types::EngineConfig;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn extract_float_default<'py>(d: &'py PyDict, key: &str, default: f64) -> PyResult<f64> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => v.extract(),
        _ => Ok(default),
    }
}

fn extract_int_default<'py>(d: &'py PyDict, key: &str, default: i64) -> PyResult<i64> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => v.extract(),
        _ => Ok(default),
    }
}

fn extract_optional_int<'py>(d: &'py PyDict, key: &str) -> PyResult<Option<i64>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn extract_string_default<'py>(d: &'py PyDict, key: &str, default: &str) -> PyResult<String> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => v.extract(),
        _ => Ok(default.to_string()),
    }
}

fn extract_optional_string<'py>(d: &'py PyDict, key: &str) -> PyResult<Option<String>> {
    match d.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn parse_engine_config(config: &PyDict) -> PyResult<EngineConfig> {
    Ok(EngineConfig {
        initial_cash: extract_float_default(config, "initial_cash", 10_000.0)?,
        fee_rate: extract_float_default(config, "fee_rate", 0.001)?,
        slippage_bps: extract_float_default(config, "slippage_bps", 2.0)?,
        loop_seconds: extract_optional_int(config, "loop_seconds")?,
        events_mode: extract_string_default(config, "events_mode", "full")?,
        snapshot_seconds: extract_int_default(config, "snapshot_seconds", 3600)?,
        checkpoint_every_bars: extract_optional_int(config, "checkpoint_every_bars")?,
        checkpoint_every_sim_seconds: extract_optional_int(config, "checkpoint_every_sim_seconds")?,
        checkpoints_dir: extract_optional_string(config, "checkpoints_dir")?,
        resume_from_checkpoint: extract_optional_string(config, "resume_from_checkpoint")?,
    })
}

fn extract_initial_state_broker(config: &PyDict) -> PyResult<Option<(f64, f64, f64)>> {
    let initial_state = match config.get_item("initial_state")? {
        Some(v) if !v.is_none() => v,
        _ => return Ok(None),
    };
    let st_dict: &PyDict = match initial_state.downcast() {
        Ok(d) => d,
        Err(_) => return Ok(None),
    };
    let broker_obj = match st_dict.get_item("broker")? {
        Some(v) if !v.is_none() => v,
        _ => return Ok(None),
    };
    let b: &PyDict = match broker_obj.downcast() {
        Ok(d) => d,
        Err(_) => return Ok(None),
    };
    let cash = extract_float_default(b, "cash", f64::NAN)?;
    let pos = extract_float_default(b, "position_qty", f64::NAN)?;
    let avg = extract_float_default(b, "avg_entry", f64::NAN)?;
    Ok(Some((cash, pos, avg)))
}

// ---------------------------------------------------------------------------
// Indicator pyfunctions
// ---------------------------------------------------------------------------

/// SMA exposed for tests/orchestrators. Returns `None` during warmup.
#[pyfunction]
fn sma(values: &PyList, period: usize) -> PyResult<Vec<Option<f64>>> {
    let mut v: Vec<f64> = Vec::with_capacity(values.len());
    for item in values.iter() {
        v.push(item.extract()?);
    }
    Ok(indicators::rolling_sma(&v, period))
}

/// Mutates the candle dicts in place, writing `sma`, `ema`, `rsi`, `atr`.
/// Lazy: only pulls the keys the indicators actually need
/// (`price_key`, `high`, `low`, `close`).
#[pyfunction]
fn apply_indicators(
    candles: &PyList,
    sma_period: usize,
    ema_period: usize,
    rsi_period: usize,
    atr_period: usize,
    price_key: &str,
) -> PyResult<()> {
    let n = candles.len();
    if n == 0 {
        return Ok(());
    }
    let mut prices: Vec<f64> = Vec::with_capacity(n);
    let mut highs: Vec<f64> = Vec::with_capacity(n);
    let mut lows: Vec<f64> = Vec::with_capacity(n);
    let mut closes: Vec<f64> = Vec::with_capacity(n);
    for i in 0..n {
        let c = candles.get_item(i)?;
        let d: &PyDict = c.downcast()?;
        let close: f64 = d
            .get_item("close")?
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("close"))?
            .extract()?;
        let p: f64 = match d.get_item(price_key)? {
            Some(v) if !v.is_none() => v.extract()?,
            _ => close,
        };
        prices.push(p);
        highs.push(
            d.get_item("high")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("high"))?
                .extract()?,
        );
        lows.push(
            d.get_item("low")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("low"))?
                .extract()?,
        );
        closes.push(close);
    }
    let sma_vals = indicators::rolling_sma(&prices, sma_period);
    let ema_vals = indicators::rolling_ema(&prices, ema_period);
    let rsi_vals = indicators::rsi(&prices, rsi_period);
    let atr_vals = indicators::atr(&highs, &lows, &closes, atr_period);
    for i in 0..n {
        let d: &PyDict = candles.get_item(i)?.downcast()?;
        d.set_item("sma", sma_vals[i])?;
        d.set_item("ema", ema_vals[i])?;
        d.set_item("rsi", rsi_vals[i])?;
        d.set_item("atr", atr_vals[i])?;
    }
    Ok(())
}

/// Build a fresh list of candle dicts with Heikin-Ashi columns added.
#[pyfunction]
fn apply_heikin_ashi(py: Python<'_>, candles: &PyList) -> PyResult<PyObject> {
    let n = candles.len();
    let out = PyList::empty(py);
    if n == 0 {
        return Ok(out.into_py(py));
    }
    let mut opens: Vec<f64> = Vec::with_capacity(n);
    let mut highs: Vec<f64> = Vec::with_capacity(n);
    let mut lows: Vec<f64> = Vec::with_capacity(n);
    let mut closes: Vec<f64> = Vec::with_capacity(n);
    for i in 0..n {
        let d: &PyDict = candles.get_item(i)?.downcast()?;
        opens.push(
            d.get_item("open")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("open"))?
                .extract()?,
        );
        highs.push(
            d.get_item("high")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("high"))?
                .extract()?,
        );
        lows.push(
            d.get_item("low")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("low"))?
                .extract()?,
        );
        closes.push(
            d.get_item("close")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("close"))?
                .extract()?,
        );
    }
    let ha = transforms::heikin_ashi(&opens, &highs, &lows, &closes);
    for (i, ha_i) in ha.iter().enumerate() {
        let original: &PyDict = candles.get_item(i)?.downcast()?;
        let nc = original.copy()?;
        nc.set_item("ha_open", ha_i.ha_open)?;
        nc.set_item("ha_high", ha_i.ha_high)?;
        nc.set_item("ha_low", ha_i.ha_low)?;
        nc.set_item("ha_close", ha_i.ha_close)?;
        out.append(nc)?;
    }
    Ok(out.into_py(py))
}

/// Write `price_source` into each candle dict, in place. Matches
/// `backtest.transforms.apply_candle_source`: when source is `"ha_close"`
/// and the dict has an `ha_close` key, use that; otherwise fall back to
/// `close`.
#[pyfunction]
fn apply_candle_source(candles: &PyList, source: &str) -> PyResult<()> {
    for item in candles.iter() {
        let d: &PyDict = item.downcast()?;
        let close: f64 = d
            .get_item("close")?
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("close"))?
            .extract()?;
        let ha_close: Option<f64> = match d.get_item("ha_close")? {
            Some(v) if !v.is_none() => Some(v.extract()?),
            _ => None,
        };
        let v = transforms::candle_source_value(close, ha_close, source);
        d.set_item("price_source", v)?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// SpotBrokerRs pyclass — 1:1 mirror of `backtest.broker.SpotBroker`.
// ---------------------------------------------------------------------------

#[pyclass]
#[derive(Clone)]
struct SpotBrokerRs {
    inner: SpotBroker,
}

#[pymethods]
impl SpotBrokerRs {
    #[new]
    #[pyo3(signature = (initial_cash, fee_rate=0.001, slippage_bps=2.0))]
    fn new(initial_cash: f64, fee_rate: f64, slippage_bps: f64) -> Self {
        Self {
            inner: SpotBroker::new(initial_cash, fee_rate, slippage_bps),
        }
    }

    #[getter]
    fn cash(&self) -> f64 {
        self.inner.state.cash
    }

    #[getter]
    fn position_qty(&self) -> f64 {
        self.inner.state.position_qty
    }

    #[getter]
    fn avg_entry(&self) -> f64 {
        self.inner.state.avg_entry
    }

    #[getter]
    fn fee_rate(&self) -> f64 {
        self.inner.fee_rate
    }

    #[getter]
    fn slippage_bps(&self) -> f64 {
        self.inner.slippage_bps
    }

    #[setter]
    fn set_cash(&mut self, v: f64) {
        self.inner.state.cash = v;
    }

    #[setter]
    fn set_position_qty(&mut self, v: f64) {
        self.inner.state.position_qty = v;
    }

    #[setter]
    fn set_avg_entry(&mut self, v: f64) {
        self.inner.state.avg_entry = v;
    }

    fn mark_equity(&self, mark_price: f64) -> f64 {
        self.inner.mark_equity(mark_price)
    }

    /// Returns a dict matching the Python broker fill (or `None` on no-op).
    #[pyo3(signature = (side, price, size_pct=1.0))]
    fn execute_market<'py>(
        &mut self,
        py: Python<'py>,
        side: &str,
        price: f64,
        size_pct: f64,
    ) -> PyResult<Option<PyObject>> {
        match self.inner.execute_market(side, price, size_pct) {
            None => Ok(None),
            Some(f) => {
                let d = PyDict::new(py);
                d.set_item("side", f.side)?;
                d.set_item("price", f.price)?;
                d.set_item("qty", f.qty)?;
                d.set_item("fee", f.fee)?;
                Ok(Some(d.into_py(py)))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// run_backtest pyfunction — main entry point used by the Python shim.
// ---------------------------------------------------------------------------

/// Build the metrics dict identical to `backtest.metrics.summarize_metrics`.
///
/// This is a Rust port; the formula is the same and numerical parity is
/// covered by `tests/test_genericbt_core_parity.py`.
fn summarize_metrics_dict<'py>(
    py: Python<'py>,
    initial_cash: f64,
    final_equity: f64,
    equity_curve: &[f64],
    trade_pnls: &[f64],
) -> PyResult<PyObject> {
    let total_return = if initial_cash > 0.0 {
        (final_equity - initial_cash) / initial_cash
    } else {
        0.0
    };
    let mdd = max_drawdown(equity_curve);

    let d = PyDict::new(py);
    d.set_item("initial_cash", initial_cash)?;
    d.set_item("final_equity", final_equity)?;
    d.set_item("total_return", total_return)?;
    d.set_item("max_drawdown", mdd)?;
    d.set_item("sharpe", equity_sharpe_ratio(equity_curve))?;
    d.set_item("sortino", sortino_ratio(equity_curve))?;
    d.set_item("calmar", calmar_ratio(total_return, mdd))?;
    d.set_item("ulcer_index", ulcer_index(equity_curve))?;
    d.set_item("win_rate", win_rate(trade_pnls))?;
    d.set_item("profit_factor", profit_factor(trade_pnls))?;
    d.set_item("num_trades", trade_pnls.len() as f64)?;
    Ok(d.into_py(py))
}

fn returns_from_equity(equity_curve: &[f64]) -> Vec<f64> {
    let mut r = Vec::with_capacity(equity_curve.len());
    for i in 1..equity_curve.len() {
        let prev = equity_curve[i - 1];
        let cur = equity_curve[i];
        if prev <= 0.0 {
            continue;
        }
        r.push((cur - prev) / prev);
    }
    r
}

fn max_drawdown(equity_curve: &[f64]) -> f64 {
    if equity_curve.is_empty() {
        return 0.0;
    }
    let mut peak = equity_curve[0];
    let mut mdd = 0.0_f64;
    for &v in equity_curve.iter() {
        if v > peak {
            peak = v;
        }
        if peak > 0.0 {
            let dd = (peak - v) / peak;
            if dd > mdd {
                mdd = dd;
            }
        }
    }
    mdd
}

fn equity_sharpe_ratio(equity_curve: &[f64]) -> f64 {
    if equity_curve.len() < 3 {
        return 0.0;
    }
    let returns = returns_from_equity(equity_curve);
    if returns.len() < 2 {
        return 0.0;
    }
    let mean: f64 = returns.iter().sum::<f64>() / returns.len() as f64;
    let var: f64 = returns
        .iter()
        .map(|r| (r - mean) * (r - mean))
        .sum::<f64>()
        / (returns.len() as f64 - 1.0);
    let std = var.sqrt();
    if std == 0.0 {
        return 0.0;
    }
    (mean / std) * (252.0_f64).sqrt()
}

fn sortino_ratio(equity_curve: &[f64]) -> f64 {
    let returns = returns_from_equity(equity_curve);
    if returns.len() < 2 {
        return 0.0;
    }
    let mean: f64 = returns.iter().sum::<f64>() / returns.len() as f64;
    let negatives: Vec<f64> = returns.iter().copied().filter(|r| *r < 0.0).collect();
    if negatives.is_empty() {
        return if mean <= 0.0 { 0.0 } else { 999.0 };
    }
    let downside_var: f64 =
        negatives.iter().map(|r| r * r).sum::<f64>() / negatives.len() as f64;
    let downside_std = downside_var.sqrt();
    if downside_std == 0.0 {
        return 0.0;
    }
    (mean / downside_std) * (252.0_f64).sqrt()
}

fn calmar_ratio(total_return: f64, mdd: f64) -> f64 {
    if mdd <= 0.0 {
        return if total_return <= 0.0 { 0.0 } else { 999.0 };
    }
    total_return / mdd
}

fn ulcer_index(equity_curve: &[f64]) -> f64 {
    if equity_curve.is_empty() {
        return 0.0;
    }
    let mut peak = equity_curve[0];
    let mut squared = 0.0_f64;
    let mut count = 0_u64;
    for &v in equity_curve.iter() {
        if v > peak {
            peak = v;
        }
        if peak > 0.0 {
            let dd = (peak - v) / peak;
            squared += dd * dd;
            count += 1;
        }
    }
    if count == 0 {
        return 0.0;
    }
    (squared / count as f64).sqrt()
}

fn win_rate(trade_pnls: &[f64]) -> f64 {
    if trade_pnls.is_empty() {
        return 0.0;
    }
    let wins = trade_pnls.iter().filter(|p| **p > 0.0).count();
    wins as f64 / trade_pnls.len() as f64
}

fn profit_factor(trade_pnls: &[f64]) -> f64 {
    let gross_profit: f64 = trade_pnls.iter().copied().filter(|p| *p > 0.0).sum();
    let gross_loss: f64 = trade_pnls
        .iter()
        .copied()
        .filter(|p| *p < 0.0)
        .sum::<f64>()
        .abs();
    if gross_loss == 0.0 {
        return if gross_profit > 0.0 { 999.0 } else { 0.0 };
    }
    gross_profit / gross_loss
}

/// Public pyfunction: execute the bar loop with the supplied strategy.
///
/// Inputs:
///   * `config` – dict reflecting `backtest.engine.EngineConfig` fields
///     (only `initial_cash`, `fee_rate`, `slippage_bps`, `loop_seconds`,
///     `events_mode`, `snapshot_seconds`, `initial_state` are consulted).
///   * `strategy` – an already-instantiated strategy instance; the shim
///     calls `on_start` before invoking us. `import_state` is also expected
///     to have been called by the shim.
///   * `candles` – pre-processed list of candle dicts (Heikin-Ashi,
///     `price_source`, `sma_*` columns already filled).
///   * `run_id`, `trial_id` – metadata copied straight through to the
///     output dict.
///
/// Returns: a Python dict with keys `metrics`, `events`, `equity_curve`,
/// `final_state`, `run_id`, `trial_id` (parity with the Python engine).
#[pyfunction]
#[pyo3(signature = (config, strategy, candles, run_id=None, trial_id=None))]
fn run_backtest(
    py: Python<'_>,
    config: &PyDict,
    strategy: &PyAny,
    candles: &PyList,
    run_id: Option<i64>,
    trial_id: Option<i64>,
) -> PyResult<PyObject> {
    let cfg = parse_engine_config(config)?;

    let mut broker = SpotBroker::new(cfg.initial_cash, cfg.fee_rate, cfg.slippage_bps);
    if let Some((cash, pos, avg)) = extract_initial_state_broker(config)? {
        if !cash.is_nan() {
            broker.state.cash = cash;
        }
        if !pos.is_nan() {
            broker.state.position_qty = if pos < 0.0 { 0.0 } else { pos };
        }
        if !avg.is_nan() {
            broker.state.avg_entry = if avg < 0.0 { 0.0 } else { avg };
        }
        if broker.state.position_qty <= 0.0 {
            broker.state.avg_entry = 0.0;
        }
    }

    let outcome = engine::run_loop(py, &cfg, strategy, candles, broker, trial_id, run_id)?;

    let final_equity = outcome.broker.mark_equity(outcome.final_px);
    let metrics = summarize_metrics_dict(
        py,
        cfg.initial_cash,
        final_equity,
        &outcome.equity_curve,
        &outcome.trade_pnls,
    )?;

    let events_list = PyList::empty(py);
    for ev in outcome.events.iter() {
        events_list.append(ev)?;
    }

    let final_state = PyDict::new(py);
    let broker_state = PyDict::new(py);
    broker_state.set_item("cash", outcome.broker.state.cash)?;
    broker_state.set_item("position_qty", outcome.broker.state.position_qty)?;
    broker_state.set_item("avg_entry", outcome.broker.state.avg_entry)?;
    final_state.set_item("broker", broker_state)?;

    let strategy_state = strategy.call_method0("export_state")?;
    final_state.set_item("strategy", strategy_state)?;

    let last_price = if candles.is_empty() {
        0.0
    } else {
        outcome.final_px
    };
    final_state.set_item("last_price", last_price)?;
    final_state.set_item("final_equity", final_equity)?;

    let out = PyDict::new(py);
    out.set_item("metrics", metrics)?;
    out.set_item("events", events_list)?;
    out.set_item("equity_curve", outcome.equity_curve)?;
    out.set_item("final_state", final_state)?;
    out.set_item("run_id", run_id)?;
    out.set_item("trial_id", trial_id)?;
    Ok(out.into_py(py))
}

// ---------------------------------------------------------------------------
// pymodule entry point
// ---------------------------------------------------------------------------

/// The pyo3 extension module. Importable as `genericbt_core._genericbt_core`
/// once built by maturin (the `pyproject.toml` `[tool.maturin] module-name`
/// determines the on-disk name).
#[pymodule]
fn _genericbt_core(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_backtest, m)?)?;
    m.add_function(wrap_pyfunction!(sma, m)?)?;
    m.add_function(wrap_pyfunction!(apply_indicators, m)?)?;
    m.add_function(wrap_pyfunction!(apply_heikin_ashi, m)?)?;
    m.add_function(wrap_pyfunction!(apply_candle_source, m)?)?;
    m.add_class::<SpotBrokerRs>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
