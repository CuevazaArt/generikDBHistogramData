"""Filesystem layout helpers for the data lake.

Single source of truth for the relative paths described in the redesign plan:

    data/
      klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/part-000.parquet
      events/run_<id>/part-<seq>.parquet
      equity/run_<id>/equity.parquet
      checkpoints/run_<id>/cp_<sim_ts>.parquet
      derived/<name>/...

The helpers in this module never read or write data themselves: callers (such
as `storage_pg.persist_run_events`) own the pyarrow writes and use
`tmp_then_rename` to make those writes atomic. Keeping the path math pure also
makes the layout trivially unit-testable without touching the disk.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator


DATA_ROOT_DEFAULT = "data"


@dataclass(frozen=True)
class StoragePaths:
    data_root: str = DATA_ROOT_DEFAULT

    # --- klines -----------------------------------------------------------

    def klines_root(self) -> str:
        return os.path.join(self.data_root, "klines")

    def klines_partition(self, symbol: str, interval: str, year: int, month: int) -> str:
        return os.path.join(
            self.klines_root(),
            f"symbol={symbol}",
            f"interval={interval}",
            f"year={int(year):04d}",
            f"month={int(month):02d}",
            "part-000.parquet",
        )

    def klines_manifest(self) -> str:
        return os.path.join(self.klines_root(), "_manifest.json")

    # --- events -----------------------------------------------------------

    def events_dir(self, run_id: int) -> str:
        return os.path.join(self.data_root, "events", f"run_{int(run_id)}")

    def events_part(self, run_id: int, seq: int) -> str:
        return os.path.join(self.events_dir(run_id), f"part-{int(seq):03d}.parquet")

    # --- equity -----------------------------------------------------------

    def equity_dir(self, run_id: int) -> str:
        return os.path.join(self.data_root, "equity", f"run_{int(run_id)}")

    def equity_file(self, run_id: int) -> str:
        return os.path.join(self.equity_dir(run_id), "equity.parquet")

    # --- checkpoints ------------------------------------------------------

    def checkpoints_dir(self, run_id: int) -> str:
        return os.path.join(self.data_root, "checkpoints", f"run_{int(run_id)}")

    def checkpoint_file(self, run_id: int, sim_ts: int) -> str:
        return os.path.join(self.checkpoints_dir(run_id), f"cp_{int(sim_ts)}.parquet")

    # --- derived ----------------------------------------------------------

    def derived_dir(self, name: str) -> str:
        safe = str(name).strip().strip(os.sep).strip("/")
        if not safe:
            raise ValueError("derived_dir name must be non-empty")
        return os.path.join(self.data_root, "derived", safe)

    # --- bulk mkdir -------------------------------------------------------

    def ensure_run_layout(self, run_id: int) -> Dict[str, str]:
        """Create the per-run directory tree and return the resulting paths."""
        paths = {
            "events_dir": self.events_dir(run_id),
            "equity_dir": self.equity_dir(run_id),
            "checkpoints_dir": self.checkpoints_dir(run_id),
        }
        for path in paths.values():
            os.makedirs(path, exist_ok=True)
        return paths


@contextmanager
def tmp_then_rename(path: str) -> Iterator[str]:
    """Yield a sibling tempfile path and atomically rename it on exit.

    Writers use the yielded path; on successful exit it is renamed onto the
    target via `os.replace` (atomic on the same filesystem). If the writer
    raises, the temp file is removed and the target is left untouched.
    """
    target = os.fspath(path)
    target_dir = os.path.dirname(target) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(target) + ".",
        suffix=".tmp",
        dir=target_dir,
    )
    os.close(fd)
    try:
        yield tmp_path
    except BaseException:
        # Clean up the partial file but propagate the original exception.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    else:
        os.replace(tmp_path, target)
