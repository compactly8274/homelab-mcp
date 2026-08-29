"""Benchmark load/stress test tool (Phase 4).

Runs an HTTP request burst against one or more URLs and reports
latency percentiles, throughput, and error distribution.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from typing import Any

from homelab_mcp.server import get_host, mcp
from homelab_mcp.tools.http_probe import http_probe_tool

_MAX_REQUESTS = 10_000
_MAX_CONCURRENCY = 500
_MAX_TIMEOUT_S = 30.0


@mcp.tool()
async def benchmark_load_tool(
    url: str,
    requests: int = 100,
    concurrency: int = 10,
    method: str = "GET",
    timeout: float = 10.0,
    host: str | None = None,
    allow_redirects: bool = True,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run an HTTP load burst and return latency percentiles + throughput.

    This tool is read-only: it only probes the target and never writes
    to it. ``require_approval`` is not applicable because the underlying
    ``http_probe_tool`` already uses safe curl-only semantics.

    Parameters
    ----------
    url : str
        Target URL. If no scheme is given, ``http://`` is prepended.
    requests : int, default 100
        Total number of HTTP requests to make. Capped at 10,000.
    concurrency : int, default 10
        Maximum in-flight requests at once. Capped at 500.
    method : str, default "GET"
        HTTP method. One of GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS.
    timeout : float, default 10.0
        Per-request curl timeout in seconds. Capped at 30.
    host : str | None, default None
        Host alias on which to run curl. If None, runs on the local
        daemon host.
    allow_redirects : bool, default True
        Follow HTTP 3xx redirects.
    headers : dict[str, str] | None, default None
        Extra HTTP headers to send on every request.

    Returns
    -------
    dict
        {
            "ok": bool,
            "host": str,
            "url": str,
            "method": str,
            "requests": int,
            "successful": int,
            "errors": int,
            "error_messages": {"message": count, ...},
            "status_counts": {code: count, ...},
            "latency_ms": {
                "min": float | None,
                "mean": float | None,
                "p50": float | None,
                "p95": float | None,
                "p99": float | None,
                "max": float | None,
            },
            "duration_ms": int,
            "rps": float,
        }
    """
    if not url:
        return {"ok": False, "error": "url is required"}

    requests = max(1, min(int(requests), _MAX_REQUESTS))
    concurrency = max(1, min(int(concurrency), _MAX_CONCURRENCY))
    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT_S))

    target_host = host or "local"
    try:
        get_host(target_host)
    except KeyError as e:
        return {
            "ok": False,
            "host": target_host,
            "error": f"unknown host: {target_host} ({e})",
        }

    semaphore = asyncio.Semaphore(concurrency)

    async def _one_request(idx: int) -> dict[str, Any]:
        async with semaphore:
            return await http_probe_tool(
                url=url,
                method=method,
                timeout=timeout,
                host=target_host,
                allow_redirects=allow_redirects,
                headers=headers,
            )

    t0 = time.monotonic()
    results = await asyncio.gather(*(_one_request(i) for i in range(requests)))
    duration_ms = int((time.monotonic() - t0) * 1000)

    successful = 0
    errors = 0
    error_counter: Counter[str] = Counter()
    status_counter: Counter[int] = Counter()
    latencies: list[float] = []

    for r in results:
        if r.get("ok"):
            successful += 1
            http_code = r.get("http_code")
            if isinstance(http_code, int):
                status_counter[http_code] += 1
            duration = r.get("duration_ms")
            if isinstance(duration, (int, float)):
                latencies.append(float(duration))
        else:
            errors += 1
            msg = r.get("error") or r.get("stderr") or "unknown error"
            error_counter[str(msg)[:120]] += 1

    rps = (requests / (duration_ms / 1000.0)) if duration_ms > 0 else float(requests)

    return {
        "ok": True,
        "host": target_host,
        "url": url,
        "method": method.upper(),
        "requests": requests,
        "successful": successful,
        "errors": errors,
        "error_messages": dict(error_counter.most_common(10)),
        "status_counts": dict(status_counter),
        "latency_ms": _latency_summary(latencies),
        "duration_ms": duration_ms,
        "rps": round(rps, 2),
    }


def _latency_summary(latencies: list[float]) -> dict[str, float | None]:
    if not latencies:
        return {"min": None, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    latencies.sort()
    n = len(latencies)
    return {
        "min": round(latencies[0], 2),
        "mean": round(sum(latencies) / n, 2),
        "p50": round(_percentile(latencies, 0.50), 2),
        "p95": round(_percentile(latencies, 0.95), 2),
        "p99": round(_percentile(latencies, 0.99), 2),
        "max": round(latencies[-1], 2),
    }


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile for sorted data."""
    if not sorted_data:
        return 0.0
    if p <= 0:
        return sorted_data[0]
    if p >= 1:
        return sorted_data[-1]
    k = (len(sorted_data) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)

