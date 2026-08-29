"""Container metrics MCP tool.

Returns point-in-time CPU, memory, network, and block I/O metrics for
one or all containers on a host. Safe to call with ``require_approval=True``.
"""

from __future__ import annotations

from typing import Any

from homelab_mcp.server import get_host, mcp
from homelab_mcp.tools import preflight


@mcp.tool()
async def container_metrics_tool(
    host: str,
    container: str | None = None,
    *,
    sample_seconds: float = 1.0,
) -> dict[str, Any]:
    """Return CPU/memory/network/block-IO metrics for a container.

    If ``container`` is omitted, metrics for all running containers on the
    host are returned and the preflight gate is skipped, since there is no
    single container to validate against and this is a read-only,
    host-wide query. The underlying backend uses ``docker stats`` on both
    LocalDocker and RemoteSSH hosts.
    """
    preflight_result: dict[str, Any] | None = None
    if container:
        preflight_result = await preflight.preflight_check_tool(
            host=host, action="container_metrics", stack=container
        )
        if not preflight_result.get("safe", False):
            return {"ok": False, "preflight": preflight_result}

    h = get_host(host)
    result = await h.container_metrics(name=container, sample_seconds=sample_seconds)
    if "error" in result:
        return {"ok": False, "error": result["error"], "preflight": preflight_result}
    return {"ok": True, "metrics": result, "preflight": preflight_result}
