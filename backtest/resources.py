"""Resource auto-calibration for safe parallel execution.

Provides:
- `detect_resources()` to inspect CPU/RAM (psutil optional, falls back to stdlib).
- `recommend_n_jobs()` returning a sane worker count given a mode and dataset hint.
- `EXECUTION_MODES` describing the three supported aggressiveness levels.

Designed to avoid the classic mistake of `n_jobs = cpu_count()` (or beyond) on
heavy workloads where SQLite contention and RAM pressure destroy throughput.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime guard
    psutil = None


EXECUTION_MODES = ("safe", "balanced", "max-stable", "adaptive_80")

# Fractions of CPU and RAM to dedicate to optimization workers per mode.
# `adaptive_80` is the new default for intensive/parallel workloads:
# it targets 80% of CPU/RAM and lets `ResourceGuard` keep us there.
_MODE_CPU_FRACTION = {"safe": 0.50, "balanced": 0.70, "max-stable": 0.85, "adaptive_80": 0.80}
_MODE_RAM_FRACTION = {"safe": 0.40, "balanced": 0.60, "max-stable": 0.75, "adaptive_80": 0.80}

# Empirical defaults assuming each worker loads ~1 candle slice in memory.
# Storing 1 candle ~ 200 bytes in dict form; we keep a safety multiplier.
_BYTES_PER_CANDLE_ESTIMATE = 400
_WORKER_BASE_RAM_BYTES = 250 * 1024 * 1024  # ~250 MB per Python worker baseline


@dataclass
class ResourceProfile:
    cpu_count: int
    ram_total_bytes: int | None
    ram_available_bytes: int | None

    def ram_total_gb(self) -> float | None:
        if self.ram_total_bytes is None:
            return None
        return round(self.ram_total_bytes / (1024 ** 3), 2)

    def ram_available_gb(self) -> float | None:
        if self.ram_available_bytes is None:
            return None
        return round(self.ram_available_bytes / (1024 ** 3), 2)


def detect_resources() -> ResourceProfile:
    cpu = max(1, os.cpu_count() or 1)
    if psutil is None:
        return ResourceProfile(cpu_count=cpu, ram_total_bytes=None, ram_available_bytes=None)
    vm = psutil.virtual_memory()
    return ResourceProfile(
        cpu_count=cpu,
        ram_total_bytes=int(vm.total),
        ram_available_bytes=int(vm.available),
    )


def estimate_worker_ram_bytes(dataset_candles: int | None) -> int:
    """Estimate the RAM cost of a single worker for a given dataset size."""
    if not dataset_candles or dataset_candles <= 0:
        return _WORKER_BASE_RAM_BYTES
    return _WORKER_BASE_RAM_BYTES + int(dataset_candles) * _BYTES_PER_CANDLE_ESTIMATE


def recommend_n_jobs(
    mode: str = "adaptive_80",
    dataset_candles: int | None = None,
    profile: ResourceProfile | None = None,
    explicit_cap: int | None = None,
) -> int:
    """Return a safe worker count for `mode`, bounded by available CPU/RAM.

    `mode` ∈ {"safe", "balanced", "max-stable", "adaptive_80"}.
    `dataset_candles` is used to estimate per-worker RAM.
    `explicit_cap` lets the caller hard-cap the result (e.g. user override).
    """
    mode = (mode or "adaptive_80").strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError(f"Unknown mode '{mode}'. Use one of: {', '.join(EXECUTION_MODES)}")

    prof = profile or detect_resources()
    cpu_fraction = _MODE_CPU_FRACTION[mode]
    ram_fraction = _MODE_RAM_FRACTION[mode]

    cpu_jobs = max(1, math.floor(prof.cpu_count * cpu_fraction))

    if prof.ram_available_bytes is not None:
        per_worker = estimate_worker_ram_bytes(dataset_candles)
        ram_budget = int(prof.ram_available_bytes * ram_fraction)
        ram_jobs = max(1, ram_budget // max(per_worker, 1))
    else:
        ram_jobs = cpu_jobs

    chosen = min(cpu_jobs, ram_jobs)
    if explicit_cap is not None and explicit_cap > 0:
        chosen = min(chosen, int(explicit_cap))
    return max(1, int(chosen))


def explain_recommendation(
    mode: str = "adaptive_80",
    dataset_candles: int | None = None,
    profile: ResourceProfile | None = None,
    explicit_cap: int | None = None,
) -> str:
    """Human-friendly explanation of how `n_jobs` was decided."""
    prof = profile or detect_resources()
    n_jobs = recommend_n_jobs(mode, dataset_candles, prof, explicit_cap)
    lines = [
        f"mode={mode}",
        f"cpu_count={prof.cpu_count}",
        f"ram_total_gb={prof.ram_total_gb()}",
        f"ram_available_gb={prof.ram_available_gb()}",
        f"dataset_candles={dataset_candles}",
        f"per_worker_ram_mb={round(estimate_worker_ram_bytes(dataset_candles) / (1024 ** 2), 1)}",
        f"recommended_n_jobs={n_jobs}",
    ]
    return " | ".join(lines)
