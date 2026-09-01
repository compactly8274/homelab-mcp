
"""Benchmark diff / baseline storage tool (Phase 6).

Stores point-in-time benchmark snapshots on disk and computes deltas
between a stored baseline and the current state. This closes the loop
started by Phase 3 (metrics), Phase 4 (load), and Phase 5 (restart).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homelab_mcp.server import mcp
from homelab_mcp.tools.benchmark_load import benchmark_load_tool
from homelab_mcp.tools.container_metrics import container_metrics_tool

_DEFAULT_BENCHMARK_DIR = "/data/benchmarks"


def _benchmark_dir() -> Path:
    return Path(os.environ.get("HOMELAB_MCP_BENCHMARK_DIR", _DEFAULT_BENCHMARK_DIR))


_UNSAFE_PATH_CHARS = ("/", "\\", "*", "?", "[", "]")


def _is_safe_path_component(value: str) -> bool:
    """Reject path separators, traversal sequences, and glob metacharacters.

    Glob metacharacters matter because ``_find_baseline`` uses this value to
    build a ``Path.glob()`` pattern; without this, a wildcard host/container/
    label would match other hosts' or containers' baseline files.
    """
    if not value or ".." in value:
        return False
    return not any(c in value for c in _UNSAFE_PATH_CHARS)


def _baseline_filename(host: str, container: str | None, label: str, ts: str) -> str:
    safe_container = container if container else "all"
    return f"{host}_{safe_container}_{label}_{ts}.json"


def _find_baseline(host: str, container: str | None, label: str) -> Path | None:
    d = _benchmark_dir()
    if not d.is_dir():
        return None
    safe_container = container if container else "all"
    prefix = f"{host}_{safe_container}_{label}_"
    candidates = sorted(d.glob(f"{prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


@mcp.tool()
async def benchmark_baseline_tool(
    host: str,
    container: str | None = None,
    *,
    label: str = "default",
    include_metrics: bool = True,
    load_url: str | None = None,
    load_requests: int = 50,
    load_concurrency: int = 5,
) -> dict[str, Any]:
    """Save a benchmark snapshot to disk for later comparison.

    Parameters
    ----------
    host : str
        Host alias the snapshot is for.
    container : str | None, optional
        Container name, or None for a host-wide snapshot.
    label : str, default "default"
        Logical label used to group baselines (e.g. "before-update").
    include_metrics : bool, default True
        Capture current container_metrics into the snapshot.
    load_url : str | None, optional
        If given, also capture a load-test result into the snapshot.
    load_requests : int, default 50
        Number of requests for the optional load test.
    load_concurrency : int, default 5
        Concurrency for the optional load test.

    Returns
    -------
    dict
        {"ok": bool, "path": str, "record": {...}, "error": str | None}
    """
    if not host:
        return {"ok": False, "path": None, "record": None, "error": "host is required"}
    if not _is_safe_path_component(host):
        return {"ok": False, "path": None, "record": None, "error": f"invalid host: {host!r}"}
    if container is not None and not _is_safe_path_component(container):
        return {"ok": False, "path": None, "record": None, "error": f"invalid container: {container!r}"}
    if not _is_safe_path_component(label):
        return {"ok": False, "path": None, "record": None, "error": f"invalid label: {label!r}"}

    ts = datetime.now(UTC).isoformat().replace(":", "_")
    record: dict[str, Any] = {
        "created_at": ts.replace("_", ":"),
        "host": host,
        "container": container,
        "label": label,
    }

    if include_metrics:
        metrics = await container_metrics_tool(host=host, container=container)
        record["metrics"] = metrics.get("metrics") if metrics.get("ok") else {"error": metrics.get("error")}

    if load_url:
        load = await benchmark_load_tool(
            url=load_url,
            requests=load_requests,
            concurrency=load_concurrency,
            host=host,
        )
        record["load"] = load if load.get("ok") else {"error": load.get("error")}

    d = _benchmark_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _baseline_filename(host, container, label, ts)
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    return {"ok": True, "path": str(path), "record": record, "error": None}


@mcp.tool()
async def benchmark_diff_tool(
    host: str,
    container: str | None = None,
    *,
    baseline_label: str = "default",
    baseline_path: str | None = None,
    sample_seconds: float = 1.0,
) -> dict[str, Any]:
    """Compare current container metrics to a stored baseline.

    Parameters
    ----------
    host : str
        Host alias.
    container : str | None, optional
        Container name, or None for host-wide comparison.
    baseline_label : str, default "default"
        Label used to look up the most recent baseline.
    baseline_path : str | None, optional
        Explicit path to a baseline file. If given, overrides label lookup.
    sample_seconds : float, default 1.0
        Passed to container_metrics_tool.

    Returns
    -------
    dict
        {
            "ok": bool,
            "baseline": {...},
            "current": {...},
            "deltas": {...},
            "error": str | None,
        }
    """
    if not host:
        return {"ok": False, "baseline": None, "current": None, "deltas": None, "error": "host is required"}
    if not _is_safe_path_component(host):
        return {"ok": False, "baseline": None, "current": None, "deltas": None, "error": f"invalid host: {host!r}"}
    if container is not None and not _is_safe_path_component(container):
        return {"ok": False, "baseline": None, "current": None, "deltas": None, "error": f"invalid container: {container!r}"}
    if not _is_safe_path_component(baseline_label):
        return {"ok": False, "baseline": None, "current": None, "deltas": None, "error": f"invalid baseline_label: {baseline_label!r}"}

    path: Path | None = None
    if baseline_path:
        try:
            path = Path(baseline_path).resolve()
            base_dir = _benchmark_dir().resolve()
        except (OSError, RuntimeError) as e:
            return {
                "ok": False, "baseline": None, "current": None, "deltas": None,
                "error": f"invalid baseline_path: {baseline_path!r} ({e})",
            }
        if not path.is_relative_to(base_dir):
            return {
                "ok": False, "baseline": None, "current": None, "deltas": None,
                "error": f"baseline_path must be inside the benchmark directory: {baseline_path!r}",
            }
        if not path.is_file():
            return {"ok": False, "baseline": None, "current": None, "deltas": None, "error": f"baseline not found: {baseline_path}"}
    else:
        path = _find_baseline(host, container, baseline_label)
        if path is None:
            return {
                "ok": False,
                "baseline": None,
                "current": None,
                "deltas": None,
                "error": f"no baseline found for {host}:{container or 'all'} label={baseline_label!r}",
            }

    baseline_record = json.loads(path.read_text(encoding="utf-8"))
    baseline_metrics = baseline_record.get("metrics") or {}
    baseline_containers = baseline_metrics.get("containers") or {}

    current = await container_metrics_tool(host=host, container=container, sample_seconds=sample_seconds)
    if not current.get("ok"):
        return {"ok": False, "baseline": baseline_record, "current": current, "deltas": None, "error": current.get("error")}
    current_metrics = current.get("metrics") or {}
    current_containers = current_metrics.get("containers") or {}

    all_names = sorted(set(baseline_containers.keys()) | set(current_containers.keys()))
    deltas: dict[str, Any] = {}
    for name in all_names:
        b = baseline_containers.get(name, {})
        c = current_containers.get(name, {})
        deltas[name] = _container_delta(b, c)

    return {
        "ok": True,
        "baseline": baseline_record,
        "current": current_metrics,
        "deltas": deltas,
        "error": None,
    }


def _container_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return a flat dict of numeric deltas for common metrics."""
    delta: dict[str, Any] = {
        "cpu_percent": _num_delta(before.get("cpu_percent"), after.get("cpu_percent")),
        "memory_usage_percent": _num_delta(
            (before.get("memory") or {}).get("usage_percent"),
            (after.get("memory") or {}).get("usage_percent"),
        ),
        "memory_usage_bytes": _num_delta(
            (before.get("memory") or {}).get("usage_bytes"),
            (after.get("memory") or {}).get("usage_bytes"),
        ),
        "network_rx_bytes": _num_delta(
            (before.get("network") or {}).get("rx_bytes"),
            (after.get("network") or {}).get("rx_bytes"),
        ),
        "network_tx_bytes": _num_delta(
            (before.get("network") or {}).get("tx_bytes"),
            (after.get("network") or {}).get("tx_bytes"),
        ),
        "block_io_read_bytes": _num_delta(
            (before.get("block_io") or {}).get("read_bytes"),
            (after.get("block_io") or {}).get("read_bytes"),
        ),
        "block_io_write_bytes": _num_delta(
            (before.get("block_io") or {}).get("write_bytes"),
            (after.get("block_io") or {}).get("write_bytes"),
        ),
        "missing_in_baseline": not bool(before),
        "missing_in_current": not bool(after),
    }
    return delta


def _num_delta(a: float | None, b: float | None) -> dict[str, Any]:
    if a is None or b is None:
        return {"before": a, "after": b, "delta": None, "delta_percent": None}
    delta = b - a
    delta_pct = round((delta / a) * 100.0, 2) if a else None
    return {"before": a, "after": b, "delta": round(delta, 2), "delta_percent": delta_pct}
