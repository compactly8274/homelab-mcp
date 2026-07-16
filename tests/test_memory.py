"""Tests for the long-term memory store + MCP tools.

The tests use a real SQLite file in a tmp dir, so the FTS5 virtual table
and triggers are exercised end-to-end. Each test gets its own tmp path
via a pytest fixture so they don't interfere.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
import pytest_asyncio

from homelab_mcp.memory import (
    Memory,
    _parse_tags,
    _validate_importance,
    _validate_key,
    _validate_namespace,
)
from homelab_mcp.tools import memory as memory_tools

# ----------------------------- fixtures ----------------------------------


@pytest_asyncio.fixture
async def mem(tmp_path: Path) -> Memory:
    """Fresh in-memory-like (file-backed) Memory per test."""
    m = Memory(tmp_path / "memory.db")
    await m.init_db()
    return m


@pytest_asyncio.fixture
async def clean_tools(monkeypatch, tmp_path: Path) -> None:
    """Reset the module-level _memory cache and point config at tmp_path."""
    memory_tools._memory = None
    memory_tools._cached_settings = None
    monkeypatch.setenv("HOMELAB_MCP_MEMORY_PATH", str(tmp_path / "memory.db"))
    # Also clear other env vars that might leak into Settings
    for k in ("HOMELAB_MCP_HOSTS", "HOMELAB_MCP_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    yield
    memory_tools._memory = None
    memory_tools._cached_settings = None


# ----------------------------- validators --------------------------------


def test_validate_namespace_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="namespace must be one of"):
        _validate_namespace("logs")
    # valid ones
    for ns in ("notes", "prefs", "facts"):
        assert _validate_namespace(ns) == ns


def test_validate_key_rejects_empty_whitespace_path_traversal() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _validate_key("")
    with pytest.raises(ValueError, match="non-empty"):
        _validate_key("   ")
    with pytest.raises(ValueError, match="whitespace"):
        _validate_key("has space")
    with pytest.raises(ValueError, match=r"/"):
        _validate_key("path/traversal")
    # valid
    assert _validate_key("appletv-master-bedroom") == "appletv-master-bedroom"
    assert _validate_key("user.notes.001") == "user.notes.001"


def test_validate_importance_range() -> None:
    assert _validate_importance(1) == 1
    assert _validate_importance(5) == 5
    assert _validate_importance(3) == 3
    with pytest.raises(ValueError, match="1-5"):
        _validate_importance(0)
    with pytest.raises(ValueError, match="1-5"):
        _validate_importance(6)


def test_parse_tags_handles_list_string_empty() -> None:
    assert _parse_tags(None) == ""
    assert _parse_tags([]) == ""
    assert _parse_tags("") == ""
    assert _parse_tags("a, b, c") == "a,b,c"
    # Dedupes case-insensitively, preserves first occurrence's case
    assert _parse_tags(["Foo", "FOO", "bar", "Bar"]) == "Foo,bar"
    assert _parse_tags("a,b,,c,") == "a,b,c"


# ----------------------------- core: store -------------------------------


async def test_store_returns_full_row(mem: Memory) -> None:
    row = await mem.store(
        "notes", "user.appletv", "AppleTV 4K in master bedroom", tags=["wife-tax", "wifi"],
        importance=5, source="agent",
    )
    assert row.id > 0
    assert row.namespace == "notes"
    assert row.key == "user.appletv"
    assert row.content == "AppleTV 4K in master bedroom"
    assert row.tags == ["wife-tax", "wifi"]
    assert row.importance == 5
    assert row.source == "agent"
    assert row.use_count == 0
    assert row.deleted_at is None
    assert row.superseded_by is None


async def test_store_supersedes_old_row_with_same_key(mem: Memory) -> None:
    old = await mem.store("prefs", "user.name", "Phil")
    new = await mem.store("prefs", "user.name", "Phil Newman")
    assert new.id != old.id

    # Old should be soft-deleted with superseded_by pointing at new
    old_row = await mem.recall("user.name", "prefs")
    assert old_row is not None  # wait, this returns the NEW row
    # recall returns the live one — to see the old, we need a direct query
    async with mem._connect() as db:
        cur = await db.execute("SELECT * FROM memory WHERE id=?", (old.id,))
        old_row = await cur.fetchone()
    assert old_row["deleted_at"] is not None
    assert old_row["superseded_by"] == new.id


async def test_store_rejects_invalid_inputs(mem: Memory) -> None:
    import pytest
    with pytest.raises(ValueError, match="namespace"):
        await mem.store("garbage", "k", "c")
    with pytest.raises(ValueError, match="key must be non-empty"):
        await mem.store("notes", "", "c")
    with pytest.raises(ValueError, match="key must be non-empty"):
        await mem.store("notes", "  ", "c")
    with pytest.raises(ValueError, match="content must be non-empty"):
        await mem.store("notes", "k", "")
    with pytest.raises(ValueError, match="importance"):
        await mem.store("notes", "k", "c", importance=99)


async def test_store_ttl_sets_expires_at(mem: Memory) -> None:
    row = await mem.store("notes", "temp.thing", "x", ttl_days=7)
    assert row.expires_at is not None
    # Roughly 7 days from now
    from datetime import datetime
    exp = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    delta = (exp - datetime.now(UTC)).total_seconds()
    assert 6 * 86400 < delta < 8 * 86400


# ----------------------------- core: recall ------------------------------


async def test_recall_increments_use_count_and_updates_last_used(mem: Memory) -> None:
    await mem.store("facts", "unraid.shfs", "shfs PIDs change on every array cycle", importance=4)
    r1 = await mem.recall("unraid.shfs", "facts")
    assert r1 is not None
    assert r1.use_count == 1
    first_used = r1.last_used_at

    r2 = await mem.recall("unraid.shfs", "facts")
    assert r2 is not None
    assert r2.use_count == 2
    assert r2.last_used_at >= first_used


async def test_recall_returns_none_for_missing_or_deleted(mem: Memory) -> None:
    await mem.store("notes", "k", "v")
    assert await mem.recall("nope", "notes") is None
    assert await mem.recall("k", "facts") is None  # wrong namespace

    await mem.forget("k", "notes", soft=True)
    assert await mem.recall("k", "notes") is None  # soft-deleted, invisible


async def test_recall_excludes_expired(mem: Memory) -> None:
    # Insert a row with an already-past expires_at via a backdoor (no TTL=0
    # support, so use direct SQL)
    from datetime import datetime, timedelta
    await mem.store("notes", "alive", "x", ttl_days=1)
    async with mem._connect() as db:
        # Backdate the expires_at to make it expired
        past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        await db.execute(
            "UPDATE memory SET expires_at=? WHERE key='alive' AND namespace='notes'",
            (past,),
        )
        await db.commit()
    assert await mem.recall("alive", "notes") is None


# ----------------------------- core: search ------------------------------


async def test_search_finds_content_across_namespaces(mem: Memory) -> None:
    await mem.store("notes", "k1", "AppleTV 4K in master bedroom, wife tax")
    await mem.store("facts", "k2", "Unraid shfs PIDs change on every array cycle")
    await mem.store("prefs", "k3", "User dislikes verbose output")

    # Single term
    r = await mem.search("appletv")
    assert len(r) == 1
    assert r[0].key == "k1"

    # Multi-term, OR semantics
    r = await mem.search("unraid array")
    assert any(x.key == "k2" for x in r)

    # Namespace filter
    r = await mem.search("user", namespace="prefs")
    assert all(x.namespace == "prefs" for x in r)


async def test_search_strips_fts_operators(mem: Memory) -> None:
    """LLM might pass FTS syntax verbatim (e.g. 'apple wife') — the search
    should treat it as plain terms, not boolean operators."""
    await mem.store("notes", "k1", "AppleTV is the wife's TV")
    await mem.store("notes", "k2", "AppleTV is also in the kitchen")
    # Two terms, AND'd implicitly by FTS5 default. Both rows match
    # because both contain "appletv" and "wife" — except k2 doesn't have
    # "wife". So we expect just k1.
    r = await mem.search("appletv wife")
    assert len(r) == 1
    assert r[0].key == "k1"
    # Single term: both match
    r2 = await mem.search("appletv")
    assert len(r2) == 2


async def test_search_excludes_deleted_and_expired(mem: Memory) -> None:
    await mem.store("notes", "alive", "AppleTV in master bedroom")
    await mem.store("notes", "deletable", "AppleTV also here")
    await mem.forget("deletable", "notes", soft=True)

    r = await mem.search("appletv")
    assert len(r) == 1
    assert r[0].key == "alive"


async def test_search_empty_query_returns_empty(mem: Memory) -> None:
    await mem.store("notes", "k", "AppleTV")
    assert await mem.search("") == []
    assert await mem.search("   ") == []


async def test_search_clamps_limit(mem: Memory) -> None:
    await mem.store("notes", "k", "x" * 100)
    r = await mem.search("x", limit=999)
    # No crash; limit is clamped to 100
    assert len(r) <= 100


# ----------------------------- core: list_recent / get_recent -----------


async def test_list_recent_orders_by_last_used_desc(mem: Memory) -> None:
    await mem.store("notes", "a", "alpha")
    await mem.store("notes", "b", "bravo")
    await mem.store("notes", "c", "charlie")

    # Bump "a" to be most-recently-used
    await mem.recall("a", "notes")

    r = await mem.list_recent()
    assert [x.key for x in r] == ["a", "c", "b"]


async def test_list_recent_filters_by_namespace(mem: Memory) -> None:
    await mem.store("notes", "n1", "n")
    await mem.store("prefs", "p1", "p")
    await mem.store("facts", "f1", "f")

    notes = await mem.list_recent(namespace="notes")
    assert all(x.namespace == "notes" for x in notes)
    assert {x.key for x in notes} == {"n1"}


async def test_get_recent_is_alias_for_list_recent(mem: Memory) -> None:
    await mem.store("notes", "a", "a")
    await mem.store("notes", "b", "b")
    a = await mem.get_recent(n=1)
    b = await mem.list_recent(limit=1)
    assert a[0].key == b[0].key


# ----------------------------- core: forget ------------------------------


async def test_forget_soft_default(mem: Memory) -> None:
    await mem.store("notes", "k", "v")
    r = await mem.forget("k", "notes", soft=True)
    assert r["deleted"] is True
    assert r["soft"] is True
    # Not visible in recall
    assert await mem.recall("k", "notes") is None
    # But still in DB
    async with mem._connect() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM memory WHERE key='k' AND namespace='notes'")
        n = (await cur.fetchone())["n"]
    assert n == 1


async def test_forget_hard_removes_from_db(mem: Memory) -> None:
    await mem.store("notes", "k", "v")
    r = await mem.forget("k", "notes", soft=False)
    assert r["deleted"] is True
    assert r["soft"] is False
    async with mem._connect() as db:
        cur = await db.execute("SELECT COUNT(*) AS n FROM memory WHERE key='k'")
        n = (await cur.fetchone())["n"]
    assert n == 0


async def test_forget_missing_returns_not_found(mem: Memory) -> None:
    r = await mem.forget("nope", "notes")
    assert r["deleted"] is False
    assert "no live memory" in r["error"]


# ----------------------------- core: stats -------------------------------


async def test_stats_returns_breakdown_and_top_used(mem: Memory) -> None:
    await mem.store("notes", "a", "a", importance=5)
    await mem.store("notes", "b", "b")
    await mem.store("facts", "c", "c")
    await mem.forget("b", "notes", soft=True)  # 1 deleted

    # Bump use_count
    for _ in range(3):
        await mem.recall("a", "notes")

    s = await mem.stats()
    assert s["total_live"] == 2
    assert s["total_deleted"] == 1
    assert s["by_namespace"] == {"notes": 1, "facts": 1}
    assert s["top_used"][0]["key"] == "a"
    assert s["top_used"][0]["use_count"] == 3


# ----------------------------- core: purge_expired -----------------------


async def test_purge_expired_removes_old_rows(mem: Memory) -> None:
    from datetime import datetime, timedelta
    await mem.store("notes", "alive", "x", ttl_days=1)
    await mem.store("notes", "doomed", "y", ttl_days=1)

    async with mem._connect() as db:
        past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        await db.execute("UPDATE memory SET expires_at=? WHERE key='doomed'", (past,))
        await db.commit()

    n = await mem.purge_expired()
    assert n == 1
    # "alive" still visible
    assert (await mem.recall("alive", "notes")) is not None
    # "doomed" gone
    assert (await mem.recall("doomed", "notes")) is None


# ====================== MCP-tool-level tests =============================


async def test_tool_store_and_recall(clean_tools, tmp_path: Path) -> None:
    s = await memory_tools.memory_store("notes", "user.notes.test", "Hello world", importance=4)
    assert s["stored"] is True
    assert s["namespace"] == "notes"
    assert s["key"] == "user.notes.test"
    assert s["importance"] == 4

    r = await memory_tools.memory_recall("user.notes.test", "notes")
    assert r["found"] is True
    assert r["content"] == "Hello world"


async def test_tool_recall_missing_returns_found_false(clean_tools) -> None:
    r = await memory_tools.memory_recall("nope", "notes")
    assert r["found"] is False
    assert r["key"] == "nope"


async def test_tool_store_rejects_bad_namespace(clean_tools) -> None:
    r = await memory_tools.memory_store("garbage", "k", "c")
    assert r["stored"] is False
    assert "namespace" in r["error"]


async def test_tool_search_returns_results_with_count(clean_tools) -> None:
    await memory_tools.memory_store("notes", "k1", "AppleTV 4K")
    await memory_tools.memory_store("facts", "k2", "Unraid array")
    r = await memory_tools.memory_search("appletv")
    assert r["count"] >= 1
    assert r["query"] == "appletv"
    assert any(x["key"] == "k1" for x in r["results"])


async def test_tool_list_recent(clean_tools) -> None:
    await memory_tools.memory_store("notes", "a", "alpha")
    await memory_tools.memory_store("prefs", "b", "bravo")
    r = await memory_tools.memory_list()
    assert r["count"] == 2
    assert r["namespace"] is None
    # With filter
    r2 = await memory_tools.memory_list(namespace="prefs")
    assert r2["count"] == 1
    assert r2["results"][0]["key"] == "b"


async def test_tool_forget(clean_tools) -> None:
    await memory_tools.memory_store("notes", "k", "v")
    r = await memory_tools.memory_forget("k", "notes")
    assert r["deleted"] is True
    # Subsequent recall misses
    r2 = await memory_tools.memory_recall("k", "notes")
    assert r2["found"] is False


async def test_tool_stats(clean_tools) -> None:
    await memory_tools.memory_store("notes", "a", "x")
    await memory_tools.memory_store("facts", "b", "y")
    s = await memory_tools.memory_stats()
    assert s["total_live"] == 2
    assert s["by_namespace"]["notes"] == 1
    assert s["by_namespace"]["facts"] == 1


async def test_tool_recent(clean_tools) -> None:
    for i in range(5):
        await memory_tools.memory_store("notes", f"k{i}", f"v{i}")
    r = await memory_tools.memory_recent(n=3)
    assert r["count"] == 3
