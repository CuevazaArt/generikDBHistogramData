"""Agartha tick-level telemetry emitter (JSONL).

Cada tick procesado por un bot Agartha live emite un registro estructurado
con: precio actual, entry, peak, trail_floor, banda PERCENT_PRICE_BY_SIDE,
accion planeada, fallback usado, etc.

Objetivos:
  - Auditar en linea la decision del trailing stop (Binance no la ejecuta).
  - Detectar reglas arbitrarias no documentadas (e.g. rechazos por banda).
  - Permitir replay/forensia tras eventos extremos.
  - Alimentar dashboards (no incluidos aqui, solo el log primitivo).

Diseño deliberadamente minimo: stdlib + dataclass. Compatible con cualquier
storage (rota archivos por dia, sube a S3, etc.).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backtest.agartha_exit_planner import ExitAction, ExitPlan


@dataclass
class AgarthaTickRecord:
    """Snapshot por tick procesado."""
    ts_utc: str
    symbol: str
    cycle_index: int
    current_price: float
    entry_price: float
    peak_price: float
    trail_floor: float
    distance_to_floor_pct: float
    band_lower: float
    band_upper: float
    action: str
    limit_price: Optional[float] = None
    reason: str = ""
    fallback_used: bool = False
    crest_detected: bool = False
    cash: Optional[float] = None
    position_qty: Optional[float] = None
    equity: Optional[float] = None
    extra: dict = field(default_factory=dict)


def _utc_iso(ms: Optional[int] = None) -> str:
    if ms is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def build_record(
    *,
    symbol: str,
    cycle_index: int,
    current_price: float,
    entry_price: float,
    plan: ExitPlan,
    cash: Optional[float] = None,
    position_qty: Optional[float] = None,
    equity: Optional[float] = None,
    ts_ms: Optional[int] = None,
    extra: Optional[dict] = None,
) -> AgarthaTickRecord:
    """Convierte un ExitPlan + contexto en un AgarthaTickRecord listo para log."""
    distance_pct = 0.0
    if plan.trail_floor > 0 and current_price > 0:
        distance_pct = (current_price - plan.trail_floor) / current_price * 100.0
    return AgarthaTickRecord(
        ts_utc=_utc_iso(ts_ms),
        symbol=str(symbol).upper(),
        cycle_index=int(cycle_index),
        current_price=float(current_price),
        entry_price=float(entry_price),
        peak_price=float(plan.peak_price),
        trail_floor=float(plan.trail_floor),
        distance_to_floor_pct=float(distance_pct),
        band_lower=float(plan.band_lower),
        band_upper=float(plan.band_upper),
        action=plan.action.value if isinstance(plan.action, ExitAction) else str(plan.action),
        limit_price=plan.limit_price,
        reason=plan.reason,
        fallback_used=bool(plan.fallback_used),
        crest_detected=bool(plan.crest_detected),
        cash=cash,
        position_qty=position_qty,
        equity=equity,
        extra=dict(extra or {}),
    )


class AgarthaJsonlLogger:
    """Append-only JSONL logger por simbolo+fecha (rota archivos por dia UTC).

    Uso:
        logger = AgarthaJsonlLogger("logs/agartha")
        logger.write(record)        # append safe-flush
        logger.close()
    """

    def __init__(self, output_dir: str, prefix: str = "agartha_ticks"):
        self.output_dir = output_dir
        self.prefix = prefix
        os.makedirs(output_dir, exist_ok=True)
        self._fh = None
        self._current_path: Optional[str] = None
        self._current_day: Optional[str] = None

    def _path_for(self, symbol: str, day_utc: str) -> str:
        safe_sym = symbol.upper().replace("/", "_")
        return os.path.join(self.output_dir, f"{self.prefix}_{safe_sym}_{day_utc}.jsonl")

    def _open_if_needed(self, symbol: str, day_utc: str) -> None:
        if self._fh is None or self._current_day != day_utc or self._current_path != self._path_for(symbol, day_utc):
            self.close()
            self._current_path = self._path_for(symbol, day_utc)
            self._current_day = day_utc
            self._fh = open(self._current_path, "a", encoding="utf-8")

    def write(self, record: AgarthaTickRecord) -> None:
        day = record.ts_utc[:10] if record.ts_utc else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._open_if_needed(record.symbol, day)
        if self._fh is None:
            return
        self._fh.write(json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self) -> "AgarthaJsonlLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def append_alert_log(output_dir: str, payload: dict) -> str:
    """Append a single alert (e.g. OUT_OF_BAND, rechazo arbitrario) a alerts.jsonl.

    Diseñado para que un proceso externo (slack-bot, pagerduty) lo consuma.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "agartha_alerts.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        record: dict[str, Any] = {"ts_utc": _utc_iso(), **payload}
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path
