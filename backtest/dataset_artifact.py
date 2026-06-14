"""Generic, reusable dataset artifacts for strategy/bot runs.

Builds an intermediate artifact that captures:
- Requested data window (symbol/interval/start/end).
- Integrity diagnostics (row count + gap checks).
- Reproducibility metadata (git snapshot, timestamps).
- Prepared data files (Parquet cache pointers or JSONL snapshot fallback).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.data_cache import CACHE_ROOT_DEFAULT, is_available as parquet_available
from backtest.data_cache import materialize_window as materialize_parquet_window
from backtest.data_integrity import find_gaps, interval_step_ms, window_stats
from backtest.report_paths import dataset_report_dir
from backtest.repro import git_snapshot
from db import iter_query_klines

ARTIFACT_SCHEMA_VERSION = 1

_KLINE_COLUMNS = [
    "symbol",
    "interval",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore_field",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_artifact_name(symbol: str, interval: str, start_ts: int, end_ts: int) -> str:
    return f"{symbol.lower()}_{interval}_{int(start_ts)}_{int(end_ts)}"


def _row_to_payload(row: Tuple[Any, ...]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for idx, col in enumerate(_KLINE_COLUMNS):
        payload[col] = row[idx] if idx < len(row) else None
    return payload


def _write_jsonl_snapshot(
    *,
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    output_path: str,
) -> int:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    rows_written = 0
    with open(output_path, "w", encoding="utf-8") as fh:
        for row in iter_query_klines(
            db_path,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            fetch_size=20_000,
        ):
            fh.write(json.dumps(_row_to_payload(row), ensure_ascii=False) + "\n")
            rows_written += 1
    return rows_written


def prepare_dataset_artifact(
    *,
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    output_base: str = "reports",
    artifact_name: str | None = None,
    cache_root: str = CACHE_ROOT_DEFAULT,
    prefer_parquet_cache: bool = True,
    overwrite_cache: bool = False,
    max_gaps: int = 1000,
) -> Dict[str, Any]:
    """Prepare a generic dataset artifact and persist ``manifest.json``."""
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    if end_ts < start_ts:
        raise ValueError("end_ts must be >= start_ts")

    symbol_norm = str(symbol).upper().strip()
    interval_norm = str(interval).strip()
    artifact_name = (artifact_name or "").strip() or _default_artifact_name(
        symbol_norm, interval_norm, start_ts, end_ts
    )
    artifact_dir = dataset_report_dir(output_base, artifact_name, ensure_manifest=False)

    stats = window_stats(
        db_path=db_path,
        symbol=symbol_norm,
        interval=interval_norm,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    gaps = find_gaps(
        db_path=db_path,
        symbol=symbol_norm,
        interval=interval_norm,
        start_ts=start_ts,
        end_ts=end_ts,
        max_gaps=max(1, int(max_gaps)),
    )

    data_files: List[Dict[str, Any]] = []
    prepared_format = "jsonl"
    parquet_paths: List[str] = []
    can_use_parquet = bool(prefer_parquet_cache and parquet_available())
    if can_use_parquet:
        parquet_paths = materialize_parquet_window(
            db_path=db_path,
            symbol=symbol_norm,
            interval=interval_norm,
            start_ts=start_ts,
            end_ts=end_ts,
            cache_root=cache_root,
            overwrite=bool(overwrite_cache),
        )
        if parquet_paths:
            prepared_format = "parquet-cache"
            for path in parquet_paths:
                data_files.append(
                    {
                        "kind": "parquet-month-bucket",
                        "path": os.path.abspath(path),
                        "exists": os.path.exists(path),
                    }
                )

    snapshot_rows = None
    if not data_files:
        snapshot_path = os.path.join(artifact_dir, "window.jsonl")
        snapshot_rows = _write_jsonl_snapshot(
            db_path=db_path,
            symbol=symbol_norm,
            interval=interval_norm,
            start_ts=start_ts,
            end_ts=end_ts,
            output_path=snapshot_path,
        )
        data_files.append(
            {
                "kind": "jsonl-window-snapshot",
                "path": os.path.abspath(snapshot_path),
                "exists": os.path.exists(snapshot_path),
                "rows": int(snapshot_rows),
            }
        )

    manifest: Dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_kind": "dataset_window",
        "artifact_name": artifact_name,
        "created_at": _utc_now_iso(),
        "window": {
            "symbol": symbol_norm,
            "interval": interval_norm,
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
        "source": {
            "db_path": os.path.abspath(db_path),
            "cache_root": os.path.abspath(cache_root),
        },
        "prepared_data": {
            "format": prepared_format,
            "files": data_files,
        },
        "integrity": {
            "row_count": int(stats.count),
            "min_open_time": stats.min_open_time,
            "max_open_time": stats.max_open_time,
            "expected_step_ms": int(interval_step_ms(interval_norm)),
            "gap_count": len(gaps),
            "has_gaps": bool(gaps),
            "gaps": [{"start_ts": int(g0), "end_ts": int(g1)} for g0, g1 in gaps],
        },
        "reproducibility": {
            "git": git_snapshot(cwd=os.getcwd()),
        },
        "notes": {
            "parquet_available": parquet_available(),
            "parquet_cache_used": bool(can_use_parquet and parquet_paths),
        },
    }

    manifest_path = os.path.join(artifact_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return manifest


def verify_dataset_artifact(manifest_path: str) -> Dict[str, Any]:
    """Verify artifact manifest structure and prepared-file existence."""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    required_top = (
        "schema_version",
        "artifact_kind",
        "window",
        "source",
        "integrity",
        "prepared_data",
        "created_at",
        "reproducibility",
    )
    missing = [k for k in required_top if k not in payload]

    files = list((payload.get("prepared_data") or {}).get("files") or [])
    missing_files = []
    for item in files:
        p = str((item or {}).get("path") or "").strip()
        if not p or not os.path.exists(p):
            missing_files.append(p)

    status = "ok" if not missing and not missing_files else "invalid"
    return {
        "status": status,
        "manifest_path": os.path.abspath(manifest_path),
        "missing_keys": missing,
        "prepared_files_count": len(files),
        "missing_files": missing_files,
        "artifact_kind": payload.get("artifact_kind"),
        "window": payload.get("window") or {},
        "integrity": payload.get("integrity") or {},
    }

