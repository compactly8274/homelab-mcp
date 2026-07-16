"""Symptom-driven recipe search tool.

Aimed at "I have symptom X, what's the most likely fix?" workflows.
Wraps :func:`homelab_mcp.tools.memory.memory_search` with a fixed
``namespace="notes"`` scope and a re-ranking that puts importance 4+
first (because those are the hard-won gotchas from past debugging).

The expectation is that the LLM first calls ``recipe_search_tool``
with a phrase like ``restart loop`` or ``inotify`` or
``qBittorrent removeFromClient`` and gets back 3-5 ranked matches,
then ``memory_recall`` on the top key for the full recipe.
"""
from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.server import mcp
from homelab_mcp.tools.memory import _get_memory  # type: ignore[attr-defined]

log = logging.getLogger(__name__)


@mcp.tool()
async def recipe_search_tool(
    query: str,
    limit: int = 5,
    min_importance: int = 0,
) -> list[dict[str, Any]]:
    """Search stored notes (the "facts/notes/prefs" memory) for a fix recipe.

    Unlike ``memory_search`` (which searches all 3 namespaces), this tool
    searches only ``notes`` and re-ranks so that higher-importance
    matches come first. Useful for: "container in restart loop", "qBittorrent
    won't import", "Lidarr downloads stuck", "out of disk" — the kind of
    phrases that match a single stored note that we don't want to fish
    out of 21 entries by hand.

    Args:
        query: Plain-English symptom or keyword. FTS5 syntax, so multi-word
            queries are ANDed by default.
        limit: Max results to return (default 5, max 50).
        min_importance: Filter to entries with importance >= this value.
            Default 0 (no filter). Set to 3 or 4 to surface only the
            high-stakes gotchas.

    Returns:
        List of {key, content, importance, tags, use_count}, ranked
        first by importance DESC, then by use_count DESC, then by
        FTS5 relevance.
    """
    mem = _get_memory()
    # Use memory_search under the hood (BM25 + FTS5 ranking).
    raw = await mem.search(query=query, namespace="notes", limit=max(limit * 3, 15))
    # Re-rank: importance DESC, then use_count DESC
    if min_importance:
        raw = [r for r in raw if r.importance >= min_importance]
    raw.sort(key=lambda r: (r.importance, r.use_count), reverse=True)
    return [
        {
            "key": r.key,
            "importance": r.importance,
            "use_count": r.use_count,
            "tags": r.tags,
            "content": r.content,
        }
        for r in raw[:limit]
    ]


@mcp.tool()
async def recipe_for_host_tool(host: str, limit: int = 5) -> list[dict[str, Any]]:
    """Shortcut: return all stored recipes mentioning a given host.

    Searches notes whose content or tags mention the host alias
    (e.g. "unraid", "truenas", "qnap", "pangolin", "keycloak",
    "apple"). Returns the full text of each so the LLM can apply
    the recipe immediately, without a second ``memory_recall`` call.

    Args:
        host: Host or service name to search for (e.g. "unraid",
            "truenas", "lidarr", "plex", "keycloak").
        limit: Max results (default 5, max 20).
    """
    mem = _get_memory()
    out: list[dict[str, Any]] = []
    # List all notes (only 13-20 of them in practice), filter by host
    rows = await mem.list_recent(namespace="notes", limit=100)
    for n in rows:
        if host.lower() in n.key.lower() or host.lower() in n.content.lower():
            out.append(
                {
                    "key": n.key,
                    "importance": n.importance,
                    "tags": n.tags,
                    "content": n.content,
                }
            )
    out.sort(key=lambda r: r["importance"], reverse=True)
    return out[:limit]
