"""Reproducibility helpers: capture VCS state for any run manifest.

Records the current git commit, branch and dirty status so a future operator
can replay or trust the exact code that produced a backtest artifact.
Falls back gracefully when git is unavailable or the repo is not a git
checkout (returns ``{"vcs": "unknown"}``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, Optional


def _run_git(args: list[str], cwd: Optional[str]) -> Optional[str]:
    if shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip()


def git_snapshot(cwd: Optional[str] = None) -> Dict[str, Any]:
    """Return git commit SHA, branch and dirty flag of the current checkout.

    Designed to be safe to call from any backtest runner; never raises.
    """
    cwd = cwd or os.getcwd()
    commit = _run_git(["rev-parse", "HEAD"], cwd)
    if not commit:
        return {"vcs": "unknown"}
    short = _run_git(["rev-parse", "--short=12", "HEAD"], cwd) or commit[:12]
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "detached"
    status = _run_git(["status", "--porcelain"], cwd)
    dirty = bool(status and status.strip())
    return {
        "vcs": "git",
        "commit": commit,
        "commit_short": short,
        "branch": branch,
        "dirty": dirty,
    }
