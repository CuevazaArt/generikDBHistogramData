//! Engine-side data types mirrored on the Rust side.
//!
//! `EngineConfig` is deserialised from the Python `EngineConfig` dataclass
//! using `serde_json` once the dataclass is converted to a dict on the
//! pyo3 boundary. This avoids the cost of repeated `getattr` calls on the
//! hot path; the dict is parsed once at the start of `run_backtest`.

use serde::Deserialize;

/// Persistence aggressiveness for events.
///
/// * `Full`    – emit one event per candle (hold/fill/reject), legacy parity.
/// * `Lite`    – emit fills + rejects + periodic equity snapshots.
/// * `Minimal` – emit fills + rejects only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventsMode {
    Full,
    Lite,
    Minimal,
}

impl EventsMode {
    pub fn parse(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "lite" => Self::Lite,
            "minimal" => Self::Minimal,
            // "full" or anything else falls back to Full, same as Python.
            _ => Self::Full,
        }
    }

    pub fn emit_holds(self) -> bool {
        matches!(self, Self::Full)
    }

    pub fn emit_snapshots(self) -> bool {
        matches!(self, Self::Lite)
    }
}

/// Engine knobs extracted from the Python `EngineConfig` dataclass.
#[derive(Debug, Clone, Deserialize)]
pub struct EngineConfig {
    #[serde(default = "default_initial_cash")]
    pub initial_cash: f64,

    #[serde(default = "default_fee_rate")]
    pub fee_rate: f64,

    #[serde(default = "default_slippage_bps")]
    pub slippage_bps: f64,

    #[serde(default)]
    pub loop_seconds: Option<i64>,

    #[serde(default = "default_events_mode")]
    pub events_mode: String,

    #[serde(default = "default_snapshot_seconds")]
    pub snapshot_seconds: i64,

    // --- Fase 2: checkpointing -----------------------------------------
    #[serde(default)]
    pub checkpoint_every_bars: Option<i64>,

    #[serde(default)]
    pub checkpoint_every_sim_seconds: Option<i64>,

    #[serde(default)]
    pub checkpoints_dir: Option<String>,

    #[serde(default)]
    pub resume_from_checkpoint: Option<String>,
}

fn default_initial_cash() -> f64 {
    10_000.0
}
fn default_fee_rate() -> f64 {
    0.001
}
fn default_slippage_bps() -> f64 {
    2.0
}
fn default_events_mode() -> String {
    "full".to_string()
}
fn default_snapshot_seconds() -> i64 {
    3600
}

/// Lightweight signal returned by Python strategies. Constructed on every
/// bar; we keep it cheap by holding only the action discriminant + size.
#[derive(Debug, Clone)]
pub struct Signal {
    pub action: String,
    pub size_pct: f64,
    pub reason: String,
    // metadata is left on the Python side (PyObject); we just forward it.
}

/// Engine event record. We store events as Python dicts directly on the
/// hot path to avoid double-conversion. This type is kept for future
/// streaming output (Fase 2 will switch the event sink to Arrow).
#[derive(Debug, Clone)]
pub struct EventRecord {
    pub seq: i64,
    pub event_time: i64,
    pub event_type: String,
}
