"""Cross-platform process isolation primitives for the orchestrator.

The orchestrator wraps each trial in `spawn_isolated_worker(...)` so that a
single OOM, segfault, or `MemoryError` cannot bring down the parent process
nor the rest of the in-flight trials. `WorkerLimits` lets callers cap RAM
and CPU per worker; the implementation prefers POSIX `resource` rlimits when
available and falls back to a psutil-driven watchdog on Windows.
"""
from __future__ import annotations

import multiprocessing
import os
import pickle
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


class OrchestrationError(RuntimeError):
    """Raised for unrecoverable orchestration issues (e.g. unpicklable target)."""


@dataclass
class WorkerLimits:
    max_ram_bytes: int | None = None
    max_cpu_seconds: int | None = None


@dataclass
class WorkerResult:
    ok: bool
    value: Any = None
    error: str | None = None
    peak_rss_mb: float | None = None
    elapsed_sec: float | None = None


def _try_pickle(obj: Any, label: str) -> None:
    try:
        pickle.dumps(obj)
    except Exception as exc:
        raise OrchestrationError(
            f"{label} is not picklable for spawn-based multiprocessing: {exc}"
        ) from exc


def apply_limits(limits: WorkerLimits) -> None:
    """Apply rlimits on POSIX; on Windows install a psutil polling watchdog.

    Best-effort: each constraint is attempted independently and failures are
    swallowed (logged via stderr) so a worker that cannot set a limit still
    runs without it instead of crashing before it begins.
    """
    if limits is None:
        return

    on_windows = sys.platform.startswith("win")

    if not on_windows:
        try:
            import resource  # type: ignore[import-not-found,unused-ignore]

            if limits.max_ram_bytes is not None:
                try:
                    resource.setrlimit(  # type: ignore[attr-defined,unused-ignore]
                        resource.RLIMIT_AS,  # type: ignore[attr-defined,unused-ignore]
                        (int(limits.max_ram_bytes), int(limits.max_ram_bytes)),
                    )
                except (ValueError, OSError) as exc:
                    sys.stderr.write(f"WARN: could not apply RAM rlimit: {exc}\n")
            if limits.max_cpu_seconds is not None:
                try:
                    resource.setrlimit(  # type: ignore[attr-defined,unused-ignore]
                        resource.RLIMIT_CPU,  # type: ignore[attr-defined,unused-ignore]
                        (int(limits.max_cpu_seconds), int(limits.max_cpu_seconds)),
                    )
                except (ValueError, OSError) as exc:
                    sys.stderr.write(f"WARN: could not apply CPU rlimit: {exc}\n")
        except ImportError:  # pragma: no cover - POSIX without resource is rare
            pass
        return

    # Windows: start a daemon watchdog that polls our RSS via psutil.
    if limits.max_ram_bytes is None and limits.max_cpu_seconds is None:
        return

    try:
        import psutil  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        sys.stderr.write(
            "WARN: psutil not installed; worker resource caps are best-effort no-ops on Windows.\n"
        )
        return

    proc = psutil.Process(os.getpid())
    started = time.monotonic()

    def _watchdog() -> None:
        while True:
            try:
                rss = int(proc.memory_info().rss)
            except Exception:
                return
            if limits.max_ram_bytes is not None and rss > int(limits.max_ram_bytes):
                # MemoryError raised in worker context is caught by `_child_entry`.
                os._exit(137)
            if (
                limits.max_cpu_seconds is not None
                and (time.monotonic() - started) > float(limits.max_cpu_seconds)
            ):
                sys.stderr.write("WARN: CPU watchdog tripped on Windows; terminating worker.\n")
                os._exit(124)
            time.sleep(0.5)

    threading.Thread(target=_watchdog, name="worker-watchdog", daemon=True).start()


def _child_entry(
    queue: "multiprocessing.queues.Queue",
    target_payload: bytes,
    args_payload: bytes,
    limits: WorkerLimits | None,
) -> None:
    """Top-level entry point for the spawned subprocess.

    Lives at module scope so spawn-mode pickling can locate it. Receives the
    target+args as pickle bytes (cheaper than relying on default introspection)
    and writes the outcome onto `queue` as a single dict.
    """
    try:
        if limits is not None:
            apply_limits(limits)
        target = pickle.loads(target_payload)
        args = pickle.loads(args_payload)
        value = target(*args)
        queue.put({"ok": True, "value": value, "error": None})
    except MemoryError as exc:
        queue.put({"ok": False, "value": None, "error": f"MemoryError: {exc}"})
    except BaseException as exc:
        queue.put(
            {
                "ok": False,
                "value": None,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )


def spawn_isolated_worker(
    target: Callable[..., Any],
    args: Tuple[Any, ...] = (),
    limits: WorkerLimits | None = None,
    timeout_sec: float | None = None,
) -> WorkerResult:
    """Run `target(*args)` in a fresh `multiprocessing.Process`.

    The subprocess uses the `spawn` start method to keep behaviour identical
    across Linux/macOS/Windows. If `psutil` is available, the parent samples
    the child's RSS while it runs to report `peak_rss_mb`. On OOM, timeout,
    pickling failures, or any uncaught exception, returns a `WorkerResult`
    with `ok=False` rather than re-raising.
    """
    _try_pickle(target, "target callable")
    _try_pickle(args, "target args")

    ctx = multiprocessing.get_context("spawn")
    queue: "multiprocessing.queues.Queue" = ctx.Queue(maxsize=1)
    target_payload = pickle.dumps(target)
    args_payload = pickle.dumps(args)

    proc = ctx.Process(
        target=_child_entry,
        args=(queue, target_payload, args_payload, limits),
        daemon=False,
    )
    started = time.monotonic()
    proc.start()

    peak_rss_mb: float | None = None
    try:
        import psutil  # type: ignore[import-not-found,import-untyped]

        try:
            handle = psutil.Process(proc.pid)
        except Exception:
            handle = None
    except ImportError:
        handle = None  # type: ignore[assignment]

    deadline = (started + float(timeout_sec)) if timeout_sec else None

    while True:
        if not proc.is_alive():
            break
        if handle is not None:
            try:
                rss_mb = float(handle.memory_info().rss) / (1024.0 * 1024.0)
                if peak_rss_mb is None or rss_mb > peak_rss_mb:
                    peak_rss_mb = rss_mb
            except Exception:
                handle = None
        if deadline is not None and time.monotonic() > deadline:
            try:
                proc.terminate()
                proc.join(timeout=5.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=2.0)
            finally:
                elapsed = time.monotonic() - started
            return WorkerResult(
                ok=False,
                value=None,
                error=f"TimeoutError: worker exceeded {timeout_sec:.1f}s",
                peak_rss_mb=peak_rss_mb,
                elapsed_sec=elapsed,
            )
        time.sleep(0.05)

    proc.join(timeout=5.0)
    elapsed = time.monotonic() - started

    payload: dict | None = None
    try:
        if not queue.empty():
            payload = queue.get_nowait()
    except Exception:
        payload = None

    if payload is None:
        exitcode = int(proc.exitcode) if proc.exitcode is not None else -1
        return WorkerResult(
            ok=False,
            value=None,
            error=f"WorkerCrash: exitcode={exitcode}",
            peak_rss_mb=peak_rss_mb,
            elapsed_sec=elapsed,
        )

    if payload.get("ok"):
        return WorkerResult(
            ok=True,
            value=payload.get("value"),
            error=None,
            peak_rss_mb=peak_rss_mb,
            elapsed_sec=elapsed,
        )
    return WorkerResult(
        ok=False,
        value=None,
        error=str(payload.get("error") or "unknown worker error"),
        peak_rss_mb=peak_rss_mb,
        elapsed_sec=elapsed,
    )


__all__ = [
    "OrchestrationError",
    "WorkerLimits",
    "WorkerResult",
    "apply_limits",
    "spawn_isolated_worker",
]
