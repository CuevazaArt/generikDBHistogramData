//! Streaming backtest loop (Rust side).
//!
//! Frontier with Python:
//!   * The strategy stays in Python (`StrategyBase.on_bar`). We acquire the
//!     GIL only when actually invoking the callback (the loop itself runs
//!     without spawning new threads — the GIL stays held by the caller — but
//!     the broker / indicator math is pure Rust and never touches Python
//!     objects on the hot path).
//!   * Candles arrive as a Python list of dicts. We pull only the keys we
//!     need (`open_time`, `price_source` or `close`) per bar; everything
//!     else is left untouched in the dict and made visible to the Python
//!     strategy via the unchanged `StrategyContext.candle` / `.candles`.
//!
//! Events are emitted directly as Python dicts to keep the wire format
//! identical to `backtest.engine.Event.to_record()`.

#![allow(deprecated)]

use std::path::Path;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::broker::SpotBroker;
use crate::checkpoint::{read_checkpoint, write_checkpoint, CheckpointRs};
use crate::types::{EngineConfig, EventsMode};

const ENGINE_KIND_RUST: &str = "rust";
const ENGINE_VERSION_RUST: &str = "0.2.0";

/// Minimal ISO-8601 UTC stamp without pulling in `chrono`. Matches the
/// Python `datetime.now(UTC).replace(microsecond=0).isoformat()` shape
/// closely enough for audit purposes; precise formatting is not load-bearing
/// (the field is opaque metadata).
fn current_iso_utc() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Convert seconds-since-epoch to a date string using a minimal civil
    // calendar implementation (Howard Hinnant's algorithm). Avoids chrono.
    let (year, month, day, hour, minute, second) = epoch_seconds_to_civil(secs as i64);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        year, month, day, hour, minute, second
    )
}

fn epoch_seconds_to_civil(z: i64) -> (i64, u32, u32, u32, u32, u32) {
    let day_seconds = 86_400_i64;
    let days = z.div_euclid(day_seconds);
    let rem = z.rem_euclid(day_seconds);
    let hour = (rem / 3600) as u32;
    let minute = ((rem % 3600) / 60) as u32;
    let second = (rem % 60) as u32;

    // Howard Hinnant's "days_from_civil" inverted.
    let z = days + 719_468;
    let era = if z >= 0 { z / 146_097 } else { (z - 146_096) / 146_097 };
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = (yoe as i64) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { (mp + 3) as u32 } else { (mp - 9) as u32 };
    let year = if m <= 2 { y + 1 } else { y };
    (year, m, d, hour, minute, second)
}

/// Result tuple consumed by the pyo3 wrapper. We keep the Python objects
/// (event dicts) inline so the wrapper can drop them straight into a
/// `PyList` without a second conversion pass.
pub struct EngineRunOutcome {
    pub events: Vec<PyObject>,
    pub equity_curve: Vec<f64>,
    pub trade_pnls: Vec<f64>,
    pub broker: SpotBroker,
    pub final_px: f64,
}

/// Read a float from a candle dict, falling back to `close` if the key is
/// missing. Mirrors `float(candle.get("price_source", candle["close"]))`.
fn read_price_source(candle: &PyAny) -> PyResult<f64> {
    let d: &PyDict = candle.downcast()?;
    if let Some(v) = d.get_item("price_source")? {
        if !v.is_none() {
            return v.extract();
        }
    }
    // SAFETY: parity with Python `candle["close"]` — KeyError if missing.
    d.get_item("close")?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("close"))?
        .extract()
}

fn read_open_time(candle: &PyAny) -> PyResult<i64> {
    let d: &PyDict = candle.downcast()?;
    d.get_item("open_time")?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("open_time"))?
        .extract()
}

/// Construct one `StrategyContext` Python object per bar. Done via the
/// already-imported class object (kept warm across the loop).
fn build_context<'py>(
    py: Python<'py>,
    ctx_cls: &'py PyAny,
    index: usize,
    candle: &'py PyAny,
    candles: &'py PyList,
    cash: f64,
    position_qty: f64,
    avg_entry: f64,
    equity: f64,
) -> PyResult<&'py PyAny> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("index", index)?;
    kwargs.set_item("candle", candle)?;
    kwargs.set_item("candles", candles)?;
    kwargs.set_item("cash", cash)?;
    kwargs.set_item("position_qty", position_qty)?;
    kwargs.set_item("avg_entry", avg_entry)?;
    kwargs.set_item("equity", equity)?;
    ctx_cls.call((), Some(kwargs))
}

/// Build a fill event dict matching `Event.to_record()`.
#[allow(clippy::too_many_arguments)]
fn fill_event_dict<'py>(
    py: Python<'py>,
    seq: i64,
    event_time: i64,
    side: &str,
    price: f64,
    qty: f64,
    cash: f64,
    equity: f64,
    position_qty: f64,
    reason: &str,
    fee: f64,
    metadata: &PyAny,
    trial_id: Option<i64>,
) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("seq", seq)?;
    d.set_item("event_time", event_time)?;
    d.set_item("event_type", "fill")?;
    d.set_item("side", side)?;
    d.set_item("price", price)?;
    d.set_item("qty", qty)?;
    d.set_item("cash", cash)?;
    d.set_item("equity", equity)?;
    d.set_item("position_qty", position_qty)?;

    let payload = PyDict::new(py);
    payload.set_item("reason", reason)?;
    payload.set_item("fee", fee)?;
    // Spread strategy-provided metadata mapping into payload (Python: **metadata).
    if !metadata.is_none() {
        let m: &PyDict = metadata.downcast()?;
        for (k, v) in m.iter() {
            payload.set_item(k, v)?;
        }
    }
    d.set_item("payload", payload)?;
    d.set_item("trial_id", trial_id)?;
    Ok(d.into_py(py))
}

#[allow(clippy::too_many_arguments)]
fn reject_event_dict<'py>(
    py: Python<'py>,
    seq: i64,
    event_time: i64,
    side: &str,
    cash: f64,
    equity: f64,
    position_qty: f64,
    reason: &str,
    metadata: &PyAny,
    trial_id: Option<i64>,
) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("seq", seq)?;
    d.set_item("event_time", event_time)?;
    d.set_item("event_type", "order_rejected")?;
    d.set_item("side", side)?;
    d.set_item("price", py.None())?;
    d.set_item("qty", py.None())?;
    d.set_item("cash", cash)?;
    d.set_item("equity", equity)?;
    d.set_item("position_qty", position_qty)?;

    let payload = PyDict::new(py);
    payload.set_item("reason", reason)?;
    if !metadata.is_none() {
        let m: &PyDict = metadata.downcast()?;
        for (k, v) in m.iter() {
            payload.set_item(k, v)?;
        }
    }
    d.set_item("payload", payload)?;
    d.set_item("trial_id", trial_id)?;
    Ok(d.into_py(py))
}

/// Serialise an arbitrary Python object to a `serde_json::Value` by way of
/// the standard library's `json.dumps`. Keeps the checkpoint payload in
/// sync with what `strategy.export_state()` would write from Python.
fn py_to_json(py: Python<'_>, obj: &PyAny) -> PyResult<serde_json::Value> {
    if obj.is_none() {
        return Ok(serde_json::Value::Null);
    }
    let json_mod = py.import("json")?;
    let dumped: String = json_mod
        .getattr("dumps")?
        .call1((obj,))?
        .extract()?;
    serde_json::from_str(&dumped).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("strategy state not JSON-able: {}", e))
    })
}

fn json_to_py<'py>(py: Python<'py>, value: &serde_json::Value) -> PyResult<&'py PyAny> {
    let json_mod = py.import("json")?;
    let raw = serde_json::to_string(value).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("cannot serialise json: {}", e))
    })?;
    json_mod.getattr("loads")?.call1((raw,))
}

/// Build a fresh `resume` audit event mirroring `Event.to_record()`.
#[allow(clippy::too_many_arguments)]
fn resume_event_dict<'py>(
    py: Python<'py>,
    seq: i64,
    sim_ts: i64,
    cash: f64,
    equity: f64,
    position_qty: f64,
    checkpoint_path: &str,
    candle_offset: i64,
    engine_kind: &str,
    engine_version: &str,
    trial_id: Option<i64>,
) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("seq", seq)?;
    d.set_item("event_time", sim_ts)?;
    d.set_item("event_type", "resume")?;
    d.set_item("side", py.None())?;
    d.set_item("price", py.None())?;
    d.set_item("qty", py.None())?;
    d.set_item("cash", cash)?;
    d.set_item("equity", equity)?;
    d.set_item("position_qty", position_qty)?;

    let payload = PyDict::new(py);
    payload.set_item("checkpoint_path", checkpoint_path)?;
    payload.set_item("candle_offset", candle_offset)?;
    payload.set_item("engine_kind", engine_kind)?;
    payload.set_item("engine_version", engine_version)?;
    d.set_item("payload", payload)?;
    d.set_item("trial_id", trial_id)?;
    Ok(d.into_py(py))
}

#[allow(clippy::too_many_arguments)]
fn snapshot_event_dict<'py>(
    py: Python<'py>,
    seq: i64,
    event_time: i64,
    event_type: &str,
    cash: f64,
    equity: f64,
    position_qty: f64,
    trial_id: Option<i64>,
) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("seq", seq)?;
    d.set_item("event_time", event_time)?;
    d.set_item("event_type", event_type)?;
    d.set_item("side", py.None())?;
    d.set_item("price", py.None())?;
    d.set_item("qty", py.None())?;
    d.set_item("cash", cash)?;
    d.set_item("equity", equity)?;
    d.set_item("position_qty", position_qty)?;
    d.set_item("payload", PyDict::new(py))?;
    d.set_item("trial_id", trial_id)?;
    Ok(d.into_py(py))
}

/// Drive the bar loop. Mirrors `backtest.engine.run_backtest` lines 142-247
/// 1:1 (events_mode handling, loop_seconds skip, trade_pnl accounting).
///
/// `strategy` must already have had `on_start` called on it. `broker` must
/// already reflect any warm-start seeding from `initial_state.broker`.
pub fn run_loop<'py>(
    py: Python<'py>,
    cfg: &EngineConfig,
    strategy: &PyAny,
    candles: &PyList,
    mut broker: SpotBroker,
    trial_id: Option<i64>,
    run_id: Option<i64>,
) -> PyResult<EngineRunOutcome> {
    let ctx_cls = py
        .import("backtest.strategy_base")?
        .getattr("StrategyContext")?;

    let mode = EventsMode::parse(&cfg.events_mode);
    let emit_holds = mode.emit_holds();
    let emit_snapshots = mode.emit_snapshots();
    let snapshot_step_ms: i64 = cfg.snapshot_seconds.max(1) * 1000;

    let n = candles.len();
    let mut events: Vec<PyObject> = Vec::new();
    let mut equity_curve: Vec<f64> = Vec::with_capacity(n);
    let mut trade_pnls: Vec<f64> = Vec::new();
    let mut seq: i64 = 0;
    let mut last_trade_entry: Option<(f64, f64)> = None;
    let mut last_exec_ts: Option<i64> = None;
    let mut last_snapshot_ts: Option<i64> = None;

    let loop_seconds_ms: Option<i64> = match cfg.loop_seconds {
        Some(s) if s > 0 => Some(s * 1000),
        _ => None,
    };

    // --- Fase 2: checkpointing knobs ---------------------------------
    let cp_every_bars = match cfg.checkpoint_every_bars {
        Some(v) if v > 0 => Some(v),
        _ => None,
    };
    let cp_every_sim_seconds = match cfg.checkpoint_every_sim_seconds {
        Some(v) if v > 0 => Some(v),
        _ => None,
    };
    let cp_dir: Option<&str> = if cp_every_bars.is_some() || cp_every_sim_seconds.is_some() {
        cfg.checkpoints_dir.as_deref()
    } else {
        None
    };
    let checkpointing_enabled = cp_dir.is_some()
        && (cp_every_bars.is_some() || cp_every_sim_seconds.is_some());
    let mut bars_since_cp: i64 = 0;
    let mut last_checkpoint_sim_ts: Option<i64> = None;

    // --- Fase 2: resume support ---------------------------------------
    let mut skip_until_index: i64 = -1;
    if let Some(path) = cfg.resume_from_checkpoint.as_deref() {
        let cp = read_checkpoint(path).map_err(|e| {
            pyo3::exceptions::PyOSError::new_err(format!(
                "failed to read checkpoint {}: {}",
                path, e
            ))
        })?;
        if let Some(map) = cp.broker_state.as_object() {
            if let Some(v) = map.get("cash").and_then(|x| x.as_f64()) {
                broker.state.cash = v;
            }
            if let Some(v) = map.get("position_qty").and_then(|x| x.as_f64()) {
                broker.state.position_qty = v.max(0.0);
            }
            if let Some(v) = map.get("avg_entry").and_then(|x| x.as_f64()) {
                broker.state.avg_entry = v.max(0.0);
            }
            if broker.state.position_qty <= 0.0 {
                broker.state.avg_entry = 0.0;
            }
        }
        let strat_state_py = json_to_py(py, &cp.strategy_state)?;
        strategy.call_method1("import_state", (strat_state_py,))?;
        seq = cp.seq;
        last_exec_ts = cp.last_exec_ts;
        last_snapshot_ts = cp.last_snapshot_ts;
        last_trade_entry = cp.last_trade_entry;
        skip_until_index = cp.candle_offset;

        let next_idx = (skip_until_index + 1) as usize;
        let resume_px = if (skip_until_index + 1) >= 0 && next_idx < n {
            read_price_source(candles.get_item(next_idx)?)?
        } else {
            0.0
        };
        let resume_equity = broker.mark_equity(resume_px);
        seq += 1;
        let evt = resume_event_dict(
            py,
            seq,
            cp.sim_ts,
            broker.state.cash,
            resume_equity,
            broker.state.position_qty,
            path,
            cp.candle_offset,
            &cp.engine_kind,
            &cp.engine_version,
            trial_id,
        )?;
        events.push(evt);
    }

    for i in 0..n {
        if skip_until_index >= 0 && (i as i64) <= skip_until_index {
            continue;
        }
        let candle = candles.get_item(i)?;
        let candle_ts = read_open_time(candle)?;

        if let (Some(step_ms), Some(last_ts)) = (loop_seconds_ms, last_exec_ts) {
            if candle_ts - last_ts < step_ms {
                continue;
            }
        }
        last_exec_ts = Some(candle_ts);

        // --- Fase 2 checkpoint trigger -------------------------------
        if checkpointing_enabled {
            bars_since_cp += 1;
            let bar_due = match cp_every_bars {
                Some(every) => bars_since_cp >= every,
                None => false,
            };
            let mut time_due = false;
            if let Some(every_s) = cp_every_sim_seconds {
                match last_checkpoint_sim_ts {
                    None => {
                        last_checkpoint_sim_ts = Some(candle_ts);
                    }
                    Some(prev) => {
                        if candle_ts - prev >= every_s * 1000 {
                            time_due = true;
                        }
                    }
                }
            }
            if bar_due || time_due {
                let strategy_state = py_to_json(py, strategy.call_method0("export_state")?)?;
                let broker_state_json = serde_json::json!({
                    "cash": broker.state.cash,
                    "position_qty": broker.state.position_qty,
                    "avg_entry": broker.state.avg_entry,
                });
                let cp = CheckpointRs {
                    run_id: run_id.unwrap_or(-1),
                    sim_ts: candle_ts,
                    candle_offset: (i as i64) - 1,
                    broker_state: broker_state_json,
                    strategy_state,
                    seq,
                    last_exec_ts,
                    last_snapshot_ts,
                    last_trade_entry,
                    created_at: current_iso_utc(),
                    engine_kind: ENGINE_KIND_RUST.into(),
                    engine_version: ENGINE_VERSION_RUST.into(),
                };
                let target = Path::new(cp_dir.unwrap_or("."))
                    .join(format!("cp_{}.json", candle_ts));
                if let Err(e) = write_checkpoint(target.to_string_lossy().as_ref(), &cp) {
                    return Err(pyo3::exceptions::PyOSError::new_err(format!(
                        "failed to write checkpoint {}: {}",
                        target.display(),
                        e
                    )));
                }
                last_checkpoint_sim_ts = Some(candle_ts);
                bars_since_cp = 0;
            }
        }

        let px = read_price_source(candle)?;
        let equity = broker.mark_equity(px);
        equity_curve.push(equity);

        let ctx = build_context(
            py,
            ctx_cls,
            i,
            candle,
            candles,
            broker.state.cash,
            broker.state.position_qty,
            broker.state.avg_entry,
            equity,
        )?;
        let signal = strategy.call_method1("on_bar", (ctx,))?;
        let action: String = signal.getattr("action")?.extract()?;

        if action == "buy" || action == "sell" {
            let size_pct: f64 = signal.getattr("size_pct")?.extract()?;
            let fill = broker.execute_market(&action, px, size_pct);
            seq += 1;
            let reason: String = signal.getattr("reason")?.extract()?;
            let metadata = signal.getattr("metadata")?;
            match fill {
                Some(f) => {
                    // Forward fill to the strategy as a Python dict, then
                    // record the canonical event.
                    let fill_dict = PyDict::new(py);
                    fill_dict.set_item("side", f.side)?;
                    fill_dict.set_item("price", f.price)?;
                    fill_dict.set_item("qty", f.qty)?;
                    fill_dict.set_item("fee", f.fee)?;
                    strategy.call_method1("on_fill", (fill_dict, signal, ctx))?;

                    let final_equity_after_fill = broker.mark_equity(px);
                    let evt = fill_event_dict(
                        py,
                        seq,
                        candle_ts,
                        f.side,
                        f.price,
                        f.qty,
                        broker.state.cash,
                        final_equity_after_fill,
                        broker.state.position_qty,
                        &reason,
                        f.fee,
                        metadata,
                        trial_id,
                    )?;
                    events.push(evt);

                    if f.side == "buy" {
                        last_trade_entry = Some((f.price, f.qty));
                    } else if f.side == "sell" {
                        if let Some((entry_price, entry_qty)) = last_trade_entry {
                            let qty = if entry_qty < f.qty { entry_qty } else { f.qty };
                            trade_pnls.push((f.price - entry_price) * qty);
                            last_trade_entry = None;
                        }
                    }
                }
                None => {
                    let final_equity_after_fill = broker.mark_equity(px);
                    let evt = reject_event_dict(
                        py,
                        seq,
                        candle_ts,
                        &action,
                        broker.state.cash,
                        final_equity_after_fill,
                        broker.state.position_qty,
                        &reason,
                        metadata,
                        trial_id,
                    )?;
                    events.push(evt);
                }
            }
        } else if emit_holds {
            seq += 1;
            let evt = snapshot_event_dict(
                py,
                seq,
                candle_ts,
                "hold",
                broker.state.cash,
                equity,
                broker.state.position_qty,
                trial_id,
            )?;
            events.push(evt);
        } else if emit_snapshots {
            let due = match last_snapshot_ts {
                None => true,
                Some(prev) => candle_ts - prev >= snapshot_step_ms,
            };
            if due {
                seq += 1;
                let evt = snapshot_event_dict(
                    py,
                    seq,
                    candle_ts,
                    "snapshot",
                    broker.state.cash,
                    equity,
                    broker.state.position_qty,
                    trial_id,
                )?;
                events.push(evt);
                last_snapshot_ts = Some(candle_ts);
            }
        }
    }

    strategy.call_method0("on_finish")?;

    // Parity with Python: final_px is from the LAST candle's `price_source`,
    // independent of any `loop_seconds` skipping that might have happened
    // earlier in the loop.
    let final_px = if n == 0 {
        0.0
    } else {
        read_price_source(candles.get_item(n - 1)?)?
    };
    Ok(EngineRunOutcome {
        events,
        equity_curve,
        trade_pnls,
        broker,
        final_px,
    })
}
