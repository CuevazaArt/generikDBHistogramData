"""Runtime resource guards for long-running backtesting workloads.

The guard samples host CPU/RAM usage and emits throttle/recovery state changes
with hysteresis windows. Callers can reduce pressure dynamically (lower
concurrency and/or apply backoff) while preserving progress.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime guard
    psutil = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ResourceGuardConfig:
    # Adaptive-80 default: target 80% of CPU/RAM, never exceed.
    cpu_cap_pct: float = 80.0
    ram_cap_pct: float = 80.0
    sample_sec: float = 5.0
    high_watermark_windows: int = 3
    recover_windows: int = 3
    # Headroom under the cap that is considered "safe to scale up".
    scale_up_headroom_pct: float = 15.0

    @staticmethod
    def from_env(prefix: str = "BACKTEST_GUARD_") -> "ResourceGuardConfig":
        def _float(name: str, default: float) -> float:
            raw = os.getenv(f"{prefix}{name}")
            if raw is None:
                return default
            try:
                return float(raw)
            except Exception:
                return default

        def _int(name: str, default: int) -> int:
            raw = os.getenv(f"{prefix}{name}")
            if raw is None:
                return default
            try:
                return int(raw)
            except Exception:
                return default

        return ResourceGuardConfig(
            cpu_cap_pct=_float("CPU_CAP_PCT", 80.0),
            ram_cap_pct=_float("RAM_CAP_PCT", 80.0),
            sample_sec=max(0.5, _float("SAMPLE_SEC", 5.0)),
            high_watermark_windows=max(1, _int("HIGH_WATERMARK_WINDOWS", 3)),
            recover_windows=max(1, _int("RECOVER_WINDOWS", 3)),
            scale_up_headroom_pct=max(0.0, _float("SCALE_UP_HEADROOM_PCT", 15.0)),
        )


class ResourceGuard:
    """Adaptive guard with sustained-threshold trigger and recovery hysteresis."""

    def __init__(self, config: Optional[ResourceGuardConfig] = None) -> None:
        self.config = config or ResourceGuardConfig()
        self._max_windows = max(self.config.high_watermark_windows, self.config.recover_windows)
        self._high_history: Deque[bool] = deque(maxlen=self._max_windows)
        self._recover_history: Deque[bool] = deque(maxlen=self._max_windows)
        self._last_sample_monotonic = 0.0
        self._throttle = False
        # `_enabled` must be set before any call that touches `_empty_snapshot`.
        self._enabled = psutil is not None
        if self._enabled:
            try:
                # Prime psutil CPU measurement baseline.
                psutil.cpu_percent(interval=None)
            except Exception:
                self._enabled = False
        self._last_snapshot: Dict[str, Any] = self._empty_snapshot(reason="init")
        self._pending_events: List[Dict[str, Any]] = []

    def _empty_snapshot(self, reason: str) -> Dict[str, Any]:
        return {
            "timestamp": _utc_now(),
            "cpu_pct": None,
            "ram_pct": None,
            "cpu_cap_pct": float(self.config.cpu_cap_pct),
            "ram_cap_pct": float(self.config.ram_cap_pct),
            "throttle_active": bool(self._throttle),
            "guard_enabled": bool(self._enabled),
            "reason": reason,
        }

    def _sample_metrics(self) -> Dict[str, Any]:
        if not self._enabled:
            return self._empty_snapshot(reason="psutil_unavailable")
        try:
            cpu = float(psutil.cpu_percent(interval=None))
            vm = psutil.virtual_memory()
            ram = float(vm.percent)
        except Exception:
            self._enabled = False
            return self._empty_snapshot(reason="sampling_failed")
        return {
            "timestamp": _utc_now(),
            "cpu_pct": cpu,
            "ram_pct": ram,
            "cpu_cap_pct": float(self.config.cpu_cap_pct),
            "ram_cap_pct": float(self.config.ram_cap_pct),
            "throttle_active": bool(self._throttle),
            "guard_enabled": True,
            "reason": "sampled",
        }

    def _update_state(self, snap: Dict[str, Any]) -> None:
        cpu = snap.get("cpu_pct")
        ram = snap.get("ram_pct")
        if cpu is None or ram is None:
            return
        high = bool(cpu >= self.config.cpu_cap_pct or ram >= self.config.ram_cap_pct)
        below = bool(cpu < self.config.cpu_cap_pct and ram < self.config.ram_cap_pct)
        self._high_history.append(high)
        self._recover_history.append(below)

        high_window = self.config.high_watermark_windows
        recover_window = self.config.recover_windows
        trigger = (
            not self._throttle
            and len(self._high_history) >= high_window
            and all(list(self._high_history)[-high_window:])
        )
        recover = (
            self._throttle
            and len(self._recover_history) >= recover_window
            and all(list(self._recover_history)[-recover_window:])
        )
        if trigger:
            self._throttle = True
            self._pending_events.append(
                {
                    "event": "resource_guard_trigger",
                    "timestamp": snap["timestamp"],
                    "snapshot": dict(snap),
                }
            )
        elif recover:
            self._throttle = False
            self._pending_events.append(
                {
                    "event": "resource_guard_recovery",
                    "timestamp": snap["timestamp"],
                    "snapshot": dict(snap),
                }
            )

    def _refresh(self, force: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        if not force and self._last_sample_monotonic > 0:
            if (now - self._last_sample_monotonic) < max(0.1, float(self.config.sample_sec)):
                snap = dict(self._last_snapshot)
                snap["throttle_active"] = bool(self._throttle)
                self._last_snapshot = snap
                return snap
        snap = self._sample_metrics()
        self._update_state(snap)
        snap["throttle_active"] = bool(self._throttle)
        self._last_snapshot = snap
        self._last_sample_monotonic = now
        return dict(self._last_snapshot)

    def should_throttle(self) -> bool:
        self._refresh(force=False)
        return bool(self._throttle)

    def should_scale_up(self) -> bool:
        """True when CPU/RAM are below cap minus headroom and not throttled."""
        snap = self._refresh(force=False)
        if self._throttle:
            return False
        cpu = snap.get("cpu_pct")
        ram = snap.get("ram_pct")
        if cpu is None or ram is None:
            return True  # no telemetry, optimistic ramp-up
        headroom = float(self.config.scale_up_headroom_pct)
        return bool(
            float(cpu) < (float(self.config.cpu_cap_pct) - headroom)
            and float(ram) < (float(self.config.ram_cap_pct) - headroom)
        )

    def suggest_concurrency(self, current: int, min: int = 1) -> int:
        current = max(int(min), int(current))
        if self.should_throttle():
            return max(int(min), (current + 1) // 2)
        if self.should_scale_up():
            return max(int(min), current + 1)
        return current

    def snapshot(self) -> Dict[str, Any]:
        return self._refresh(force=False)

    def consume_events(self) -> List[Dict[str, Any]]:
        out = list(self._pending_events)
        self._pending_events.clear()
        return out
