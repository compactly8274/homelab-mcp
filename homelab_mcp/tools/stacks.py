"""Read-only stack and host introspection tools."""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_host, mcp
from homelab_mcp.state import State
from homelab_mcp.tools._state import get_state

log = logging.getLogger(__name__)


@mcp.tool()
async def list_stacks_tool(host: str | None = None) -> list[dict[str, Any]]:
    """List all stacks on the given host (or all configured hosts).

    A stack is a compose project (grouped by ``com.docker.compose.project``)
    or a single un-managed container. Each entry has ``name``, ``host``,
    ``managed_by``, and either ``services`` (compose) or ``image`` (single).
    """
    state: State = get_state()
    from homelab_mcp.server import _host_clients
    targets = {host: _host_clients[host]} if host else dict(_host_clients)
    out: list[dict[str, Any]] = []
    for name, h in targets.items():
        try:
            stacks = await h.list_stacks()
            out.extend(stacks)
        except Exception as e:
            log.exception("list_stacks failed for %s: %s", name, e)
    return out


@mcp.tool()
async def stack_status_tool(host: str, stack: str) -> dict[str, Any]:
    """Full state of one stack: containers, health, image digests."""
    h = get_host(host)
    containers = await h.list_containers(all=True)
    matching = [c for c in containers if c.get("NAME") == stack or c.get("PROJECT") == stack]
    if not matching:
        return {"host": host, "stack": stack, "found": False}
    state: State = get_state()
    out_rows: list[dict[str, Any]] = []
    for c in matching:
        try:
            info = await h.inspect_container(c["NAME"])
            digest = (info.get("RepoDigests") or [None])[0]
        except Exception:
            digest = None
        out_rows.append({**c, "image_digest": digest})
    return {"host": host, "stack": stack, "found": True, "containers": out_rows}
