"""Parallel orchestrator for backtesting workloads.

Replaces the single-process Optuna thread-pool with a real parallel executor
that runs each job in a fresh subprocess via `worker_isolation`. The
orchestrator integrates `ResourceGuard` so it can shrink/grow the live worker
count based on host CPU/RAM pressure, and it persists every concurrency
transition either to `ops.resource_events` (when PG is the metadata backend)
or to `logs/orchestrator.jsonl` as a fallback.

Public surface:

- `OrchestratorConfig`: pure-data knobs for executor/concurrency/timeouts.
- `Orchestrator`: `map(fn, jobs) -> [WorkerResult | FailureResult]`.
- `from_app_config(...)`: convenience builder with sensible defaults.
- `FailureResult`: sentinel returned for any job that crashed/timed-out.

Design notes:

- Optional dependencies (`ray`, `joblib`) are imported lazily inside the
  branches that need them so that this module always imports cleanly.
- All concurrency decisions are gated on `time.monotonic` and `psutil`
  callsites so tests can monkeypatch them deterministically.
- The orchestrator is intentionally agnostic about what `fn` does; each job
  payload is opaque and forwarded as-is to `fn(job)` inside an isolated
  subprocess.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from backtest.config import AppConfig
from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.resources import recommend_n_jobs
from backtest.worker_isolation import (
    OrchestrationError,
    WorkerLimits,
    WorkerResult,
    spawn_isolated_worker,
)


DEFAULT_LOG_PATH = os.path.join("logs", "orchestrator.jsonl")


@dataclass
class FailureResult:
    """Sentinel returned in place of a real value when a job crashed.

    `error` is a single-line summary; `peak_rss_mb` and `elapsed_sec` come
    from `WorkerResult`. The orchestrator never raises on individual job
    failure; callers decide whether to abort the whole map.
    """

    index: int
    error: str
    peak_rss_mb: float | None = None
    elapsed_sec: float | None = None


@dataclass
class OrchestratorConfig:
    executor: str = "joblib"
    n_jobs: int = 1
    ram_cap_pct: float = 80.0
    cpu_cap_pct: float = 80.0
    per_worker_ram_mb: int | None = None
    per_trial_timeout_sec: int | None = None
    guard_sample_sec: float = 5.0
    guard_high_windows: int = 3
    guard_recover_windows: int = 3
    max_jobs_ceiling: int | None = None
    log_path: str = DEFAULT_LOG_PATH
    audit_run_id: int | None = None
    # Test-friendly knobs: when True, force the guard to re-sample on every call.
    force_resample: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_import_ray():
    """Importable-or-None helper, isolated for monkeypatching in tests."""
    try:
        import ray  # type: ignore[import-not-found]

        return ray
    except Exception:
        return None


def _try_import_joblib():
    try:
        import joblib  # type: ignore[import-not-found]

        return joblib
    except Exception:
        return None


def _run_one(fn: Callable[[Any], Any], job: Any, limits: WorkerLimits | None, timeout: float | None) -> WorkerResult:
    """Module-level helper so spawn-based multiprocessing can pickle it."""
    return spawn_isolated_worker(fn, (job,), limits=limits, timeout_sec=timeout)


class Orchestrator:
    """Run `fn(job)` over a list of jobs with crash isolation and throttling."""

    def __init__(
        self,
        config: OrchestratorConfig,
        guard: ResourceGuard | None = None,
    ) -> None:
        self.config = config
        self.guard = guard or ResourceGuard(
            ResourceGuardConfig(
                cpu_cap_pct=float(config.cpu_cap_pct),
                ram_cap_pct=float(config.ram_cap_pct),
                sample_sec=float(config.guard_sample_sec),
                high_watermark_windows=int(config.guard_high_windows),
                recover_windows=int(config.guard_recover_windows),
            )
        )
        self._concurrency = max(1, int(config.n_jobs))
        if config.max_jobs_ceiling is not None:
            self._ceiling = max(1, int(config.max_jobs_ceiling))
        else:
            self._ceiling = max(
                int(self._concurrency),
                int(recommend_n_jobs(mode="adaptive_80")),
            )
        self._log_path = str(config.log_path)
        self._audit_lock = Lock()
        self._app_config: AppConfig | None = None
        try:
            self._app_config = AppConfig.from_env()
        except Exception:
            self._app_config = None

    # --- Public API --------------------------------------------------------

    def map(self, fn: Callable[[Any], Any], jobs: List[Any]) -> List[Any]:
        if not jobs:
            return []

        executor = (self.config.executor or "joblib").strip().lower()
        if executor not in {"ray", "joblib", "serial"}:
            raise ValueError(f"Unknown executor '{executor}'")

        limits = self._worker_limits()
        timeout = (
            float(self.config.per_trial_timeout_sec)
            if self.config.per_trial_timeout_sec
            else None
        )

        if executor == "ray":
            ray = _try_import_ray()
            if ray is None:
                sys.stderr.write(
                    "WARN: executor='ray' requested but 'ray' is not importable; "
                    "falling back to joblib.\n"
                )
                executor = "joblib"

        if executor == "serial":
            return self._map_waves(fn, jobs, limits, timeout, _runner=self._run_serial_wave)
        if executor == "joblib":
            return self._map_waves(fn, jobs, limits, timeout, _runner=self._run_joblib_wave)
        return self._map_waves(fn, jobs, limits, timeout, _runner=self._run_ray_wave)

    # --- Wave dispatch -----------------------------------------------------

    def _map_waves(
        self,
        fn: Callable[[Any], Any],
        jobs: List[Any],
        limits: WorkerLimits | None,
        timeout: float | None,
        *,
        _runner: Callable[..., List[Any]],
    ) -> List[Any]:
        """Iterate `jobs` in dynamically sized waves, applying throttle hooks."""
        results: List[Any | None] = [None] * len(jobs)
        pending_indices = list(range(len(jobs)))

        while pending_indices:
            self._maybe_resize()
            wave_size = max(1, min(int(self._concurrency), len(pending_indices)))
            wave_indices = pending_indices[:wave_size]
            wave_jobs = [jobs[i] for i in wave_indices]
            pending_indices = pending_indices[wave_size:]

            wave_results = _runner(fn, wave_jobs, limits, timeout)
            for local_idx, original_idx in enumerate(wave_indices):
                outcome = wave_results[local_idx]
                results[original_idx] = self._normalize_outcome(outcome, original_idx)
        return results

    def _run_serial_wave(
        self,
        fn: Callable[[Any], Any],
        wave: List[Any],
        limits: WorkerLimits | None,
        timeout: float | None,
    ) -> List[WorkerResult]:
        return [_run_one(fn, job, limits, timeout) for job in wave]

    def _run_joblib_wave(
        self,
        fn: Callable[[Any], Any],
        wave: List[Any],
        limits: WorkerLimits | None,
        timeout: float | None,
    ) -> List[WorkerResult]:
        if len(wave) == 1:
            return [_run_one(fn, wave[0], limits, timeout)]
        joblib = _try_import_joblib()
        if joblib is None:
            with ThreadPoolExecutor(max_workers=max(1, len(wave))) as pool:
                futures = [pool.submit(_run_one, fn, job, limits, timeout) for job in wave]
                return [f.result() for f in futures]
        from joblib import Parallel, delayed  # type: ignore[import-not-found]

        return list(
            Parallel(n_jobs=max(1, len(wave)), prefer="processes")(
                delayed(_run_one)(fn, job, limits, timeout) for job in wave
            )
        )

    def _run_ray_wave(
        self,
        fn: Callable[[Any], Any],
        wave: List[Any],
        limits: WorkerLimits | None,
        timeout: float | None,
    ) -> List[WorkerResult]:
        ray = _try_import_ray()
        if ray is None:
            return self._run_joblib_wave(fn, wave, limits, timeout)
        if not ray.is_initialized():
            try:
                ray.init(ignore_reinit_error=True, configure_logging=False)
            except Exception:
                return self._run_joblib_wave(fn, wave, limits, timeout)
        remote = ray.remote(num_cpus=1)(_run_one)
        futures = [remote.remote(fn, job, limits, timeout) for job in wave]
        return list(ray.get(futures))

    # --- Result normalization ---------------------------------------------

    def _normalize_outcome(self, outcome: Any, index: int) -> Any:
        if isinstance(outcome, WorkerResult):
            if outcome.ok:
                return outcome.value
            self._emit_event(
                "trial_failed",
                {
                    "index": int(index),
                    "error": outcome.error,
                    "peak_rss_mb": outcome.peak_rss_mb,
                    "elapsed_sec": outcome.elapsed_sec,
                },
            )
            return FailureResult(
                index=index,
                error=outcome.error or "unknown error",
                peak_rss_mb=outcome.peak_rss_mb,
                elapsed_sec=outcome.elapsed_sec,
            )
        return outcome

    # --- Resource guard hooks ---------------------------------------------

    def _maybe_resize(self) -> None:
        if self.config.force_resample:
            # Reset the guard's last sample so it re-reads psutil unconditionally.
            try:
                self.guard._last_sample_monotonic = 0.0
            except Exception:
                pass

        snapshot = self.guard.snapshot()
        suggested = self.guard.suggest_concurrency(self._concurrency, min=1)
        new_concurrency = max(1, min(int(self._ceiling), int(suggested)))

        if snapshot.get("throttle_active") and new_concurrency >= self._concurrency:
            new_concurrency = max(1, self._concurrency // 2)

        if new_concurrency < self._concurrency:
            self._emit_event(
                "orchestrator_throttle",
                {
                    "from": int(self._concurrency),
                    "to": int(new_concurrency),
                    "snapshot": snapshot,
                },
            )
        elif new_concurrency > self._concurrency:
            self._emit_event(
                "orchestrator_scale_up",
                {
                    "from": int(self._concurrency),
                    "to": int(new_concurrency),
                    "snapshot": snapshot,
                },
            )

        self._concurrency = new_concurrency

        # Flush any guard-level state transitions (trigger/recovery).
        for ev in self.guard.consume_events():
            self._emit_event(
                "resource_guard",
                {
                    "event": ev.get("event"),
                    "snapshot": ev.get("snapshot"),
                    "timestamp": ev.get("timestamp"),
                },
            )

    def _worker_limits(self) -> WorkerLimits | None:
        if self.config.per_worker_ram_mb is None and self.config.per_trial_timeout_sec is None:
            return None
        ram_bytes = (
            int(self.config.per_worker_ram_mb) * 1024 * 1024
            if self.config.per_worker_ram_mb
            else None
        )
        return WorkerLimits(max_ram_bytes=ram_bytes, max_cpu_seconds=None)

    # --- Audit sink --------------------------------------------------------

    def _emit_event(self, event_type: str, snapshot: Dict[str, Any]) -> None:
        payload = {
            "ts": _utc_now_iso(),
            "event_type": event_type,
            "snapshot": snapshot,
            "run_id": self.config.audit_run_id,
        }
        # Try PG first when configured; fall back to JSONL on any error.
        if self._try_emit_pg(event_type, payload):
            return
        self._emit_jsonl(payload)

    def _try_emit_pg(self, event_type: str, payload: Dict[str, Any]) -> bool:
        cfg = self._app_config
        if cfg is None or cfg.metadata_backend != "pg" or not cfg.pg_dsn:
            return False
        try:
            from backtest import storage_pg

            with storage_pg.transaction(cfg.pg_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO ops.resource_events (run_id, event, snapshot) "
                        "VALUES (%s, %s, %s::jsonb)",
                        (
                            int(self.config.audit_run_id)
                            if self.config.audit_run_id is not None
                            else None,
                            event_type,
                            json.dumps(payload, ensure_ascii=False, default=str),
                        ),
                    )
                    cur.execute(
                        "INSERT INTO ops.audit_log (run_id, event_type, payload) "
                        "VALUES (%s, %s, %s::jsonb)",
                        (
                            int(self.config.audit_run_id)
                            if self.config.audit_run_id is not None
                            else None,
                            event_type,
                            json.dumps(payload, ensure_ascii=False, default=str),
                        ),
                    )
            return True
        except Exception:
            return False

    def _emit_jsonl(self, payload: Dict[str, Any]) -> None:
        with self._audit_lock:
            try:
                Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            except Exception:
                # Audit is best-effort; never crash the dispatch loop.
                pass


def from_app_config(app_config: AppConfig) -> Orchestrator:
    """Build an `Orchestrator` with reasonable defaults derived from the host."""
    executor = "ray" if _try_import_ray() is not None else "joblib"
    n_jobs = int(recommend_n_jobs(mode="adaptive_80"))
    cfg = OrchestratorConfig(executor=executor, n_jobs=n_jobs)
    return Orchestrator(cfg)


__all__ = [
    "FailureResult",
    "OrchestrationError",
    "Orchestrator",
    "OrchestratorConfig",
    "from_app_config",
]
