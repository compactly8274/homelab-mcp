"""Image-drift visibility tools (no apply, no rollback)."""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import mcp
from homelab_mcp.state import State
from homelab_mcp.tools._state import get_state

log = logging.getLogger(__name__)


@mcp.tool()
async def trigger_scan_tool(host: str | None = None) -> list[dict[str, Any]]:
    """Run a drift scan on the given host (or all hosts) right now.

    Returns a list of containers where the remote image digest differs
    from the local one.
    """
    from homelab_mcp.updater.scanner import scan_host
    state: State = get_state()
    from homelab_mcp.server import _host_clients
    targets = {host: _host_clients[host]} if host else dict(_host_clients)
    out: list[dict[str, Any]] = []
    for name, h in targets.items():
        try:
            rows = await scan_host(h, state)
            out.extend(rows)
        except Exception as e:
            log.exception("scan %s failed: %s", name, e)
    return out


@mcp.tool()
async def list_pending_updates_tool(host: str | None = None) -> list[dict[str, Any]]:
    """Image-drift rows from the visibility cron (or on-demand scans)."""
    state: State = get_state()
    return await state.list_pending_updates(host=host)


@mcp.tool()
async def pending_update_dismiss_tool(
    host: str, stack: str, latest_digest: str
) -> dict[str, Any]:
    """Dismiss a pending update row (e.g. after deciding not to apply)."""
    state: State = get_state()
    deleted = await state.mark_update_seen(host, stack, latest_digest)
    return {"deleted": deleted, "host": host, "stack": stack, "latest_digest": latest_digest}
