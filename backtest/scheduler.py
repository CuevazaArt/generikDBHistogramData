"""Adaptive parallel job scheduler for heavy backtesting workloads.

Generalizes the wave/throttle pattern previously inlined in
`scripts/launch_xrpusdt_2024_dorothy_pf_parallel.py` so any orchestrator can
spawn N independent subprocess "branches" while staying inside CPU/RAM caps
(default `adaptive_80`: target 80% of each).

Design goals:
- Each branch is a black-box subprocess command (kept simple, OS-aware).
- The scheduler starts conservatively (`dynamic_concurrent=1`) and uses
  `ResourceGuard` to scale up when there is headroom, or shrink + back off
  when CPU/RAM stay above the cap for `high_watermark_windows` samples.
- Per-branch stdout/stderr is captured to a log file.
- A JSONL master log records `plan`, `started`, `finished`, throttle events.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from backtest.guards import ResourceGuard, ResourceGuardConfig


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BranchSpec:
    """A unit of work the scheduler will spawn as a subprocess."""

    name: str
    command: Sequence[str]
    log_path: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulerConfig:
    repo_root: str
    master_log_jsonl: str
    cpu_cap_pct: float = 80.0
    ram_cap_pct: float = 80.0
    guard_sample_sec: float = 5.0
    guard_high_windows: int = 3
    guard_recover_windows: int = 3
    guard_backoff_sec: float = 10.0
    poll_seconds: float = 5.0
    initial_concurrency: int = 1
    extra_env: Dict[str, str] = field(default_factory=dict)


def _append_jsonl(path: str, payload: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_guard(cfg: SchedulerConfig) -> ResourceGuard:
    return ResourceGuard(
        ResourceGuardConfig(
            cpu_cap_pct=float(cfg.cpu_cap_pct),
            ram_cap_pct=float(cfg.ram_cap_pct),
            sample_sec=float(cfg.guard_sample_sec),
            high_watermark_windows=int(cfg.guard_high_windows),
            recover_windows=int(cfg.guard_recover_windows),
        )
    )


def _compute_max_concurrency(branches: int, cpu_cap_pct: float) -> int:
    cpu_count = os.cpu_count() or 1
    budget = max(1, math.floor(cpu_count * (float(cpu_cap_pct) / 100.0)))
    return max(1, min(int(branches), int(budget)))


def run_branches(
    cfg: SchedulerConfig,
    branches: List[BranchSpec],
    progress_cb: Callable[[Dict[str, Any]], None] | None = None,
) -> List[Dict[str, Any]]:
    """Run branches in parallel, adapting concurrency to host pressure.

    Returns a list with one descriptor per branch (exit_code, durations,
    log path). The same descriptors are also appended to the master JSONL.
    """
    guard = _build_guard(cfg)
    max_concurrent = _compute_max_concurrency(len(branches), cfg.cpu_cap_pct)
    dynamic_concurrent = max(1, int(cfg.initial_concurrency))

    plan = {
        "event": "plan",
        "timestamp": _utc_now_iso(),
        "cpu_cap_pct": cfg.cpu_cap_pct,
        "ram_cap_pct": cfg.ram_cap_pct,
        "max_concurrent": max_concurrent,
        "branches": [
            {"name": b.name, "log_path": b.log_path, "metadata": b.metadata}
            for b in branches
        ],
    }
    _append_jsonl(cfg.master_log_jsonl, plan)
    if progress_cb is not None:
        progress_cb(plan)

    env = os.environ.copy()
    env.update({k: str(v) for k, v in (cfg.extra_env or {}).items()})

    pending = list(branches)
    active: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    while pending or active:
        snapshot = guard.snapshot()
        suggested = guard.suggest_concurrency(dynamic_concurrent, min=1)
        dynamic_concurrent = max(1, min(max_concurrent, suggested))
        if snapshot.get("throttle_active"):
            dynamic_concurrent = max(1, min(dynamic_concurrent, max_concurrent // 2 or 1))

        for event in guard.consume_events():
            payload = {
                **event,
                "dynamic_concurrent": int(dynamic_concurrent),
                "active_count": len(active),
                "pending_count": len(pending),
            }
            _append_jsonl(cfg.master_log_jsonl, payload)
            if progress_cb is not None:
                progress_cb(payload)

        while pending and len(active) < dynamic_concurrent:
            branch = pending.pop(0)
            Path(branch.log_path).parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(branch.log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                list(branch.command),
                cwd=str(cfg.repo_root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            started_payload = {
                "event": "started",
                "timestamp": _utc_now_iso(),
                "branch": branch.name,
                "pid": int(proc.pid),
                "command": list(branch.command),
                "log_path": branch.log_path,
                "metadata": branch.metadata,
            }
            _append_jsonl(cfg.master_log_jsonl, started_payload)
            if progress_cb is not None:
                progress_cb(started_payload)
            active.append(
                {
                    "branch": branch,
                    "proc": proc,
                    "log_handle": log_handle,
                    "started_at": time.time(),
                }
            )

        if not active:
            if snapshot.get("throttle_active"):
                backoff = max(1.0, float(cfg.guard_backoff_sec))
                payload = {
                    "event": "scheduler_backoff",
                    "timestamp": _utc_now_iso(),
                    "seconds": backoff,
                    "pending_count": len(pending),
                    "snapshot": snapshot,
                }
                _append_jsonl(cfg.master_log_jsonl, payload)
                if progress_cb is not None:
                    progress_cb(payload)
                time.sleep(backoff)
            continue

        time.sleep(max(1.0, float(cfg.poll_seconds)))
        survivors: List[Dict[str, Any]] = []
        for item in active:
            proc = item["proc"]
            branch = item["branch"]
            code = proc.poll()
            if code is None:
                survivors.append(item)
                continue
            item["log_handle"].close()
            elapsed = time.time() - float(item["started_at"])
            finished_payload = {
                "event": "finished",
                "timestamp": _utc_now_iso(),
                "branch": branch.name,
                "pid": int(proc.pid),
                "exit_code": int(code),
                "elapsed_sec": float(elapsed),
                "log_path": branch.log_path,
                "metadata": branch.metadata,
            }
            _append_jsonl(cfg.master_log_jsonl, finished_payload)
            if progress_cb is not None:
                progress_cb(finished_payload)
            results.append(finished_payload)
        active = survivors

    return results
