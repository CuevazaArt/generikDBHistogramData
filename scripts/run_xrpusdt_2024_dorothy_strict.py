from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.calendar_windows import monthly_windows as _calendar_monthly_windows
from backtest.cleanup import abort_stale_runs
from backtest.engine import EngineConfig
from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.report_paths import strict_report_dir, write_manifest
from backtest.repro import git_snapshot
from backtest.run_briefing import build_run_briefing_payload, write_run_briefing
from backtest.registry import get_strategy
from backtest.resources import detect_resources, estimate_worker_ram_bytes
from backtest.runner import execute_and_persist
from backtest.storage import summarize_run
from backtest.telemetry import TelemetryConfig, TelemetryRecorder, estimate_workload


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _count_candles(db_path: str, symbol: str, interval: str, start_ts: int, end_ts: int) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT COUNT(*)
            FROM klines
            WHERE symbol = ? AND interval = ? AND open_time BETWEEN ? AND ?
            """,
            (symbol, interval, int(start_ts), int(end_ts)),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _build_profit_grid(raw: str | None) -> list[float]:
    if not raw:
        return [0.01, 0.03, 0.05, 0.06]
    values = [float(x.strip()) for x in raw.split(",") if x.strip()]
    unique = sorted({round(v, 12) for v in values})
    if not unique:
        raise ValueError("profit_factor_grid cannot be empty")
    return [float(v) for v in unique]


def _build_margin_grid(args: argparse.Namespace) -> List[float]:
    raw = getattr(args, "margin_drop_grid", None)
    if raw:
        return _build_profit_grid(str(raw))
    return [float(args.margin_drop_factor)]


def _param_combos(profit_values: List[float], margin_values: List[float]) -> List[Dict[str, float]]:
    combos: List[Dict[str, float]] = []
    for margin in margin_values:
        for pf in profit_values:
            combos.append({"profit_factor": float(pf), "margin_drop_factor": float(margin)})
    return combos


def _enrich_strategy_params(
    combo: Dict[str, float],
    args: argparse.Namespace,
    quote_order_qty: float,
    max_rungs: int,
) -> Dict[str, Any]:
    params = {
        **combo,
        "quote_order_qty_usdt": float(quote_order_qty),
        "max_rungs": int(max_rungs),
        "symbol": str(args.symbol).upper(),
        "initial_run_cash": float(args.initial_cash),
        "volumen_incremental": bool(getattr(args, "volumen_incremental", False)),
        "volumen_incremental_multiplier": float(getattr(args, "volumen_incremental_multiplier", 1.05)),
        "require_trend_gate": not bool(getattr(args, "no_trend_gate", False)),
    }
    if bool(getattr(args, "require_entry_gate", False)):
        params["require_entry_gate"] = True
    return params


def _windows_label(execution_windows: List[Tuple[str, int, int]]) -> str:
    names = [str(w[0]) for w in execution_windows]
    if not names:
        return "(sin ventanas)"
    if len(names) == 1:
        return names[0]
    return f"{names[0]}..{names[-1]}"


def _dorothy_briefing_notes(args: argparse.Namespace) -> List[str]:
    notes: List[str] = []
    if bool(getattr(args, "no_trend_gate", False)):
        notes.append(
            "Gate de tendencia DESACTIVO: compras/DCAs posibles en BEARISH; "
            "las ventas por TP se evaluan antes del gate."
        )
    if bool(getattr(args, "require_entry_gate", False)):
        notes.append(
            "Gate de entrada ACTIVO (live parity): compras solo con pec_entry_gate=BLOCKED."
        )
    if bool(getattr(args, "volumen_incremental", False)):
        notes.append(
            "VolumenIncremental activo: notional base x multiplier cuando cash > initial_run_cash."
        )
    if bool(getattr(args, "chain_by_month", False)):
        notes.append("Cadena mensual: estado broker + active_sell_limits heredado entre ventanas.")
    return notes


def _write_pre_run_briefing(
    *,
    output_dir: str,
    study_name: str,
    args: argparse.Namespace,
    execution_windows: List[Tuple[str, int, int]],
    param_combos: List[Dict[str, float]],
    profit_values: List[float],
    margin_values: List[float],
    quote_order_qty: float,
    max_rungs: int,
    window_candles: int,
    resources: Dict[str, Any],
    sample_strategy_params: Dict[str, Any],
) -> Dict[str, str]:
    """Emit RUN_BRIEFING before any backtest work starts (directiva operativa #8)."""
    payload = build_run_briefing_payload(
        study_name=study_name,
        strategy="dorothy",
        symbol=str(args.symbol),
        interval=str(args.interval),
        start_ts=int(args.start_ts),
        end_ts=int(args.end_ts),
        strategy_params=sample_strategy_params,
        engine={
            "db_path": str(args.db),
            "initial_cash": float(args.initial_cash),
            "fee_rate": float(args.fee_rate),
            "slippage_bps": float(args.slippage_bps),
            "loop_seconds": int(args.loop_seconds),
            "events_mode": "lite",
            "snapshot_seconds": 3600,
            "quote_order_qty_usdt": float(quote_order_qty),
            "max_rungs": int(max_rungs),
        },
        execution={
            "chain_by_month": bool(getattr(args, "chain_by_month", False)),
            "execution_windows": execution_windows,
            "windows_label": _windows_label(execution_windows),
            "window_candle_count": int(window_candles),
            "executor": str(getattr(args, "executor", "serial") or "serial"),
            "n_jobs": int(getattr(args, "n_jobs", 1) or 1),
            "seed_run_id": getattr(args, "seed_run_id", None),
        },
        optimization={
            "profit_factor_grid": profit_values,
            "margin_drop_grid": margin_values,
            "param_combos": param_combos,
        },
        accessories={
            "volumen_incremental": {
                "active": bool(getattr(args, "volumen_incremental", False)),
                "detail": f"multiplier={float(getattr(args, 'volumen_incremental_multiplier', 1.05))}",
            },
        },
        gates={
            "trend_ha_bullish": {
                "active": not bool(getattr(args, "no_trend_gate", False)),
                "description": "Gate 1: pec_trend == BULLISH (Heikin-Ashi MA1>MA2)",
            },
            "entry_price_below_open": {
                "active": bool(getattr(args, "require_entry_gate", False)),
                "description": "Gate 2: pec_entry_gate == BLOCKED (precio < open vela)",
            },
        },
        resource_plan=resources,
        reproducibility=git_snapshot(),
        notes=_dorothy_briefing_notes(args),
    )
    return write_run_briefing(output_dir, payload)


def _configure_compute_threads(cpu_cap_pct: float) -> int:
    """Let numpy/OpenBLAS use multiple cores during indicator passes."""
    n = os.cpu_count() or 4
    threads = n if float(cpu_cap_pct) >= 99.0 else max(1, int(n * float(cpu_cap_pct) / 100.0))
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(threads)
    return threads


def _strict_chain_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Picklable worker: one full chained run for a single (profit_factor, margin_drop) combo."""
    root = str(job["project_root"])
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    args = argparse.Namespace(**job["args_ns"])
    _configure_compute_threads(float(job.get("cpu_cap_pct", 100.0)))
    strategy_cls = get_strategy("dorothy")
    guard = _guard_from_args(args)
    guard_events: List[Dict[str, Any]] = []
    strategy_params = dict(job["strategy_params"])
    strategy_params["quote_order_qty_usdt"] = float(job["quote_order_qty_usdt"])
    strategy_params["max_rungs"] = int(job["max_rungs"])
    return _run_chain_for_profit_factor(
        args=args,
        strategy_cls=strategy_cls,
        execution_windows=list(job["execution_windows"]),
        strategy_params=strategy_params,
        guard=guard,
        guard_events=guard_events,
        chain_seed_state=job.get("chain_seed"),
    )


def _quarter_windows_2024() -> List[Tuple[str, int, int]]:
    return [
        ("Q1", 1704067200000, 1711929599000),
        ("Q2", 1711929600000, 1719791999000),
        ("Q3", 1719792000000, 1727740799000),
        ("Q4", 1727740800000, 1735689599000),
    ]


_MS_PER_DAY = 24 * 60 * 60 * 1000


def _monthly_windows(
    start_ts: int,
    end_ts: int,
    from_month: int = 1,
    through_month: int = 12,
) -> List[Tuple[str, int, int]]:
    """Generic calendar-month windows clipped to [start_ts, end_ts].

    Thin wrapper over :func:`backtest.calendar_windows.monthly_windows` so the
    strict script can pick a year-agnostic plan from CLI args. Names use the
    unambiguous ``YYYY-MM`` format (e.g. ``"2024-01"``) and span any year
    range covered by ``[start_ts, end_ts]``. ``from_month``/``through_month``
    apply per year (see helper docstring for multi-year behavior).
    """
    return _calendar_monthly_windows(
        int(start_ts),
        int(end_ts),
        from_month=int(from_month),
        through_month=int(through_month),
    )


def _load_seed_state(db_path: str, run_id: int) -> Dict[str, Any]:
    """Restore broker/strategy state saved in bt_metrics.extra_json.final_state."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT extra_json FROM bt_metrics WHERE run_id = ? LIMIT 1",
            (int(run_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise ValueError(f"No metrics/extra_json found for run_id={run_id}")
    extra = json.loads(row[0])
    state = extra.get("final_state")
    if not isinstance(state, dict):
        raise ValueError(f"run_id={run_id} extra_json has no final_state dict")
    return state


def _execution_windows(start_ts: int, end_ts: int) -> List[Tuple[str, int, int]]:
    """Build chain steps clipped to [start_ts, end_ts].

    Spans of at most 32 calendar days use a single window (one month).
    Longer spans use quarterly steps, each clipped to the requested range.
    """
    if start_ts >= end_ts:
        raise ValueError(f"start_ts must be < end_ts (got {start_ts} >= {end_ts})")
    span_ms = int(end_ts) - int(start_ts)
    if span_ms <= 32 * _MS_PER_DAY:
        return [("MONTH", int(start_ts), int(end_ts))]
    windows: List[Tuple[str, int, int]] = []
    for name, q_start, q_end in _quarter_windows_2024():
        if q_end < start_ts or q_start > end_ts:
            continue
        windows.append((name, max(q_start, start_ts), min(q_end, end_ts)))
    if not windows:
        return [("RANGE", int(start_ts), int(end_ts))]
    return windows


def _decide_resource_cap(combo_count: int, dataset_candles: int, cpu_cap_pct: float) -> dict:
    profile = detect_resources()
    cpu_ratio = max(0.1, min(1.0, float(cpu_cap_pct) / 100.0))
    cpu_jobs = max(1, math.floor(profile.cpu_count * cpu_ratio))
    if profile.ram_available_bytes is not None:
        per_worker = estimate_worker_ram_bytes(dataset_candles)
        ram_jobs = max(1, math.floor((profile.ram_available_bytes * 0.90) / max(per_worker, 1)))
    else:
        per_worker = None
        ram_jobs = cpu_jobs
    return {
        "cpu_count": profile.cpu_count,
        "ram_total_bytes": profile.ram_total_bytes,
        "ram_available_bytes": profile.ram_available_bytes,
        "cpu_cap_pct": cpu_cap_pct,
        "cpu_jobs_cap": cpu_jobs,
        "ram_jobs_90pct": ram_jobs,
        "estimated_worker_ram_bytes": per_worker,
        "combo_count": combo_count,
        # Forced to 1 for stability on heavy 1s annual workloads.
        "n_jobs_effective": 1,
    }


def _state_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    broker = state.get("broker", {}) if isinstance(state, dict) else {}
    strategy = state.get("strategy", {}) if isinstance(state, dict) else {}
    return {
        "cash": float(broker.get("cash", 0.0)),
        "position_qty": float(broker.get("position_qty", 0.0)),
        "avg_entry": float(broker.get("avg_entry", 0.0)),
        "active_limits": len(strategy.get("active_sell_limits", []) if isinstance(strategy, dict) else []),
        "final_equity": float(state.get("final_equity", 0.0)) if isinstance(state, dict) else 0.0,
    }


def _guard_from_args(args: argparse.Namespace) -> ResourceGuard:
    env_cfg = ResourceGuardConfig.from_env()
    cfg = ResourceGuardConfig(
        cpu_cap_pct=float(args.guard_cpu_cap_pct if args.guard_cpu_cap_pct is not None else env_cfg.cpu_cap_pct),
        ram_cap_pct=float(args.guard_ram_cap_pct if args.guard_ram_cap_pct is not None else env_cfg.ram_cap_pct),
        sample_sec=float(args.guard_sample_sec if args.guard_sample_sec is not None else env_cfg.sample_sec),
        high_watermark_windows=int(
            args.guard_high_windows if args.guard_high_windows is not None else env_cfg.high_watermark_windows
        ),
        recover_windows=int(args.guard_recover_windows if args.guard_recover_windows is not None else env_cfg.recover_windows),
    )
    return ResourceGuard(cfg)


def _guard_wait_if_needed(
    guard: ResourceGuard,
    guard_events: List[Dict[str, Any]],
    context: str,
    backoff_sec: float,
) -> None:
    snap = guard.snapshot()
    for event in guard.consume_events():
        guard_events.append({"context": context, **event})
    if snap.get("throttle_active"):
        guard_events.append(
            {
                "event": "resource_guard_backoff",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "context": context,
                "seconds": float(backoff_sec),
                "snapshot": snap,
            }
        )
        time.sleep(max(1.0, float(backoff_sec)))


def _run_chain_for_profit_factor(
    args: argparse.Namespace,
    strategy_cls,
    execution_windows: List[Tuple[str, int, int]],
    strategy_params: Dict[str, Any],
    guard: ResourceGuard,
    guard_events: List[Dict[str, Any]],
    chain_seed_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state: Dict[str, Any] | None = dict(chain_seed_state) if chain_seed_state else None
    quarter_runs: List[Dict[str, Any]] = []
    for quarter_name, q_start, q_end in execution_windows:
        _guard_wait_if_needed(
            guard=guard,
            guard_events=guard_events,
            context=f"{strategy_params.get('profit_factor')}-{quarter_name}",
            backoff_sec=float(args.guard_backoff_sec),
        )
        initial_cash = float(args.initial_cash)
        if state:
            broker_state = state.get("broker", {})
            if isinstance(broker_state, dict):
                initial_cash = float(broker_state.get("cash", initial_cash))
        cfg = EngineConfig(
            db_path=args.db,
            symbol=args.symbol,
            interval=args.interval,
            start_ts=q_start,
            end_ts=q_end,
            initial_cash=initial_cash,
            fee_rate=args.fee_rate,
            slippage_bps=args.slippage_bps,
            loop_seconds=args.loop_seconds,
            events_mode="lite",
            snapshot_seconds=3600,
        )
        before_state = _state_summary(state or {})
        result = execute_and_persist(
            config=cfg,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            initial_state=state,
        )
        metrics = summarize_run(args.db, run_id=int(result.run_id))["metrics"]
        state = result.final_state
        quarter_runs.append(
            {
                "quarter": quarter_name,
                "start_ts": q_start,
                "end_ts": q_end,
                "run_id": int(result.run_id),
                "metrics": metrics,
                "state_before": before_state,
                "state_after": _state_summary(state),
            }
        )
    final_state = state or {}
    return {
        "profit_factor": float(strategy_params["profit_factor"]),
        "params": dict(strategy_params),
        "final_state": final_state,
        "final_equity": float(final_state.get("final_equity", 0.0)),
        "quarter_runs": quarter_runs,
    }


def _write_log_markdown(path: str, payload: Dict[str, Any]) -> None:
    symbol = str(payload.get("symbol", "")).upper() or "?"
    interval = str(payload.get("interval", "")).lower() or "?"
    window_names = [str(w[0]) for w in payload.get("execution_windows", [])]
    if window_names:
        windows_label = f"{window_names[0]}..{window_names[-1]}"
    else:
        windows_label = "(sin ventanas)"
    lines = [
        f"# Reinicio estricto {symbol} {interval} {windows_label} (encadenado)",
        "",
        "## Configuracion",
        f"- study_name: `{payload['study_name']}`",
        f"- symbol/interval: `{payload['symbol']}` / `{payload['interval']}`",
        f"- initial_cash: `{payload['initial_cash']}`",
        f"- margin_drop_factor fijo: `{payload['margin_drop_factor']}`",
        f"- profit_factor grid: `{payload['profit_factor_grid']}`",
        f"- loop_seconds: `{payload['loop_seconds']}`",
        f"- max_rungs aplicado: `{payload['max_rungs']}`",
        f"- quote_order_qty_usdt: `{payload['quote_order_qty_usdt']}`",
        f"- resource_plan: `{json.dumps(payload['resource_plan'], ensure_ascii=False)}`",
        f"- resource_guard: `{json.dumps(payload['resource_guard'], ensure_ascii=False)}`",
        "",
        "## Metodologia",
        f"- Se evaluo cada `profit_factor` de la malla en cadena cronologica ({windows_label}).",
        "- Cada ventana inicia con el estado final de la ventana anterior (broker + estrategia).",
        "- Se selecciona el `profit_factor` con mayor `final_equity` acumulada tras la ultima ventana.",
        "",
        "## Resultados por candidato",
    ]
    for candidate in payload["candidates"]:
        lines.append(f"- profit_factor `{candidate['profit_factor']}` -> final_equity `{candidate['final_equity']:.6f}`")
    lines += [
        "",
        "## Candidato ganador",
        f"- best_profit_factor: `{payload['best_candidate']['profit_factor']}`",
        f"- best_final_equity: `{payload['best_candidate']['final_equity']:.6f}`",
        "",
        "## Estado por ventana (ganador)",
    ]
    for window in payload["best_candidate"]["quarter_runs"]:
        before = window["state_before"]
        after = window["state_after"]
        lines.extend(
            [
                f"### {window['quarter']} (run_id={window['run_id']})",
                f"- Estado inicial: cash={before['cash']:.6f}, pos={before['position_qty']:.6f}, avg_entry={before['avg_entry']:.6f}, active_limits={before['active_limits']}",
                f"- Estado final: cash={after['cash']:.6f}, pos={after['position_qty']:.6f}, avg_entry={after['avg_entry']:.6f}, active_limits={after['active_limits']}, equity={after['final_equity']:.6f}",
                f"- Metricas ventana: `{json.dumps(window['metrics'], ensure_ascii=False)}`",
                "",
            ]
        )
    guard_events = payload.get("resource_guard_events", []) or []
    lines += [
        "## Eventos de guardia de recursos",
        f"- total_events: `{len(guard_events)}`",
    ]
    for event in guard_events[:20]:
        lines.append(f"- `{json.dumps(event, ensure_ascii=False)}`")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).strip() + "\n")


def run(args: argparse.Namespace) -> None:
    if bool(getattr(args, "chain_by_month", False)):
        execution_windows = _monthly_windows(
            int(args.start_ts),
            int(args.end_ts),
            from_month=int(args.from_month),
            through_month=int(args.through_month),
        )
    else:
        execution_windows = _execution_windows(int(args.start_ts), int(args.end_ts))
    profit_values = _build_profit_grid(args.profit_factor_grid)
    margin_values = _build_margin_grid(args)
    param_combos = _param_combos(profit_values, margin_values)
    quote_order_qty = 8.0
    max_rungs_arg = getattr(args, "max_rungs", None)
    if max_rungs_arg is None:
        max_rungs = min(200, math.floor(args.initial_cash / quote_order_qty))
    elif int(max_rungs_arg) <= 0:
        max_rungs = 0  # unlimited (see DorothyHubStrategy)
    else:
        max_rungs = min(int(max_rungs_arg), math.floor(args.initial_cash / quote_order_qty))
    window_candles = _count_candles(args.db, args.symbol, args.interval, args.start_ts, args.end_ts)
    resources = _decide_resource_cap(
        combo_count=len(param_combos), dataset_candles=window_candles, cpu_cap_pct=args.cpu_cap_pct
    )

    if bool(getattr(args, "explain_only", False)):
        # Dry-run: estimate cost without touching DB or spawning work.
        explain = {
            "study_preview": True,
            "symbol": args.symbol,
            "interval": args.interval,
            "window_candle_count": window_candles,
            "execution_windows": execution_windows,
            "profit_factor_grid": profit_values,
            "margin_drop_grid": margin_values,
            "param_combos": param_combos,
            "max_rungs": max_rungs,
            "resource_plan": resources,
            "workload_estimate": estimate_workload(
                dataset_candles=window_candles,
                n_trials=len(param_combos) * len(execution_windows),
            ),
            "executor": getattr(args, "executor", "serial"),
            "n_jobs": getattr(args, "n_jobs", 1),
            "note": (
                "Los meses dentro de cada combo se encadenan en serie (estado DCA). "
                "Paralelismo solo entre combos distintos de la malla."
            ),
        }
        print(json.dumps(explain, ensure_ascii=False, indent=2))
        return

    stale_runs = abort_stale_runs(args.db)
    guard = _guard_from_args(args)
    guard_events: List[Dict[str, Any]] = []

    chain_seed: Optional[Dict[str, Any]] = None
    seed_run_id = getattr(args, "seed_run_id", None)
    if seed_run_id is not None:
        chain_seed = _load_seed_state(args.db, int(seed_run_id))
        print(
            json.dumps(
                {
                    "seed_run_id": int(seed_run_id),
                    "seed_summary": _state_summary(chain_seed),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    if bool(getattr(args, "chain_by_month", False)):
        mode_tag = "monthly_chain"
    elif len(execution_windows) == 1 and execution_windows[0][0] == "MONTH":
        mode_tag = "month"
    else:
        mode_tag = "chain"
    symbol_slug = str(args.symbol).lower()
    interval_slug = str(args.interval).lower()
    study_name = f"dorothy_{symbol_slug}_{interval_slug}_{mode_tag}_{_now_tag()}"
    output_dir = strict_report_dir(args.output_root, study_name)
    os.makedirs(output_dir, exist_ok=True)
    write_manifest(
        output_dir,
        title=f"Strict run {study_name}",
        summary="Quarterly chained strict run artifacts.",
    )
    sample_params = _enrich_strategy_params(param_combos[0], args, quote_order_qty, max_rungs)
    briefing_paths = _write_pre_run_briefing(
        output_dir=output_dir,
        study_name=study_name,
        args=args,
        execution_windows=execution_windows,
        param_combos=param_combos,
        profit_values=profit_values,
        margin_values=margin_values,
        quote_order_qty=quote_order_qty,
        max_rungs=max_rungs,
        window_candles=window_candles,
        resources=resources,
        sample_strategy_params=sample_params,
    )
    print(json.dumps({"pre_run_briefing": briefing_paths}, ensure_ascii=False), flush=True)
    strategy_cls = get_strategy("dorothy")
    telemetry = TelemetryRecorder(TelemetryConfig(output_dir=output_dir))
    telemetry.sample(
        phase="run:start",
        extra={"profit_grid": profit_values, "margin_grid": margin_values, "max_rungs": max_rungs},
    )

    executor = str(getattr(args, "executor", "serial") or "serial").strip().lower()
    n_jobs = max(1, int(getattr(args, "n_jobs", 1) or 1))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args_ns = {k: v for k, v in vars(args).items() if not k.startswith("_")}

    candidates: List[Dict[str, Any]] = []
    if executor != "serial" and len(param_combos) > 1:
        from backtest.orchestrator import FailureResult, Orchestrator, OrchestratorConfig

        orch = Orchestrator(
            OrchestratorConfig(
                executor=executor,
                n_jobs=n_jobs,
                ram_cap_pct=float(
                    args.guard_ram_cap_pct if args.guard_ram_cap_pct is not None else guard.config.ram_cap_pct
                ),
                cpu_cap_pct=float(
                    args.guard_cpu_cap_pct if args.guard_cpu_cap_pct is not None else guard.config.cpu_cap_pct
                ),
                per_worker_ram_mb=getattr(args, "per_worker_ram_mb", None),
            )
        )
        jobs = [
            {
                "project_root": project_root,
                "args_ns": args_ns,
                "execution_windows": execution_windows,
                "strategy_params": _enrich_strategy_params(combo, args, quote_order_qty, max_rungs),
                "quote_order_qty_usdt": quote_order_qty,
                "max_rungs": max_rungs,
                "chain_seed": chain_seed,
                "cpu_cap_pct": float(args.cpu_cap_pct),
            }
            for combo in param_combos
        ]
        print(
            json.dumps(
                {
                    "parallel_combos": len(param_combos),
                    "executor": executor,
                    "n_jobs": n_jobs,
                    "execution_windows": [w[0] for w in execution_windows],
                    "monthly_chain": "serial dentro de cada worker",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        results = orch.map(_strict_chain_job, jobs)
        for combo, result in zip(param_combos, results):
            if isinstance(result, FailureResult):
                raise RuntimeError(
                    f"Combo pf={combo['profit_factor']} margin={combo['margin_drop_factor']} failed: {result.error}"
                )
            candidates.append(result)
    else:
        threads = _configure_compute_threads(float(args.cpu_cap_pct))
        print(
            json.dumps(
                {
                    "parallel_combos": 1,
                    "executor": "serial",
                    "execution_windows": [w[0] for w in execution_windows],
                    "monthly_chain": "serial por mes (orden cronologico)",
                    "omp_threads": threads,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for combo in param_combos:
            params = _enrich_strategy_params(combo, args, quote_order_qty, max_rungs)
            label = f"pf={combo['profit_factor']}_mdf={combo['margin_drop_factor']}"
            telemetry.sample(phase=f"{label}:start")
            candidates.append(
                _run_chain_for_profit_factor(
                    args=args,
                    strategy_cls=strategy_cls,
                    execution_windows=execution_windows,
                    strategy_params=params,
                    guard=guard,
                    guard_events=guard_events,
                    chain_seed_state=chain_seed,
                )
            )
            telemetry.sample(phase=f"{label}:end")

    if not candidates:
        raise RuntimeError("No se genero ningun candidato para evaluar")
    best_candidate = max(candidates, key=lambda c: float(c.get("final_equity", 0.0)))
    payload = {
        "study_name": study_name,
        "output_dir": output_dir,
        "symbol": args.symbol,
        "interval": args.interval,
        "start_ts": args.start_ts,
        "end_ts": args.end_ts,
        "initial_cash": args.initial_cash,
        "fee_rate": args.fee_rate,
        "slippage_bps": args.slippage_bps,
        "loop_seconds": args.loop_seconds,
        "margin_drop_factor": args.margin_drop_factor,
        "quote_order_qty_usdt": quote_order_qty,
        "max_rungs": max_rungs,
        "profit_factor_grid": profit_values,
        "margin_drop_grid": margin_values,
        "param_combos": param_combos,
        "executor": executor,
        "n_jobs": n_jobs,
        "resource_plan": resources,
        "resource_guard": {
            "cpu_cap_pct": guard.config.cpu_cap_pct,
            "ram_cap_pct": guard.config.ram_cap_pct,
            "sample_sec": guard.config.sample_sec,
            "high_watermark_windows": guard.config.high_watermark_windows,
            "recover_windows": guard.config.recover_windows,
            "backoff_sec": float(args.guard_backoff_sec),
            "final_snapshot": guard.snapshot(),
        },
        "resource_guard_events": guard_events,
        "stale_run_cleanup": stale_runs,
        "window_candle_count": window_candles,
        "execution_windows": execution_windows,
        "candidates": candidates,
        "best_candidate": best_candidate,
        "code_version": git_snapshot(),
    }
    manifest_path = os.path.join(output_dir, "run_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log_path = os.path.join(output_dir, "RESTART_LOG.md")
    _write_log_markdown(log_path, payload)
    telemetry.sample(phase="run:end")
    telemetry.close()
    print(json.dumps({"study_name": study_name, "output_dir": output_dir, "best_candidate": best_candidate}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Strict Dorothy chained restart (default symbol XRPUSDT, interval 1s, "
            "window 2024-01) - el modo --chain-by-month soporta cualquier "
            "anio o rango multianual definido por --start_ts/--end_ts."
        )
    )
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--interval", default="1s")
    parser.add_argument("--start_ts", type=int, default=1704067200000, help="UTC ms; default 2024-01-01")
    parser.add_argument(
        "--end_ts",
        type=int,
        default=1706745599000,
        help=(
            "UTC ms; default 2024-01-31 23:59:59 (un mes). Anio 2024 completo: "
            "1735689599000. Anio 2025 completo: 1767225599000."
        ),
    )
    parser.add_argument("--initial_cash", type=float, default=1000.0)
    parser.add_argument("--fee_rate", type=float, default=0.001)
    parser.add_argument("--slippage_bps", type=float, default=2.0)
    parser.add_argument("--loop_seconds", type=int, default=29)
    parser.add_argument("--margin_drop_factor", type=float, default=0.0005)
    parser.add_argument(
        "--margin-drop-grid",
        dest="margin_drop_grid",
        default=None,
        help="Malla margin_drop (coma). Si se omite, usa --margin_drop_factor.",
    )
    parser.add_argument("--profit_factor_grid", default="0.01,0.03,0.05,0.06")
    parser.add_argument(
        "--executor",
        choices=["serial", "joblib", "ray"],
        default="serial",
        help="Paraleliza solo entre combos de la malla (no entre meses encadenados).",
    )
    parser.add_argument("--n-jobs", type=int, default=1, help="Workers para --executor joblib|ray.")
    parser.add_argument(
        "--per-worker-ram-mb",
        type=int,
        default=None,
        help="RAM max por worker aislado (orquestador).",
    )
    parser.add_argument("--cpu_cap_pct", type=float, default=90.0)
    parser.add_argument("--guard_cpu_cap_pct", type=float, default=None)
    parser.add_argument("--guard_ram_cap_pct", type=float, default=None)
    parser.add_argument("--guard_sample_sec", type=float, default=None)
    parser.add_argument("--guard_high_windows", type=int, default=None)
    parser.add_argument("--guard_recover_windows", type=int, default=None)
    parser.add_argument("--guard_backoff_sec", type=float, default=10.0)
    parser.add_argument("--output_root", default="reports")
    parser.add_argument(
        "--explain_only",
        action="store_true",
        help="Print resource/workload estimate and exit without running the chain.",
    )
    parser.add_argument(
        "--chain-by-month",
        action="store_true",
        help=(
            "Encadena meses calendario (YYYY-MM) derivados dinamicamente de "
            "[start_ts, end_ts] en orden cronologico, en lugar de un solo "
            "bloque o trimestres. Soporta cualquier anio o rango multianual."
        ),
    )
    parser.add_argument(
        "--from-month",
        type=int,
        default=1,
        help=(
            "Filtro mes-de-anio (1-12) al usar --chain-by-month. En rangos "
            "multianuales se aplica por anio. Default 1 = sin filtro inferior."
        ),
    )
    parser.add_argument(
        "--through-month",
        type=int,
        default=12,
        help=(
            "Filtro mes-de-anio (1-12) al usar --chain-by-month. En rangos "
            "multianuales se aplica por anio. Default 12 = sin filtro superior."
        ),
    )
    parser.add_argument(
        "--seed-run-id",
        type=int,
        default=None,
        help="run_id cuyo final_state inicia la cadena (p.ej. 4 tras enero).",
    )
    parser.add_argument(
        "--max-rungs",
        type=int,
        default=None,
        help="Tope de rungs DCA; 0 o negativo = sin limite. Omitir usa min(200, cash/8).",
    )
    parser.add_argument(
        "--volumen-incremental",
        action="store_true",
        help="Activa accesorio VolumenIncremental (x1.05 notional si cash > capital inicial de corrida).",
    )
    parser.add_argument(
        "--volumen-incremental-multiplier",
        type=float,
        default=1.05,
        help="Multiplicador cuando cash disponible supera initial_cash de la corrida.",
    )
    parser.add_argument(
        "--no-trend-gate",
        action="store_true",
        help="Desactiva gate 1 (pec_trend BULLISH). Las ventas por TP siguen activas.",
    )
    parser.add_argument(
        "--no-entry-gate",
        action="store_true",
        help="Sin efecto si entry gate no esta activo; usar --require-entry-gate para live parity.",
    )
    parser.add_argument(
        "--require-entry-gate",
        action="store_true",
        help="Activa gate 2 (pec_entry_gate BLOCKED) para compras, como en live Pecunator.",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
