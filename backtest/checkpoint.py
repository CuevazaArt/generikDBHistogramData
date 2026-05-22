"""Checkpoint serialisation for engine warm-restart / resume.

A checkpoint is a small JSON document that captures everything needed to
restart the bar loop from a previously-processed candle without losing
positions, fills, or in-flight strategy state. Checkpoints are intentionally
tiny (a few kilobytes at most) and emitted at user-controlled intervals
(``checkpoint_every_bars`` or ``checkpoint_every_sim_seconds``); the cost is
dominated by `strategy.export_state()` plus a single atomic file rename, so
even per-bar emission is feasible if a user wants belt-and-braces durability.

The on-disk layout matches the redesign plan:

    data/checkpoints/run_<id>/cp_<sim_ts>.json

where ``<sim_ts>`` is the candle ``open_time`` at write time. The directory
is created by :class:`backtest.storage_paths.StoragePaths.ensure_run_layout`;
this module never assumes the directory exists when called and uses
``os.makedirs(..., exist_ok=True)`` defensively through
:func:`backtest.storage_paths.tmp_then_rename`.

Schema parity with the PostgreSQL ``meta.checkpoints`` JSONB column is kept
so the same payload can be ingested by the coordinator later (Fase 3).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.storage_paths import tmp_then_rename


__all__ = [
    "Checkpoint",
    "read_checkpoint",
    "write_checkpoint",
    "latest_checkpoint_path",
    "now_iso_utc",
]


# Numeric form of the canonical filename. The integer captures the sim_ts;
# ordering by it gives us the latest checkpoint without needing to read any
# JSON. We also tolerate (and prefer) higher sim_ts on tie via filename.
_CP_FILENAME_RE = re.compile(r"^cp_(?P<sim_ts>-?\d+)\.json$")


def now_iso_utc() -> str:
    """ISO-8601 timestamp in UTC, second precision (matches ops audit log)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Checkpoint:
    """Snapshot of an in-progress run.

    Field meanings:

    * ``run_id`` — the ``meta.runs`` row id (when known). ``-1`` for ad-hoc
      runs that have not been registered yet.
    * ``sim_ts`` — last processed candle's ``open_time`` in ms.
    * ``candle_offset`` — index into the candle iterator. On resume we slice
      ``candles[candle_offset + 1:]`` so the next loop iteration is the bar
      AFTER the one persisted here.
    * ``broker_state`` — ``{"cash", "position_qty", "avg_entry"}``.
    * ``strategy_state`` — opaque dict from ``strategy.export_state()``.
    * ``seq`` — last emitted event sequence number.
    * ``last_exec_ts`` — value of the ``loop_seconds`` clamp at write time
      (``None`` if not applicable).
    * ``last_snapshot_ts`` — value of the equity-snapshot clamp (``None`` if
      events_mode is not ``lite``).
    * ``last_trade_entry`` — pending ``(entry_price, entry_qty)`` from an
      open buy that has not yet been matched with a sell. ``None`` when no
      buy is currently open. Stored as a tuple so JSON round-tripping is
      lossless.
    * ``created_at`` — wall-clock UTC when the file was written.
    * ``engine_kind`` — ``"python"`` or ``"rust"``; the resume path needs
      this to refuse cross-engine checkpoints if they ever diverge.
    * ``engine_version`` — free-form version stamp.
    """

    run_id: int
    sim_ts: int
    candle_offset: int
    broker_state: Dict[str, Any]
    strategy_state: Dict[str, Any]
    seq: int
    last_exec_ts: Optional[int]
    last_snapshot_ts: Optional[int]
    last_trade_entry: Optional[Tuple[float, float]]
    created_at: str = field(default_factory=now_iso_utc)
    engine_kind: str = "python"
    engine_version: str = "0.0.0"

    def to_dict(self) -> Dict[str, Any]:
        # asdict preserves the tuple as a list; we keep that shape on disk
        # because JSON has no tuple type. read_checkpoint reverses it.
        d = asdict(self)
        if self.last_trade_entry is not None:
            d["last_trade_entry"] = [float(self.last_trade_entry[0]), float(self.last_trade_entry[1])]
        return d

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Checkpoint":
        lte = payload.get("last_trade_entry")
        lte_tuple: Optional[Tuple[float, float]]
        if lte is None:
            lte_tuple = None
        else:
            # Accept either list (from JSON) or tuple (from in-memory transfer).
            lte_tuple = (float(lte[0]), float(lte[1]))
        return cls(
            run_id=int(payload["run_id"]),
            sim_ts=int(payload["sim_ts"]),
            candle_offset=int(payload["candle_offset"]),
            broker_state=dict(payload.get("broker_state") or {}),
            strategy_state=dict(payload.get("strategy_state") or {}),
            seq=int(payload.get("seq", 0)),
            last_exec_ts=(
                int(payload["last_exec_ts"])
                if payload.get("last_exec_ts") is not None
                else None
            ),
            last_snapshot_ts=(
                int(payload["last_snapshot_ts"])
                if payload.get("last_snapshot_ts") is not None
                else None
            ),
            last_trade_entry=lte_tuple,
            created_at=str(payload.get("created_at") or now_iso_utc()),
            engine_kind=str(payload.get("engine_kind") or "python"),
            engine_version=str(payload.get("engine_version") or "0.0.0"),
        )


def write_checkpoint(path: str, cp: Checkpoint) -> None:
    """Persist ``cp`` to ``path`` atomically (tmp file then rename).

    Uses :func:`backtest.storage_paths.tmp_then_rename` so partial writes
    are never visible to other processes that might be tailing the
    checkpoint directory.
    """
    payload = json.dumps(cp.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    with tmp_then_rename(path) as tmp_path:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)


def read_checkpoint(path: str) -> Checkpoint:
    """Load a checkpoint from disk. Raises ``FileNotFoundError`` if missing."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return Checkpoint.from_dict(payload)


def latest_checkpoint_path(checkpoints_dir: str) -> Optional[str]:
    """Return the highest-``sim_ts`` checkpoint in ``checkpoints_dir`` or None.

    Ordering is by the integer embedded in the filename (``cp_<sim_ts>.json``).
    Files that do not match the canonical naming are ignored. Returns
    ``None`` when the directory does not exist or holds no matching files.
    """
    if not checkpoints_dir or not os.path.isdir(checkpoints_dir):
        return None
    candidates: List[Tuple[int, str]] = []
    for name in os.listdir(checkpoints_dir):
        match = _CP_FILENAME_RE.match(name)
        if not match:
            continue
        try:
            ts = int(match.group("sim_ts"))
        except ValueError:
            continue
        candidates.append((ts, os.path.join(checkpoints_dir, name)))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p[0])
    return candidates[-1][1]
