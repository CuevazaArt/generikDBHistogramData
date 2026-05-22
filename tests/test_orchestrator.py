"""Tests for backtest.orchestrator."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.orchestrator import (
    FailureResult,
    Orchestrator,
    OrchestratorConfig,
    _try_import_ray,
)


# --- Top-level worker functions (must be picklable for spawn-based MP) ---

def _job_double(x):
    return x * 2


def _job_with_oom(x):
    if x == 2:
        raise MemoryError("simulated OOM")
    return x * x


def _job_noop(x):
    return x


# --- Helpers --------------------------------------------------------------

def _make_orchestrator(tmp_path, **cfg_overrides):
    defaults = dict(
        executor="serial",
        n_jobs=1,
        ram_cap_pct=80.0,
        cpu_cap_pct=80.0,
        guard_sample_sec=0.0,
        guard_high_windows=1,
        guard_recover_windows=1,
        log_path=str(tmp_path / "orchestrator.jsonl"),
    )
    defaults.update(cfg_overrides)
    return Orchestrator(OrchestratorConfig(**defaults))


# --- Tests ----------------------------------------------------------------

def test_orchestrator_serial(tmp_path):
    orch = _make_orchestrator(tmp_path, executor="serial", n_jobs=1)
    jobs = [1, 2, 3, 4, 5]
    results = orch.map(_job_double, jobs)
    assert results == [2, 4, 6, 8, 10]


def test_orchestrator_joblib_isolation(tmp_path):
    orch = _make_orchestrator(tmp_path, executor="joblib", n_jobs=2)
    results = orch.map(_job_with_oom, [1, 2, 3, 4])

    assert results[0] == 1
    assert isinstance(results[1], FailureResult)
    assert "MemoryError" in results[1].error
    assert results[2] == 9
    assert results[3] == 16


def test_orchestrator_throttle_emits_event(tmp_path, monkeypatch):
    # Force psutil to report 95% RAM and 95% CPU so the guard triggers.
    import backtest.guards as guards_mod

    class _FakeVm:
        percent = 95.0
        available = 1
        total = 100

    monkeypatch.setattr(guards_mod.psutil, "virtual_memory", lambda: _FakeVm())
    monkeypatch.setattr(guards_mod.psutil, "cpu_percent", lambda interval=None: 95.0)

    log_path = tmp_path / "orchestrator.jsonl"
    orch = _make_orchestrator(
        tmp_path,
        executor="serial",
        n_jobs=4,
        guard_high_windows=1,
        guard_recover_windows=1,
        guard_sample_sec=0.0,
        force_resample=True,
        log_path=str(log_path),
    )

    results = orch.map(_job_noop, [1, 2, 3])
    assert results == [1, 2, 3]

    assert log_path.exists(), "orchestrator did not write its audit log"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    event_types = [entry.get("event_type") for entry in lines]
    assert "orchestrator_throttle" in event_types, f"no throttle event in {event_types}"


def test_orchestrator_handles_ray_absent(tmp_path, monkeypatch, capsys):
    import backtest.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "_try_import_ray", lambda: None)

    orch = _make_orchestrator(tmp_path, executor="ray", n_jobs=2)
    results = orch.map(_job_double, [1, 2, 3])
    assert results == [2, 4, 6]

    err = capsys.readouterr().err
    assert "ray" in err.lower()
    assert "joblib" in err.lower()


def test_orchestrator_from_app_config_smoke():
    from backtest.config import AppConfig
    from backtest.orchestrator import from_app_config

    cfg = AppConfig.from_env()
    orch = from_app_config(cfg)
    assert orch.config.n_jobs >= 1
    assert orch.config.executor in {"ray", "joblib"}
