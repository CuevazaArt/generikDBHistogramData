"""Append-only Parquet sink for run events.

Trades large `events_mode=full` runs from SQLite (where millions of `hold`
rows cripple write throughput) to a per-run Parquet file under
`reports/entregables/runs/run_<id>/events.parquet`.

`pyarrow` is optional. When unavailable, callers should fall back to the
SQLite path; `dump_events_to_parquet` returns `None` in that case.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

try:
    import pyarrow as pa  # type: ignore[import-not-found]
    import pyarrow.parquet as pq  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency
    pa = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]


_COLUMNS = [
    "trial_id",
    "seq",
    "event_time",
    "event_type",
    "side",
    "price",
    "qty",
    "cash",
    "equity",
    "position_qty",
    "payload_json",
]


def is_available() -> bool:
    return pa is not None and pq is not None


def _event_to_dict(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trial_id": e.get("trial_id"),
        "seq": int(e["seq"]),
        "event_time": int(e["event_time"]) if e.get("event_time") is not None else None,
        "event_type": str(e["event_type"]),
        "side": e.get("side"),
        "price": float(e["price"]) if e.get("price") is not None else None,
        "qty": float(e["qty"]) if e.get("qty") is not None else None,
        "cash": float(e["cash"]) if e.get("cash") is not None else None,
        "equity": float(e["equity"]) if e.get("equity") is not None else None,
        "position_qty": float(e["position_qty"]) if e.get("position_qty") is not None else None,
        "payload_json": e.get("payload_json")
        if isinstance(e.get("payload_json"), str)
        else None,
    }


def dump_events_to_parquet(
    output_path: str,
    events: Iterable[Dict[str, Any]],
    batch_size: int = 50000,
) -> str | None:
    """Write events to a Parquet file in append-friendly batches.

    Returns the absolute path of the written file, or None if pyarrow is
    not installed. Existing files at `output_path` are overwritten.
    """
    if not is_available():
        return None
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    schema = pa.schema(
        [
            ("trial_id", pa.int64()),
            ("seq", pa.int64()),
            ("event_time", pa.int64()),
            ("event_type", pa.string()),
            ("side", pa.string()),
            ("price", pa.float64()),
            ("qty", pa.float64()),
            ("cash", pa.float64()),
            ("equity", pa.float64()),
            ("position_qty", pa.float64()),
            ("payload_json", pa.string()),
        ]
    )
    writer = pq.ParquetWriter(output_path, schema, compression="zstd")
    try:
        buffer: List[Dict[str, Any]] = []
        for e in events:
            buffer.append(_event_to_dict(e))
            if len(buffer) >= batch_size:
                table = pa.Table.from_pylist(buffer, schema=schema)
                writer.write_table(table)
                buffer.clear()
        if buffer:
            table = pa.Table.from_pylist(buffer, schema=schema)
            writer.write_table(table)
    finally:
        writer.close()
    return os.path.abspath(output_path)
