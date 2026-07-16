"""Tests for the dashboard + recipe-search tools (v0.5.0).

Covers:
- health_dashboard_tool: aggregation across hosts, graceful per-host
  failure, summary roll-up, top-problems heuristic.
- recipe_search_tool: importance-first re-ranking, min_importance filter.
- recipe_for_host_tool: keyword match on key + content, importance sort.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# --- health_dashboard_tool --------------------------------------------------


def _fake_host(
    name: str,
    *,
    containers: list[dict[str, Any]] | None = None,
    stacks: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    fail: Exception | None = None,
) -> Any:
    h = MagicMock()
    h.name = name
    if fail is not None:
        h.list_containers = AsyncMock(side_effect=fail)
        h.list_stacks = AsyncMock(side_effect=fail)
        h.events = AsyncMock(side_effect=fail)
    else:
        h.list_containers = AsyncMock(return_value=containers or [])
        h.list_stacks = AsyncMock(return_value=stacks or [])
        h.events = AsyncMock(return_value=events or [])
    return h


async def test_dashboard_empty_hosts() -> None:
    """No hosts configured → empty summary, no crash."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    server._host_clients = {}
    result = await health_dashboard_tool()
    assert result["summary"]["total_hosts"] == 0
    assert result["summary"]["total_containers"] == 0
    assert result["top_problems"] == []
    assert result["hosts"] == []


async def test_dashboard_aggregates_across_hosts() -> None:
    """Two healthy hosts → counts are summed correctly."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    h1 = _fake_host(
        "unraid",
        containers=[
            {"NAME": "plex", "STATE": "running", "ID": "1"},
            {"NAME": "old", "STATE": "exited (0) 3 days ago", "ID": "2"},
            {"NAME": "sick", "STATE": "running (unhealthy)", "ID": "3"},
        ],
        stacks=[{"name": "plex"}, {"name": "old"}, {"name": "sick"}],
        events=[],
    )
    h2 = _fake_host(
        "truenas",
        containers=[{"NAME": "lidarr", "STATE": "running", "ID": "4"}],
        stacks=[{"name": "lidarr"}],
        events=[],
    )
    server._host_clients = {"unraid": h1, "truenas": h2}
    result = await health_dashboard_tool()
    assert result["summary"]["total_hosts"] == 2
    assert result["summary"]["reachable"] == 2
    assert result["summary"]["total_containers"] == 4
    assert result["summary"]["running"] == 2  # plex + lidarr
    assert result["summary"]["stopped"] == 1  # old
    assert result["summary"]["unhealthy"] == 1  # sick
    assert result["summary"]["total_stacks"] == 4


async def test_dashboard_marks_unreachable_host() -> None:
    """A host whose list_containers throws is marked unreachable, not crashed."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    h_good = _fake_host("unraid", containers=[{"NAME": "x", "STATE": "running"}])
    h_bad = _fake_host("truenas", fail=ConnectionError("ssh timed out"))
    server._host_clients = {"unraid": h_good, "truenas": h_bad}
    result = await health_dashboard_tool()
    assert result["summary"]["reachable"] == 1
    assert result["summary"]["unreachable"] == 1
    truenas = next(h for h in result["hosts"] if h["host"] == "truenas")
    assert truenas["reachable"] is False
    assert "ssh timed out" in truenas["error"]


async def test_dashboard_top_problems() -> None:
    """The top_problems list surfaces unhealthy + unreachable hosts first."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    h = _fake_host(
        "unraid",
        containers=[
            {"NAME": "a", "STATE": "running (unhealthy)"},
            {"NAME": "b", "STATE": "running (unhealthy)"},
        ],
        stacks=[],
        events=[],
    )
    h_bad = _fake_host("truenas", fail=OSError("no route to host"))
    server._host_clients = {"unraid": h, "truenas": h_bad}
    result = await health_dashboard_tool(top_problems=2)
    assert len(result["top_problems"]) == 2
    assert any("UNREACHABLE" in p and "truenas" in p for p in result["top_problems"])
    assert any("unhealthy" in p.lower() and "unraid" in p for p in result["top_problems"])


async def test_dashboard_only_host() -> None:
    """only_host=X restricts to a single host."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    h1 = _fake_host("unraid", containers=[{"NAME": "a", "STATE": "running"}])
    h2 = _fake_host("truenas", containers=[{"NAME": "b", "STATE": "running"}])
    server._host_clients = {"unraid": h1, "truenas": h2}
    result = await health_dashboard_tool(only_host="unraid")
    assert result["summary"]["total_hosts"] == 1
    assert result["hosts"][0]["host"] == "unraid"


async def test_dashboard_unknown_only_host() -> None:
    """only_host with an unknown name → error dict, not crash."""
    from homelab_mcp import server
    from homelab_mcp.tools.dashboard import health_dashboard_tool

    server._host_clients = {"unraid": _fake_host("unraid")}
    result = await health_dashboard_tool(only_host="nonexistent")
    assert "error" in result
    assert "unraid" in result["known_hosts"]


# --- recipe_search_tool + recipe_for_host_tool -----------------------------


class _FakeRow:
    def __init__(self, key: str, content: str, importance: int, use_count: int, tags: list[str] | None = None) -> None:
        self.key = key
        self.content = content
        self.importance = importance
        self.use_count = use_count
        self.tags = tags or []


async def test_recipe_search_ranks_by_importance() -> None:
    """Recipe search re-ranks: importance DESC beats FTS5 score."""
    from homelab_mcp.tools.recipes import recipe_search_tool

    # Two matches: lower BM25 score but higher importance should come first.
    low_imp = _FakeRow("low", "container restart loop common", importance=2, use_count=0)
    high_imp = _FakeRow("high", "container restart loop unraid specific", importance=5, use_count=0)
    fake_mem = MagicMock()
    fake_mem.search = AsyncMock(return_value=[low_imp, high_imp])  # FTS5 order
    with pytest.MonkeyPatch.context() as mp:
        # Patch the _get_memory() helper
        import homelab_mcp.tools.recipes as recipes_mod
        mp.setattr(recipes_mod, "_get_memory", lambda: fake_mem)
        out = await recipe_search_tool("restart loop")
    assert [r["key"] for r in out] == ["high", "low"]


async def test_recipe_search_min_importance_filter() -> None:
    """min_importance=4 drops the importance=2 entry."""
    from homelab_mcp.tools.recipes import recipe_search_tool

    rows = [
        _FakeRow("a", "x", importance=5, use_count=0),
        _FakeRow("b", "x", importance=3, use_count=0),
        _FakeRow("c", "x", importance=2, use_count=0),
    ]
    fake_mem = MagicMock()
    fake_mem.search = AsyncMock(return_value=rows)
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.recipes as recipes_mod
        mp.setattr(recipes_mod, "_get_memory", lambda: fake_mem)
        out = await recipe_search_tool("anything", min_importance=4)
    assert [r["key"] for r in out] == ["a"]


async def test_recipe_for_host_matches_in_key_and_content() -> None:
    """recipe_for_host_tool returns entries mentioning the host in either field."""
    from homelab_mcp.tools.recipes import recipe_for_host_tool

    rows = [
        _FakeRow("mem_07_unraid_104_shfs", "shfs PIDs change on every array cycle", 4, 0, ["unraid"]),
        _FakeRow("mem_99_other", "totally unrelated", 3, 0, []),
        _FakeRow("mem_11_51_tools", "tooling on unraid 104", 3, 0, []),
    ]
    fake_mem = MagicMock()
    fake_mem.list_recent = AsyncMock(return_value=rows)
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.recipes as recipes_mod
        mp.setattr(recipes_mod, "_get_memory", lambda: fake_mem)
        out = await recipe_for_host_tool("unraid")
    keys = {r["key"] for r in out}
    assert "mem_07_unraid_104_shfs" in keys
    assert "mem_11_51_tools" in keys
    assert "mem_99_other" not in keys


async def test_recipe_for_host_sorts_by_importance() -> None:
    """recipe_for_host_tool orders results importance DESC."""
    from homelab_mcp.tools.recipes import recipe_for_host_tool

    rows = [
        _FakeRow("lo", "unraid info", 2, 0),
        _FakeRow("hi", "unraid info", 5, 0),
    ]
    fake_mem = MagicMock()
    fake_mem.list_recent = AsyncMock(return_value=rows)
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.recipes as recipes_mod
        mp.setattr(recipes_mod, "_get_memory", lambda: fake_mem)
        out = await recipe_for_host_tool("unraid")
    assert [r["key"] for r in out] == ["hi", "lo"]
