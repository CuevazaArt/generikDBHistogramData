"""Thin entrypoint for the Agartha cluster CLI.

Run with:
    python scripts/agartha_cluster_cli.py <subcommand> [options]

Subcommands (see ``python scripts/agartha_cluster_cli.py -h``):
    init-db, load-universe, set-params, schedule-batch,
    status, report, creds, live-up, supervisor

Detailed docs: docs/AGARTHA_CLUSTER.md
"""
from __future__ import annotations

import sys

from backtest.agartha_cluster.cli import main


if __name__ == "__main__":
    sys.exit(main())
