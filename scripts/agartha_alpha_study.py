"""Pipeline canonico de estudio Alpha para Agartha.

Modelo base: cada simbolo Alpha que se vaya a evaluar pasa por estos pasos
en orden, generando un set estandar de entregables comparable entre simbolos.

Pasos:
  1. Resolve symbol via token list (--symbol PHAROS -> ALPHA_964USDT)
  2. Download alpha (interval por default 15m, full history desde listing)
  3. Prepare dataset artifact (parquet cache + manifest + alpha_token.json)
  4. Optuna spectrum study: 100 trials con saltos grandes + 20 extremos
  5. Sweet spots report (top 20 + scatter trailing/activation/breakeven)
  6. Tres runs canonicos con el mejor seteo (best params del study):
     A. initial_cash=10  multitrades
     B. initial_cash=100 single-shot
     C. initial_cash=100 multitrades
  7. ALPHA_STUDY_INDEX.md consolidado con links a todos los entregables.

Uso:
  python scripts/agartha_alpha_study.py --symbol PHAROS
  python scripts/agartha_alpha_study.py --symbol BILL --skip_download --skip_optuna
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _utc_now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _run(cmd: list[str], env_extra: dict | None = None) -> int:
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    if env_extra:
        env.update(env_extra)
    return subprocess.call(cmd, env=env)


def _read_best_params(study_dir: Path) -> Dict[str, Any]:
    mapping_path = study_dir / "trial_to_run.json"
    if not mapping_path.exists():
        raise SystemExit(f"trial_to_run.json no existe en {study_dir}; corre primero el optuna step.")
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    return payload.get("best_params", {})


def _write_index(
    out_path: Path,
    *,
    symbol: str,
    alpha_symbol: str,
    interval: str,
    dataset_dir: Path,
    study_dir: Path,
    run_dirs: Dict[str, Path],
    best_params: Dict[str, Any],
    best_value: float,
) -> None:
    rel = lambda p: os.path.relpath(p, out_path.parent)
    lines = [
        f"# Alpha Study Index - {symbol} ({alpha_symbol})",
        "",
        f"Pipeline ejecutado: download + dataset prep + optuna spectrum + sweet spots + 3 runs canonicos.",
        f"Interval: `{interval}` · generado: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Dataset",
        f"- Artifact: [`{rel(dataset_dir)}`]({rel(dataset_dir)})",
        f"  - `manifest.json`, `alpha_token.json`, parquet cache reutilizable.",
        "",
        "## Optuna spectrum study",
        f"- Carpeta: [`{rel(study_dir)}`]({rel(study_dir)})",
        f"- Best `total_return`: **{best_value*100:+.2f}%**",
        "- Best params:",
    ]
    for k, v in best_params.items():
        lines.append(f"  - `{k}` = `{v}`")
    lines += [
        "- Entregables:",
        f"  - `SWEET_SPOTS.md`",
        f"  - `param_scatter.png`",
        f"  - `spectrum.png` (overlay equity+DD de los 100 trials)",
        f"  - `trial_to_run.json`",
        "",
        "## Runs canonicos (best params)",
        "",
        "| Run | initial_cash | max_cycles | Carpeta |",
        "|---|---:|---:|---|",
    ]
    for label, run_dir in run_dirs.items():
        lines.append(f"| {label} | - | - | [`{rel(run_dir)}`]({rel(run_dir)}) |")
    lines += [
        "",
        "Para cada run: `equity_drawdown.png`, `RUN_SUMMARY.md`, `run_manifest.json`.",
        "",
        "## Re-ejecutar este estudio",
        "",
        "```powershell",
        f"python scripts/agartha_alpha_study.py --symbol {symbol} --interval {interval}",
        "```",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agartha Alpha study pipeline (canonical).")
    parser.add_argument("--symbol", required=True, help="Simbolo humano (ej. PHAROS, BILL)")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--output_root", default="reports")
    parser.add_argument("--initial_cash_small", type=float, default=10.0)
    parser.add_argument("--initial_cash_large", type=float, default=100.0)
    parser.add_argument("--quote_order_qty_usdt", type=float, default=10.0)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--extreme", type=int, default=20)
    parser.add_argument("--start_ts", type=int, default=None,
                        help="Default: listingTime del token Alpha")
    parser.add_argument("--end_ts", type=int, default=None,
                        help="Default: ahora")
    parser.add_argument("--skip_download", action="store_true")
    parser.add_argument("--skip_optuna", action="store_true")
    parser.add_argument("--skip_runs", action="store_true")
    args = parser.parse_args()

    symbol_human = args.symbol.upper()
    # Resolver el quote correcto via exchange-info (algunos Alpha tokens son USDC).
    if symbol_human.endswith(("USDT", "USDC")):
        pair_symbol = symbol_human
    else:
        from binance_hist_downloader import BinanceDownloader
        _dl = BinanceDownloader()
        try:
            tokens = _dl.get_alpha_token_list()
            matches = [t for t in tokens if str(t.get("symbol", "")).upper() == symbol_human]
            if not matches:
                raise SystemExit(f"Token '{symbol_human}' no encontrado en Alpha token list.")
            # Rutina: si hay duplicados, preferir activo + mayor liquidez + mayor volumen.
            def _score(t: dict) -> tuple:
                try:
                    liq = float(t.get("liquidity") or 0.0)
                except Exception:
                    liq = 0.0
                try:
                    vol = float(t.get("volume24h") or 0.0)
                except Exception:
                    vol = 0.0
                return (not bool(t.get("offline", False)), not bool(t.get("offsell", False)), liq, vol)
            matches = sorted(matches, key=_score, reverse=True)
            if len(matches) > 1:
                alts = [f"{m.get('alphaId')}({m.get('chainName')},offline={m.get('offline')})" for m in matches[1:]]
                print(f"[alpha-study] '{symbol_human}' tiene {len(matches)} candidatos; uso {matches[0].get('alphaId')}; alternativas: {alts}")
            match = matches[0]
            alpha_id = str(match["alphaId"]).upper()
            tradeable = _dl.alpha_symbols_for_alpha_id(alpha_id)
            if not tradeable:
                raise SystemExit(f"AlphaId {alpha_id} sin pares tradeables en exchange-info.")
            preferred = f"{alpha_id}USDT"
            ordered = [preferred] + [t for t in tradeable if t != preferred] if preferred in tradeable else list(tradeable)
            chosen = None
            for cand in ordered:
                try:
                    sample = list(_dl.download_klines_alpha_api(cand, args.interval, limit=5))
                except Exception as exc:
                    print(f"[alpha-study] probe {cand}: error {exc}")
                    continue
                if sample:
                    chosen = cand
                    break
                else:
                    print(f"[alpha-study] probe {cand}: 0 velas; siguiente.")
            if chosen is None:
                raise SystemExit(f"Ningun par tradeable de {alpha_id} devolvio velas: {tradeable}")
            chosen_quote = chosen[len(alpha_id):]
            pair_symbol = f"{symbol_human}{chosen_quote}"
            print(f"[alpha-study] {symbol_human} -> {chosen} (par humano: {pair_symbol})")
        except SystemExit:
            raise
        except Exception as exc:
            print(f"[alpha-study] resolve fallback (USDT): {exc}")
            pair_symbol = f"{symbol_human}USDT"
    study_name = f"agartha_{pair_symbol.lower()}_{args.interval}_alpha_study"
    artifact_name = f"{pair_symbol.lower()}_{args.interval}_alpha"

    # PASO 1+2+3: download + prepare dataset
    if not args.skip_download:
        cmd = [
            sys.executable, "scripts/download_and_prepare_alpha.py",
            "--symbol", pair_symbol, "--interval", args.interval,
            "--db", args.db, "--output_root", args.output_root,
            "--artifact_name", artifact_name,
        ]
        if args.start_ts is not None:
            cmd += ["--start", str(args.start_ts)]
        if args.end_ts is not None:
            cmd += ["--end", str(args.end_ts)]
        rc = _run(cmd)
        if rc != 0:
            raise SystemExit(f"download_and_prepare_alpha failed rc={rc}")

    # Resolver ventana real desde DB / artifact
    import sqlite3
    c = sqlite3.connect(args.db)
    try:
        row = c.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines WHERE symbol=? AND interval=?",
            (pair_symbol, args.interval),
        ).fetchone()
    finally:
        c.close()
    if not row or row[2] == 0:
        raise SystemExit(f"No data in DB for {pair_symbol}/{args.interval}.")
    start_ts = int(args.start_ts) if args.start_ts is not None else int(row[0])
    end_ts = int(args.end_ts) if args.end_ts is not None else int(row[1])
    print(f"[alpha-study] window: {start_ts} -> {end_ts} ({row[2]} velas)")

    # PASO 4: optuna spectrum
    if not args.skip_optuna:
        cmd = [
            sys.executable, "scripts/agartha_optuna_spectrum.py",
            "--db", args.db,
            "--symbol", pair_symbol, "--interval", args.interval,
            "--start_ts", str(start_ts), "--end_ts", str(end_ts),
            "--initial_cash", str(args.initial_cash_small),
            "--quote_order_qty_usdt", str(args.quote_order_qty_usdt),
            "--max_cycles", "0",
            "--study", study_name,
            "--trials", str(args.trials), "--extreme", str(args.extreme),
            "--output_root", args.output_root,
        ]
        rc = _run(cmd)
        if rc != 0:
            raise SystemExit(f"optuna spectrum failed rc={rc}")

        # PASO 5: sweet spots report (lee desde Optuna SQLite separado, pero
        # el reporter actual lee bt_trials del db principal. Para este pipeline
        # el spectrum ya genera el reporte canonico; mantenemos compatibilidad
        # con el reporter existente si los trials se persistieron tambien en
        # bt_trials por execute_and_persist).

    # Cargar best params para los 3 runs canonicos
    study_dir = Path(args.output_root) / "entregables" / "studies" / study_name
    best_params = _read_best_params(study_dir)
    if not best_params:
        raise SystemExit(f"No best_params en {study_dir}/trial_to_run.json")
    print(f"[alpha-study] best_params: {best_params}")

    # PASO 6: 3 runs canonicos
    run_dirs: Dict[str, Path] = {}
    if not args.skip_runs:
        run_configs = [
            ("A_cash10_multi", args.initial_cash_small, 0),
            ("B_cash100_single", args.initial_cash_large, 1),
            ("C_cash100_multi", args.initial_cash_large, 0),
        ]
        for label, cash, mc in run_configs:
            cmd = [
                sys.executable, "scripts/run_agartha_bill_pilot.py",
                "--db", args.db,
                "--symbol", pair_symbol, "--interval", args.interval,
                "--initial_cash", str(cash),
                "--quote_order_qty_usdt", str(args.quote_order_qty_usdt),
                "--trailing_stop_pct", str(best_params.get("trailing_stop_pct", 30.0)),
                "--activation_profit_pct", str(best_params.get("activation_profit_pct", 0.0)),
                "--breakeven_lock_pct", str(best_params.get("breakeven_lock_pct", 0.0)),
                "--max_cycles", str(mc),
                "--output_root", args.output_root,
            ]
            rc = _run(cmd)
            if rc != 0:
                print(f"[alpha-study] WARNING: run {label} failed rc={rc}")
                continue
            # Identificar la carpeta mas reciente para esta config
            strict_dir = Path(args.output_root) / "entregables" / "strict"
            matches = sorted(
                [p for p in strict_dir.iterdir() if p.is_dir() and pair_symbol.lower() in p.name and args.interval in p.name],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                run_dirs[label] = matches[0]

    # PASO 7: index consolidado
    dataset_dir = Path(args.output_root) / "entregables" / "datasets" / artifact_name
    index_path = Path(args.output_root) / "entregables" / "studies" / study_name / "ALPHA_STUDY_INDEX.md"
    alpha_symbol = json.loads((dataset_dir / "alpha_token.json").read_text(encoding="utf-8")).get("alpha_symbol", "?") if (dataset_dir / "alpha_token.json").exists() else "?"
    best_value_raw = 0.0
    try:
        best_value_raw = json.loads((study_dir / "trial_to_run.json").read_text(encoding="utf-8")).get("best_value", 0.0)
    except Exception:
        pass
    _write_index(
        index_path, symbol=symbol_human, alpha_symbol=alpha_symbol, interval=args.interval,
        dataset_dir=dataset_dir, study_dir=study_dir, run_dirs=run_dirs,
        best_params=best_params, best_value=float(best_value_raw),
    )
    print(f"\n[alpha-study] DONE. Index: {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
