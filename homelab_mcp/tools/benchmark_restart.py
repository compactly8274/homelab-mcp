
"""Benchmark restart tool (Phase 5).

Measures how long a container or stack takes to restart and become
healthy again. Captures pre/post metrics so the result can be fed into
the diff tool (Phase 6).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homelab_mcp.server import get_host, mcp
from homelab_mcp.tools.container_action import container_action_tool
from homelab_mcp.tools.container_metrics import container_metrics_tool
from homelab_mcp.tools.http_probe import http_probe_tool

_MAX_WAIT_S = 300.0
_PROBE_INTERVAL_S = 1.0


@mcp.tool()
async def benchmark_restart_tool(
    host: str,
    target: str,
    *,
    probe_url: str | None = None,
    probe_interval: float = 1.0,
    probe_timeout: float = 10.0,
    max_wait_after_restart: float = 120.0,
    record_metrics: bool = True,
) -> dict[str, Any]:
    """Restart a container or stack and measure settle time + downtime.

    Parameters
    ----------
    host : str
        Host alias on which the target lives.
    target : str
        Container name or stack (compose project) to restart.
    probe_url : str | None, optional
        If given, poll this URL after restart until it returns a 2xx/3xx
        response. Otherwise settle is determined by the container state
        becoming "running".
    probe_interval : float, default 1.0
        Seconds between health probes after restart.
    probe_timeout : float, default 10.0
        Per-probe HTTP timeout in seconds.
    max_wait_after_restart : float, default 120.0
        Maximum seconds to wait for the target to become healthy.
    record_metrics : bool, default True
        Capture pre/post container metrics for the target.

    Returns
    -------
    dict
        {
            "ok": bool,
            "host": str,
            "target": str,
            "restart": {...},          # result from container_action_tool
            "preflight": {...},        # preflight verdict
            "pre_metrics": {...},
            "post_metrics": {...},
            "restart_started_at": float,
            "restart_finished_at": float,
            "healthy_at": float | None,
            "downtime_ms": int | None,
            "settle_ms": int | None,
            "error": str | None,
        }
    """
    if not target:
        return {"ok": False, "host": host, "target": target, "error": "target is required"}
    if max_wait_after_restart <= 0:
        return {"ok": False, "host": host, "target": target, "error": "max_wait_after_restart must be positive"}

    probe_interval = max(0.1, min(float(probe_interval), 30.0))
    probe_timeout = max(1.0, min(float(probe_timeout), 30.0))
    max_wait_after_restart = min(float(max_wait_after_restart), _MAX_WAIT_S)

    # Verify host is known
    try:
        host_client = get_host(host)
    except KeyError as e:
        return {"ok": False, "host": host, "target": target, "error": f"unknown host: {host!r} ({e})"}

    # Resolve target: container vs stack
    containers = await host_client.list_containers(all=True)
    is_container = any(c.get("NAME") == target for c in containers)
    if not is_container:
        stack_members = [c.get("NAME") for c in containers if c.get("PROJECT") == target]
        if not stack_members:
            return {
                "ok": False,
                "host": host,
                "target": target,
                "error": f"target {target!r} is neither a container nor a stack on host {host!r}",
            }

    # Capture pre-restart metrics
    pre_metrics = None
    if record_metrics:
        pre = await container_metrics_tool(host=host, container=target if is_container else None)
        if pre.get("ok"):
            pre_metrics = pre.get("metrics")

    # Restart through the preflight-gated tool
    restart_started_at = time.monotonic()
    restart_result = await container_action_tool(
        host=host,
        target=target,
        action="restart",
        require_approval=True,
    )
    restart_finished_at = time.monotonic()
    preflight = restart_result.get("preflight")

    if restart_result.get("action") != "applied":
        return {
            "ok": False,
            "host": host,
            "target": target,
            "restart": restart_result,
            "preflight": preflight,
            "pre_metrics": pre_metrics,
            "post_metrics": None,
            "restart_started_at": restart_started_at,
            "restart_finished_at": restart_finished_at,
            "healthy_at": None,
            "downtime_ms": None,
            "settle_ms": None,
            "error": restart_result.get("message") or restart_result.get("error") or "restart was not applied",
        }

    # Poll until healthy or timeout
    healthy_at: float | None = None
    deadline = restart_finished_at + max_wait_after_restart
    probe_url_for_check = probe_url
    if probe_url_for_check:
        # For stacks, probe the given URL; for containers without URL, fall back to state polling
        pass
    else:
        # If no probe URL, we still try to detect running state for the target
        pass

    while time.monotonic() < deadline:
        now = time.monotonic()
        if probe_url_for_check:
            probe = await http_probe_tool(
                url=probe_url_for_check,
                method="GET",
                timeout=probe_timeout,
                host=host,
                allow_redirects=True,
            )
            if probe.get("ok"):
                code = probe.get("http_code")
                if isinstance(code, int) and 200 <= code < 400:
                    healthy_at = now
                    break
        else:
            cs = await host_client.list_containers(all=False)
            if is_container:
                if any(c.get("NAME") == target and c.get("STATE") == "running" for c in cs):
                    healthy_at = now
                    break
            else:
                running_members = [c.get("NAME") for c in cs if c.get("PROJECT") == target and c.get("STATE") == "running"]
                if running_members and len(running_members) == len(stack_members):
                    healthy_at = now
                    break
        sleep_s = min(probe_interval, deadline - time.monotonic())
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

    # Capture post-restart metrics
    post_metrics = None
    if record_metrics:
        post = await container_metrics_tool(host=host, container=target if is_container else None)
        if post.get("ok"):
            post_metrics = post.get("metrics")

    settle_ms = None
    downtime_ms = None
    if healthy_at:
        settle_ms = int((healthy_at - restart_finished_at) * 1000)
        downtime_ms = int((healthy_at - restart_started_at) * 1000)

    return {
        "ok": healthy_at is not None,
        "host": host,
        "target": target,
        "restart": restart_result,
        "preflight": preflight,
        "pre_metrics": pre_metrics,
        "post_metrics": post_metrics,
        "restart_started_at": restart_started_at,
        "restart_finished_at": restart_finished_at,
        "healthy_at": healthy_at,
        "downtime_ms": downtime_ms,
        "settle_ms": settle_ms,
        "error": None if healthy_at else f"target did not become healthy within {max_wait_after_restart}s",
    }
