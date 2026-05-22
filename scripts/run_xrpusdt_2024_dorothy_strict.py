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

from backtest.cleanup import abort_stale_runs
from backtest.engine import EngineConfig
from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.report_paths import strict_report_dir, write_manifest
from backtest.registry import get_strategy
from backtest.repro import git_snapshot
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

# XRPUSDT 1s 2024 monthly bounds (from data/klines manifests).
_MONTH_WINDOWS_2024_1S: List[Tuple[str, int, int]] = [
    ("M01", 1704067200000, 1706745599000),
    ("M02", 1706745600000, 1709251199000),
    ("M03", 1709251200000, 1711929599000),
    ("M04", 1711929600000, 1714521599000),
    ("M05", 1714521600000, 1717199999000),
    ("M06", 1717200000000, 1719791999000),
    ("M07", 1719792000000, 1722470399000),
    ("M08", 1722470400000, 1725148799000),
    ("M09", 1725148800000, 1727740799000),
    ("M10", 1727740800000, 1730419199000),
    ("M11", 1730419200000, 1733011199000),
    ("M12", 1733011200000, 1735689599000),
]


def _monthly_windows(
    start_ts: int,
    end_ts: int,
    from_month: int = 1,
    through_month: int = 12,
) -> List[Tuple[str, int, int]]:
    """Calendar months for 2024 clipped to [start_ts, end_ts]."""
    windows: List[Tuple[str, int, int]] = []
    for name, m_start, m_end in _MONTH_WINDOWS_2024_1S:
        month_num = int(name[1:])
        if month_num < int(from_month) or month_num > int(through_month):
            continue
        if m_end < start_ts or m_start > end_ts:
            continue
        windows.append((name, max(m_start, start_ts), min(m_end, end_ts)))
    if not windows:
        raise ValueError("No monthly windows overlap the requested range")
    return windows


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
    lines = [
        "# Reinicio estricto XRPUSDT 1s 2024 (trimestral encadenado)",
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
        "- Se evaluo cada `profit_factor` de la malla en cadena Q1->Q2->Q3->Q4.",
        "- Cada trimestre inicia con el estado final del trimestre anterior (broker + estrategia).",
        "- Se selecciona el `profit_factor` con mayor `final_equity` acumulada tras Q4.",
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
        "## Estado por trimestre (ganador)",
    ]
    for quarter in payload["best_candidate"]["quarter_runs"]:
        before = quarter["state_before"]
        after = quarter["state_after"]
        lines.extend(
            [
                f"### {quarter['quarter']} (run_id={quarter['run_id']})",
                f"- Estado inicial: cash={before['cash']:.6f}, pos={before['position_qty']:.6f}, avg_entry={before['avg_entry']:.6f}, active_limits={before['active_limits']}",
                f"- Estado final: cash={after['cash']:.6f}, pos={after['position_qty']:.6f}, avg_entry={after['avg_entry']:.6f}, active_limits={after['active_limits']}, equity={after['final_equity']:.6f}",
                f"- Metricas trimestre: `{json.dumps(quarter['metrics'], ensure_ascii=False)}`",
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
    study_name = f"dorothy_xrpusdt_1s_{mode_tag}_{_now_tag()}"
    output_dir = strict_report_dir(args.output_root, study_name)
    os.makedirs(output_dir, exist_ok=True)
    write_manifest(
        output_dir,
        title=f"Strict run {study_name}",
        summary="Quarterly chained strict run artifacts.",
    )
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
                "strategy_params": combo,
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
                    "monthly_chain": "serial M01..M12",
                    "omp_threads": threads,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for combo in param_combos:
            params = {
                **combo,
                "quote_order_qty_usdt": float(quote_order_qty),
                "max_rungs": int(max_rungs),
            }
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
    parser = argparse.ArgumentParser(description="Strict 2024 XRPUSDT Dorothy quarterly chained restart")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--interval", default="1s")
    parser.add_argument("--start_ts", type=int, default=1704067200000, help="UTC ms; default 2024-01-01")
    parser.add_argument(
        "--end_ts",
        type=int,
        default=1706745599000,
        help="UTC ms; default 2024-01-31 23:59:59 (un mes). Anio completo: 1735689599000",
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
        help="Encadena un mes tras otro (M01..M12) en lugar de un solo bloque o trimestres.",
    )
    parser.add_argument("--from-month", type=int, default=1, help="Primer mes 2024 (1-12) al usar --chain-by-month.")
    parser.add_argument("--through-month", type=int, default=12, help="Ultimo mes 2024 (1-12) al usar --chain-by-month.")
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
    run(parser.parse_args())


if __name__ == "__main__":
    main()
