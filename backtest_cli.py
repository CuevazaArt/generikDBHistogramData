"""Simple terminal interface for backtesting and optimization."""
import argparse
import datetime as dt
import json
import os
import sys
from statistics import mean
from typing import Optional

from backtest.cleanup import abort_stale_runs, purge_aborted_run_events
from backtest.dataset_artifact import prepare_dataset_artifact, verify_dataset_artifact
from backtest.engine import EngineConfig
from backtest.guards import ResourceGuardConfig
from backtest.optimize import optimize_strategy
from backtest.report_paths import run_report_dir, study_report_dir, write_manifest
from backtest.resources import detect_resources, explain_recommendation, recommend_n_jobs
from backtest.sweet_spot import SweetSpotConfig, run_sweet_spot_search
from backtest.sweet_spot_report import build_unified_report
from backtest.plots import (
    export_run_integrated_report,
    export_run_bot_summary,
    export_study_optuna_summary,
    export_study_summary_table,
    export_summary,
    plot_equity_and_drawdown,
    plot_fill_activity_heatmap,
    plot_monthly_return_heatmap,
    plot_monthly_return_spectrum,
    plot_optuna_param_heatmap,
    plot_signal_histograms,
    plot_trials,
)
from backtest import library as bot_library
from backtest.registry import get_strategy, params_from_cli
from backtest.runner import execute_and_persist, execute_and_persist_resumable
from backtest.storage import (
    list_runs,
    run_equity_curve,
    run_descriptor,
    run_events,
    run_signal_events,
    study_trials,
    summarize_run,
    top_trials,
    trial_objectives,
)
from db import init_db


DEFAULT_N_JOBS = max(1, os.cpu_count() or 1)


def _parse_ts(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    return int(v)


def _ms_to_iso(v: Optional[int]) -> Optional[str]:
    if v is None:
        return None
    try:
        return dt.datetime.fromtimestamp(v / 1000.0, tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def _run_reports_dir(base_output_dir: str, run_id: int) -> str:
    return run_report_dir(base_output_dir, run_id)


def _study_reports_dir(base_output_dir: str, study_name: str) -> str:
    return study_report_dir(base_output_dir, study_name)


def _run_diagnostics(db_path: str, run_id: int) -> dict:
    rows = run_events(db_path, run_id=run_id)
    if not rows:
        return {}
    util_values = []
    max_open = 0
    cur_open = 0
    for _seq, _ts, event_type, _side, cash, equity, payload_json in rows:
        if equity is not None and cash is not None and float(equity) > 0:
            u = 1.0 - (float(cash) / float(equity))
            util_values.append(max(0.0, min(1.0, u)))
        payload = {}
        if payload_json:
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {}
        if event_type == "fill":
            if payload.get("active_limits_after") is not None:
                cur_open = int(payload["active_limits_after"])
            elif payload.get("remaining_limits") is not None:
                cur_open = int(payload["remaining_limits"])
        if cur_open > max_open:
            max_open = cur_open
    return {
        "capital_utilization_avg_pct": round((mean(util_values) if util_values else 0.0) * 100.0, 4),
        "capital_utilization_max_pct": round((max(util_values) if util_values else 0.0) * 100.0, 4),
        "max_open_orders_simultaneous": int(max_open),
    }


def _warn_if_extreme_1s(args: argparse.Namespace) -> None:
    """Emit advisory stderr lines when the operator picks risky 1s settings.

    Pure warnings; never coerces or rewrites argument values. Only the `run`
    handler calls this (Fase 4 scope).
    """
    if getattr(args, "interval", None) != "1s":
        return
    msgs: list[str] = []
    events_mode = getattr(args, "events_mode", None)
    if events_mode == "full":
        msgs.append(
            "events_mode='full' con interval=1s genera ~31M eventos para un anio. "
            "Considera --events_mode lite para reducir I/O de Parquet."
        )
    snapshot_seconds = getattr(args, "snapshot_seconds", None)
    if snapshot_seconds is not None and snapshot_seconds < 60:
        msgs.append(
            f"snapshot_seconds={snapshot_seconds} con interval=1s genera muchos snapshots; "
            "valores >=60 son recomendables para runs largos."
        )
    cp_bars = getattr(args, "checkpoint_every_bars", None)
    if cp_bars is None:
        msgs.append(
            "interval=1s sin --checkpoint_every_bars: un crash perdera todo el progreso. "
            "Recomendado --checkpoint_every_bars 500000 (cada ~5.8 dias de sim)."
        )
    for m in msgs:
        print(f"[advertencia 1s] {m}", file=sys.stderr)


def _run_once(args: argparse.Namespace) -> None:
    _warn_if_extreme_1s(args)
    strategy_cls = get_strategy(args.strategy)
    strategy_params = params_from_cli(args, args.strategy)
    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        start_ts=_parse_ts(args.start_ts),
        end_ts=_parse_ts(args.end_ts),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        use_heikin_ashi=args.heikin_ashi,
        loop_seconds=int(args.loop_seconds) if args.loop_seconds is not None else None,
        sma_fast=int(strategy_params.get("fast", 10)),
        sma_slow=int(strategy_params.get("slow", 30)),
        checkpoint_every_bars=getattr(args, "checkpoint_every_bars", None),
        checkpoint_every_sim_seconds=getattr(args, "checkpoint_every_sim_seconds", None),
        checkpoints_dir=getattr(args, "checkpoints_dir", None),
    )
    # Dispatch through the resume-aware runner only when --resume was used;
    # otherwise stay on the legacy fast path.
    if os.environ.get("BACKTEST_RESUME_RUN_ID"):
        runner_fn = execute_and_persist_resumable
    else:
        runner_fn = execute_and_persist
    result = runner_fn(
        config=cfg,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
    )
    print(f"Run terminado. run_id={result.run_id}")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))


def _optimize(args: argparse.Namespace) -> None:
    strategy_cls = get_strategy(args.strategy)
    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        start_ts=_parse_ts(args.start_ts),
        end_ts=_parse_ts(args.end_ts),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        use_heikin_ashi=args.heikin_ashi,
    )
    n_jobs = int(args.n_jobs) if int(args.n_jobs) > 0 else DEFAULT_N_JOBS
    executor = getattr(args, "executor", "serial")
    if executor != "serial":
        from backtest.config import AppConfig
        from backtest.optimize import optimize_strategy_parallel

        study = optimize_strategy_parallel(
            db_path=args.db,
            study_name=args.study,
            strategy_cls=strategy_cls,
            base_config=cfg,
            trials=int(args.trials),
            n_jobs=n_jobs,
            executor=executor,
            app_config=AppConfig.from_env(),
            ram_cap_pct=float(getattr(args, "ram_cap_pct", 80.0)),
            cpu_cap_pct=float(getattr(args, "cpu_cap_pct", 80.0)),
            per_worker_ram_mb=getattr(args, "per_worker_ram_mb", None),
            per_trial_timeout_sec=getattr(args, "per_trial_timeout_sec", None),
        )
    else:
        study = optimize_strategy(
            db_path=args.db,
            study_name=args.study,
            strategy_cls=strategy_cls,
            base_config=cfg,
            trials=args.trials,
            n_jobs=n_jobs,
            timeout=args.timeout,
        )
    print(f"Optimización completa. best_value={study.best_value:.6f}")
    print(f"best_params={study.best_params}")
    print(f"n_jobs usados: {n_jobs} (cpu_count={DEFAULT_N_JOBS})")
    print(f"executor: {executor}")


def _show(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        summary = summarize_run(args.db, run_id=args.run_id, events_limit=args.events_limit)
        desc = run_descriptor(args.db, run_id=args.run_id) or {}
        if desc:
            desc["first_event_iso_utc"] = _ms_to_iso(desc.get("first_event_time"))
            desc["last_event_iso_utc"] = _ms_to_iso(desc.get("last_event_time"))
            desc.update(_run_diagnostics(args.db, run_id=args.run_id))
        print(f"Resumen run_id={args.run_id}")
        if desc:
            print("Descriptor:")
            print(json.dumps(desc, ensure_ascii=False, indent=2))
        print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
        print("Eventos recientes:")
        for e in summary["recent_events"]:
            print(e)
        return
    print("Últimos runs:")
    for r in list_runs(args.db, limit=args.limit):
        print(r)
    if args.study:
        print(f"Top trials ({args.study}):")
        for t in top_trials(args.db, study_name=args.study, limit=10):
            print(t)


def _plot(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        run_output_dir = _run_reports_dir(args.output_dir, args.run_id)
        write_manifest(
            run_output_dir,
            title=f"Run {args.run_id} deliverables",
            summary="Artifacts generated by backtest_cli plot --run_id.",
        )
        eq_rows = run_equity_curve(args.db, run_id=args.run_id)
        paths = plot_equity_and_drawdown(
            eq_rows,
            output_dir=run_output_dir,
            run_id=args.run_id,
            max_plot_points=getattr(args, "max_plot_points", None),
        )
        signal_rows = run_signal_events(args.db, run_id=args.run_id)
        signal_paths = plot_signal_histograms(
            signal_rows=signal_rows,
            output_dir=run_output_dir,
            run_id=args.run_id,
            bins=args.signal_bins,
        )
        metrics = summarize_run(args.db, run_id=args.run_id)["metrics"]
        descriptor = run_descriptor(args.db, run_id=args.run_id) or {}
        descriptor["first_event_iso_utc"] = _ms_to_iso(descriptor.get("first_event_time"))
        descriptor["last_event_iso_utc"] = _ms_to_iso(descriptor.get("last_event_time"))
        descriptor.update(_run_diagnostics(args.db, run_id=args.run_id))
        spectrum_path = plot_monthly_return_spectrum(eq_rows, output_dir=run_output_dir, run_id=args.run_id)
        heatmap_path = plot_monthly_return_heatmap(eq_rows, output_dir=run_output_dir, run_id=args.run_id)
        activity_heatmap = plot_fill_activity_heatmap(
            run_events(args.db, run_id=args.run_id),
            output_dir=run_output_dir,
            run_id=args.run_id,
        )
        export = export_summary(run_output_dir, f"run_{args.run_id}", metrics, eq_rows, descriptor=descriptor)
        bot_summary_path = export_run_bot_summary(
            output_dir=run_output_dir,
            file_stem=f"run_{args.run_id}",
            descriptor=descriptor,
            metrics=metrics,
            bot_description=(
                "Dorothy es una estrategia de acumulacion y descarga por niveles. "
                "Compra cuando el precio cae por debajo de un umbral relativo al anchor "
                "de limite activo, y cierra solo cuando se activan niveles objetivo de venta."
            ),
            optuna_summary=(
                "Optuna se aplico para maximizar total_return explorando combinaciones de "
                "profit_factor y margin_drop_factor, manteniendo fijas las restricciones "
                "operativas (nocional 6-10 USDT y maximo de 200 ordenes abiertas)."
            ),
        )
        graph_catalog = {**paths, **signal_paths}
        if spectrum_path:
            graph_catalog["monthly_return_spectrum"] = spectrum_path
        if heatmap_path:
            graph_catalog["monthly_return_heatmap"] = heatmap_path
        if activity_heatmap:
            graph_catalog["fill_activity_heatmap"] = activity_heatmap
        integrated_report_path = export_run_integrated_report(
            output_dir=run_output_dir,
            file_stem=f"run_{args.run_id}",
            descriptor=descriptor,
            metrics=metrics,
            equity_rows=eq_rows,
            graph_paths=graph_catalog,
        )
        print(f"Gráficas y archivos exportados en: {run_output_dir}")
        extra = {}
        if spectrum_path:
            extra["monthly_return_spectrum"] = spectrum_path
        if heatmap_path:
            extra["monthly_return_heatmap"] = heatmap_path
        if activity_heatmap:
            extra["fill_activity_heatmap"] = activity_heatmap
        extra["bot_summary_md"] = bot_summary_path
        extra["integrated_report_md"] = integrated_report_path
        print({**paths, **signal_paths, **extra, **export})
    if args.study:
        study_output_dir = _study_reports_dir(args.output_dir, args.study)
        write_manifest(
            study_output_dir,
            title=f"Study {args.study} deliverables",
            summary="Artifacts generated by backtest_cli plot --study.",
        )
        objective_rows = trial_objectives(args.db, study_name=args.study, limit=1000)
        p = plot_trials(
            trial_rows=objective_rows,
            output_dir=study_output_dir,
            study_name=args.study,
        )
        summary_paths = export_study_summary_table(
            output_dir=study_output_dir,
            study_name=args.study,
            trials=study_trials(args.db, study_name=args.study, limit=2000),
        )
        param_heatmap = plot_optuna_param_heatmap(
            trials=study_trials(args.db, study_name=args.study, limit=2000),
            output_dir=study_output_dir,
            study_name=args.study,
        )
        summary_payload = {}
        try:
            with open(summary_paths["study_summary_json"], "r", encoding="utf-8") as fh:
                summary_payload = json.load(fh)
        except Exception:
            summary_payload = {}
        optuna_summary_md = export_study_optuna_summary(
            output_dir=study_output_dir,
            study_name=args.study,
            summary_payload=summary_payload,
        )
        print(f"Resumen final de estudio en: {study_output_dir}")
        extra_summary = dict(summary_paths)
        if param_heatmap:
            extra_summary["study_param_heatmap"] = param_heatmap
        extra_summary["optuna_summary_md"] = optuna_summary_md
        print(extra_summary)
        if p:
            print(f"Gráfica de trials: {p}")


def _sweet_spot(args: argparse.Namespace) -> None:
    profile = detect_resources()
    print(_build_separator())
    print("Buscador de seteo dulce - resumen de recursos")
    print(explain_recommendation(args.mode, profile=profile))
    print(_build_separator())

    cfg = SweetSpotConfig(
        db_path=args.db,
        strategy_name=args.strategy,
        symbol=args.symbol,
        interval=args.interval,
        full_start_ts=int(args.start_ts),
        full_end_ts=int(args.end_ts),
        initial_cash=float(args.initial_cash),
        fee_rate=float(args.fee_rate),
        slippage_bps=float(args.slippage_bps),
        use_heikin_ashi=bool(args.heikin_ashi),
        loop_seconds=int(args.loop_seconds) if args.loop_seconds is not None else None,
        coarse_window_pct=float(args.coarse_window_pct),
        coarse_trials=int(args.coarse_trials),
        coarse_mode=args.mode,
        coarse_objective_metric=args.objective_metric,
        coarse_direction=args.direction,
        coarse_sampler=args.sampler,
        coarse_seed=int(args.seed) if args.seed is not None else None,
        focused_top_k=int(args.top_k),
        focused_mode="safe",
        focused_events_mode="full",
        guard_cpu_cap_pct=float(args.guard_cpu_cap_pct),
        guard_ram_cap_pct=float(args.guard_ram_cap_pct),
        guard_sample_sec=float(args.guard_sample_sec),
        guard_high_watermark_windows=int(args.guard_high_windows),
        guard_recover_windows=int(args.guard_recover_windows),
        guard_backoff_sec=float(args.guard_backoff_sec),
        coarse_wave_trials=int(args.coarse_wave_trials),
    )
    result = run_sweet_spot_search(cfg, progress_cb=lambda m: print(m))
    if result.best_focused_run is None:
        print("No se obtuvo ningun candidato valido en la fase focal.")
        return
    bundle = build_unified_report(args.db, result, output_dir=args.output_dir)
    print(_build_separator())
    print(f"Reporte unificado: {bundle['report_md']}")
    print(f"Carpeta del reporte: {bundle['report_dir']}")
    print(f"Mejor run_id: {bundle['best_run_id']}")
    print(f"Mejor seteo: {bundle['best_params']}")
    print(_build_separator())


def _cleanup(args: argparse.Namespace) -> None:
    aborted = abort_stale_runs(args.db)
    purged = purge_aborted_run_events(args.db) if args.purge_events else {"deleted_events": 0}
    print({"aborted_runs": aborted["aborted_runs"], **purged})


def _library(args: argparse.Namespace) -> None:
    """Dispatch library subactions (list/show/new/publish/validate/...)."""
    action = (getattr(args, "library_action", None) or "list").strip().lower()
    if action == "list":
        entries = bot_library.list_entries(
            kind=getattr(args, "kind", None),
            tag=getattr(args, "tag", None),
            include_workspace=bool(getattr(args, "workspace", False)),
        )
        print(f"Entradas encontradas: {len(entries)}")
        for entry in entries:
            tag_str = ",".join(entry.manifest.get("tags") or [])
            workspace_tag = " [workspace]" if entry.workspace else ""
            ref_tag = " [reference_only]" if entry.is_reference_only() else ""
            print(
                f"- {entry.name:35s} kind={entry.kind:9s} v={entry.version:8s}"
                f" tags={tag_str}{workspace_tag}{ref_tag}"
            )
        return
    if action == "show":
        entry = bot_library.load_entry(args.name, include_workspace=True)
        print(f"== {entry.name} ==")
        print(json.dumps(entry.manifest, ensure_ascii=False, indent=2, default=str))
        print("\n-- notes (head) --")
        notes = bot_library.get_notes(args.name)
        head = "\n".join(notes.splitlines()[:30]) if notes else "(sin notes.md)"
        print(head)
        presets = bot_library.list_presets(args.name)
        print("\n-- presets --")
        print(", ".join(presets) if presets else "(ninguno)")
        return
    if action == "new":
        path = bot_library.scaffold_entry(args.name, kind=args.kind, workspace=True)
        print(f"Draft creado en: {path}")
        return
    if action == "publish":
        path = bot_library.publish_entry(args.name, target_kind=getattr(args, "kind", None))
        print(f"Publicado en: {path}")
        return
    if action == "validate":
        result = bot_library.validate_entry(
            args.name, include_workspace=bool(getattr(args, "workspace", False))
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if action == "notes":
        text = bot_library.get_notes(args.name)
        print(text if text else "(sin notes.md)")
        return
    if action == "presets":
        presets = bot_library.list_presets(args.name)
        print("\n".join(presets) if presets else "(sin presets)")
        return
    if action == "refresh":
        path = bot_library.refresh_index()
        print(f"Index regenerado: {path}")
        return
    if action == "import-aporte":
        path = bot_library.import_aporte(args.name)
        print(f"Draft importado en: {path}")
        return
    print(f"Acción library desconocida: {action}")


def _walk_forward(args: argparse.Namespace) -> None:
    """Run a rolling walk-forward evaluation and write the report bundle."""
    import csv
    from datetime import datetime, timezone

    from backtest.walkforward_runner import (
        WalkForwardConfig,
        run_walk_forward,
    )

    strategy_cls = get_strategy(args.strategy)
    strategy_params = params_from_cli(args, args.strategy) if hasattr(args, "fast") else {}
    if not strategy_params:
        # Walk-forward CLI does not surface the per-strategy hyper-params yet, so we fall
        # back to the strategy class defaults. Optimize-per-fold can override them anyway.
        strategy_params = {}

    train_window_ms = int(float(args.train_window_days) * 86_400_000)
    test_window_ms = int(float(args.test_window_days) * 86_400_000)
    step_ms = int(float(args.step_days) * 86_400_000)
    if train_window_ms <= 0 or test_window_ms <= 0 or step_ms <= 0:
        print("ERROR: train/test/step window sizes must be > 0 days.", file=sys.stderr)
        raise SystemExit(2)

    cfg = WalkForwardConfig(
        full_start_ts=int(args.start_ts),
        full_end_ts=int(args.end_ts),
        train_window_ms=train_window_ms,
        test_window_ms=test_window_ms,
        step_ms=step_ms,
        anchored=bool(args.anchored),
    )
    engine_cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        initial_cash=float(args.initial_cash),
        fee_rate=float(args.fee_rate),
        slippage_bps=float(args.slippage_bps),
        events_mode="lite",
    )

    optimization_kwargs = None
    if bool(args.optimize_per_fold):
        optimization_kwargs = {"trials": int(args.trials_per_fold), "n_jobs": 1}

    result = run_walk_forward(
        cfg=cfg,
        strategy_name=strategy_cls.name,
        strategy_params=strategy_params,
        engine_config=engine_cfg,
        db_path=args.db,
        optimize_per_fold=bool(args.optimize_per_fold),
        optimization_kwargs=optimization_kwargs,
    )

    output_dir = str(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "fold_summary.csv")
    md_path = os.path.join(output_dir, "walk_forward_report.md")

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "fold_index",
            "train_start_ts",
            "train_end_ts",
            "test_start_ts",
            "test_end_ts",
            "train_run_id",
            "test_run_id",
            "train_total_return",
            "test_total_return",
            "test_sharpe",
        ])
        for fold in result.fold_results:
            train_ts = fold.get("train_window") or (None, None)
            test_ts = fold.get("test_window") or (None, None)
            tm = fold.get("train_metrics") or {}
            xm = fold.get("test_metrics") or {}
            writer.writerow([
                int(fold.get("fold_index", 0)),
                int(train_ts[0] or 0),
                int(train_ts[1] or 0),
                int(test_ts[0] or 0),
                int(test_ts[1] or 0),
                fold.get("train_run_id"),
                fold.get("test_run_id"),
                float(tm.get("total_return", 0.0)),
                float(xm.get("total_return", 0.0)),
                float(xm.get("sharpe", 0.0)),
            ])

    aggregated = result.aggregated or {}
    decay_pct = float(aggregated.get("decay_test_vs_train_pct", 0.0))
    if decay_pct > 25.0:
        verdict = "Decaimiento alto: posible sobreajuste en train."
    elif decay_pct > 5.0:
        verdict = "Decaimiento moderado: revisar parametros y robustez."
    elif decay_pct < -5.0:
        verdict = "Test mejor que train: muestra buena generalizacion."
    else:
        verdict = "Train y test alineados: estrategia estable en ventanas."

    now_iso = datetime.now(timezone.utc).isoformat()
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Reporte walk-forward - {args.strategy} {args.symbol} {args.interval}\n\n")
        f.write("## Resumen rapido\n\n")
        f.write("| Dato | Valor |\n")
        f.write("|---|---:|\n")
        f.write(f"| Folds totales | {int(aggregated.get('n_folds', 0))} |\n")
        f.write(f"| Modo | {'anchored (expandiendo)' if bool(args.anchored) else 'rolling'} |\n")
        f.write(f"| Optimize per fold | {'si' if bool(args.optimize_per_fold) else 'no'} |\n")
        f.write(f"| Train mean total_return | {float(aggregated.get('train_mean_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Test mean total_return | {float(aggregated.get('test_mean_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Decay test vs train | {decay_pct:.2f}% |\n")
        f.write(f"| Test mean sharpe | {float(aggregated.get('test_mean_sharpe', 0.0)):.4f} |\n")
        f.write(f"| Test median sharpe | {float(aggregated.get('test_median_sharpe', 0.0)):.4f} |\n")
        f.write(f"| Test best total_return | {float(aggregated.get('test_best_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Test worst total_return | {float(aggregated.get('test_worst_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Correlacion train/test | {float(aggregated.get('train_test_correlation_total_return', 0.0)):.4f} |\n")
        f.write(f"| Veredicto | {verdict} |\n\n")

        f.write("## Configuracion\n\n")
        f.write("| Campo | Valor |\n")
        f.write("|---|---|\n")
        f.write(f"| Estrategia | `{args.strategy}` |\n")
        f.write(f"| Simbolo | `{args.symbol}` |\n")
        f.write(f"| Timeframe | `{args.interval}` |\n")
        f.write(f"| Periodo | `{int(args.start_ts)}` -> `{int(args.end_ts)}` (ms UTC) |\n")
        f.write(f"| train_window_days | {float(args.train_window_days):.4f} |\n")
        f.write(f"| test_window_days | {float(args.test_window_days):.4f} |\n")
        f.write(f"| step_days | {float(args.step_days):.4f} |\n")
        f.write(f"| initial_cash | {float(args.initial_cash):.2f} |\n")
        f.write(f"| fee_rate | {float(args.fee_rate):.6f} |\n")
        f.write(f"| slippage_bps | {float(args.slippage_bps):.4f} |\n")
        if bool(args.optimize_per_fold):
            f.write(f"| trials_per_fold | {int(args.trials_per_fold)} |\n")
        f.write(f"| generado | `{now_iso}` |\n\n")

        f.write("## Lectura practica\n\n")
        f.write("- `decay_test_vs_train_pct` > 0 indica que el test rinde peor que el train (overfitting).\n")
        f.write("- Cerca de 0 sugiere parametros estables. Negativo significa que el test supero al train.\n")
        f.write("- Revisar `correlacion train/test`: valores altos indican que el ranking de folds es consistente.\n\n")

        f.write("## Folds\n\n")
        f.write("| fold | train_total_return | test_total_return | test_sharpe |\n")
        f.write("|---:|---:|---:|---:|\n")
        for row in aggregated.get("per_fold_summary", []) or []:
            f.write(
                f"| {int(row.get('fold_index', 0))} "
                f"| {float(row.get('train_total_return', 0.0)) * 100.0:.2f}% "
                f"| {float(row.get('test_total_return', 0.0)) * 100.0:.2f}% "
                f"| {float(row.get('test_sharpe', 0.0)):.4f} |\n"
            )
        f.write("\n")

        f.write("## Archivos\n\n")
        f.write(f"- `{os.path.basename(csv_path)}`: detalle por fold (timestamps + run_ids).\n")
        f.write("- Artefactos por fold: `data/events/run_<run_id>/...` y `data/equity/run_<run_id>/equity.parquet`.\n")

    print(f"Reporte walk-forward: {md_path}")


def _multi_symbol(args: argparse.Namespace) -> None:
    """Run a strategy across a basket of symbols and write the report bundle."""
    import csv
    from datetime import datetime, timezone

    from backtest.multi_symbol import MultiSymbolConfig, run_multi_symbol

    strategy_cls = get_strategy(args.strategy)
    strategy_params = params_from_cli(args, args.strategy) if hasattr(args, "fast") else {}
    if not strategy_params:
        strategy_params = {}

    symbols = [s.strip() for s in str(args.symbols or "").split(",") if s.strip()]
    if not symbols:
        print("ERROR: --symbols vacio. Pasa al menos un simbolo separado por comas.", file=sys.stderr)
        raise SystemExit(2)

    cfg = MultiSymbolConfig(
        symbols=symbols,
        interval=args.interval,
        start_ts=int(args.start_ts) if args.start_ts is not None else None,
        end_ts=int(args.end_ts) if args.end_ts is not None else None,
        initial_cash_per_symbol=float(args.initial_cash_per_symbol),
        share_cash_pool=bool(args.share_cash_pool),
    )
    engine_cfg = EngineConfig(
        db_path=args.db,
        symbol=symbols[0],
        interval=args.interval,
        initial_cash=float(args.initial_cash_per_symbol),
        fee_rate=float(args.fee_rate),
        slippage_bps=float(args.slippage_bps),
        events_mode="lite",
    )

    result = run_multi_symbol(
        cfg=cfg,
        strategy_name=strategy_cls.name,
        strategy_params=strategy_params,
        engine_config=engine_cfg,
        db_path=args.db,
    )

    output_dir = str(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "per_symbol_summary.csv")
    md_path = os.path.join(output_dir, "multi_symbol_report.md")

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "symbol",
            "run_id",
            "total_return",
            "sharpe",
            "win_rate",
            "num_trades",
            "final_equity",
        ])
        for row in result.aggregated.get("per_symbol_summary", []) or []:
            symbol = str(row.get("symbol"))
            payload = result.per_symbol.get(symbol, {})
            metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
            writer.writerow([
                symbol,
                payload.get("run_id"),
                float(row.get("total_return", 0.0)),
                float(row.get("sharpe", 0.0)),
                float(row.get("win_rate", 0.0)),
                float(row.get("num_trades", 0.0)),
                float(metrics.get("final_equity", 0.0)),
            ])

    aggregated = result.aggregated or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Reporte multi-symbol - {args.strategy} {args.interval}\n\n")
        f.write("## Resumen rapido\n\n")
        f.write("| Dato | Valor |\n")
        f.write("|---|---:|\n")
        f.write(f"| Simbolos evaluados | {int(aggregated.get('n_symbols', 0))} |\n")
        f.write(f"| Estrategia | `{args.strategy}` |\n")
        f.write(f"| Pool compartido | {'si (no implementado)' if bool(args.share_cash_pool) else 'no (independiente por simbolo)'} |\n")
        f.write(f"| Mean total_return | {float(aggregated.get('mean_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Median total_return | {float(aggregated.get('median_total_return', 0.0)) * 100.0:.2f}% |\n")
        f.write(f"| Mejor simbolo | `{aggregated.get('best_symbol', '')}` ({float(aggregated.get('best_symbol_total_return', 0.0)) * 100.0:.2f}%) |\n")
        f.write(f"| Peor simbolo | `{aggregated.get('worst_symbol', '')}` ({float(aggregated.get('worst_symbol_total_return', 0.0)) * 100.0:.2f}%) |\n")
        f.write(f"| Dispersion (best - worst) | {float(aggregated.get('dispersion_pct', 0.0)) * 100.0:.2f}% |\n\n")

        f.write("## Configuracion\n\n")
        f.write("| Campo | Valor |\n")
        f.write("|---|---|\n")
        f.write(f"| Simbolos | `{', '.join(symbols)}` |\n")
        f.write(f"| Timeframe | `{args.interval}` |\n")
        start_str = str(args.start_ts) if args.start_ts is not None else "<auto>"
        end_str = str(args.end_ts) if args.end_ts is not None else "<auto>"
        f.write(f"| Periodo | `{start_str}` -> `{end_str}` (ms UTC) |\n")
        f.write(f"| initial_cash_per_symbol | {float(args.initial_cash_per_symbol):.2f} |\n")
        f.write(f"| fee_rate | {float(args.fee_rate):.6f} |\n")
        f.write(f"| slippage_bps | {float(args.slippage_bps):.4f} |\n")
        f.write(f"| generado | `{now_iso}` |\n\n")

        f.write("## Lectura practica\n\n")
        f.write("- Una dispersion alta entre el mejor y el peor simbolo sugiere que la estrategia depende fuertemente del activo.\n")
        f.write("- Una dispersion baja con `total_return` positivo indica robustez cruzada.\n")
        f.write("- `--share_cash_pool` esta reservado para una fase futura (joint pool).\n\n")

        f.write("## Por simbolo\n\n")
        f.write("| simbolo | total_return | sharpe | win_rate | num_trades |\n")
        f.write("|---|---:|---:|---:|---:|\n")
        for row in aggregated.get("per_symbol_summary", []) or []:
            f.write(
                f"| `{row.get('symbol', '')}` "
                f"| {float(row.get('total_return', 0.0)) * 100.0:.2f}% "
                f"| {float(row.get('sharpe', 0.0)):.4f} "
                f"| {float(row.get('win_rate', 0.0)) * 100.0:.2f}% "
                f"| {int(float(row.get('num_trades', 0.0)))} |\n"
            )
        f.write("\n")

        f.write("## Archivos\n\n")
        f.write(f"- `{os.path.basename(csv_path)}`: detalle por simbolo (run_id + metricas).\n")
        f.write("- Artefactos por simbolo: `data/events/run_<run_id>/...` y `data/equity/run_<run_id>/equity.parquet`.\n")

    print(f"Reporte multi-symbol: {md_path}")


def _cache(args: argparse.Namespace) -> None:
    """Manage the optional Parquet cache for kline windows.

    Subactions:
      - materialize: build per-month Parquet files for a symbol/interval/window.
      - verify: report which monthly buckets exist and which are missing.

    Both honor `BACKTEST_PARQUET_CACHE` by checking pyarrow availability and
    falling back gracefully when missing.
    """
    from backtest.data_cache import (
        CACHE_ROOT_DEFAULT,
        _bucket_path,
        _month_buckets,
        is_available,
        materialize_window,
    )

    if not is_available():
        print({"status": "unavailable", "reason": "pyarrow not installed"})
        return

    if args.action == "materialize":
        paths = materialize_window(
            db_path=args.db,
            symbol=args.symbol,
            interval=args.interval,
            start_ts=int(args.start_ts),
            end_ts=int(args.end_ts),
            cache_root=args.cache_root,
            overwrite=bool(args.overwrite),
        )
        print({"status": "ok", "files": paths, "count": len(paths)})
        return

    if args.action == "verify":
        present, missing = [], []
        for year, month, _bs, _be in _month_buckets(int(args.start_ts), int(args.end_ts)):
            p = _bucket_path(args.cache_root, args.symbol, args.interval, year, month)
            (present if os.path.exists(p) else missing).append(p)
        print({"status": "ok", "present": present, "missing": missing})
        return

    print({"status": "error", "reason": f"unknown cache action: {args.action}"})


def _dataset(args: argparse.Namespace) -> None:
    """Prepare or verify generic reusable dataset artifacts."""
    if args.action == "prepare":
        payload = prepare_dataset_artifact(
            db_path=args.db,
            symbol=args.symbol,
            interval=args.interval,
            start_ts=int(args.start_ts),
            end_ts=int(args.end_ts),
            output_base=args.output_dir,
            artifact_name=args.name,
            cache_root=args.cache_root,
            prefer_parquet_cache=not bool(args.no_parquet_cache),
            overwrite_cache=bool(args.overwrite_cache),
            max_gaps=int(args.max_gaps),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.action == "verify":
        payload = verify_dataset_artifact(args.manifest)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(json.dumps({"status": "error", "reason": f"unknown dataset action: {args.action}"}))


def _attach_engine_flags(p: argparse.ArgumentParser, *, include_resume: bool = True) -> None:
    """Forward-compatible Fase 0/1/2 flags shared by run/optimize/sweet-spot."""
    p.add_argument(
        "--engine",
        choices=("python", "rust"),
        default="python",
        help="Backtest engine to use. 'rust' falls back to 'python' if genericbt_core is not installed.",
    )
    if include_resume:
        p.add_argument(
            "--resume",
            type=int,
            default=None,
            metavar="RUN_ID",
            help="Resume a previously interrupted run (forward-compat; not implemented until Fase 2).",
        )
    _attach_read_side_flags(p)


def _attach_read_side_flags(p: argparse.ArgumentParser) -> None:
    """Storage-selection flags used by every subcommand that reads metadata."""
    p.add_argument(
        "--pg_dsn",
        default=None,
        help="PostgreSQL DSN. Overrides env PG_DSN when provided.",
    )
    p.add_argument(
        "--metadata-backend",
        dest="metadata_backend",
        choices=("sqlite", "pg", "auto"),
        default="auto",
        help="Where to read/write run/trial metadata. 'auto' picks 'pg' if PG_DSN is set, else 'sqlite'.",
    )


def _apply_runtime_flags(args: argparse.Namespace) -> None:
    """Stash CLI overrides into os.environ so AppConfig.from_env() observes them.

    EngineConfig itself is intentionally untouched here; consumers downstream pick
    these values up through the central AppConfig facade.
    """
    pg_dsn = getattr(args, "pg_dsn", None)
    if pg_dsn:
        os.environ["PG_DSN"] = pg_dsn

    backend = getattr(args, "metadata_backend", None)
    if backend:
        if backend == "auto":
            resolved = "pg" if os.environ.get("PG_DSN") else "sqlite"
        else:
            resolved = backend
        os.environ["BACKTEST_METADATA_BACKEND"] = resolved

    resume = getattr(args, "resume", None)
    if resume is not None:
        os.environ["BACKTEST_RESUME_RUN_ID"] = str(int(resume))
        # Fase 2: the real dispatch happens in `_run_once`, which switches
        # to `execute_and_persist_resumable`. We do not print anything here
        # because the runner emits its own `[resume] ...` line once the
        # checkpoint path is resolved (so the message also tells the user
        # which file was actually loaded).

    engine = getattr(args, "engine", None)
    if engine:
        if engine == "rust":
            try:
                import genericbt_core  # noqa: F401
                os.environ["BACKTEST_ENGINE_KIND"] = "rust"
            except ImportError:
                sys.stderr.write(
                    "WARNING: --engine rust requested but 'genericbt_core' is not importable. "
                    "Falling back to the Python engine.\n"
                )
                os.environ["BACKTEST_ENGINE_KIND"] = "python"
        else:
            os.environ["BACKTEST_ENGINE_KIND"] = engine


def _pg_init(args: argparse.Namespace) -> int:
    """Apply pending PostgreSQL migrations and report current schema version."""
    from backtest.migrations import (
        apply_migrations,
        current_version,
        list_migration_files,
    )

    dsn = getattr(args, "dsn", None) or os.getenv("PG_DSN")
    dry_run = bool(getattr(args, "dry_run", False))

    files = list_migration_files()
    if dry_run:
        if dsn:
            print(f"DSN: {dsn}")
        else:
            print("DSN: <not set> (PG_DSN env unset and --dsn omitted)")
        print(f"Discovered {len(files)} migration files:")
        for path in files:
            print(f"  - {path.name}")
        print("(dry-run: no statements executed)")
        return 0

    if not dsn:
        print("ERROR: --dsn not provided and PG_DSN env is unset", file=sys.stderr)
        return 1

    try:
        applied = apply_migrations(dsn)
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1

    if applied:
        print(f"Applied {len(applied)} migrations: {', '.join(applied)}")
    else:
        print("Database already up to date. No migrations applied.")
    try:
        final = current_version(dsn)
    except Exception as exc:
        print(f"WARNING: could not read current schema version: {exc}", file=sys.stderr)
        final = None
    print(f"Current schema version: {final if final is not None else '<none>'}")
    return 0


def _migrate(args: argparse.Namespace) -> int:
    """Delegate the legacy-data migration to scripts/migrate_to_pg.py."""
    dsn = getattr(args, "dsn", None) or os.getenv("PG_DSN")
    dry_run = bool(getattr(args, "dry_run", False))
    if not dsn and not dry_run:
        print("ERROR: --dsn not provided and PG_DSN env is unset", file=sys.stderr)
        return 1

    try:
        from scripts.migrate_to_pg import run_migration
    except ImportError as exc:
        print(f"ERROR: cannot import scripts.migrate_to_pg: {exc}", file=sys.stderr)
        return 1

    try:
        return int(run_migration(
            dsn=dsn or "",
            from_sqlite=getattr(args, "from_sqlite", "klines.db"),
            data_root=getattr(args, "data_root", "data"),
            dry_run=dry_run,
        ))
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1


def _build_separator(width: int = 60) -> str:
    return "-" * width


def _menu(db_path: str) -> None:
    while True:
        print("\n=== Backtesting Terminal ===")
        print("0) Salir")
        print("1) Ejecutar backtest")
        print("2) Optimizar estrategia (Optuna)")
        print("3) Ver runs/trials")
        print("4) Graficar run")
        print("5) Buscar seteo dulce (sweet-spot + reporte)")
        print("6) Limpiar runs colgados")
        print("8) Inicializar PostgreSQL local")
        print("9) Migrar artefactos legacy a PostgreSQL")
        choice = input("Opción: ").strip()
        if choice == "1":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            args = argparse.Namespace(
                db=db_path,
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                start_ts=None,
                end_ts=None,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
                heikin_ashi=False,
                loop_seconds=60,
                fast=int((input("SMA fast [10]: ").strip() or "10")),
                slow=int((input("SMA slow [30]: ").strip() or "30")),
                profit_factor=float((input("Dorothy profit_factor [0.05]: ").strip() or "0.05")),
                margin_drop_factor=float((input("Dorothy margin_drop_factor [0.004]: ").strip() or "0.004")),
                quote_order_qty_usdt=float((input("Dorothy quote_order_qty_usdt [8]: ").strip() or "8")),
                max_rungs=int((input("Dorothy max_rungs [5]: ").strip() or "5")),
            )
            _run_once(args)
        elif choice == "2":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            study = input("Study name [sma_opt]: ").strip() or "sma_opt"
            trials = int((input("Trials [30]: ").strip() or "30"))
            jobs = int((input(f"n_jobs CPU [{DEFAULT_N_JOBS}]: ").strip() or str(DEFAULT_N_JOBS)))
            args = argparse.Namespace(
                db=db_path,
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                start_ts=None,
                end_ts=None,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
                heikin_ashi=False,
                study=study,
                trials=trials,
                n_jobs=jobs,
                timeout=None,
                quote_order_qty_usdt=8.0,
                max_rungs=5,
            )
            _optimize(args)
        elif choice == "3":
            _show(argparse.Namespace(db=db_path, run_id=None, limit=20, study=None, events_limit=25))
        elif choice == "4":
            run_id = int(input("run_id: ").strip())
            _plot(argparse.Namespace(db=db_path, run_id=run_id, study=None, output_dir="reports", signal_bins=30))
        elif choice == "5":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            start_ts = input("start_ts (ms UTC, requerido): ").strip()
            end_ts = input("end_ts (ms UTC, requerido): ").strip()
            mode = (input("Modo recursos [safe|balanced|max-stable|adaptive_80] (def adaptive_80): ").strip() or "adaptive_80").lower()
            loop_seconds_raw = input("loop_seconds (vacio=desactivado): ").strip()
            env_guard = ResourceGuardConfig.from_env()
            sweet_args = argparse.Namespace(
                db=db_path,
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                start_ts=start_ts,
                end_ts=end_ts,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
                heikin_ashi=False,
                loop_seconds=int(loop_seconds_raw) if loop_seconds_raw else None,
                mode=mode,
                coarse_window_pct=0.25,
                coarse_trials=int((input("trials fase 1 [60]: ").strip() or "60")),
                top_k=int((input("top_k fase 2 [5]: ").strip() or "5")),
                objective_metric="total_return",
                direction="maximize",
                sampler="tpe",
                seed=42,
                guard_cpu_cap_pct=float(env_guard.cpu_cap_pct),
                guard_ram_cap_pct=float(env_guard.ram_cap_pct),
                guard_sample_sec=float(env_guard.sample_sec),
                guard_high_windows=int(env_guard.high_watermark_windows),
                guard_recover_windows=int(env_guard.recover_windows),
                guard_backoff_sec=10.0,
                coarse_wave_trials=12,
                output_dir="reports",
            )
            _sweet_spot(sweet_args)
        elif choice == "6":
            _cleanup(argparse.Namespace(db=db_path, purge_events=False))
        elif choice in ("0", "7"):
            # "7" kept as a backwards-compatible alias for the previous Salir slot.
            break
        elif choice == "8":
            default_dsn = os.getenv("PG_DSN", "")
            prompt = f"DSN PostgreSQL [{default_dsn or '<vacio>'}]: "
            dsn = input(prompt).strip() or default_dsn
            rc = _pg_init(argparse.Namespace(dsn=dsn or None, dry_run=False))
            if rc != 0:
                print(f"(pg-init terminó con código {rc})")
        elif choice == "9":
            default_dsn = os.getenv("PG_DSN", "")
            prompt = f"DSN PostgreSQL [{default_dsn or '<vacio>'}]: "
            dsn = input(prompt).strip() or default_dsn
            from_sqlite = input("Ruta SQLite legacy [klines.db]: ").strip() or "klines.db"
            data_root = input("Raíz del backup Parquet [data]: ").strip() or "data"
            dry = (input("Dry-run? [s/N]: ").strip().lower() in ("s", "y", "yes", "si", "sí"))
            rc = _migrate(argparse.Namespace(
                dsn=dsn or None,
                from_sqlite=from_sqlite,
                data_root=data_root,
                dry_run=dry,
            ))
            if rc != 0:
                print(f"(migrate terminó con código {rc})")
        else:
            print("Opción inválida.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtesting + optimization terminal")
    parser.add_argument("--db", default="klines.db")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run")
    p_run.add_argument("--strategy", default="dorothy")
    p_run.add_argument("--symbol", required=True)
    p_run.add_argument("--interval", required=True)
    p_run.add_argument("--start_ts")
    p_run.add_argument("--end_ts")
    p_run.add_argument("--initial_cash", type=float, default=10000.0)
    p_run.add_argument("--fee_rate", type=float, default=0.001)
    p_run.add_argument("--slippage_bps", type=float, default=2.0)
    p_run.add_argument("--heikin_ashi", action="store_true")
    p_run.add_argument("--loop_seconds", type=int, help="Strategy execution loop in seconds")
    p_run.add_argument("--fast", type=int, default=10)
    p_run.add_argument("--slow", type=int, default=30)
    p_run.add_argument("--profit_factor", type=float, default=0.05)
    p_run.add_argument("--margin_drop_factor", type=float, default=0.004)
    p_run.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_run.add_argument("--max_rungs", type=int, default=5)
    # --- Agartha (moonshot + trailing): aplican solo cuando --strategy agartha ---
    p_run.add_argument("--trailing_stop_pct", type=float, default=30.0,
                       help="Agartha: % de retroceso desde el pico antes de vender.")
    p_run.add_argument("--activation_profit_pct", type=float, default=0.0,
                       help="Agartha: % de ganancia minima antes de activar trailing (0=desde inicio).")
    p_run.add_argument("--max_holding_bars", type=int, default=0,
                       help="Agartha: time stop en barras (0=sin limite).")
    p_run.add_argument("--breakeven_lock_pct", type=float, default=0.0,
                       help="Agartha: si peak>=entry*(1+x/100), el trailing nunca baja del entry.")
    p_run.add_argument("--partial_tp_pct", type=float, default=0.0,
                       help="Agartha: TP parcial a este % sobre entry (0=off).")
    p_run.add_argument("--partial_tp_size_pct", type=float, default=0.0,
                       help="Agartha: fraccion de la posicion a vender en TP parcial (0..1).")
    p_run.add_argument("--allow_reentry", action="store_true",
                       help="Agartha: permitir re-entrada tras cierre (default single-shot).")
    # --- Fase 2: checkpoint flags (only on p_run; other subparsers untouched) ---
    p_run.add_argument("--checkpoint_every_bars", type=int, default=None)
    p_run.add_argument("--checkpoint_every_sim_seconds", type=int, default=None)
    p_run.add_argument(
        "--checkpoints_dir",
        type=str,
        default=None,
        help="Default: data/checkpoints/run_<id> based on StoragePaths.",
    )

    p_opt = sub.add_parser("optimize")
    p_opt.add_argument("--strategy", default="dorothy")
    p_opt.add_argument("--symbol", required=True)
    p_opt.add_argument("--interval", required=True)
    p_opt.add_argument("--study", default="sma_opt")
    p_opt.add_argument("--start_ts")
    p_opt.add_argument("--end_ts")
    p_opt.add_argument("--initial_cash", type=float, default=10000.0)
    p_opt.add_argument("--fee_rate", type=float, default=0.001)
    p_opt.add_argument("--slippage_bps", type=float, default=2.0)
    p_opt.add_argument("--heikin_ashi", action="store_true")
    p_opt.add_argument("--trials", type=int, default=30)
    p_opt.add_argument("--n_jobs", type=int, default=DEFAULT_N_JOBS)
    p_opt.add_argument("--timeout", type=int)
    p_opt.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_opt.add_argument("--max_rungs", type=int, default=5)
    # Agartha defaults para optimize (los rangos se barren via search_overrides)
    p_opt.add_argument("--trailing_stop_pct", type=float, default=30.0)
    p_opt.add_argument("--activation_profit_pct", type=float, default=0.0)
    p_opt.add_argument("--max_holding_bars", type=int, default=0)
    p_opt.add_argument("--breakeven_lock_pct", type=float, default=0.0)
    p_opt.add_argument("--partial_tp_pct", type=float, default=0.0)
    p_opt.add_argument("--partial_tp_size_pct", type=float, default=0.0)
    p_opt.add_argument("--allow_reentry", action="store_true")
    p_opt.add_argument(
        "--executor",
        choices=["ray", "joblib", "serial"],
        default="joblib",
        help="Parallel executor backend. 'serial' uses the legacy single-process loop.",
    )
    p_opt.add_argument(
        "--ram_cap_pct",
        type=float,
        default=80.0,
        help="ResourceGuard RAM cap percentage for the orchestrator during optimize.",
    )
    p_opt.add_argument(
        "--cpu_cap_pct",
        type=float,
        default=80.0,
        help="ResourceGuard CPU cap percentage for the orchestrator during optimize.",
    )
    p_opt.add_argument(
        "--per_worker_ram_mb",
        type=int,
        default=None,
        help="Hard RAM cap (MB) applied per isolated trial subprocess.",
    )
    p_opt.add_argument(
        "--per_trial_timeout_sec",
        type=int,
        default=None,
        help="Wall-clock timeout (seconds) per trial subprocess.",
    )

    p_show = sub.add_parser("show")
    p_show.add_argument("--run_id", type=int)
    p_show.add_argument("--limit", type=int, default=20)
    p_show.add_argument("--study")
    p_show.add_argument("--events_limit", type=int, default=25)

    p_plot = sub.add_parser("plot")
    p_plot.add_argument("--run_id", type=int)
    p_plot.add_argument("--study")
    p_plot.add_argument("--output_dir", default="reports")
    p_plot.add_argument("--signal_bins", type=int, default=30)
    p_plot.add_argument(
        "--max_plot_points", type=int, default=None,
        help="Downsamplea la curva de equity a N puntos via LTTB antes de plotear. "
             "Recomendado 10000 para interval=1s; None preserva todos los puntos.",
    )

    p_sweet = sub.add_parser("sweet-spot", help="Buscar el seteo dulce en dos fases y producir reporte unificado")
    env_guard = ResourceGuardConfig.from_env()
    p_sweet.add_argument("--strategy", default="dorothy")
    p_sweet.add_argument("--symbol", required=True)
    p_sweet.add_argument("--interval", required=True)
    p_sweet.add_argument("--start_ts", required=True, help="Inicio del periodo (ms UTC)")
    p_sweet.add_argument("--end_ts", required=True, help="Fin del periodo (ms UTC)")
    p_sweet.add_argument("--initial_cash", type=float, default=10000.0)
    p_sweet.add_argument("--fee_rate", type=float, default=0.001)
    p_sweet.add_argument("--slippage_bps", type=float, default=2.0)
    p_sweet.add_argument("--heikin_ashi", action="store_true")
    p_sweet.add_argument("--loop_seconds", type=int)
    p_sweet.add_argument(
        "--mode",
        default="adaptive_80",
        choices=("safe", "balanced", "max-stable", "adaptive_80"),
    )
    p_sweet.add_argument("--coarse_window_pct", type=float, default=0.25)
    p_sweet.add_argument("--coarse_trials", type=int, default=60)
    p_sweet.add_argument("--top_k", type=int, default=5)
    p_sweet.add_argument("--objective_metric", default="total_return")
    p_sweet.add_argument("--direction", default="maximize", choices=("maximize", "minimize"))
    p_sweet.add_argument("--sampler", default="tpe", choices=("tpe", "random"))
    p_sweet.add_argument("--seed", type=int, default=42)
    p_sweet.add_argument("--guard_cpu_cap_pct", type=float, default=float(env_guard.cpu_cap_pct))
    p_sweet.add_argument("--guard_ram_cap_pct", type=float, default=float(env_guard.ram_cap_pct))
    p_sweet.add_argument("--guard_sample_sec", type=float, default=float(env_guard.sample_sec))
    p_sweet.add_argument("--guard_high_windows", type=int, default=int(env_guard.high_watermark_windows))
    p_sweet.add_argument("--guard_recover_windows", type=int, default=int(env_guard.recover_windows))
    p_sweet.add_argument("--guard_backoff_sec", type=float, default=10.0)
    p_sweet.add_argument("--coarse_wave_trials", type=int, default=12)
    p_sweet.add_argument("--output_dir", default="reports")
    p_sweet.add_argument("--max_plot_points", type=int, default=None, help="(idem)")
    p_sweet.add_argument(
        "--executor",
        choices=["ray", "joblib", "serial"],
        default="joblib",
        help="Parallel executor backend used during the coarse and focused sweet-spot phases.",
    )
    p_sweet.add_argument(
        "--ram_cap_pct",
        type=float,
        default=80.0,
        help="ResourceGuard RAM cap percentage shared by both sweet-spot phases.",
    )
    p_sweet.add_argument(
        "--cpu_cap_pct",
        type=float,
        default=80.0,
        help="ResourceGuard CPU cap percentage shared by both sweet-spot phases.",
    )
    p_sweet.add_argument(
        "--per_worker_ram_mb",
        type=int,
        default=None,
        help="Hard RAM cap (MB) applied per isolated trial subprocess during sweet-spot search.",
    )
    p_sweet.add_argument(
        "--per_trial_timeout_sec",
        type=int,
        default=None,
        help="Wall-clock timeout (seconds) per trial subprocess during sweet-spot search.",
    )

    p_clean = sub.add_parser("cleanup", help="Marcar runs colgados como aborted y purgar eventos")
    p_clean.add_argument("--purge_events", action="store_true")

    p_cache = sub.add_parser("cache", help="Gestionar cache columnar Parquet de klines")
    p_cache.add_argument("action", choices=("materialize", "verify"))
    p_cache.add_argument("--symbol", required=True)
    p_cache.add_argument("--interval", required=True)
    p_cache.add_argument("--start_ts", type=int, required=True, help="Inicio del periodo (ms UTC)")
    p_cache.add_argument("--end_ts", type=int, required=True, help="Fin del periodo (ms UTC)")
    p_cache.add_argument("--cache_root", default="reports/cache/parquet")
    p_cache.add_argument("--overwrite", action="store_true")

    p_dataset = sub.add_parser("dataset", help="Preparar/verificar artefactos de dataset reutilizables")
    p_dataset.add_argument("action", choices=("prepare", "verify"))
    p_dataset.add_argument("--manifest", help="Ruta a manifest.json del artefacto (requerido para verify)")
    p_dataset.add_argument("--symbol", help="Simbolo, p.ej. BTCUSDT (requerido para prepare)")
    p_dataset.add_argument("--interval", help="Intervalo, p.ej. 1m o 1s (requerido para prepare)")
    p_dataset.add_argument("--start_ts", type=int, help="Inicio de ventana (ms UTC) para prepare")
    p_dataset.add_argument("--end_ts", type=int, help="Fin de ventana (ms UTC) para prepare")
    p_dataset.add_argument("--name", default=None, help="Nombre opcional del artefacto")
    p_dataset.add_argument("--output_dir", default="reports")
    p_dataset.add_argument("--cache_root", default="reports/cache/parquet")
    p_dataset.add_argument("--no_parquet_cache", action="store_true")
    p_dataset.add_argument("--overwrite_cache", action="store_true")
    p_dataset.add_argument("--max_gaps", type=int, default=1000)

    p_wf = sub.add_parser("walk-forward", help="Evaluacion walk-forward (folds rodantes)")
    p_wf.add_argument("--strategy", required=True)
    p_wf.add_argument("--symbol", required=True)
    p_wf.add_argument("--interval", required=True)
    p_wf.add_argument("--start_ts", type=int, required=True)
    p_wf.add_argument("--end_ts", type=int, required=True)
    p_wf.add_argument("--train_window_days", type=float, required=True)
    p_wf.add_argument("--test_window_days", type=float, required=True)
    p_wf.add_argument("--step_days", type=float, required=True)
    p_wf.add_argument("--anchored", action="store_true")
    p_wf.add_argument("--optimize_per_fold", action="store_true")
    p_wf.add_argument("--trials_per_fold", type=int, default=30)
    p_wf.add_argument("--initial_cash", type=float, default=10000.0)
    p_wf.add_argument("--fee_rate", type=float, default=0.001)
    p_wf.add_argument("--slippage_bps", type=float, default=2.0)
    p_wf.add_argument("--output_dir", default="reports/walkforward")

    p_multi = sub.add_parser("multi-symbol", help="Una estrategia sobre varios simbolos")
    p_multi.add_argument("--strategy", required=True)
    p_multi.add_argument("--symbols", required=True, help="lista separada por comas, p.ej. BTCUSDT,XRPUSDT")
    p_multi.add_argument("--interval", required=True)
    p_multi.add_argument("--start_ts", type=int)
    p_multi.add_argument("--end_ts", type=int)
    p_multi.add_argument("--initial_cash_per_symbol", type=float, default=10000.0)
    p_multi.add_argument("--share_cash_pool", action="store_true")
    p_multi.add_argument("--fee_rate", type=float, default=0.001)
    p_multi.add_argument("--slippage_bps", type=float, default=2.0)
    p_multi.add_argument("--output_dir", default="reports/multi_symbol")

    p_lib = sub.add_parser("library", help="Operar sobre la biblioteca de bots/indicadores/tools")
    lib_sub = p_lib.add_subparsers(dest="library_action")
    p_lib_list = lib_sub.add_parser("list", help="Listar entradas de la biblioteca")
    p_lib_list.add_argument("--kind", choices=("bot", "indicator", "tool"))
    p_lib_list.add_argument("--tag")
    p_lib_list.add_argument("--workspace", action="store_true")
    p_lib_show = lib_sub.add_parser("show", help="Mostrar manifest + notes head")
    p_lib_show.add_argument("name")
    p_lib_new = lib_sub.add_parser("new", help="Scaffold de un draft en workspace/")
    p_lib_new.add_argument("name")
    p_lib_new.add_argument("--kind", default="bot", choices=("bot", "indicator", "tool"))
    p_lib_publish = lib_sub.add_parser("publish", help="Publicar draft de workspace/ a su carpeta final")
    p_lib_publish.add_argument("name")
    p_lib_publish.add_argument("--kind", choices=("bot", "indicator", "tool"))
    p_lib_validate = lib_sub.add_parser("validate", help="Validar manifest + imports + instanciación")
    p_lib_validate.add_argument("name")
    p_lib_validate.add_argument("--workspace", action="store_true")
    p_lib_notes = lib_sub.add_parser("notes", help="Imprimir notes.md")
    p_lib_notes.add_argument("name")
    p_lib_presets = lib_sub.add_parser("presets", help="Listar presets de la entrada")
    p_lib_presets.add_argument("name")
    lib_sub.add_parser("refresh", help="Regenerar library/_index.json")
    p_lib_import = lib_sub.add_parser(
        "import-aporte",
        help="Scaffold draft prefilled con la lógica de aportes/<name>.py",
    )
    p_lib_import.add_argument("name")

    p_pg = sub.add_parser(
        "pg-init",
        help="Aplicar migraciones SQL sobre PostgreSQL (idempotente).",
    )
    p_pg.add_argument("--dsn", default=None, help="DSN PostgreSQL. Por defecto, env PG_DSN.")
    p_pg.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Listar las migraciones pendientes sin aplicarlas.",
    )

    p_mig = sub.add_parser(
        "migrate",
        help="Migrar artefactos legacy (SQLite + Parquet) a PostgreSQL.",
    )
    p_mig.add_argument("--dsn", default=None, help="DSN PostgreSQL. Por defecto, env PG_DSN.")
    p_mig.add_argument(
        "--from-sqlite",
        dest="from_sqlite",
        default="klines.db",
        help="Ruta al SQLite legacy (default: klines.db).",
    )
    p_mig.add_argument(
        "--data-root",
        dest="data_root",
        default="data",
        help="Raíz del backup Parquet de klines (default: data).",
    )
    p_mig.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Mostrar qué se migraría sin tocar PostgreSQL ni el sistema de ficheros.",
    )

    _attach_engine_flags(p_run)
    _attach_engine_flags(p_opt)
    _attach_engine_flags(p_sweet)
    _attach_read_side_flags(p_show)
    _attach_read_side_flags(p_plot)

    sub.add_parser("menu")
    args = parser.parse_args()
    _apply_runtime_flags(args)

    # PG-only subcommands short-circuit before touching the SQLite metadata DB
    # so a missing PG / missing psycopg never blocks legacy invocations either.
    if args.cmd == "pg-init":
        raise SystemExit(_pg_init(args))
    if args.cmd == "migrate":
        raise SystemExit(_migrate(args))

    init_db(args.db)
    try:
        bot_library.register_with_strategy_registry()
    except Exception:
        # Library wiring is best-effort: never break legacy CLI invocations.
        pass
    if args.cmd == "run":
        _run_once(args)
    elif args.cmd == "optimize":
        _optimize(args)
    elif args.cmd == "show":
        _show(args)
    elif args.cmd == "plot":
        _plot(args)
    elif args.cmd == "sweet-spot":
        _sweet_spot(args)
    elif args.cmd == "cleanup":
        _cleanup(args)
    elif args.cmd == "cache":
        _cache(args)
    elif args.cmd == "dataset":
        if args.action == "prepare":
            missing = [
                name
                for name in ("symbol", "interval", "start_ts", "end_ts")
                if getattr(args, name, None) in (None, "")
            ]
            if missing:
                raise SystemExit(f"dataset prepare requiere: {', '.join(missing)}")
        elif args.action == "verify":
            if not getattr(args, "manifest", None):
                raise SystemExit("dataset verify requiere --manifest")
        _dataset(args)
    elif args.cmd == "walk-forward":
        _walk_forward(args)
    elif args.cmd == "multi-symbol":
        _multi_symbol(args)
    elif args.cmd == "library":
        _library(args)
    else:
        _menu(args.db)


if __name__ == "__main__":
    main()

