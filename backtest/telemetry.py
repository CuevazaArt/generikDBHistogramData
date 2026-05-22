"""Standard telemetry helpers for runs and orchestrators.

Emits JSONL system telemetry (CPU/RAM/disk-IO) and a small CSV summary per
run/wave so post-mortems do not require re-instrumenting code. Designed to
work with or without `psutil` (no-op if unavailable).

The contract is intentionally narrow: callers create a `TelemetryRecorder`
once, call `sample("phase_name")` periodically (or via a context manager),
and finally `close()` to flush totals.
"""
from __future__ import annotations

import csv
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TelemetryConfig:
    output_dir: str
    jsonl_name: str = "system.jsonl"
    csv_name: str = "system_telemetry.csv"


class TelemetryRecorder:
    """Persist CPU/RAM/IO samples per phase to JSONL + CSV.

    Use it like:

        rec = TelemetryRecorder(TelemetryConfig(output_dir=run_dir))
        rec.sample(phase="phase1")
        ...
        rec.close()
    """

    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        os.makedirs(self.config.output_dir, exist_ok=True)
        self._jsonl_path = os.path.join(self.config.output_dir, self.config.jsonl_name)
        self._csv_path = os.path.join(self.config.output_dir, self.config.csv_name)
        self._closed = False
        self._proc = psutil.Process() if psutil is not None else None
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
                if self._proc is not None:
                    self._proc.cpu_percent(interval=None)
            except Exception:
                pass
        self._samples: List[Dict[str, Any]] = []
        self._t0 = time.monotonic()

    def _snapshot(self, phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        snap: Dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "phase": phase,
            "elapsed_sec": round(time.monotonic() - self._t0, 3),
        }
        if psutil is not None:
            try:
                vm = psutil.virtual_memory()
                snap.update(
                    {
                        "host_cpu_pct": float(psutil.cpu_percent(interval=None)),
                        "host_ram_pct": float(vm.percent),
                        "host_ram_available_mb": round(vm.available / (1024 * 1024), 2),
                    }
                )
                if self._proc is not None:
                    snap.update(
                        {
                            "proc_cpu_pct": float(self._proc.cpu_percent(interval=None)),
                            "proc_rss_mb": round(self._proc.memory_info().rss / (1024 * 1024), 2),
                            "proc_threads": int(self._proc.num_threads()),
                        }
                    )
            except Exception:
                pass
        if extra:
            snap.update(extra)
        return snap

    def sample(self, phase: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._closed:
            return {}
        snap = self._snapshot(phase=phase, extra=extra)
        self._samples.append(snap)
        with open(self._jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return snap

    @contextmanager
    def phase(self, name: str):
        self.sample(phase=f"{name}:start")
        try:
            yield self
        finally:
            self.sample(phase=f"{name}:end")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._samples:
            return
        fieldnames = sorted({k for s in self._samples for k in s.keys()})
        with open(self._csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for s in self._samples:
                writer.writerow(s)


def estimate_workload(
    dataset_candles: int,
    n_trials: int,
    bytes_per_candle: int = 400,
    base_worker_mb: int = 250,
) -> Dict[str, Any]:
    """Cheap estimator for `--explain_only` previews.

    Returns rough RAM-per-worker and total-trial estimates so users can
    sanity-check whether a long run is even feasible before launching it.
    """
    per_worker_bytes = int(base_worker_mb * 1024 * 1024 + max(0, int(dataset_candles)) * int(bytes_per_candle))
    return {
        "dataset_candles": int(dataset_candles),
        "n_trials": int(n_trials),
        "estimated_worker_ram_mb": round(per_worker_bytes / (1024 * 1024), 2),
        "estimated_workers_within_8gb": max(1, int((8 * 1024 * 1024 * 1024) // max(1, per_worker_bytes))),
        "estimated_workers_within_16gb": max(1, int((16 * 1024 * 1024 * 1024) // max(1, per_worker_bytes))),
    }
