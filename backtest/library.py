"""Library system for bots, indicators and tools.

Each entry lives in a self-contained directory with its logic, manifest,
notes and presets:

    library/bots/<name>/
        manifest.yaml
        strategy.py
        notes.md
        presets/default.yaml

Manifests can also reference already-imported classes via the
``module.path:ClassName`` form (this is how the 11 existing strategies are
migrated). New drafts live under ``library/workspace/`` until published.

This module exposes the public API documented in ``docs/LIBRARY.md``: listing,
loading, scaffolding, publishing, validating, refreshing the index and
registering library bots with :mod:`backtest.registry` so the existing
CLI/runner can resolve them by name.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml


LIBRARY_ROOT = Path(__file__).resolve().parent.parent / "library"
WORKSPACE_SUBDIR = "workspace"
INDEX_FILENAME = "_index.json"
SCHEMA_VERSION = 1

KIND_TO_FOLDER = {"bot": "bots", "indicator": "indicators", "tool": "tools"}
KIND_TO_MODULE_FILENAME = {"bot": "strategy.py", "indicator": "indicator.py", "tool": "tool.py"}

_module_cache: Dict[str, Any] = {}


@dataclass
class LibraryEntry:
    name: str
    kind: str
    version: str
    manifest_path: Path
    dir: Path
    manifest: Dict[str, Any] = field(default_factory=dict)
    notes_path: Optional[Path] = None
    preset_paths: List[Path] = field(default_factory=list)
    workspace: bool = False

    def is_reference_only(self) -> bool:
        return bool(self.manifest.get("reference_only"))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"manifest at {path} must be a mapping, got {type(data).__name__}")
    return data


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


def _candidate_dirs(include_workspace: bool) -> List[tuple[str, Path]]:
    out: List[tuple[str, Path]] = []
    for kind, folder in KIND_TO_FOLDER.items():
        out.append((kind, LIBRARY_ROOT / folder))
    if include_workspace:
        out.append(("workspace", LIBRARY_ROOT / WORKSPACE_SUBDIR))
    return out


def _iter_entry_dirs(include_workspace: bool):
    for kind, base in _candidate_dirs(include_workspace):
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.yaml"
            if not manifest_path.exists():
                continue
            yield kind, child, manifest_path


def _entry_from_dir(
    kind_hint: str,
    entry_dir: Path,
    manifest_path: Path,
    workspace: bool = False,
) -> LibraryEntry:
    manifest = _read_yaml(manifest_path)
    name = str(manifest.get("name") or entry_dir.name)
    kind = str(manifest.get("kind") or (kind_hint if kind_hint != "workspace" else "bot"))
    version = str(manifest.get("version") or "0.0.0")
    notes_file = manifest.get("notes_file", "notes.md")
    notes_path = (entry_dir / notes_file) if notes_file else None
    if notes_path is not None and not notes_path.exists():
        notes_path = None
    preset_dir_name = manifest.get("preset_dir", "presets")
    preset_paths: List[Path] = []
    if preset_dir_name:
        preset_dir = entry_dir / preset_dir_name
        if preset_dir.exists():
            preset_paths = sorted(preset_dir.glob("*.yaml")) + sorted(preset_dir.glob("*.yml"))
    return LibraryEntry(
        name=name,
        kind=kind,
        version=version,
        manifest_path=manifest_path,
        dir=entry_dir,
        manifest=manifest,
        notes_path=notes_path,
        preset_paths=preset_paths,
        workspace=workspace,
    )


def list_entries(
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    include_workspace: bool = False,
) -> List[LibraryEntry]:
    """Enumerate all library entries (optionally filtered)."""
    out: List[LibraryEntry] = []
    for kind_hint, entry_dir, manifest_path in _iter_entry_dirs(include_workspace):
        try:
            entry = _entry_from_dir(
                kind_hint=kind_hint,
                entry_dir=entry_dir,
                manifest_path=manifest_path,
                workspace=(kind_hint == "workspace"),
            )
        except Exception:
            continue
        if kind and entry.kind != kind:
            continue
        if tag and tag not in (entry.manifest.get("tags") or []):
            continue
        out.append(entry)
    return out


def _locate_entry(name: str, include_workspace: bool) -> tuple[str, Path, Path]:
    target = name.strip().lower()
    for kind_hint, entry_dir, manifest_path in _iter_entry_dirs(include_workspace):
        manifest = _read_yaml(manifest_path)
        manifest_name = str(manifest.get("name") or entry_dir.name).lower()
        if manifest_name == target or entry_dir.name.lower() == target:
            return kind_hint, entry_dir, manifest_path
        aliases = [str(a).lower() for a in manifest.get("registry_aliases") or []]
        if target in aliases:
            return kind_hint, entry_dir, manifest_path
    raise FileNotFoundError(f"library entry '{name}' not found (include_workspace={include_workspace})")


def load_entry(name: str, include_workspace: bool = False) -> LibraryEntry:
    """Locate and return the :class:`LibraryEntry` for ``name``."""
    kind_hint, entry_dir, manifest_path = _locate_entry(name, include_workspace)
    return _entry_from_dir(
        kind_hint=kind_hint,
        entry_dir=entry_dir,
        manifest_path=manifest_path,
        workspace=(kind_hint == "workspace"),
    )


def _load_module_from_path(file_path: Path) -> Any:
    abs_path = str(file_path.resolve())
    cached = _module_cache.get(abs_path)
    if cached is not None:
        return cached
    module_name = f"_library_dynamic_{abs(hash(abs_path))}"
    spec = importlib.util.spec_from_file_location(module_name, abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {abs_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _module_cache[abs_path] = module
    return module


def resolve_entry_point(entry: LibraryEntry) -> Any:
    """Resolve a manifest ``entry_point`` to a Python object (class or callable)."""
    ep = entry.manifest.get("entry_point")
    if not ep:
        raise ValueError(f"entry '{entry.name}' has no entry_point")
    module_path, _, symbol_name = str(ep).rpartition(":")
    if not module_path or not symbol_name:
        raise ValueError(f"invalid entry_point '{ep}' for '{entry.name}' (expected 'module:Symbol')")
    if module_path.startswith("library."):
        module_filename = KIND_TO_MODULE_FILENAME.get(entry.kind, "strategy.py")
        module_file = entry.dir / module_filename
        if not module_file.exists():
            for fallback in KIND_TO_MODULE_FILENAME.values():
                cand = entry.dir / fallback
                if cand.exists():
                    module_file = cand
                    break
        if not module_file.exists():
            raise FileNotFoundError(f"no module file found for entry '{entry.name}' under {entry.dir}")
        module = _load_module_from_path(module_file)
    else:
        module = importlib.import_module(module_path)
    if not hasattr(module, symbol_name):
        raise AttributeError(f"symbol '{symbol_name}' not found in module '{module_path}' for entry '{entry.name}'")
    return getattr(module, symbol_name)


def list_presets(name: str) -> List[str]:
    """List preset names available for an entry (without file extension)."""
    entry = load_entry(name, include_workspace=True)
    return [p.stem for p in entry.preset_paths]


def load_preset(name: str, preset: str) -> Dict[str, Any]:
    """Load a single preset YAML, returning its parameter dict."""
    entry = load_entry(name, include_workspace=True)
    for path in entry.preset_paths:
        if path.stem == preset:
            return _read_yaml(path)
    raise FileNotFoundError(f"preset '{preset}' not found for entry '{name}'")


def get_notes(name: str) -> str:
    """Return the contents of ``notes.md`` for ``name`` (empty string when absent)."""
    entry = load_entry(name, include_workspace=True)
    if entry.notes_path and entry.notes_path.exists():
        return entry.notes_path.read_text(encoding="utf-8")
    return ""


def set_notes(name: str, text: str) -> None:
    """Overwrite the ``notes.md`` of an entry."""
    entry = load_entry(name, include_workspace=True)
    target = entry.notes_path or (entry.dir / "notes.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _scaffold_template(kind: str, name: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "kind": kind,
        "version": "0.1.0",
        "author": "",
        "description": "",
        "entry_point": None,
        "registry_aliases": [],
        "tags": [],
        "created_at": _utcnow_iso(),
        "updated_at": _utcnow_iso(),
        "default_params": {},
        "search_space": {},
        "data_requirements": {
            "symbols": ["*"],
            "intervals": ["1m", "1h"],
            "required_columns": ["open", "high", "low", "close", "volume"],
            "derived_columns": [],
        },
        "data_contributions": {"derived_dataset": None},
        "notes_file": "notes.md",
        "preset_dir": "presets",
    }
    if kind == "bot":
        base["entry_point"] = f"library.bots.{name}.strategy:{_pascal(name)}Strategy"
    elif kind == "indicator":
        base["entry_point"] = f"library.indicators.{name}.indicator:compute"
    elif kind == "tool":
        base["entry_point"] = f"library.tools.{name}.tool:run"
    return base


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)


_BOT_TEMPLATE = '''"""{title} strategy draft scaffolded by `library new`."""
from backtest.strategy_base import Signal, StrategyBase, StrategyContext


class {class_name}Strategy(StrategyBase):
    name = "{name}"

    def __init__(self, **params):
        super().__init__(**params)

    def on_bar(self, ctx: StrategyContext) -> Signal:
        _ = ctx
        return Signal(action="hold", reason="not_implemented_yet")
'''

_INDICATOR_TEMPLATE = '''"""{title} indicator draft scaffolded by `library new`."""
from typing import Any, List


def compute(candles: List[dict], **params: Any) -> None:
    """Annotate ``candles`` in place with the indicator output."""
    _ = params
    for c in candles:
        c.setdefault("{name}", None)
'''

_TOOL_TEMPLATE = '''"""{title} tool draft scaffolded by `library new`."""
from typing import Any


def run(args: Any, provider: Any) -> Any:
    """Entry point invoked by the CLI tool dispatcher."""
    _ = args, provider
    return {{"status": "not_implemented_yet"}}
'''


def scaffold_entry(name: str, kind: str = "bot", workspace: bool = True) -> Path:
    """Create a starter skeleton for a new entry.

    By default the skeleton lives under ``library/workspace/<name>/`` so it
    will not be auto-registered until ``publish_entry`` moves it under the
    appropriate ``library/<kind>s/`` directory.
    """
    kind = kind.strip().lower()
    if kind not in KIND_TO_FOLDER:
        raise ValueError(f"unsupported kind '{kind}'")
    parent = LIBRARY_ROOT / (WORKSPACE_SUBDIR if workspace else KIND_TO_FOLDER[kind])
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / name
    if target.exists():
        raise FileExistsError(f"entry directory already exists: {target}")
    target.mkdir(parents=True, exist_ok=False)
    manifest = _scaffold_template(kind, name)
    _write_yaml(target / "manifest.yaml", manifest)
    notes_path = target / "notes.md"
    notes_path.write_text(
        f"# {name}\n\nDraft scaffolded on {_utcnow_iso()}.\n\n"
        "## Thesis\n\nDescribe the rationale of this entry.\n\n"
        "## Decisions\n\nList key design decisions and tradeoffs.\n\n"
        "## Observations\n\nDocument observations from backtests / live runs.\n",
        encoding="utf-8",
    )
    presets_dir = target / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(presets_dir / "default.yaml", manifest["default_params"])
    examples_dir = target / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    (examples_dir / ".gitkeep").write_text("", encoding="utf-8")
    tests_dir = target / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / ".gitkeep").write_text("", encoding="utf-8")
    module_filename = KIND_TO_MODULE_FILENAME[kind]
    title = name.replace("_", " ").title()
    class_name = _pascal(name)
    if kind == "bot":
        code = _BOT_TEMPLATE.format(title=title, class_name=class_name, name=name)
    elif kind == "indicator":
        code = _INDICATOR_TEMPLATE.format(title=title, name=name)
    else:
        code = _TOOL_TEMPLATE.format(title=title)
    (target / module_filename).write_text(code, encoding="utf-8")
    return target


def publish_entry(name: str, target_kind: Optional[str] = None) -> Path:
    """Move an entry from the workspace into its kind-specific directory."""
    entry = load_entry(name, include_workspace=True)
    if not entry.workspace:
        raise ValueError(f"entry '{name}' is already published under {entry.dir}")
    kind = (target_kind or entry.kind or "bot").strip().lower()
    if kind not in KIND_TO_FOLDER:
        raise ValueError(f"unsupported kind '{kind}'")
    validation = validate_entry(name, include_workspace=True)
    if not validation["ok"]:
        raise RuntimeError(
            f"cannot publish '{name}': validation failed: {'; '.join(validation['errors'])}"
        )
    destination = LIBRARY_ROOT / KIND_TO_FOLDER[kind] / entry.dir.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    shutil.move(str(entry.dir), str(destination))
    manifest = _read_yaml(destination / "manifest.yaml")
    manifest["kind"] = kind
    manifest["updated_at"] = _utcnow_iso()
    _write_yaml(destination / "manifest.yaml", manifest)
    refresh_index()
    return destination


_REQUIRED_MANIFEST_KEYS = ("schema_version", "name", "kind", "version")


def validate_entry(name: str, include_workspace: bool = True) -> Dict[str, Any]:
    """Validate manifest, module import and a dry-instantiation with defaults."""
    errors: List[str] = []
    warnings: List[str] = []
    try:
        entry = load_entry(name, include_workspace=include_workspace)
    except Exception as exc:
        return {"ok": False, "errors": [f"load_entry failed: {exc}"], "warnings": warnings}
    for key in _REQUIRED_MANIFEST_KEYS:
        if key not in entry.manifest:
            errors.append(f"missing manifest key '{key}'")
    if entry.manifest.get("schema_version") not in (None, SCHEMA_VERSION):
        warnings.append(
            f"schema_version {entry.manifest.get('schema_version')} differs from supported {SCHEMA_VERSION}"
        )
    if entry.kind not in KIND_TO_FOLDER:
        errors.append(f"invalid kind '{entry.kind}'")
    ep = entry.manifest.get("entry_point")
    if not ep and not entry.is_reference_only():
        errors.append("missing entry_point (and entry is not flagged as reference_only)")
    if ep:
        try:
            symbol = resolve_entry_point(entry)
        except Exception as exc:
            errors.append(f"entry_point '{ep}' failed to resolve: {exc}")
            symbol = None
        if symbol is not None and entry.kind == "bot":
            try:
                defaults = entry.manifest.get("default_params") or {}
                symbol(**defaults)
            except Exception as exc:
                errors.append(f"bot instantiation with default_params failed: {exc}")
    req_cols = (entry.manifest.get("data_requirements") or {}).get("required_columns") or []
    if req_cols:
        sample = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "open_time": 0}
        missing = [col for col in req_cols if col not in sample]
        if missing:
            warnings.append(f"required columns not in synthetic sample: {missing}")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "entry": entry.name,
        "kind": entry.kind,
    }


def refresh_index() -> Path:
    """Regenerate ``library/_index.json`` from disk and return its path."""
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    entries_payload: List[Dict[str, Any]] = []
    for entry in list_entries(include_workspace=False):
        entries_payload.append(
            {
                "name": entry.name,
                "kind": entry.kind,
                "version": entry.version,
                "dir": str(entry.dir.relative_to(LIBRARY_ROOT.parent)).replace("\\", "/"),
                "tags": entry.manifest.get("tags", []),
                "description": entry.manifest.get("description", ""),
                "entry_point": entry.manifest.get("entry_point"),
                "registry_aliases": entry.manifest.get("registry_aliases", []),
                "reference_only": bool(entry.manifest.get("reference_only")),
                "notes_file": entry.manifest.get("notes_file"),
                "preset_dir": entry.manifest.get("preset_dir"),
                "presets": [p.stem for p in entry.preset_paths],
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utcnow_iso(),
        "entries": entries_payload,
        "counts": {
            "total": len(entries_payload),
            "bots": sum(1 for e in entries_payload if e["kind"] == "bot"),
            "indicators": sum(1 for e in entries_payload if e["kind"] == "indicator"),
            "tools": sum(1 for e in entries_payload if e["kind"] == "tool"),
            "reference_only": sum(1 for e in entries_payload if e["reference_only"]),
        },
    }
    index_path = LIBRARY_ROOT / INDEX_FILENAME
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path


def import_aporte(name: str) -> Path:
    """Scaffold a workspace draft prefilled with the legacy aportes source.

    Notes.md quotes the live-bot logic for reference; we do NOT import the
    live code path since the new entry is meant to be a clean adapter.
    """
    aporte_path = LIBRARY_ROOT.parent / "aportes" / f"{name}.py"
    if not aporte_path.exists():
        raise FileNotFoundError(f"aporte not found: {aporte_path}")
    target = scaffold_entry(name=name, kind="bot", workspace=True)
    notes_path = target / "notes.md"
    source = aporte_path.read_text(encoding="utf-8")
    notes_path.write_text(
        f"# {name} (imported from aportes/{name}.py)\n\n"
        f"Scaffolded from the live-bot source on {_utcnow_iso()}. The block below is\n"
        "preserved verbatim as design context; the backtest adapter must be\n"
        "implemented separately under `strategy.py`.\n\n"
        "## Live-bot source (for reference only)\n\n"
        "```python\n" + source + "\n```\n",
        encoding="utf-8",
    )
    return target


def _build_default_params_callable(name: str, defaults: Dict[str, Any]) -> Callable[[Any], Dict[str, Any]]:
    cleaned = dict(defaults)

    def _resolver(args: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, default in cleaned.items():
            out[key] = getattr(args, key, default)
        return out

    _resolver.__name__ = f"_library_params_{name}"
    return _resolver


def _build_search_space_callable(name: str, space: Dict[str, Any]) -> Callable[..., Dict[str, Any]]:
    spec = dict(space)

    def _resolver(trial: Any, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        overrides = overrides or {}
        params: Dict[str, Any] = {}
        for key, hint in spec.items():
            if key in overrides:
                params[key] = overrides[key]
                continue
            if not isinstance(hint, dict):
                params[key] = hint
                continue
            htype = str(hint.get("type", "float")).lower()
            lo = hint.get("min")
            hi = hint.get("max")
            choices = hint.get("values")
            if choices is not None:
                params[key] = trial.suggest_categorical(key, list(choices))
            elif htype == "int":
                params[key] = int(trial.suggest_int(key, int(lo), int(hi)))
            elif htype == "categorical":
                params[key] = trial.suggest_categorical(key, list(hint.get("values", [])))
            else:
                params[key] = float(trial.suggest_float(key, float(lo), float(hi)))
        return params

    _resolver.__name__ = f"_library_search_space_{name}"
    return _resolver


def register_with_strategy_registry() -> None:
    """Idempotently insert all bot manifests into :mod:`backtest.registry`.

    This is safe to call multiple times: each call replays the manifests from
    disk and updates the registry / override hooks in place. Existing
    hard-coded strategies are left intact; library entries pointing at the
    same class just refresh the alias map.
    """
    from backtest import registry

    for entry in list_entries(kind="bot", include_workspace=False):
        if entry.is_reference_only():
            continue
        ep = entry.manifest.get("entry_point")
        if not ep:
            continue
        try:
            cls = resolve_entry_point(entry)
        except Exception:
            continue
        key = entry.name.strip().lower()
        registry.STRATEGY_REGISTRY[key] = cls
        for alias in entry.manifest.get("registry_aliases") or []:
            alias_key = str(alias).strip().lower()
            if alias_key:
                registry.STRATEGY_REGISTRY[alias_key] = cls
        defaults = entry.manifest.get("default_params") or {}
        if defaults and key not in registry.PARAMS_FROM_CLI_OVERRIDES:
            registry.PARAMS_FROM_CLI_OVERRIDES[key] = _build_default_params_callable(key, defaults)
        search_space = entry.manifest.get("search_space") or {}
        if search_space and key not in registry.SUGGEST_PARAMS_OVERRIDES:
            registry.SUGGEST_PARAMS_OVERRIDES[key] = _build_search_space_callable(key, search_space)
