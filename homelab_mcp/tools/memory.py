"""MCP tools for the long-term memory store.

Backed by :mod:`homelab_mcp.memory`. The 7 tools are:

- ``memory_store(ns, key, content, tags, importance, ttl_days)``
- ``memory_recall(key, namespace)``
- ``memory_search(query, namespace, limit)``
- ``memory_list(namespace, limit)``
- ``memory_recent(n)``
- ``memory_forget(key, namespace, soft)``
- ``memory_stats()``

DB path is configured via ``HOMELAB_MCP_MEMORY_PATH`` (default
``$HOMELAB_MCP_STATE_DIR/memory.db``). The DB is created on first call
if it doesn't exist; no separate init step required.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homelab_mcp.config import Settings
from homelab_mcp.memory import Memory, MemoryRow
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)

_DEFAULT_DB_NAME = "memory.db"
_memory: Memory | None = None
_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


def _db_path() -> Path:
    s = _get_settings()
    override = getattr(s, "memory_path", "")
    if override:
        return Path(override)
    return s.state_dir / _DEFAULT_DB_NAME


def _get_memory() -> Memory:
    global _memory
    if _memory is None:
        _memory = Memory(_db_path())
    return _memory


def _row_out(row: MemoryRow) -> dict[str, Any]:
    return row.to_dict()


# ----------------------------- write -------------------------------------


@mcp.tool()
async def memory_store(
    namespace: str,
    key: str,
    content: str,
    tags: list[str] | str | None = None,
    importance: int = 3,
    source: str = "agent",
    ttl_days: int | None = None,
) -> dict[str, Any]:
    """Store a memory for later recall (long-term note, fact, or preference).

    Use this instead of dumping everything into the system prompt. Retrieval
    is opt-in via ``memory_recall`` / ``memory_search``.

    Args:
        namespace: ``"notes"`` (free-form session notes), ``"prefs"``
            (user preferences), or ``"facts"`` (atomic facts about the
            homelab / external systems).
        key: short identifier. Must be unique within (namespace). No
            whitespace, max 200 chars. Re-storing the same key supersedes
            the old entry (preserved for audit).
        content: the actual text to remember.
        tags: optional list of tags (or comma-separated string) for
            searchability. Deduplicated, case-insensitive.
        importance: 1-5 (default 3). 5 = critical, must-remember; 1 = throwaway.
        source: who created it (``"agent"``, ``"user"``, ``"claude-code"``).
        ttl_days: optional. If set, the memory is hard-deleted after N days.

    Returns:
        dict with ``stored`` (True), ``id``, ``namespace``, ``key``,
        ``importance``, and ``superseded_id`` (the old row's id, if any).
        On error: ``{"error": "..."}``.
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        row = await mem.store(
            namespace=namespace,
            key=key,
            content=content,
            tags=tags,
            importance=importance,
            source=source,
            ttl_days=ttl_days,
        )
        out = _row_out(row)
        # Surface "we replaced something" so the LLM can decide whether
        # to mention it to the user
        out["stored"] = True
        return out
    except ValueError as e:
        return {"error": str(e), "stored": False, "namespace": namespace, "key": key}
    except Exception as e:
        log.exception("memory_store failed")
        return {"error": f"memory_store failed: {e}", "stored": False}


@mcp.tool()
async def memory_forget(
    key: str,
    namespace: str = "notes",
    soft: bool = True,
) -> dict[str, Any]:
    """Forget a memory by (namespace, key). Default is soft-delete.

    Soft-delete preserves the row with a ``deleted_at`` timestamp and a
    back-link to the row that replaced it (if any). Hard-delete removes
    it from the DB entirely. Soft-deleted rows are excluded from all
    search/recall/list operations.

    Args:
        key: the key of the memory to forget.
        namespace: which namespace to look in (default ``"notes"``).
        soft: ``True`` (default) for soft-delete, ``False`` for hard-delete.

    Returns:
        dict with ``deleted`` (bool), ``key``, ``namespace``, ``soft``,
        and either ``id`` (the deleted row) or ``error`` (not found).
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        return await mem.forget(key=key, namespace=namespace, soft=soft)
    except ValueError as e:
        return {"deleted": False, "error": str(e), "key": key, "namespace": namespace}
    except Exception as e:
        log.exception("memory_forget failed")
        return {"deleted": False, "error": f"memory_forget failed: {e}", "key": key}


# ----------------------------- read --------------------------------------


@mcp.tool()
async def memory_recall(key: str, namespace: str = "notes") -> dict[str, Any]:
    """Get a specific memory by (namespace, key).

    Increments use_count and updates last_used_at. Use this when you
    know the exact key. For "I don't know the key, just the topic",
    use ``memory_search`` instead.

    Args:
        key: the exact key of the memory to retrieve.
        namespace: which namespace to look in (default ``"notes"``).

    Returns:
        dict with the full memory row, or ``{"found": False, "key": key,
        "namespace": namespace}`` if no live memory exists.
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        row = await mem.recall(key=key, namespace=namespace)
        if row is None:
            return {"found": False, "key": key, "namespace": namespace}
        out = _row_out(row)
        out["found"] = True
        return out
    except ValueError as e:
        return {"found": False, "error": str(e), "key": key, "namespace": namespace}
    except Exception as e:
        log.exception("memory_recall failed")
        return {"found": False, "error": f"memory_recall failed: {e}", "key": key}


@mcp.tool()
async def memory_search(
    query: str,
    namespace: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Full-text search across memory content, keys, and tags.

    Use this when you don't know the exact key. The query is matched
    against FTS5-indexed content (with porter stemming) — no need for
    exact phrases. Returns live (non-deleted, non-expired) rows,
    BM25-ranked by FTS5 then by importance.

    Args:
        query: search terms. Whitespace-separated, FTS5 syntax is
            stripped (passes through as plain terms). E.g. "AppleTV wifi".
        namespace: optional. Filter to one of ``notes`` / ``prefs`` /
            ``facts``. Omit to search all.
        limit: max results to return (default 10, max 100).

    Returns:
        dict with ``query``, ``namespace`` (or ``None``), ``count``,
        and ``results`` (list of memory rows, ordered by relevance).
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        rows = await mem.search(query=query, namespace=namespace, limit=limit)
        return {
            "query": query,
            "namespace": namespace,
            "count": len(rows),
            "results": [_row_out(r) for r in rows],
        }
    except ValueError as e:
        return {"query": query, "namespace": namespace, "count": 0,
                "results": [], "error": str(e)}
    except Exception as e:
        log.exception("memory_search failed")
        return {"query": query, "namespace": namespace, "count": 0,
                "results": [], "error": f"memory_search failed: {e}"}


@mcp.tool()
async def memory_list(
    namespace: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent memories, ordered by last_used_at DESC.

    Use this for a "what do I know?" overview. For chronological "what
    did I just learn?", use ``memory_recent`` (same order, simpler name).

    Args:
        namespace: optional. Filter to one of ``notes`` / ``prefs`` /
            ``facts``. Omit to list all.
        limit: max results (default 50, max 500).

    Returns:
        dict with ``namespace``, ``count``, and ``results``.
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        rows = await mem.list_recent(namespace=namespace, limit=limit)
        return {
            "namespace": namespace,
            "count": len(rows),
            "results": [_row_out(r) for r in rows],
        }
    except ValueError as e:
        return {"namespace": namespace, "count": 0,
                "results": [], "error": str(e)}
    except Exception as e:
        log.exception("memory_list failed")
        return {"namespace": namespace, "count": 0,
                "results": [], "error": f"memory_list failed: {e}"}


@mcp.tool()
async def memory_recent(n: int = 20) -> dict[str, Any]:
    """Return the N most recently used memories (across all namespaces).

    Cheaper than ``memory_list`` for "what did I just learn?" lookups
    — no namespace filter, no large payload, same ordering.

    Args:
        n: how many to return (default 20, max 100).

    Returns:
        dict with ``count`` and ``results``.
    """
    try:
        n = max(1, min(100, int(n)))
        mem = _get_memory()
        await mem.init_db()
        rows = await mem.get_recent(n=n)
        return {"count": len(rows), "results": [_row_out(r) for r in rows]}
    except Exception as e:
        log.exception("memory_recent failed")
        return {"count": 0, "results": [], "error": f"memory_recent failed: {e}"}


@mcp.tool()
async def memory_stats() -> dict[str, Any]:
    """Aggregate stats: total live/deleted counts, per-namespace, top-10 used.

    Useful for "is the memory store getting full?" triage and for
    surfacing which memories the agent actually relies on.

    Returns:
        dict with ``total_live``, ``total_deleted``, ``by_namespace``
        ({notes: N, prefs: N, facts: N}), and ``top_used`` (list of
        {namespace, key, use_count, last_used_at, importance}).
    """
    try:
        mem = _get_memory()
        await mem.init_db()
        return await mem.stats()
    except Exception as e:
        log.exception("memory_stats failed")
        return {"error": f"memory_stats failed: {e}"}
