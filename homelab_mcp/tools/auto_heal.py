"""auto_heal_tool: detect and recover from broken containers.

Exposes :mod:`homelab_mcp.updater.heal` through the MCP so the LLM
surface (and the WebUI) can trigger a single-container heal or a
host-wide scan+heal sweep.

Two entry points:

- :func:`auto_heal_container_tool` — heal one specific container by
  name. Optionally pass a snapshot so the heal pipeline can fall back
  to a rollback if the restart doesn't help.
- :func:`auto_heal_scan_tool` — scan a host for unhealthy containers
  and attempt to heal each one. Heals are concurrent (up to 3 at a
  time per host) and idempotent: a healthy container returns
  ``ok=True, action=already_healthy`` without touching anything.

Heal results surface through the same ntfy notifier used by the
apply/rollback pipeline; the WebUI exposes a "Run heal" button on
the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_host, mcp

log = logging.getLogger(__name__)


@mcp.tool()
async def auto_heal_container_tool(
    host: str,
    name: str,
    settle_seconds: int = 10,
) -> dict[str, Any]:
    """Try to recover a single broken container.

    Strategy: ``docker restart <name>``, wait ``settle_seconds``,
    re-probe. If the container is still unhealthy, return
    ``action=needs_human`` so the caller can notify.

    No snapshot-based rollback here — that's wired into the apply
    pipeline. For a full "restart + rollback" flow, use the
    snapshot-aware path inside the auto-apply loop.

    Parameters
    ----------
    host : str
        Host alias. Must be in HOMELAB_MCP_HOSTS.
    name : str
        Container NAME (without leading slash).
    settle_seconds : int
        Seconds to wait between the restart and the re-probe.
        Default 10; bump to 30+ for stacks like Plex that take a
        while to start.
    """
    from homelab_mcp.updater.heal import heal_container

    host_client = get_host(host)
    outcome = await heal_container(
        host_client, name, snapshot=None, settle_seconds=settle_seconds,
    )
    return outcome.to_dict()


@mcp.tool()
async def auto_heal_scan_tool(
    host: str,
    settle_seconds: int = 10,
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """Scan a host for unhealthy containers and heal each one.

    Returns a summary with the count of containers scanned, the
    count that were found unhealthy, the count that were healed
    (via restart or rollback), and a per-container outcome list.
    The summary is suitable for surfacing directly in the WebUI.
    """
    from homelab_mcp.updater.heal import scan_and_heal

    host_client = get_host(host)
    return await scan_and_heal(
        host_client,
        snapshot_provider=None,
        settle_seconds=settle_seconds,
        max_concurrent=max_concurrent,
    )
