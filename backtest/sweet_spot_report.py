"""Build a single, human-friendly report for a sweet-spot search.

The output is a Markdown file that a non-technical reader can use to:
- Understand whether the strategy reached a "sweet" setup.
- Read each generated chart in plain words.
- Replicate the winning setup with a short usage guide.

The generator orchestrates the existing plotting helpers so the report links
to the same artifacts produced by `plot --run_id`.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.plots import (
    ensure_dir,
    export_run_bot_summary,
    export_run_integrated_report,
    export_summary,
    plot_equity_and_drawdown,
    plot_fill_activity_heatmap,
    plot_monthly_return_heatmap,
    plot_monthly_return_spectrum,
    plot_signal_histograms,
)
from backtest.report_paths import study_report_dir, write_manifest
from backtest.storage import (
    run_descriptor,
    run_equity_curve,
    run_events,
    run_signal_events,
    summarize_run,
)
from backtest.sweet_spot import SweetSpotResult


def _load_via_duckdb(
    run_id: int, data_root: str = "data"
) -> Optional[Dict[str, List[Tuple]]]:
    """Return equity/signal/event rows from Parquet, or ``None`` to fall back.

    The shapes match what ``backtest.storage`` returns from SQLite so the
    rest of :func:`build_unified_report` is backend agnostic. ``None`` means
    the Parquet artefacts are missing or DuckDB is not installed; the caller
    should then read from SQLite as before.
    """
    try:
        from backtest import duckdb_reads
    except ImportError:
        return None
    if not duckdb_reads.is_available():
        return None
    if not duckdb_reads.has_equity_parquet(int(run_id), data_root):
        return None
    try:
        eq_rows = duckdb_reads.equity_curve_from_parquet(int(run_id), data_root=data_root)
        sig_rows = duckdb_reads.signal_events_from_parquet(int(run_id), data_root=data_root)
        ev_rows = duckdb_reads.run_events_from_parquet(int(run_id), data_root=data_root)
    except Exception:
        return None
    return {"eq_rows": eq_rows, "sig_rows": sig_rows, "ev_rows": ev_rows}


def _ms_to_iso(v: Optional[int]) -> str:
    if v is None:
        return ""
    try:
        return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100.0:.2f}%"
    except Exception:
        return "-"


def _money(x: Any) -> str:
    try:
        return f"{float(x):,.2f}"
    except Exception:
        return "-"


def _classify_setup(metrics: Dict[str, float]) -> Dict[str, str]:
    """Return a non-technical verdict + traffic-light labels per dimension."""
    total_return = float(metrics.get("total_return", 0.0) or 0.0)
    max_dd = float(metrics.get("max_drawdown", 0.0) or 0.0)
    sortino = float(metrics.get("sortino", 0.0) or 0.0)
    calmar = float(metrics.get("calmar", 0.0) or 0.0)
    win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    num_trades = float(metrics.get("num_trades", 0.0) or 0.0)

    def light(score: int) -> str:
        return {2: "verde", 1: "amarillo", 0: "rojo"}.get(score, "amarillo")

    rtn_score = 2 if total_return >= 0.20 else (1 if total_return > 0 else 0)
    dd_score = 2 if max_dd <= 0.15 else (1 if max_dd <= 0.30 else 0)
    sortino_score = 2 if sortino >= 1.5 else (1 if sortino >= 0.5 else 0)
    calmar_score = 2 if calmar >= 1.0 else (1 if calmar >= 0.3 else 0)
    win_score = 2 if win_rate >= 0.55 else (1 if win_rate >= 0.40 else 0)
    pf_score = 2 if profit_factor >= 1.5 else (1 if profit_factor >= 1.1 else 0)
    activity_score = 2 if num_trades >= 50 else (1 if num_trades >= 10 else 0)

    overall = rtn_score + dd_score + sortino_score + calmar_score + win_score + pf_score
    if overall >= 9 and rtn_score >= 1 and dd_score >= 1:
        verdict = "Setup dulce: cumple en ganancia, riesgo y consistencia."
    elif overall >= 6 and rtn_score >= 1:
        verdict = "Setup aceptable: gana pero con observaciones a vigilar."
    else:
        verdict = "Setup no recomendado en su forma actual."

    return {
        "verdict": verdict,
        "ganancia": light(rtn_score),
        "riesgo": light(dd_score),
        "estabilidad": light(sortino_score),
        "eficiencia": light(calmar_score),
        "aciertos": light(win_score),
        "balance_trades": light(pf_score),
        "actividad": light(activity_score),
    }


def _equity_story(equity_rows: List[Tuple]) -> str:
    if not equity_rows:
        return "No hay puntos de equity registrados; la curva no es interpretable."
    start = float(equity_rows[0][2])
    end = float(equity_rows[-1][2])
    direction = "subi\u00f3" if end > start else ("baj\u00f3" if end < start else "se mantuvo plano")
    delta_pct = (end - start) / start * 100.0 if start > 0 else 0.0
    return (
        f"La curva de capital {direction} a lo largo del periodo, terminando un "
        f"{delta_pct:+.2f}% respecto al inicio. Una curva que sube de forma constante "
        f"es preferible a una que sube fuerte y luego cae."
    )


def _drawdown_story(metrics: Dict[str, float]) -> str:
    dd = float(metrics.get("max_drawdown", 0.0) or 0.0)
    if dd <= 0.10:
        return f"La caida maxima fue baja ({_pct(dd)}); el riesgo emocional y financiero del setup es manejable."
    if dd <= 0.25:
        return f"La caida maxima fue moderada ({_pct(dd)}); aceptable si la ganancia compensa."
    return f"La caida maxima fue alta ({_pct(dd)}); aunque el cierre sea positivo, sufrir esa baja en vivo es duro."


def _returns_story(metrics: Dict[str, float]) -> str:
    sortino = float(metrics.get("sortino", 0.0) or 0.0)
    sharpe = float(metrics.get("sharpe", 0.0) or 0.0)
    if sortino >= 1.5:
        return "La distribucion de retornos privilegia las subidas y limita las bajadas; el bot opera con estabilidad."
    if sharpe >= 1.0:
        return "Los retornos diarios son razonables, aunque hay episodios negativos relevantes."
    return "Los retornos diarios son inestables: hay sesgo a perder o ganar de forma muy variable."


def _monthly_story(equity_rows: List[Tuple]) -> str:
    monthly: Dict[str, List[float]] = {}
    for _seq, ts, eq in equity_rows:
        if ts is None:
            continue
        key = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(key, []).append(float(eq))
    if not monthly:
        return "No hay datos mensuales suficientes para describir el comportamiento."
    rets: List[Tuple[str, float]] = []
    for k in sorted(monthly.keys()):
        arr = monthly[k]
        if not arr or arr[0] == 0:
            rets.append((k, 0.0))
        else:
            rets.append((k, (arr[-1] - arr[0]) / arr[0]))
    pos = sum(1 for _, v in rets if v > 0)
    neg = sum(1 for _, v in rets if v < 0)
    total = len(rets)
    best = max(rets, key=lambda x: x[1]) if rets else ("-", 0.0)
    worst = min(rets, key=lambda x: x[1]) if rets else ("-", 0.0)
    return (
        f"De {total} meses analizados, {pos} fueron positivos y {neg} negativos. "
        f"El mejor mes fue {best[0]} ({best[1] * 100.0:.2f}%) y el peor {worst[0]} ({worst[1] * 100.0:.2f}%). "
        f"Una proporcion alta de meses positivos sugiere comportamiento sostenido en el tiempo."
    )


def _signal_story(num_trades: float, equity_rows: List[Tuple]) -> str:
    if not equity_rows:
        return "No se observan operaciones; revisar parametros y rango de datos."
    if num_trades < 5:
        return "El bot opera muy poco: lo cual reduce la confianza estadistica de los resultados."
    if num_trades < 50:
        return "El bot opera de forma moderada; muestra es razonable pero no muy grande."
    return "El bot opera con frecuencia suficiente para que los resultados sean estadisticamente significativos."


def _fill_activity_story(activity_rows: List[Tuple]) -> str:
    hours: Dict[int, int] = {}
    days: Dict[int, int] = {}
    for _seq, ts, event_type, side, *_rest in activity_rows:
        if ts is None or event_type != "fill" or side not in ("buy", "sell"):
            continue
        d = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
        hours[d.hour] = hours.get(d.hour, 0) + 1
        days[d.weekday()] = days.get(d.weekday(), 0) + 1
    if not hours:
        return "No hay operaciones registradas, no es posible analizar concentracion temporal."
    top_hour = max(hours.items(), key=lambda x: x[1])
    top_day_idx = max(days.items(), key=lambda x: x[1])[0] if days else 0
    day_names = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    return (
        f"La mayor concentracion de operaciones ocurre los {day_names[top_day_idx]} y alrededor de las {top_hour[0]:02d}:00 UTC. "
        f"Confirma si esa franja coincide con tu disponibilidad real de monitoreo."
    )


def _usage_guide(strategy_name: str, symbol: str, interval: str, loop_seconds: Optional[int], params: Dict[str, Any]) -> str:
    extras = ""
    if loop_seconds:
        extras = f"--loop_seconds {loop_seconds} "
    param_flags = " ".join(f"--{k} {v}" for k, v in params.items())
    return (
        "Para replicar el setup ganador, ejecuta este comando en tu maquina:\n\n"
        "```bash\n"
        f"python backtest_cli.py --db klines.db run --strategy {strategy_name} "
        f"--symbol {symbol} --interval {interval} {extras}{param_flags}\n"
        "```\n\n"
        "Consejos de uso:\n"
        "- Empieza con capital moderado y revisa resultados semanalmente.\n"
        "- Si la caida actual supera la maxima registrada, pausa y revisa.\n"
        "- Vuelve a correr `sweet-spot` cada 1-3 meses por si el mercado cambia.\n"
        "- No aumentes el capital despues de una racha corta de aciertos.\n"
    )


def build_unified_report(
    db_path: str,
    sweet_result: SweetSpotResult,
    output_dir: str,
) -> Dict[str, str]:
    """Generate the full report bundle for the best focused run.

    Returns a dict with the paths of every artifact produced.
    """
    if sweet_result.best_focused_run is None or sweet_result.best_focused_run.get("run_id") is None:
        raise RuntimeError("Sweet spot search did not produce a valid focused run to report on.")

    best = sweet_result.best_focused_run
    run_id = int(best["run_id"])
    ensure_dir(output_dir)
    report_dir = study_report_dir(output_dir, sweet_result.focused_study_name)
    ensure_dir(report_dir)
    write_manifest(
        report_dir,
        title=f"Sweet-spot study {sweet_result.focused_study_name}",
        summary="Unified report and artifacts for sweet-spot best focused run.",
    )

    # Pull persisted data for the winning run. DuckDB-over-Parquet is tried
    # first; on miss we fall through to the legacy SQLite path. Metrics and
    # descriptor still come from the metadata DB regardless of backend.
    parquet_rows = _load_via_duckdb(run_id, data_root="data")
    if parquet_rows is not None:
        eq_rows = parquet_rows["eq_rows"]
        sig_rows = parquet_rows["sig_rows"]
        ev_rows = parquet_rows["ev_rows"]
        print(f"[reports] run_id={run_id} backend=duckdb", file=sys.stderr)
    else:
        eq_rows = run_equity_curve(db_path, run_id=run_id)
        sig_rows = run_signal_events(db_path, run_id=run_id)
        ev_rows = run_events(db_path, run_id=run_id)
        print(f"[reports] run_id={run_id} backend=sqlite", file=sys.stderr)
    metrics = summarize_run(db_path, run_id=run_id)["metrics"]
    descriptor = run_descriptor(db_path, run_id=run_id) or {}
    descriptor["first_event_iso_utc"] = _ms_to_iso(descriptor.get("first_event_time"))
    descriptor["last_event_iso_utc"] = _ms_to_iso(descriptor.get("last_event_time"))

    # Reuse existing plot/exporters so we don't fork artifact formats.
    file_stem = f"sweet_{run_id}"
    equity_paths = plot_equity_and_drawdown(eq_rows, output_dir=report_dir, run_id=run_id)
    signal_paths = plot_signal_histograms(signal_rows=sig_rows, output_dir=report_dir, run_id=run_id, bins=30)
    spectrum_path = plot_monthly_return_spectrum(eq_rows, output_dir=report_dir, run_id=run_id)
    heatmap_path = plot_monthly_return_heatmap(eq_rows, output_dir=report_dir, run_id=run_id)
    activity_path = plot_fill_activity_heatmap(ev_rows, output_dir=report_dir, run_id=run_id)
    export_paths = export_summary(report_dir, file_stem, metrics, eq_rows, descriptor=descriptor)
    export_run_bot_summary(
        output_dir=report_dir,
        file_stem=file_stem,
        descriptor=descriptor,
        metrics=metrics,
        bot_description=(
            "Resumen tecnico del bot ganador en su mejor seteo encontrado por el buscador en dos fases."
        ),
        optuna_summary=(
            "Optuna primero exploro el espacio en una ventana corta y luego revalido los mejores "
            "candidatos sobre el periodo completo."
        ),
    )
    integrated_paths = {**equity_paths, **signal_paths}
    if spectrum_path:
        integrated_paths["monthly_return_spectrum"] = spectrum_path
    if heatmap_path:
        integrated_paths["monthly_return_heatmap"] = heatmap_path
    if activity_path:
        integrated_paths["fill_activity_heatmap"] = activity_path
    export_run_integrated_report(
        output_dir=report_dir,
        file_stem=file_stem,
        descriptor=descriptor,
        metrics=metrics,
        equity_rows=eq_rows,
        graph_paths=integrated_paths,
    )

    # Compose the non-technical narrative.
    verdict = _classify_setup(metrics)
    sweet_md = os.path.join(report_dir, f"{file_stem}_sweet_spot_report.md")

    def _img(title: str, key: str) -> str:
        path = integrated_paths.get(key)
        if not path:
            return ""
        rel = os.path.basename(str(path).replace("\\", "/"))
        return f"### {title}\n\n![{title}]({rel})\n\n"

    with open(sweet_md, "w", encoding="utf-8") as f:
        f.write(f"# Reporte unificado - {sweet_result.focused_study_name}\n\n")
        f.write("Este reporte resume la busqueda del 'seteo dulce' del bot en lenguaje claro.\n\n")

        f.write("## Conclusion principal\n\n")
        f.write(f"- **Veredicto**: {verdict['verdict']}\n")
        f.write(f"- **Ganancia**: {verdict['ganancia']}  |  ")
        f.write(f"**Riesgo**: {verdict['riesgo']}  |  ")
        f.write(f"**Estabilidad**: {verdict['estabilidad']}  |  ")
        f.write(f"**Eficiencia**: {verdict['eficiencia']}\n")
        f.write(f"- **Aciertos**: {verdict['aciertos']}  |  ")
        f.write(f"**Balance ganancia/perdida**: {verdict['balance_trades']}  |  ")
        f.write(f"**Actividad**: {verdict['actividad']}\n\n")

        f.write("## Resultado numerico (resumen)\n\n")
        f.write("| Dato | Valor |\n|---|---:|\n")
        f.write(f"| Capital inicial | {_money(metrics.get('initial_cash'))} |\n")
        f.write(f"| Capital final | {_money(metrics.get('final_equity'))} |\n")
        f.write(f"| Ganancia total | {_pct(metrics.get('total_return'))} |\n")
        f.write(f"| Caida maxima | {_pct(metrics.get('max_drawdown'))} |\n")
        f.write(f"| Operaciones realizadas | {int(float(metrics.get('num_trades', 0) or 0))} |\n")
        f.write(f"| Porcentaje de aciertos | {_pct(metrics.get('win_rate'))} |\n")
        f.write(f"| Sortino (estabilidad) | {float(metrics.get('sortino', 0) or 0):.2f} |\n")
        f.write(f"| Calmar (eficiencia) | {float(metrics.get('calmar', 0) or 0):.2f} |\n\n")

        f.write("## Que dice cada grafica\n\n")
        f.write("**Curva de capital**: " + _equity_story(eq_rows) + "\n\n")
        f.write(_img("Curva de capital", "equity"))
        f.write("**Caida temporal (drawdown)**: " + _drawdown_story(metrics) + "\n\n")
        f.write(_img("Caida temporal", "drawdown"))
        f.write("**Distribucion de retornos**: " + _returns_story(metrics) + "\n\n")
        f.write(_img("Distribucion de retornos", "returns_hist"))
        f.write("**Espectro mensual**: " + _monthly_story(eq_rows) + "\n\n")
        f.write(_img("Espectro mensual", "monthly_return_spectrum"))
        f.write(_img("Mapa de calor mensual", "monthly_return_heatmap"))
        f.write("**Actividad operativa**: " + _signal_story(float(metrics.get("num_trades", 0) or 0), eq_rows) + "\n\n")
        f.write(_img("Entradas y salidas ejecutadas", "trade_signal_hist"))
        f.write(_img("Activacion de senales", "signal_activation_hist"))
        f.write("**Concentracion por dia/hora**: " + _fill_activity_story(ev_rows) + "\n\n")
        f.write(_img("Actividad por dia/hora", "fill_activity_heatmap"))

        f.write("## Setup ganador\n\n")
        f.write(f"- Estrategia: `{sweet_result.config_snapshot.get('strategy', '')}`\n")
        f.write(f"- Simbolo: `{sweet_result.config_snapshot.get('symbol', '')}`\n")
        f.write(f"- Timeframe de datos: `{sweet_result.config_snapshot.get('interval', '')}`\n")
        f.write(f"- Loop del bot: `{sweet_result.config_snapshot.get('loop_seconds') or 'por vela'}`\n")
        f.write(f"- Periodo evaluado: `{_ms_to_iso(sweet_result.full_window[0])}` -> `{_ms_to_iso(sweet_result.full_window[1])}`\n")
        f.write(f"- Periodo de busqueda inicial: `{_ms_to_iso(sweet_result.coarse_window[0])}` -> `{_ms_to_iso(sweet_result.coarse_window[1])}`\n\n")
        f.write("Parametros encontrados:\n\n")
        f.write("| Parametro | Valor |\n|---|---:|\n")
        for k, v in (best.get("params") or {}).items():
            f.write(f"| `{k}` | `{v}` |\n")
        f.write("\n")

        f.write("## Como replicarlo (guia rapida)\n\n")
        f.write(
            _usage_guide(
                strategy_name=str(sweet_result.config_snapshot.get("strategy", "")),
                symbol=str(sweet_result.config_snapshot.get("symbol", "")),
                interval=str(sweet_result.config_snapshot.get("interval", "")),
                loop_seconds=sweet_result.config_snapshot.get("loop_seconds"),
                params=best.get("params") or {},
            )
        )

        f.write("## Otros candidatos evaluados\n\n")
        if sweet_result.focused_runs:
            f.write("| Puesto | run_id | Ganancia | Caida maxima | Operaciones |\n")
            f.write("|---:|---:|---:|---:|---:|\n")
            for r in sweet_result.focused_runs:
                m = r.get("metrics", {}) or {}
                f.write(
                    f"| {r['rank']} | {r.get('run_id', '-')} | "
                    f"{_pct(m.get('total_return'))} | {_pct(m.get('max_drawdown'))} | "
                    f"{int(float(m.get('num_trades', 0) or 0))} |\n"
                )

    # Bundle index of artifacts.
    index_json = os.path.join(report_dir, f"{file_stem}_artifacts.json")
    bundle = {
        "report_md": sweet_md,
        "report_dir": report_dir,
        "best_run_id": run_id,
        "focused_study_name": sweet_result.focused_study_name,
        "coarse_study_name": sweet_result.coarse_study_name,
        "best_params": best.get("params") or {},
        "best_metrics": metrics,
        "graphs": integrated_paths,
        "exports": export_paths,
    }
    with open(index_json, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    bundle["artifacts_index_json"] = index_json
    return bundle
