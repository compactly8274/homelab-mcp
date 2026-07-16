"""apply_all_pending_tool: bulk-apply every pending update on a host.

This is the cron-equivalent, but callable on demand via MCP. Useful
when you want to apply everything in one shot (e.g. after a long
drift period, or as a manual weekly update) without waiting for the
6h cron tick.

Returns one entry per (host, stack) with the same shape as
``apply_update_tool``: action, verdict, apply_result, etc. The
overall call does NOT abort on a single failure — each row is
isolated so a problem applying one stack doesn't block the rest.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_state, mcp
from homelab_mcp.tools.apply_update import apply_update_tool

log = logging.getLogger(__name__)


@mcp.tool()
async def apply_all_pending_tool(
    host: str,
    force: bool = False,
    max_rows: int = 50,
) -> dict[str, Any]:
    """Apply every pending update for a host, one stack at a time.

    Parameters
    ----------
    host : str
        The host alias to process. Must be in HOMELAB_MCP_HOSTS.
    force : bool, default False
        If True, the per-row apply_update call uses force=True,
        which overrides safe-only to apply BREAKING updates. The
        healthcheck + rollback still run.
    max_rows : int, default 50
        Safety cap on how many rows to process in a single call.
        Prevents runaway applies if something has gone wrong and
        there are 1000+ pending rows. The 6h cron will pick up
        the rest on its next tick.

    Returns
    -------
    dict
        Result with:
        - host: the host that was processed
        - processed: count of rows attempted
        - applied: count of action == "applied"
        - notified_breaking: count of action == "notified_breaking"
        - notified_caution: count of action == "notified_caution"
        - failed: count of action == "failed"
        - results: list of per-row results (each has the full
          shape of apply_update_tool's return value)
    """
    state = get_state()
    pending = await state.list_pending_updates(host=host)
    if not pending:
        return {
            "host": host,
            "processed": 0,
            "applied": 0,
            "notified_breaking": 0,
            "notified_caution": 0,
            "failed": 0,
            "results": [],
            "message": f"no pending updates for {host}",
        }
    rows = pending[:max_rows]
    results: list[dict[str, Any]] = []
    counts = {"applied": 0, "notified_breaking": 0, "notified_caution": 0,
              "failed": 0, "no_pending_update": 0, "other": 0}
    for row in rows:
        stack = row.get("stack", "")
        if not stack:
            continue
        try:
            # We re-use apply_update_tool for each row. Note: this
            # calls list_pending_updates again internally; that's
            # a small redundant read but keeps the code path
            # uniform with single-row applies.
            r = await apply_update_tool(host=host, stack=stack, force=force)
        except Exception as e:
            log.exception("apply_all_pending: row %s/%s raised: %s", host, stack, e)
            r = {"action": "failed", "host": host, "stack": stack, "error": str(e)}
        results.append(r)
        action = r.get("action", "other")
        if action in counts:
            counts[action] += 1
        else:
            counts["other"] += 1
    return {
        "host": host,
        "processed": len(results),
        "applied": counts["applied"],
        "notified_breaking": counts["notified_breaking"],
        "notified_caution": counts["notified_caution"],
        "failed": counts["failed"],
        "results": results,
    }
