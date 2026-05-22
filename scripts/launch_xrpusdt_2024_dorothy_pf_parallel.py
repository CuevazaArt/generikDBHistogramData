from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.report_paths import strict_report_dir, write_manifest


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_profit_factors(raw: str) -> List[float]:
    values = [float(x.strip()) for x in raw.split(",") if x.strip()]
    unique = sorted({round(v, 12) for v in values})
    if not unique:
        raise ValueError("profit_factors cannot be empty")
    return [float(v) for v in unique]


def _pf_slug(pf: float) -> str:
    txt = f"{pf:.2f}".replace(".", "p")
    return f"pf_{txt}"


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_master_markdown(path: Path, payload: Dict[str, Any]) -> None:
    lines = [
        "# Master launch log - XRPUSDT 2024 strict quarterly branches",
        "",
        "## Resource plan",
        f"- cpu_count: `{payload['cpu_count']}`",
        f"- cpu_cap_pct: `{payload['cpu_cap_pct']}`",
        f"- cpu_branch_budget: `{payload['cpu_branch_budget']}`",
        f"- max_concurrent_active: `{payload['max_concurrent_active']}`",
        f"- guard_cpu_cap_pct: `{payload['guard']['cpu_cap_pct']}`",
        f"- guard_ram_cap_pct: `{payload['guard']['ram_cap_pct']}`",
        f"- guard_sample_sec: `{payload['guard']['sample_sec']}`",
        f"- total_branches: `{payload['total_branches']}`",
        "",
        "## Run root",
        f"- run_root: `{payload['run_root']}`",
        f"- master_log_jsonl: `{payload['master_log_jsonl']}`",
        "",
        "## Branch map",
    ]
    for b in payload["branches"]:
        lines.extend(
            [
                f"### {b['branch']}",
                f"- profit_factor: `{b['profit_factor']}`",
                f"- db_path: `{b['db_path']}`",
                f"- output_root: `{b['output_root']}`",
                f"- process_log: `{b['process_log']}`",
                "",
            ]
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


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


def _build_branches(
    args: argparse.Namespace,
    run_root: Path,
    profit_factors: List[float],
    guard: ResourceGuard,
) -> List[Dict[str, Any]]:
    db_dir = run_root / "db"
    logs_dir = run_root / "logs"
    branches_dir = run_root / "branches"
    db_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    branches_dir.mkdir(parents=True, exist_ok=True)

    branches: List[Dict[str, Any]] = []
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
            str(guard.config.cpu_cap_pct),
            "--guard_ram_cap_pct",
            str(guard.config.ram_cap_pct),
            "--guard_sample_sec",
            str(guard.config.sample_sec),
            "--guard_high_windows",
            str(guard.config.high_watermark_windows),
            "--guard_recover_windows",
            str(guard.config.recover_windows),
            "--guard_backoff_sec",
            str(args.guard_backoff_sec),
            "--output_root",
            str(output_root),
        ]
        branches.append(
            {
                "branch": slug,
                "profit_factor": pf,
                "db_path": str(db_copy),
                "output_root": str(output_root),
                "process_log": str(process_log),
                "command": cmd,
            }
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
    cpu_count = os.cpu_count() or 1
    cpu_branch_budget = max(1, math.floor(cpu_count * (float(args.cpu_cap_pct) / 100.0)))
    max_concurrent = max(1, min(len(profit_factors), cpu_branch_budget))
    guard = _guard_from_args(args)
    # Start conservatively and scale up as guard samples stay healthy.
    dynamic_concurrent = 1

    branches = _build_branches(args, run_root, profit_factors, guard=guard)
    master_log_jsonl = run_root / "MASTER_LAUNCH_LOG.jsonl"
    master_log_md = run_root / "MASTER_LAUNCH_LOG.md"
    plan_payload = {
        "timestamp": _now_iso(),
        "run_root": str(run_root),
        "cpu_count": cpu_count,
        "cpu_cap_pct": float(args.cpu_cap_pct),
        "cpu_branch_budget": cpu_branch_budget,
        "max_concurrent_active": max_concurrent,
        "total_branches": len(branches),
        "master_log_jsonl": str(master_log_jsonl),
        "guard": {
            "cpu_cap_pct": guard.config.cpu_cap_pct,
            "ram_cap_pct": guard.config.ram_cap_pct,
            "sample_sec": guard.config.sample_sec,
            "high_watermark_windows": guard.config.high_watermark_windows,
            "recover_windows": guard.config.recover_windows,
        },
        "branches": branches,
    }
    _append_jsonl(master_log_jsonl, {"event": "plan", **plan_payload})
    _write_master_markdown(master_log_md, plan_payload)
    print(json.dumps({"event": "plan", **plan_payload}, ensure_ascii=False), flush=True)

    pending = list(branches)
    active: List[Dict[str, Any]] = []
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "." if not py_path else f".{os.pathsep}{py_path}"

    while pending or active:
        guard_snapshot = guard.snapshot()
        dynamic_concurrent = min(max_concurrent, max(1, guard.suggest_concurrency(dynamic_concurrent, min=1)))
        if guard_snapshot.get("throttle_active"):
            dynamic_concurrent = max(1, min(dynamic_concurrent, max_concurrent // 2 or 1))
        for event in guard.consume_events():
            _append_jsonl(
                master_log_jsonl,
                {
                    **event,
                    "dynamic_concurrent": int(dynamic_concurrent),
                    "active_count": len(active),
                    "pending_count": len(pending),
                },
            )

        while pending and len(active) < dynamic_concurrent:
            branch = pending.pop(0)
            log_handle = open(branch["process_log"], "a", encoding="utf-8")
            proc = subprocess.Popen(
                branch["command"],
                cwd=str(repo_root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            started_payload = {
                "event": "started",
                "timestamp": _now_iso(),
                "pid": int(proc.pid),
                "branch": branch["branch"],
                "profit_factor": branch["profit_factor"],
                "db_path": branch["db_path"],
                "output_root": branch["output_root"],
                "process_log": branch["process_log"],
                "status": "started",
                "command": branch["command"],
            }
            _append_jsonl(master_log_jsonl, started_payload)
            print(json.dumps(started_payload, ensure_ascii=False), flush=True)
            active.append({"branch": branch, "proc": proc, "log_handle": log_handle, "started_at": time.time()})

        if not active:
            if guard_snapshot.get("throttle_active"):
                backoff = max(1.0, float(args.guard_backoff_sec))
                _append_jsonl(
                    master_log_jsonl,
                    {
                        "event": "resource_guard_backoff",
                        "timestamp": _now_iso(),
                        "seconds": backoff,
                        "dynamic_concurrent": int(dynamic_concurrent),
                        "pending_count": len(pending),
                        "snapshot": guard_snapshot,
                    },
                )
                time.sleep(backoff)
            continue

        time.sleep(max(1, int(args.poll_seconds)))
        survivors: List[Dict[str, Any]] = []
        for item in active:
            proc = item["proc"]
            branch = item["branch"]
            code = proc.poll()
            if code is None:
                survivors.append(item)
                continue
            item["log_handle"].close()
            _append_jsonl(
                master_log_jsonl,
                {
                    "event": "finished",
                    "timestamp": _now_iso(),
                    "pid": int(proc.pid),
                    "branch": branch["branch"],
                    "profit_factor": branch["profit_factor"],
                    "status": "finished",
                    "exit_code": int(code),
                    "db_path": branch["db_path"],
                    "output_root": branch["output_root"],
                    "process_log": branch["process_log"],
                },
            )
            print(
                json.dumps(
                    {
                        "event": "finished",
                        "timestamp": _now_iso(),
                        "pid": int(proc.pid),
                        "branch": branch["branch"],
                        "profit_factor": branch["profit_factor"],
                        "status": "finished",
                        "exit_code": int(code),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        active = survivors


def main() -> None:
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
    parser.add_argument("--cpu_cap_pct", type=float, default=90.0)
    parser.add_argument("--guard_cpu_cap_pct", type=float, default=None)
    parser.add_argument("--guard_ram_cap_pct", type=float, default=None)
    parser.add_argument("--guard_sample_sec", type=float, default=None)
    parser.add_argument("--guard_high_windows", type=int, default=None)
    parser.add_argument("--guard_recover_windows", type=int, default=None)
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
