"""Stack health dashboard tool.

A single ``health_dashboard_tool()`` call returns a one-screen triage
view across every configured host, replacing what would otherwise be
5+ ``ssh && docker ps && docker inspect && ...`` invocations. The
output is designed for an LLM to reason over and for a human to
skim, not to be a full state dump.

For each host we surface:
- reachability: True/False, plus a one-line error if unreachable
- container counts: running / stopped / unhealthy / total
- stacks: total + count with pending updates
- top problems: the 3 worst-offender stacks (unhealthy, restart-looping,
  or with pending image updates)
- last events: 3 most recent docker events on the host

The tool is read-only and never modifies state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homelab_mcp import server as _server
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)


async def _host_snapshot(name: str, host: Any) -> dict[str, Any]:
    """Build a one-host snapshot. Failures are captured as data, not raised."""
    snap: dict[str, Any] = {"host": name, "reachable": True, "error": None}
    try:
        # Run 4 calls in parallel: list_containers, list_stacks, events, summary
        containers, stacks, events = await asyncio.gather(
            host.list_containers(all=True),
            host.list_stacks(),
            host.events(since_seconds=600),
            return_exceptions=True,
        )
        if isinstance(containers, Exception):
            raise containers
        if isinstance(stacks, Exception):
            raise stacks
        if isinstance(events, Exception):
            events = []

        # Container counts by state
        states: dict[str, int] = {"running": 0, "stopped": 0, "unhealthy": 0, "other": 0}
        unhealthy_names: list[str] = []
        for c in containers:
            s = (c.get("STATE") or "").lower()
            if "running" in s and "unhealthy" in s:
                states["unhealthy"] += 1
                unhealthy_names.append(c.get("NAME", "?"))
            elif "running" in s:
                states["running"] += 1
            elif "exited" in s or "stopped" in s or "dead" in s:
                states["stopped"] += 1
            else:
                states["other"] += 1

        # Stack counts. Also look up pending_updates per stack so the
        # WebUI can show "X stacks with pending updates" without
        # having to make a second /api/pendings call. Fix 2026-07-18:
        # the WebUI was reading h.stacks.with_pending_updates which
        # wasn't returned here, so the count always showed 0.
        stack_names = sorted(s.get("name", "?") for s in stacks)
        pending_count = 0
        try:
            from homelab_mcp import server as _server
            state = _server.get_state()
            rows = await state.list_pending_updates(host=name)
            stack_set = set(stack_names)
            pending_count = sum(1 for r in rows if r.get("stack") in stack_set)
        except Exception:
            # State DB not available or host unknown; leave as 0
            pending_count = 0

        # Container counts
        snap["containers"] = {
            "total": len(containers),
            "running": states["running"],
            "stopped": states["stopped"],
            "unhealthy": states["unhealthy"],
            "other": states["other"],
        }
        snap["stacks"] = {
            "total": len(stacks),
            "names": stack_names,
            "with_pending_updates": pending_count,
        }
        snap["recent_events"] = events[:3] if isinstance(events, list) else []
        snap["unhealthy_containers"] = unhealthy_names[:10]
        snap["last_error"] = None
    except Exception as e:
        log.warning("dashboard: %s failed: %s", name, e)
        snap["reachable"] = False
        snap["error"] = f"{type(e).__name__}: {e}"
    return snap


@mcp.tool()
async def health_dashboard_tool(
    only_host: str | None = None,
    top_problems: int = 3,
) -> dict[str, Any]:
    """One-screen triage view of every configured host.

    Replaces what would otherwise be 5+ ``ssh && docker ps`` calls. Returns
    a dict shaped like::

        {
          "generated_at": "2026-07-16T21:30:00Z",
          "summary": {
            "total_hosts": 3, "reachable": 3, "unreachable": 0,
            "total_containers": 117, "unhealthy": 0, "stopped": 12,
            "total_stacks": 47
          },
          "hosts": [
            {"host": "unraid", "reachable": true, "containers": {...}, "stacks": {...}, ...},
            ...
          ],
          "top_problems": [
            "truenas: 1 container unhealthy (lidarr)",
            "unraid: 3 stacks with pending image updates",
            ...
          ]
        }

    Args:
        only_host: Restrict to one host alias. Default: every configured host.
        top_problems: How many top problems to surface. Default 3.
    """
    if only_host:
        if only_host not in _server._host_clients:
            return {"error": f"unknown host {only_host!r}", "known_hosts": list(_server._host_clients)}
        targets = {only_host: _server._host_clients[only_host]}
    else:
        targets = dict(_server._host_clients)

    hosts = await asyncio.gather(
        *(_host_snapshot(name, h) for name, h in targets.items())
    )

    # Roll-up summary
    total_containers = sum(h.get("containers", {}).get("total", 0) for h in hosts)
    total_running = sum(h.get("containers", {}).get("running", 0) for h in hosts)
    total_stopped = sum(h.get("containers", {}).get("stopped", 0) for h in hosts)
    total_unhealthy = sum(h.get("containers", {}).get("unhealthy", 0) for h in hosts)
    total_stacks = sum(h.get("stacks", {}).get("total", 0) for h in hosts)
    reachable = sum(1 for h in hosts if h.get("reachable"))

    # Build top problems list
    problems: list[str] = []
    for h in hosts:
        if not h.get("reachable"):
            problems.append(f"{h['host']}: UNREACHABLE — {h.get('error', '?')}")
        if h.get("containers", {}).get("unhealthy", 0) > 0:
            names = ", ".join(h.get("unhealthy_containers", [])[:3])
            problems.append(
                f"{h['host']}: {h['containers']['unhealthy']} container(s) unhealthy "
                f"({names}{'...' if len(h.get('unhealthy_containers', [])) > 3 else ''})"
            )
    # Trim to requested count
    problems = problems[:top_problems]

    return {
        "generated_at": int(time.time()),
        "summary": {
            "total_hosts": len(hosts),
            "reachable": reachable,
            "unreachable": len(hosts) - reachable,
            "total_containers": total_containers,
            "running": total_running,
            "stopped": total_stopped,
            "unhealthy": total_unhealthy,
            "total_stacks": total_stacks,
        },
        "hosts": hosts,
        "top_problems": problems,
    }
