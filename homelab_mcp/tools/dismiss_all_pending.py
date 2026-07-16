"""dismiss_all_pending_tool: bulk-mark a host's drift rows as seen.

Sometimes you want to clear the pending_updates table without
applying anything — e.g. you've decided to pin a stack at a
specific version, or you want the visibility cron to ignore a
non-actionable drift (like a multi-arch manifest difference the
registry treats as a new digest but the image is the same).

This is the bulk equivalent of ``pending_update_dismiss_tool``.
It deletes rows by (host, latest_digest), so it only removes
exact matches — passing no filter would mean we'd need to know
which digest to delete per row, which is a per-row decision.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import get_state, mcp

log = logging.getLogger(__name__)


@mcp.tool()
async def dismiss_all_pending_tool(
    host: str,
    stack: str | None = None,
) -> dict[str, Any]:
    """Dismiss (delete) all pending update rows for a host.

    Parameters
    ----------
    host : str
        The host alias. Must be in HOMELAB_MCP_HOSTS.
    stack : str, optional
        If given, only dismiss rows for this specific stack. If
        None, dismiss ALL pending rows for the host. The
        stack-scoped form is safer; the unscoped form is a "blanket
        ignore everything new" hammer.

    Returns
    -------
    dict with:
        - host, stack (or null)
        - dismissed: number of rows deleted
        - rows: list of {stack, latest_digest} that were removed
    """
    state = get_state()
    pending = await state.list_pending_updates(host=host)
    target = [r for r in pending if stack is None or r.get("stack") == stack]
    dismissed = 0
    rows: list[dict[str, str]] = []
    for r in target:
        try:
            n = await state.mark_update_seen(
                host=host,
                stack=r["stack"],
                latest_digest=r["latest_digest"],
            )
            if n:
                dismissed += n
                rows.append({"stack": r["stack"], "latest_digest": r["latest_digest"]})
        except Exception as e:
            log.warning("dismiss %s/%s failed: %s", host, r.get("stack"), e)
    return {
        "host": host,
        "stack": stack,
        "dismissed": dismissed,
        "rows": rows,
    }
