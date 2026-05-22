//! Checkpoint serialisation for the Rust engine.
//!
//! Mirrors `backtest.checkpoint.Checkpoint` 1:1 so a file written by the
//! Rust loop can be loaded by the Python loop and vice versa. The on-disk
//! format is JSON (small, human-inspectable, schema-compatible with the
//! `meta.checkpoints` PostgreSQL JSONB column).
//!
//! `write_checkpoint` uses a `<basename>.<rand>.tmp` sidecar + `std::fs::rename`
//! so partial writes are never visible to readers (parity with the Python
//! `tmp_then_rename` context manager).
//!
//! NOTE: this module is intentionally pyo3-free. The bar loop in
//! `engine.rs` calls into Python only to fetch `strategy.export_state()` /
//! `strategy.import_state(...)`; everything else here is plain Rust IO.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// Serialised checkpoint payload. Field names match the Python `Checkpoint`
/// dataclass so files round-trip across engines.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CheckpointRs {
    pub run_id: i64,
    pub sim_ts: i64,
    pub candle_offset: i64,
    pub broker_state: serde_json::Value,
    pub strategy_state: serde_json::Value,
    pub seq: i64,
    pub last_exec_ts: Option<i64>,
    pub last_snapshot_ts: Option<i64>,
    pub last_trade_entry: Option<(f64, f64)>,
    pub created_at: String,
    pub engine_kind: String,
    pub engine_version: String,
}

/// Build a `<target>.<nanos>.tmp` sidecar path on the same directory.
///
/// We avoid pulling in the `tempfile` crate to keep the dependency surface
/// small; nanos-since-epoch is unique enough for a per-run writer.
fn sidecar_path(target: &Path) -> PathBuf {
    let mut name = target
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_default();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    name.push(format!(".{}.tmp", nanos));
    let parent = target.parent().unwrap_or_else(|| Path::new("."));
    parent.join(name)
}

/// Persist `cp` to `path` atomically (tmp file + rename).
pub fn write_checkpoint(path: &str, cp: &CheckpointRs) -> io::Result<()> {
    let target = Path::new(path);
    if let Some(parent) = target.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let tmp = sidecar_path(target);
    {
        let mut fh = fs::File::create(&tmp)?;
        let bytes = serde_json::to_vec_pretty(cp).map_err(io::Error::from)?;
        fh.write_all(&bytes)?;
        fh.sync_all()?;
    }
    // `rename` is atomic on the same filesystem on every supported platform.
    match fs::rename(&tmp, target) {
        Ok(()) => Ok(()),
        Err(e) => {
            // Best-effort cleanup of the sidecar so we never leave a partial
            // file behind even if the rename fails.
            let _ = fs::remove_file(&tmp);
            Err(e)
        }
    }
}

/// Load a checkpoint from disk. Returns the same Rust error type as the
/// `fs` family so callers can match on `ErrorKind::NotFound`.
pub fn read_checkpoint(path: &str) -> io::Result<CheckpointRs> {
    let bytes = fs::read(path)?;
    serde_json::from_slice(&bytes).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    fn temp_path(name: &str) -> PathBuf {
        let mut p = env::temp_dir();
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("genericbt_cp_test_{}_{}.json", nanos, name));
        p
    }

    #[test]
    fn roundtrip_minimal() {
        let cp = CheckpointRs {
            run_id: 42,
            sim_ts: 1700000000000,
            candle_offset: 99,
            broker_state: serde_json::json!({"cash": 10_000.0, "position_qty": 0.0, "avg_entry": 0.0}),
            strategy_state: serde_json::json!({}),
            seq: 7,
            last_exec_ts: Some(1700000000000),
            last_snapshot_ts: None,
            last_trade_entry: None,
            created_at: "2026-05-22T10:00:00+00:00".into(),
            engine_kind: "rust".into(),
            engine_version: "0.2.0".into(),
        };
        let path = temp_path("min");
        let s = path.to_string_lossy().to_string();
        write_checkpoint(&s, &cp).expect("write_checkpoint");
        let loaded = read_checkpoint(&s).expect("read_checkpoint");
        assert_eq!(loaded.run_id, cp.run_id);
        assert_eq!(loaded.sim_ts, cp.sim_ts);
        assert_eq!(loaded.candle_offset, cp.candle_offset);
        assert_eq!(loaded.seq, cp.seq);
        assert_eq!(loaded.engine_kind, "rust");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn roundtrip_with_trade_entry() {
        let cp = CheckpointRs {
            run_id: 1,
            sim_ts: 17,
            candle_offset: 0,
            broker_state: serde_json::json!({"cash": 1.0, "position_qty": 2.0, "avg_entry": 3.0}),
            strategy_state: serde_json::json!({"k": "v"}),
            seq: 0,
            last_exec_ts: None,
            last_snapshot_ts: Some(99),
            last_trade_entry: Some((1.25, 0.5)),
            created_at: "2026-05-22T10:00:00+00:00".into(),
            engine_kind: "rust".into(),
            engine_version: "0.2.0".into(),
        };
        let path = temp_path("trade");
        let s = path.to_string_lossy().to_string();
        write_checkpoint(&s, &cp).expect("write_checkpoint");
        let loaded = read_checkpoint(&s).expect("read_checkpoint");
        let (lp, lq) = loaded.last_trade_entry.expect("trade entry");
        assert!((lp - 1.25).abs() < 1e-12);
        assert!((lq - 0.5).abs() < 1e-12);
        let _ = fs::remove_file(&path);
    }
}
