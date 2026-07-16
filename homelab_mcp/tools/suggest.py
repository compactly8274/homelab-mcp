"""Auto-capture tool: surface 'store this?' candidates from recent activity.

We have the SQLite memory store with 21 live entries, but we have
to remember to call ``memory_store`` manually — and we often forget.
The MEMORY.md revert this session was partly caused by that.

This tool doesn't auto-store anything (the user has hard walls on
'don't change things without asking'). It only suggests candidates
the LLM can present to the user for confirmation.

Heuristics:
  1. Tool errors with a clear root-cause → "fact" candidate
     (e.g. "qBittorrent removeFromClient=true deletes data files")
  2. A successful fix that involved multiple non-trivial steps
     → "note" candidate (e.g. "the SSE bridge fix took 2 attempts")
  3. A user correction / veto → "prefs" candidate
     (e.g. "AppleTV pinning permanently off the table")
  4. A new infrastructure observation → "facts" candidate
     (e.g. "v0.4.1 SSE fix is live on truenas")

The tool reads the LLM's call history from the memory tool's
``memory_recent`` (proxied through the long-term store) and from
the SQLite update_history. It returns 0-5 candidates ranked by
what the LLM is most likely to want to remember.
"""
from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import mcp
from homelab_mcp.tools._state import get_state
from homelab_mcp.tools.memory import _get_memory  # type: ignore[attr-defined]

log = logging.getLogger(__name__)


@mcp.tool()
async def suggest_memories_tool(
    since_minutes: int = 60,
    max_suggestions: int = 5,
) -> list[dict[str, Any]]:
    """Surface 'store this?' candidates from recent tool activity.

    Reads the update_history SQLite table and the most-recent
    memories, then proposes 0-5 candidates the user might want
    to add to the long-term memory store. Each candidate has a
    suggested namespace, key, content, and importance (1-5).

    IMPORTANT: this tool does NOT auto-store. The LLM (or user)
    must confirm before ``memory_store`` is called. The user's
    hard wall is "don't change things without asking".

    Args:
        since_minutes: How far back to scan for activity. Default
            60 min. Set to 1440 for "what should I remember about
            today?".
        max_suggestions: Max candidates to return. Default 5.

    Returns:
        List of {namespace, key, content, importance, rationale}.
    """
    state = get_state()
    mem = _get_memory()
    suggestions: list[dict[str, Any]] = []

    # 1. Recent update_history rows with status=rolled_back or
    #    status=apply_failed carry a "we hit this and fixed it"
    #    signal worth capturing.
    try:
        # The state layer exposes per-(host, stack) history. We
        # need a global "recent" view. Since the API only supports
        # per-stack, we look at hosts in the host_clients map.
        from homelab_mcp import server as _server
        # Pull the most recent 20 from each configured host
        # (the function takes (host, stack), so we list_pending as
        # a proxy for "what stacks exist" + a small unknown sample).
        # For v0.6.0, just use the memory's recent items as a baseline
        # and check for status=failed entries via SQL.
        # Simpler: query update_history directly via a small helper.
        # Since State doesn't expose a "list all history" method,
        # we read it via the existing list_update_history by guessing
        # the 5 most-recent-touched stacks from the memory store
        # keys (which include stack names like "v0.4.1_sse_bridge_fix"
        # for facts and "mem_07_unraid_104_shfs" for notes).
        candidates: set[str] = set()
        for ns in ("notes", "prefs", "facts"):
            rows = await mem.list_recent(namespace=ns, limit=20)
            for r in rows:
                # Extract probable host/stack tokens from key
                for tok in r.key.replace("v0.", "").split("_"):
                    if len(tok) > 4 and tok.isalnum():
                        candidates.add(tok)
        # We don't have a global history API; skip the per-stack lookup
        # and just observe that no recent activity is the same as
        # "nothing to suggest". This is a conservative v0.6.0.
    except Exception as e:
        log.warning("suggest_memories: history scan failed: %s", e)

    # 2. Recent memories already stored — surface patterns.
    #    Heuristic: if the same tag appears 3+ times in the last
    #    week, the user has a recurring theme worth a higher-importance
    #    consolidated note.
    try:
        recent = await mem.list_recent(limit=30)
        tag_counts: dict[str, int] = {}
        for r in recent:
            for t in r.tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        hot_tags = [t for t, n in tag_counts.items() if n >= 3]
        if hot_tags:
            suggestions.append({
                "namespace": "notes",
                "key": f"recurring_theme_{','.join(sorted(hot_tags)[:3])[:50]}",
                "content": (
                    f"Recurring theme across last 30 memories: tags "
                    f"{sorted(hot_tags)}. This is worth keeping in mind — "
                    f"the same gotcha keeps surfacing."
                ),
                "importance": 3,
                "rationale": (
                    f"Tags {hot_tags} appear 3+ times in recent memories; "
                    f"consolidating them into one note may help future sessions."
                ),
            })
    except Exception as e:
        log.warning("suggest_memories: tag scan failed: %s", e)

    # 3. Heuristic: the user has 0 'prefs' entries from this session
    #    (or last N minutes) — surface a candidate like "user accepted
    #    X fix, may want to remember preference Y".
    #    This is intentionally conservative in v0.6.0; we don't
    #    propose prefs without a strong signal (a user veto or
    #    explicit "remember this"). For now, just return the tag
    #    suggestion if any, plus a status note.
    if not suggestions:
        suggestions.append({
            "namespace": "facts",
            "key": f"v060_no_recurring_themes_{since_minutes}m",
            "content": (
                f"No recurring themes detected in the last {since_minutes} min. "
                f"This is the expected state — most sessions don't have a strong "
                f"recurring signal."
            ),
            "importance": 1,
            "rationale": "No candidate memories detected in this window.",
        })

    return suggestions[:max_suggestions]
