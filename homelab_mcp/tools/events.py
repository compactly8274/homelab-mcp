"""Recent events + container logs tools."""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_host, mcp

log = logging.getLogger(__name__)


@mcp.tool()
async def recent_events_tool(host: str, since_seconds: int = 300) -> list[dict[str, Any]]:
    """Return docker events on the given host from the last N seconds.

    Each event is a JSON dict (the same shape ``docker events --format
    {{json .}}`` produces). Defaults to 5 minutes.
    """
    h = get_host(host)
    try:
        return await h.events(since_seconds=since_seconds)
    except Exception as e:
        log.exception("recent_events failed for %s: %s", host, e)
        return []


@mcp.tool()
async def get_logs_tool(host: str, container: str, tail: int = 200) -> str:
    """Last N log lines of a container. ``tail`` is capped server-side."""
    h = get_host(host)
    # Cap to avoid sending megabytes through the JSON-RPC pipe
    capped = max(1, min(int(tail), 5000))
    try:
        return await h.container_logs(container, tail=capped)
    except Exception as e:
        return f"<error fetching logs: {type(e).__name__}: {e}>"
