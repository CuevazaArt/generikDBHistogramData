"""Launch isolated quarterly strict runs per profit_factor.

Thin wrapper around `backtest.scheduler.run_branches` so the heavy lifting
(adaptive concurrency, guard hysteresis, JSONL master log) lives in shared
code reusable by other orchestrators.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from backtest.guards import ResourceGuardConfig
from backtest.report_paths import strict_report_dir, write_manifest
from backtest.scheduler import BranchSpec, SchedulerConfig, run_branches


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _parse_profit_factors(raw: str) -> List[float]:
    values = [float(x.strip()) for x in raw.split(",") if x.strip()]
    unique = sorted({round(v, 12) for v in values})
    if not unique:
        raise ValueError("profit_factors cannot be empty")
    return [float(v) for v in unique]


def _pf_slug(pf: float) -> str:
    txt = f"{pf:.2f}".replace(".", "p")
    return f"pf_{txt}"


def _build_branches(args: argparse.Namespace, run_root: Path, profit_factors: List[float]) -> List[BranchSpec]:
    db_dir = run_root / "db"
    logs_dir = run_root / "logs"
    branches_dir = run_root / "branches"
    db_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    branches_dir.mkdir(parents=True, exist_ok=True)

    branches: List[BranchSpec] = []
    for pf in profit_factors:
        slug = _pf_slug(pf)
        db_copy = db_dir / f"klines_{slug}.db"
        output_root = branches_dir / slug
        process_log = logs_dir / f"{slug}.log"
        output_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.base_db, db_copy)
        cmd = [
            sys.executable,
            "scripts/run_xrpusdt_2024_dorothy_strict.py",
            "--db",
            str(db_copy),
            "--symbol",
            args.symbol,
            "--interval",
            args.interval,
            "--start_ts",
            str(args.start_ts),
            "--end_ts",
            str(args.end_ts),
            "--initial_cash",
            str(args.initial_cash),
            "--fee_rate",
            str(args.fee_rate),
            "--slippage_bps",
            str(args.slippage_bps),
            "--loop_seconds",
            str(args.loop_seconds),
            "--margin_drop_factor",
            str(args.margin_drop_factor),
            "--profit_factor_grid",
            f"{pf:.12g}",
            "--cpu_cap_pct",
            str(args.cpu_cap_pct),
            "--guard_cpu_cap_pct",
            str(args.guard_cpu_cap_pct),
            "--guard_ram_cap_pct",
            str(args.guard_ram_cap_pct),
            "--guard_sample_sec",
            str(args.guard_sample_sec),
            "--guard_high_windows",
            str(args.guard_high_windows),
            "--guard_recover_windows",
            str(args.guard_recover_windows),
            "--guard_backoff_sec",
            str(args.guard_backoff_sec),
            "--output_root",
            str(output_root),
        ]
        branches.append(
            BranchSpec(
                name=slug,
                command=cmd,
                log_path=str(process_log),
                metadata={
                    "profit_factor": pf,
                    "db_path": str(db_copy),
                    "output_root": str(output_root),
                },
            )
        )
    return branches


def run(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo_root).resolve()
    output_root = (repo_root / args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_name = f"dorothy_xrpusdt_1s_2024_pf_parallel_{_now_tag()}"
    run_root = Path(strict_report_dir(str(output_root), run_name))
    run_root.mkdir(parents=True, exist_ok=True)
    write_manifest(
        str(run_root),
        title=f"Strict parallel run {run_name}",
        summary="Parallel per-profit-factor strict quarterly execution.",
    )

    profit_factors = _parse_profit_factors(args.profit_factors)
    branches = _build_branches(args, run_root, profit_factors)
    master_log = run_root / "MASTER_LAUNCH_LOG.jsonl"

    extra_env = {"PYTHONPATH": "." + os.pathsep + os.environ.get("PYTHONPATH", "")}
    sched_cfg = SchedulerConfig(
        repo_root=str(repo_root),
        master_log_jsonl=str(master_log),
        cpu_cap_pct=float(args.cpu_cap_pct),
        ram_cap_pct=float(args.guard_ram_cap_pct),
        guard_sample_sec=float(args.guard_sample_sec),
        guard_high_windows=int(args.guard_high_windows),
        guard_recover_windows=int(args.guard_recover_windows),
        guard_backoff_sec=float(args.guard_backoff_sec),
        poll_seconds=float(args.poll_seconds),
        initial_concurrency=1,
        extra_env=extra_env,
    )

    results = run_branches(sched_cfg, branches)
    failed = [r for r in results if int(r.get("exit_code", 0)) != 0]
    print(
        {
            "run_root": str(run_root),
            "master_log": str(master_log),
            "total_branches": len(branches),
            "failed_branches": [r["branch"] for r in failed],
        }
    )


def main() -> None:
    env_guard = ResourceGuardConfig.from_env()
    parser = argparse.ArgumentParser(description="Launch isolated quarterly strict runs per profit_factor")
    parser.add_argument("--repo_root", default=".")
    parser.add_argument("--base_db", default="klines.db")
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--interval", default="1s")
    parser.add_argument("--start_ts", type=int, default=1704067200000)
    parser.add_argument("--end_ts", type=int, default=1735689599000)
    parser.add_argument("--initial_cash", type=float, default=1000.0)
    parser.add_argument("--fee_rate", type=float, default=0.001)
    parser.add_argument("--slippage_bps", type=float, default=2.0)
    parser.add_argument("--loop_seconds", type=int, default=29)
    parser.add_argument("--margin_drop_factor", type=float, default=0.0005)
    parser.add_argument("--profit_factors", default="0.01,0.03,0.05,0.06")
    parser.add_argument("--cpu_cap_pct", type=float, default=float(env_guard.cpu_cap_pct))
    parser.add_argument("--guard_cpu_cap_pct", type=float, default=float(env_guard.cpu_cap_pct))
    parser.add_argument("--guard_ram_cap_pct", type=float, default=float(env_guard.ram_cap_pct))
    parser.add_argument("--guard_sample_sec", type=float, default=float(env_guard.sample_sec))
    parser.add_argument("--guard_high_windows", type=int, default=int(env_guard.high_watermark_windows))
    parser.add_argument("--guard_recover_windows", type=int, default=int(env_guard.recover_windows))
    parser.add_argument("--guard_backoff_sec", type=float, default=10.0)
    parser.add_argument("--output_root", default="reports")
    parser.add_argument("--poll_seconds", type=int, default=5)
    args = parser.parse_args()

    base_db = Path(args.base_db)
    if not base_db.is_absolute():
        base_db = (Path(args.repo_root) / base_db).resolve()
    if not base_db.exists():
        raise FileNotFoundError(f"Base DB not found: {base_db}")
    args.base_db = str(base_db)
    run(args)


if __name__ == "__main__":
    main()
