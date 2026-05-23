"""Central helpers for the tester's per-run output layout.

All artifacts produced by `backtest_cli.py`, `backtest/sweet_spot_report.py`
and the `scripts/*` orchestrators land under a single tree:

    <base>/entregables/runs/run_<run_id>/
    <base>/entregables/studies/<study_name>/
    <base>/entregables/strict/<strict_run_name>/

`<base>` defaults to ``reports`` and can be any user-supplied directory.

Helpers in this module:

- Are the single source of truth for the layout (no path string is built
  ad-hoc anywhere else in the codebase).
- Create the target directory if it does not exist.
- Seed a minimal ``MANIFEST.md`` describing the bucket so directives 6/7
  of ``docs/TESTER_CAPABILITIES.md`` are satisfied even when downstream
  code crashes before writing its own reports.

Backwards compatibility:

- If the caller passes a base that already ends in ``entregables`` (e.g.
  ``reports/entregables``) the helpers will not double-prefix.
- If the caller passes a legacy bucket directly (``reports/runs`` or
  ``reports/strict_runs`` or ``reports/studies``), the helpers detect it
  and route the artifact into the new bucket while preserving any
  user-supplied subfolder name.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional


ENTREGABLES_DIRNAME = "entregables"
RUNS_BUCKET = "runs"
STUDIES_BUCKET = "studies"
STRICT_BUCKET = "strict"
DATASETS_BUCKET = "datasets"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_base(base: str) -> str:
    """Return the canonical ``<root>/entregables`` for a user-supplied base.

    The base argument may be:
      - ``reports`` (typical CLI default) -> ``reports/entregables``
      - ``reports/entregables`` -> kept as-is
      - ``reports/runs`` / ``reports/studies`` / ``reports/strict_runs``
        (legacy buckets) -> resolved to ``reports/entregables`` so the
        new tree is always used.
      - any other absolute/relative directory -> ``<dir>/entregables``
    """
    base = base or "reports"
    base = base.rstrip(os.sep).rstrip("/")
    head, tail = os.path.split(base)
    if tail == ENTREGABLES_DIRNAME:
        return base
    if tail in {RUNS_BUCKET, STUDIES_BUCKET, STRICT_BUCKET, "strict_runs", "sweet_spot"}:
        # Legacy bucket passed in directly; jump back to the parent and use the new tree.
        return os.path.join(head or ".", ENTREGABLES_DIRNAME)
    return os.path.join(base, ENTREGABLES_DIRNAME)


def entregables_root(base: str = "reports") -> str:
    """Return ``<base>/entregables`` (created lazily by the helpers below)."""
    return _normalize_base(base)


def run_report_dir(base: str, run_id: int, *, ensure_manifest: bool = True) -> str:
    """Return (and create) the per-run report folder.

    Layout: ``<base>/entregables/runs/run_<run_id>/``.
    Seeds a minimal MANIFEST.md unless ``ensure_manifest=False``.
    """
    target = os.path.join(_normalize_base(base), RUNS_BUCKET, f"run_{int(run_id)}")
    os.makedirs(target, exist_ok=True)
    if ensure_manifest:
        _ensure_manifest(
            target,
            title=f"run_{int(run_id)}",
            kind="Backtest individual (carpeta dedicada por run)",
            extra_lines=[
                f"- run_id: `{int(run_id)}`",
                "- Artefactos esperados: `*_integrated_report.md`, `*_bot_summary.md`,",
                "  `*_metrics.json`, `*_report.json`, `*_equity.csv`,",
                "  `*_final_table.{md,csv}` y graficas (`equity`, `drawdown`,",
                "  `returns_hist`, `monthly_*`, `signal_*`, `fill_activity_heatmap`).",
            ],
        )
    return target


def study_report_dir(base: str, study_name: str, *, ensure_manifest: bool = True) -> str:
    """Return (and create) the per-study report folder.

    Layout: ``<base>/entregables/studies/<study_name>/``.
    """
    safe = _safe_segment(study_name)
    target = os.path.join(_normalize_base(base), STUDIES_BUCKET, safe)
    os.makedirs(target, exist_ok=True)
    if ensure_manifest:
        _ensure_manifest(
            target,
            title=safe,
            kind="Estudio Optuna / sweet-spot",
            extra_lines=[
                f"- study_name: `{study_name}`",
                "- Artefactos esperados: `*_summary.{md,csv,json}`,",
                "  `*_optuna_summary.md`, `*_param_heatmap.png`, `*_trials.png`,",
                "  y (si es sweet-spot) `sweet_<run_id>_*` con reporte unificado.",
            ],
        )
    return target


def strict_report_dir(base: str, name: str, *, ensure_manifest: bool = True) -> str:
    """Return (and create) the per-strict-run report folder.

    Layout: ``<base>/entregables/strict/<name>/``.
    """
    safe = _safe_segment(name)
    target = os.path.join(_normalize_base(base), STRICT_BUCKET, safe)
    os.makedirs(target, exist_ok=True)
    if ensure_manifest:
        _ensure_manifest(
            target,
            title=safe,
            kind="Corrida estricta (trimestral encadenada, parallel pf, etc.)",
            extra_lines=[
                "- Artefactos esperados: `run_manifest.json`, `RESTART_LOG.md`,",
                "  bitacoras `MASTER_LAUNCH_LOG.*` (si aplica), `db/` aislada por",
                "  branch (si aplica), `logs/` por proceso (si aplica).",
            ],
        )
    return target


def dataset_report_dir(base: str, name: str, *, ensure_manifest: bool = True) -> str:
    """Return (and create) the prepared-dataset artifact folder.

    Layout: ``<base>/entregables/datasets/<name>/``.
    """
    safe = _safe_segment(name)
    target = os.path.join(_normalize_base(base), DATASETS_BUCKET, safe)
    os.makedirs(target, exist_ok=True)
    if ensure_manifest:
        _ensure_manifest(
            target,
            title=safe,
            kind="Artefacto de dataset preparado para runs genericos",
            extra_lines=[
                "- Artefactos esperados: `manifest.json` y un archivo de datos",
                "  preparado (cache Parquet reutilizable o snapshot JSONL).",
            ],
        )
    return target


def _safe_segment(name: str) -> str:
    """Strip path separators so caller-supplied names cannot escape the bucket."""
    s = (name or "").strip()
    if not s:
        raise ValueError("name cannot be empty")
    s = s.replace("\\", "_").replace("/", "_")
    return s


def _ensure_manifest(target_dir: str, *, title: str, kind: str, extra_lines: Optional[list[str]] = None) -> str:
    """Drop a minimal MANIFEST.md if none exists, so directives are met from t=0."""
    path = os.path.join(target_dir, "MANIFEST.md")
    if os.path.exists(path):
        return path
    lines = [
        f"# MANIFEST - {title}",
        "",
        "## Tipo",
        kind,
        "",
        "## Origen",
        f"- creado: `{_utc_now_iso()}`",
        f"- carpeta: `{target_dir}`",
        "",
        "## Contenido",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    else:
        lines.append("- (sin descripcion adicional al momento de creacion)")
    lines.extend(
        [
            "",
            "## Notas",
            "- Este MANIFEST.md fue sembrado automaticamente al iniciar el run/estudio.",
            "- El proceso puede sobrescribirlo al finalizar con datos definitivos.",
        ]
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).strip() + "\n")
    return path


def write_manifest(
    target_dir: str,
    *,
    title: str,
    summary: str,
    kind: str = "Entregable generado por el tester",
    extra_lines: Optional[list[str]] = None,
) -> str:
    """Public entry point to seed/refresh a MANIFEST.md.

    Convenience wrapper used by callers that want to (re-)write a manifest with
    a free-form summary. Preserves the directive-aligned skeleton produced by
    :func:`_ensure_manifest` and appends the caller's summary.
    """
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "MANIFEST.md")
    body_lines = [
        f"# MANIFEST - {title}",
        "",
        "## Tipo",
        kind,
        "",
        "## Origen",
        f"- creado/actualizado: `{_utc_now_iso()}`",
        f"- carpeta: `{target_dir}`",
        "",
        "## Resumen",
        summary.strip() if summary else "(sin resumen)",
    ]
    if extra_lines:
        body_lines.extend(["", "## Notas"] + list(extra_lines))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body_lines).strip() + "\n")
    return path


__all__ = [
    "ENTREGABLES_DIRNAME",
    "RUNS_BUCKET",
    "STUDIES_BUCKET",
    "STRICT_BUCKET",
    "DATASETS_BUCKET",
    "entregables_root",
    "run_report_dir",
    "study_report_dir",
    "strict_report_dir",
    "dataset_report_dir",
    "write_manifest",
]
