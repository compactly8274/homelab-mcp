"""get_update_history_tool: surface past update attempts.

Every apply (successful, failed, or rolled back) is recorded in the
``update_history`` table by the pipeline. This tool reads that
table and returns the rows. Useful for an AI agent that wants to
say "have I successfully updated nextcloud before?" or "did the
last roll-back actually restore the prior digest?".

This is purely a read on the existing state — no side effects, no
notifies, no applies.
"""

from __future__ import annotations

from typing import Any

from homelab_mcp.server import get_state, mcp


@mcp.tool()
async def get_update_history_tool(
    host: str,
    stack: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most-recent update_history rows for (host, stack).

    Parameters
    ----------
    host : str
        The host alias (must be in HOMELAB_MCP_HOSTS).
    stack : str
        The stack name (e.g. "nextcloud", "immich").
    limit : int, default 20
        Max rows to return. Capped at 200 to keep the response small.

    Returns
    -------
    list of dict, newest first. Each row has:
        - id
        - from_digest, to_digest
        - manifest_digest, config_digest (when recorded)
        - status: "applied", "apply_failed", "rolled_back", "failed"
        - started_at, finished_at (ISO strings)
        - rollback_to_digest (only when status == "rolled_back")
        - reason: free-text note (LLM summary, error, etc.)
    """
    state = get_state()
    capped_limit = max(1, min(int(limit), 200))
    return await state.list_update_history(host=host, stack=stack, limit=capped_limit)
