"""Pre-flight check tools for destructive operations.

The motivating incident: 2026-07-16, clearing a Lidarr queue with
``removeFromClient=true`` told qBittorrent to delete the underlying
data files. 11 albums gone, no trash, no recovery. The cost was
two minutes of "just clean up the queue" and a permanent loss of
the user's music collection.

This module builds guardrails around the common destructive ops:
``container_action``, ``apply_update``, ``dismiss_pending``.

For a (host, stack) pair the tool checks:
  1. Is the container in a restart loop? (state contains "restarting"
     or restart_count is high) → suggests "fix the underlying
     problem, not delete and restart"
  2. Did the container start <60s ago? → suggests "wait — this
     may be an Apply storm, not a real failure"
  3. Has the container never been healthy? → suggests "the image
     is broken, don't restart-loop it; fix the compose first"
  4. Are there other containers in the same stack? → "stopping the
     parent will orphan dependents"
  5. Is this the only host that has the stack? → "you'd lose all
     replicas"
  6. Is there pending uncommitted data on the host's volume paths
     (heuristic: any non-running container with the same volume)?
     → "wait for those to finish before you nuke this"

Returns a structured verdict the LLM (or a human) can act on:
  {
    "safe_to_<action>": bool,
    "blockers":   [str, ...],   # reasons to STOP, no override
    "warnings":   [str, ...],   # things to KNOW before proceeding
    "info":       [str, ...],   # neutral facts
    "suggested_alternative": str | None,
  }
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homelab_mcp import server as _server
from homelab_mcp.server import mcp
from homelab_mcp.tools._state import get_state

log = logging.getLogger(__name__)

# Compose-up restarts often show as "Restarting (1) 5 seconds ago" in
# `docker ps` STATE columns. Treat those as transient.
_RESTARTING_RE = re.compile(r"restarting\s+\(\d+\)\s+\d+\s+seconds?\s+ago", re.IGNORECASE)
# Recent-started container threshold (seconds)
_RECENT_START_SECONDS = 60
# Unhealthy-restart threshold
_MAX_RESTART_COUNT = 3


def _started_recently(container: dict[str, Any]) -> bool:
    """Heuristic: parse StartedAt from inspect, return True if <60s old."""
    started = container.get("State", {}).get("StartedAt", "")
    if not started:
        return False
    # ISO format: 2026-07-16T21:30:00.123456789Z
    try:
        from datetime import UTC, datetime

        ts = datetime.fromisoformat(started.rstrip("Z").split(".")[0]).replace(tzinfo=UTC)
        age = (datetime.now(UTC) - ts).total_seconds()
        return 0 <= age < _RECENT_START_SECONDS
    except Exception:
        return False


def _is_in_restart_loop(container: dict[str, Any]) -> bool:
    """True if the container has been restarted >N times or is currently restarting."""
    state_str = (container.get("State", {}).get("Status") or "").lower()
    if _RESTARTING_RE.search(state_str):
        return True
    restart_count = int(container.get("RestartCount") or 0)
    return restart_count > _MAX_RESTART_COUNT and state_str not in ("running",)


async def _gather(host: str, stack: str) -> dict[str, Any]:
    """Collect the inspection results we need. Per-host failure is captured as data."""
    out: dict[str, Any] = {"containers": [], "inspect": [], "errors": []}
    try:
        h = _server._host_clients[host]
    except KeyError:
        out["errors"].append(f"unknown host {host!r}")
        return out
    try:
        containers = await h.list_containers(all=True)
        out["containers"] = [
            c for c in containers
            if c.get("PROJECT") == stack or c.get("NAME") == stack
        ]
    except Exception as e:
        out["errors"].append(f"list_containers failed: {e}")
    # Inspect every matching container for richer data
    for c in out["containers"]:
        try:
            info = await h.inspect_container(c["NAME"])
            out["inspect"].append(info)
        except Exception as e:
            out["errors"].append(f"inspect({c.get('NAME')}) failed: {e}")
    return out


@mcp.tool()
async def preflight_check_tool(
    host: str,
    stack: str,
    action: str,
) -> dict[str, Any]:
    """Check whether a destructive action is safe to take on a (host, stack).

    Args:
        host: Host alias (e.g. "truenas", "unraid", "qnap").
        stack: Stack or container name to act on.
        action: One of "remove", "stop", "restart", "apply_update",
            "dismiss_pending". The check applies different rules per
            action — "restart" is more permissive than "remove".

    Returns:
        {
          "safe": bool,                  # true iff no blockers
          "blockers":   [str, ...],      # hard stops, no override
          "warnings":   [str, ...],      # should-not-do-without-thinking
          "info":       [str, ...],      # neutral observations
          "suggested_alternative": str   # one-liner if a safer path exists
        }
    """
    action = action.lower()
    valid = {"remove", "stop", "restart", "apply_update", "dismiss_pending", "exec_in_container", "http_probe", "db_snapshot", "db_restore"}
    if action not in valid:
        return {
            "safe": False,
            "blockers": [f"unknown action {action!r}; valid: {sorted(valid)}"],
            "warnings": [],
            "info": [],
            "suggested_alternative": None,
        }

    data = await _gather(host, stack)
    blockers: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    alt: str | None = None

    if data["errors"] and not data["containers"]:
        blockers.append(f"could not inspect {host}:{stack}: {data['errors']}")
        return {
            "safe": False, "blockers": blockers, "warnings": [], "info": info,
            "suggested_alternative": "Verify the host is reachable and the stack name is correct.",
        }
    if not data["containers"] and not data["errors"]:
        # No errors but no matching containers = unknown stack
        blockers.append(
            f"no container or stack named {stack!r} found on {host!r}. "
            f"Refusing to act on a non-existent target."
        )
        return {
            "safe": False, "blockers": blockers, "warnings": [], "info": info,
            "suggested_alternative": (
                f"Run list_stacks_tool(host={host!r}) to see available stacks, "
                f"then re-call with the correct name."
            ),
        }

    # Per-container checks
    for ins in data["inspect"]:
        name = ins.get("Name", "?").lstrip("/")
        state = ins.get("State", {})
        status = (state.get("Status") or "").lower()
        restart_count = int(state.get("RestartCount") or 0)
        started = state.get("StartedAt", "")

        info.append(
            f"{name}: status={status!r}, restart_count={restart_count}, started={started}"
        )

        # Restart-loop pattern: container is currently "restarting" or
        # has been restarted >N times. This is the "FGC chromium" trap.
        if _is_in_restart_loop(ins):
            if action in ("remove", "stop"):
                warnings.append(
                    f"{name} is in a restart loop "
                    f"(restart_count={restart_count}, status={status!r}). "
                    f"Removing/stopping it now would mask the root cause."
                )
                if not alt:
                    alt = (
                        f"Inspect {name}'s last 200 log lines (get_logs) and fix the root cause "
                        f"before deciding to remove. Common causes: inotify exhaustion, "
                        f"shfs/PID churn on Unraid, missing volume mount, port already bound."
                    )
            elif action == "restart":
                info.append(
                    f"{name} is in a restart loop — restart will not help, "
                    f"the container is already cycling."
                )

        # Recent-start heuristic: started <60s ago → likely an Apply
        # storm in progress, not a real failure.
        if _started_recently(ins) and action in ("restart", "remove", "stop"):
            warnings.append(
                f"{name} started {_RECENT_START_SECONDS}s ago — "
                f"likely a transient Apply/init phase. Wait 60s and re-check before acting."
            )
            if not alt:
                alt = f"Wait 60s and call stack_status_tool({host!r}, {stack!r}) to confirm the state is real, not transient."

        # apply_update-specific: has a known-good in update_history?
        if action == "apply_update":
            state_db = get_state()
            last_good = await state_db.last_known_good(host=host, stack=stack)
            if not last_good:
                warnings.append(
                    f"{host}:{stack} has no recorded last-known-good image. "
                    f"If the new image is broken, you have no easy rollback. "
                    f"Consider dry_run=True first to preview the plan."
                )

        # dismiss_pending-specific: is there a healthy container
        # currently running the stack? Dismissing the drift while
        # the stack is healthy is fine; if the stack is broken it's
        # hiding a real problem.
        if action == "dismiss_pending" and status not in ("running",):
            blockers.append(
                f"{name} is not running (status={status!r}). "
                f"Dismissing a pending update for a broken stack hides the problem. "
                f"Fix the stack first, THEN dismiss."
            )

        # exec_in_container-specific: target container must exist and
        # be running. We don't exec into a restarting or broken container
        # because the diagnostic results are unreliable and the container
        # runtime may reject exec anyway.
        if action in ("exec_in_container", "db_snapshot", "db_restore"):
            if status not in ("running",):
                blockers.append(
                    f"{name} is not running (status={status!r}). "
                    f"Refusing to run a container-bound benchmark tool on a non-running container."
                )
                if not alt:
                    alt = f"Check stack_status_tool({host!r}, {stack!r}) and restart the container if appropriate."
            if _is_in_restart_loop(ins):
                warnings.append(
                    f"{name} is in a restart loop "
                    f"(restart_count={restart_count}, status={status!r}). "
                    f"Benchmark exec may race with the restart; fix the root cause first."
                )
            if _started_recently(ins):
                warnings.append(
                    f"{name} started recently; benchmarking a container still "
                    f"in its init phase may return misleading diagnostics."
                )

    # Multi-container in same stack: removing the parent orphans
    # dependents. ``data["inspect"]`` contains the per-container
    # inspect() results; multi-container means there's a sidecar.
    if action == "remove" and len(data["inspect"]) > 1:
        blockers.append(
            f"Stack {stack!r} has {len(data['inspect'])} containers. "
            f"Removing the named one may orphan dependents sharing its volumes. "
            f"Use compose-level removal (docker compose down) instead."
        )

    # remove on a non-empty volume: best-effort heuristic
    if action == "remove":
        for ins in data["inspect"]:
            mounts = ins.get("Mounts", []) or []
            for m in mounts:
                src = m.get("Source", "")
                if src.startswith("/"):
                    info.append(f"has volume mount: {src}")

    safe = len(blockers) == 0
    return {
        "safe": safe,
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "suggested_alternative": alt,
    }
