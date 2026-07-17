"""container_action_tool: start/stop/restart containers and stacks.

Exposes the host-level container_action method through MCP so the
LLM surface can manipulate running containers. All actions go
through the preflight gate (same gate as apply_update_tool) so
the LLM cannot, e.g., stop a stack that is currently in restart
loop without first seeing a warning.

The motivating incident: 2026-07-16, 11-album MEAL. The qBittorrent
delete with files was triggered by an automated queue clear. A
preflight-gated container_action would have surfaced the
'destructive cascade' warning before pulling the trigger.

Usage:
    container_action_tool(host, target, action)
    - host:    the host alias
    - target:  a container NAME or a stack (compose project name).
               Container names take precedence.
    - action:  start | stop | restart | kill | pause | unpause

Gating:
    preflight_check_tool(host, target, action) runs first
    (when require_approval=True, the default). On any blocker
    the tool returns {action: "blocked", preflight: {...}}
    without touching the container. require_approval=False
    bypasses the gate (production-grade operations).
"""
from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_host, mcp

log = logging.getLogger(__name__)


_VALID_ACTIONS = ("start", "stop", "restart", "kill", "pause", "unpause")


@mcp.tool()
async def container_action_tool(
    host: str,
    target: str,
    action: str,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Start, stop, restart, kill, pause, or unpause a container or stack.

    See module docstring for the full design rationale and the
    preflight gate behavior.

    Parameters
    ----------
    host : str
        Host alias. Must be configured in HOMELAB_MCP_HOSTS.
    target : str
        Container NAME (exact, without the leading slash) OR a
        compose project name (= stack). Container names take
        precedence; if no container has that name, the target
        is treated as a stack and all containers in the stack
        are acted on.
    action : str
        One of: start, stop, restart, kill, pause, unpause.
    require_approval : bool, default True
        If True, the preflight gate runs first. On blockers
        (e.g. a non-existent target, a multi-container stack
        with a destructive action, an unknown host) the tool
        returns {action: "blocked", preflight: {...}} without
        touching anything. Re-call with require_approval=False
        to override.

    Returns
    -------
    dict
        - On success: {action: "applied", host, target, kind:
          "container" | "stack", container: NAME, exit_code,
          stdout, stderr, duration_ms}.
        - On preflight block: {action: "blocked", preflight:
          {...}}.
        - On unknown host: {action: "failed", error: ...}.
        - On unsupported action: {action: "failed", error: ...}.
    """
    # Unknown-host check first so the user gets a clear error.
    try:
        host_client = get_host(host)
    except KeyError as e:
        return {
            "action": "failed",
            "host": host,
            "target": target,
            "error": f"unknown host {host!r}: {e}",
        }

    if action not in _VALID_ACTIONS:
        return {
            "action": "failed",
            "host": host,
            "target": target,
            "error": (
                f"unsupported action {action!r}. "
                f"Must be one of: {', '.join(_VALID_ACTIONS)}"
            ),
        }

    # Pre-flight gate. dry_run-style: there is no dry_run here
    # because the action itself is the side effect, so the gate
    # IS the preview. Set require_approval=False to bypass.
    if require_approval:
        from homelab_mcp.tools.preflight import preflight_check_tool
        verdict = await preflight_check_tool(
            host=host, stack=target, action=action
        )
        if not verdict["safe"]:
            return {
                "action": "blocked",
                "host": host,
                "target": target,
                "requested_action": action,
                "preflight": verdict,
                "message": (
                    f"preflight refused: {len(verdict['blockers'])} blocker(s). "
                    f"Re-call with require_approval=False to override."
                ),
            }

    # Disambiguate target: is it a single container or a stack?
    # Container name match takes precedence (containers can have
    # the same name as their project in some setups).
    is_container = await _is_container(host_client, target)
    if is_container:
        result = await host_client.container_action(name=target, action=action)
        return {
            "action": "applied",
            "host": host,
            "target": target,
            "kind": "container",
            "container": target,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
        }

    # Treat as a stack: list the containers in the project and
    # act on each one. We use the per-container container_action
    # so the call is atomic per container.
    containers = await host_client.list_containers(all=True)
    members = [c for c in containers if c.get("PROJECT") == target]
    if not members:
        return {
            "action": "failed",
            "host": host,
            "target": target,
            "error": (
                f"{target!r} is neither a container name nor a stack on "
                f"host {host!r}. Use preflight_check_tool first to verify."
            ),
        }

    results: list[dict[str, Any]] = []
    for c in members:
        name = c.get("NAME", "")
        if not name:
            continue
        r = await host_client.container_action(name=name, action=action)
        results.append({
            "container": name,
            "exit_code": r.exit_code,
            "stderr": r.stderr,
            "duration_ms": r.duration_ms,
        })

    # Overall success = every member succeeded.
    all_ok = all(r["exit_code"] == 0 for r in results)
    return {
        "action": "applied" if all_ok else "failed",
        "host": host,
        "target": target,
        "kind": "stack",
        "members": results,
        "member_count": len(results),
    }


async def _is_container(host_client: Any, name: str) -> bool:
    """Return True if ``name`` is the name of a container on the host.

    We use list_containers (cheap) rather than inspect (one round
    trip per name) so the same call is reused for the disambiguate
    + the stack fan-out step.
    """
    cs = await host_client.list_containers(all=True)
    return any(c.get("NAME") == name for c in cs)
