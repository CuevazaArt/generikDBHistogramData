"""Pre-run briefing artifacts for backtest and strict-run executions.

Before starting a heavy or comparative run, emit a human-readable
``RUN_BRIEFING.md`` plus ``run_briefing.json`` capturing every knob that
can affect performance (strategy params, gates, accessories, grids,
engine flags, resource plan, reproducibility).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backtest.repro import git_snapshot

BRIEFING_MD = "RUN_BRIEFING.md"
BRIEFING_JSON = "run_briefing.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gate_label(active: bool) -> str:
    return "ACTIVO" if active else "DESACTIVO"


def _md_section(title: str, lines: Sequence[str]) -> List[str]:
    out = [f"## {title}", ""]
    out.extend(lines)
    out.append("")
    return out


def build_run_briefing_payload(
    *,
    study_name: str,
    strategy: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    strategy_params: Mapping[str, Any],
    engine: Mapping[str, Any],
    execution: Mapping[str, Any],
    optimization: Mapping[str, Any] | None = None,
    accessories: Mapping[str, Any] | None = None,
    gates: Mapping[str, Any] | None = None,
    resource_plan: Mapping[str, Any] | None = None,
    reproducibility: Mapping[str, Any] | None = None,
    notes: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Build the canonical pre-run briefing document."""
    return {
        "schema_version": 1,
        "artifact_kind": "run_briefing",
        "created_at": _utc_now_iso(),
        "study_name": study_name,
        "strategy": strategy,
        "market": {
            "symbol": str(symbol).upper(),
            "interval": str(interval),
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
        },
        "strategy_params": dict(strategy_params),
        "engine": dict(engine),
        "execution": dict(execution),
        "optimization": dict(optimization or {}),
        "accessories": dict(accessories or {}),
        "gates": dict(gates or {}),
        "resource_plan": dict(resource_plan or {}),
        "reproducibility": dict(reproducibility or git_snapshot()),
        "performance_notes": list(notes or []),
    }


def render_run_briefing_markdown(payload: Mapping[str, Any]) -> str:
    """Render ``RUN_BRIEFING.md`` from a briefing payload."""
    market = payload.get("market") or {}
    engine = payload.get("engine") or {}
    execution = payload.get("execution") or {}
    optimization = payload.get("optimization") or {}
    accessories = payload.get("accessories") or {}
    gates = payload.get("gates") or {}
    resource_plan = payload.get("resource_plan") or {}
    repro = payload.get("reproducibility") or {}
    params = payload.get("strategy_params") or {}

    lines = [
        f"# Pre-run briefing — {payload.get('study_name', '?')}",
        "",
        f"- Generado: `{payload.get('created_at', '')}`",
        f"- Estrategia/bot: `{payload.get('strategy', '?')}`",
        "",
    ]

    lines.extend(
        _md_section(
            "Mercado y ventana",
            [
                f"- symbol / interval: `{market.get('symbol')}` / `{market.get('interval')}`",
                f"- start_ts / end_ts: `{market.get('start_ts')}` / `{market.get('end_ts')}`",
                f"- execution_windows: `{execution.get('windows_label', execution.get('execution_windows', '?'))}`",
                f"- window_candle_count: `{execution.get('window_candle_count', '?')}`",
            ],
        )
    )

    gate_lines = []
    for key, meta in gates.items():
        if isinstance(meta, dict):
            gate_lines.append(
                f"- {key}: **{_gate_label(bool(meta.get('active', False)))}** — {meta.get('description', '')}"
            )
        else:
            gate_lines.append(f"- {key}: `{meta}`")
    if not gate_lines:
        gate_lines = ["- (sin gates declarados)"]
    lines.extend(_md_section("Gates y filtros", gate_lines))

    acc_lines = []
    for key, meta in accessories.items():
        if isinstance(meta, dict):
            active = bool(meta.get("active", False))
            acc_lines.append(
                f"- {key}: **{'ACTIVO' if active else 'DESACTIVO'}**"
                + (f" — {meta.get('detail', '')}" if meta.get("detail") else "")
            )
        else:
            acc_lines.append(f"- {key}: `{meta}`")
    if not acc_lines:
        acc_lines = ["- (sin accesorios declarados)"]
    lines.extend(_md_section("Accesorios de estrategia", acc_lines))

    param_lines = [f"- `{k}`: `{v}`" for k, v in sorted(params.items())]
    lines.extend(_md_section("Parametros de estrategia", param_lines or ["- (vacios)"]))

    opt_lines = []
    for key in ("profit_factor_grid", "margin_drop_grid", "param_combos"):
        if key in optimization:
            opt_lines.append(f"- {key}: `{json.dumps(optimization[key], ensure_ascii=False)}`")
    if not opt_lines:
        opt_lines = ["- corrida puntual (sin malla de optimizacion)"]
    lines.extend(_md_section("Optimizacion / malla", opt_lines))

    engine_lines = [f"- `{k}`: `{v}`" for k, v in sorted(engine.items())]
    lines.extend(_md_section("Motor de ejecucion", engine_lines or ["- (defaults)"]))

    exec_lines = [f"- `{k}`: `{v}`" for k, v in sorted(execution.items()) if k != "execution_windows"]
    lines.extend(_md_section("Orquestacion", exec_lines or ["- serial"]))

    if resource_plan:
        lines.extend(
            _md_section(
                "Plan de recursos",
                [f"- `{k}`: `{v}`" for k, v in sorted(resource_plan.items())],
            )
        )

    if repro:
        lines.extend(
            _md_section(
                "Reproducibilidad",
                [f"- `{k}`: `{v}`" for k, v in sorted(repro.items())],
            )
        )

    perf_notes = list(payload.get("performance_notes") or [])
    if perf_notes:
        lines.extend(_md_section("Notas de impacto en desempeno", [f"- {n}" for n in perf_notes]))

    lines.append("---")
    lines.append("")
    lines.append(
        "Este briefing debe generarse **antes** de iniciar la corrida. "
        "Cualquier cambio posterior en codigo, params o datos invalida la comparacion "
        "salvo que se regenere el briefing y se documente en el manifest final."
    )
    lines.append("")
    return "\n".join(lines)


def write_run_briefing(output_dir: str, payload: Mapping[str, Any]) -> Dict[str, str]:
    """Persist briefing markdown + json under ``output_dir``."""
    os.makedirs(output_dir, exist_ok=True)
    md_path = os.path.join(output_dir, BRIEFING_MD)
    json_path = os.path.join(output_dir, BRIEFING_JSON)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_run_briefing_markdown(payload))
        fh.write("\n")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return {"markdown": os.path.abspath(md_path), "json": os.path.abspath(json_path)}


__all__ = [
    "BRIEFING_JSON",
    "BRIEFING_MD",
    "build_run_briefing_payload",
    "render_run_briefing_markdown",
    "write_run_briefing",
]
