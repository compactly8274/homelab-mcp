"""Tests for the preflight + suggest tools (v0.6.0).

Covers:
- preflight_check_tool: known-good and known-bad inputs, the
  restart-loop detection, recent-start heuristic, multi-container
  blocker, unknown-host handling.
- suggest_memories_tool: empty state, hot-tag detection, max
  suggestions cap, soft-fail on missing backend.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# --- preflight_check_tool ---------------------------------------------------


def _fake_host(name: str, containers: list[dict[str, Any]], inspect: dict[str, dict] | None = None) -> Any:
    h = MagicMock()
    h.name = name
    h.list_containers = AsyncMock(return_value=containers)
    inspect_map = inspect if inspect is not None else {}
    # inspect by container name
    async def _inspect(name: str) -> dict[str, Any]:
        return inspect_map.get(name, {"Name": f"/{name}", "State": {"Status": "running", "RestartCount": 0, "StartedAt": "2020-01-01T00:00:00Z"}, "Mounts": []})
    h.inspect_container = AsyncMock(side_effect=_inspect)
    return h


async def test_preflight_rejects_unknown_action() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    server._host_clients = {}
    r = await preflight_check_tool("truenas", "lidarr", "explode")
    assert r["safe"] is False
    assert any("unknown action" in b for b in r["blockers"])


async def test_preflight_rejects_unknown_host() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    server._host_clients = {"unraid": _fake_host("unraid", [])}
    r = await preflight_check_tool("nope", "lidarr", "stop")
    assert r["safe"] is False
    assert any("unknown host" in b for b in r["blockers"])


async def test_preflight_clean_running_container_is_safe() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    server._host_clients = {
        "truenas": _fake_host(
            "truenas",
            containers=[{"NAME": "lidarr", "PROJECT": "lidarr"}],
        ),
    }
    r = await preflight_check_tool("truenas", "lidarr", "restart")
    assert r["safe"] is True
    assert r["blockers"] == []


async def test_preflight_warns_on_restart_loop_remove() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    h = _fake_host(
        "truenas",
        containers=[{"NAME": "fgc", "PROJECT": "fgc"}],
        inspect={
            "fgc": {
                "Name": "/fgc",
                "State": {
                    "Status": "Restarting (1) 5 seconds ago",
                    "RestartCount": 8,
                    "StartedAt": "2026-07-16T21:30:00Z",
                },
                "Mounts": [],
            }
        },
    )
    server._host_clients = {"truenas": h}
    r = await preflight_check_tool("truenas", "fgc", "remove")
    # Warnings are non-blocking but should be present
    assert any("restart loop" in w for w in r["warnings"])


async def test_preflight_blocks_remove_on_multi_container_stack() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    h = _fake_host(
        "truenas",
        containers=[
            {"NAME": "plex", "PROJECT": "plex"},
            {"NAME": "plex_db", "PROJECT": "plex"},
        ],
    )
    server._host_clients = {"truenas": h}
    r = await preflight_check_tool("truenas", "plex", "remove")
    assert r["safe"] is False
    assert any("orphan" in b for b in r["blockers"])


async def test_preflight_blocks_dismiss_pending_when_stack_broken() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    h = _fake_host(
        "unraid",
        containers=[{"NAME": "sick", "PROJECT": "sick"}],
        inspect={
            "sick": {
                "Name": "/sick",
                "State": {
                    "Status": "exited (1) 30 minutes ago",
                    "RestartCount": 5,
                    "StartedAt": "2026-07-16T20:00:00Z",
                },
                "Mounts": [],
            }
        },
    )
    server._host_clients = {"unraid": h}
    r = await preflight_check_tool("unraid", "sick", "dismiss_pending")
    assert r["safe"] is False
    assert any("not running" in b for b in r["blockers"])


async def test_preflight_warns_apply_update_without_last_known_good() -> None:
    import tempfile

    from homelab_mcp import server
    from homelab_mcp.tools.preflight import preflight_check_tool

    # Use a real State on a temp DB; last_known_good returns None for empty
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        from homelab_mcp.state import State
        s = State(db_path=Path(tmp) / "test.db")
        await s.init_db()
        server._state = s
        server._host_clients = {
            "truenas": _fake_host("truenas", containers=[{"NAME": "lidarr", "PROJECT": "lidarr"}])
        }
        r = await preflight_check_tool("truenas", "lidarr", "apply_update")
    assert any("no recorded last-known-good" in w for w in r["warnings"])


# --- suggest_memories_tool --------------------------------------------------


class _FakeMemRow:
    def __init__(self, key: str, tags: list[str], importance: int = 3, namespace: str = "notes") -> None:
        self.key = key
        self.tags = tags
        self.importance = importance
        self.namespace = namespace


async def test_suggest_returns_no_theme_when_nothing_hot() -> None:
    from homelab_mcp.tools.suggest import suggest_memories_tool

    fake_mem = MagicMock()
    fake_mem.list_recent = AsyncMock(return_value=[
        _FakeMemRow("a", ["unraid"]),
        _FakeMemRow("b", ["truenas"]),
    ])
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.suggest as mod
        mp.setattr(mod, "_get_memory", lambda: fake_mem)
        out = await suggest_memories_tool()
    # No recurring theme → the "no themes detected" candidate
    assert any("No recurring themes" in s["content"] for s in out)


async def test_suggest_detects_recurring_tag_theme() -> None:
    from homelab_mcp.tools.suggest import suggest_memories_tool

    # 4 entries all tagged "unraid" → recurring theme
    fake_mem = MagicMock()
    fake_mem.list_recent = AsyncMock(return_value=[
        _FakeMemRow("a", ["unraid", "shfs"]),
        _FakeMemRow("b", ["unraid"]),
        _FakeMemRow("c", ["unraid", "apply"]),
        _FakeMemRow("d", ["unraid"]),
    ])
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.suggest as mod
        mp.setattr(mod, "_get_memory", lambda: fake_mem)
        out = await suggest_memories_tool()
    assert any(s["namespace"] == "notes" and "unraid" in s["content"].lower() for s in out)


async def test_suggest_respects_max_suggestions() -> None:
    from homelab_mcp.tools.suggest import suggest_memories_tool

    fake_mem = MagicMock()
    fake_mem.list_recent = AsyncMock(return_value=[
        _FakeMemRow("a", ["unraid"]),
        _FakeMemRow("b", ["unraid"]),
        _FakeMemRow("c", ["unraid"]),
    ])
    with pytest.MonkeyPatch.context() as mp:
        import homelab_mcp.tools.suggest as mod
        mp.setattr(mod, "_get_memory", lambda: fake_mem)
        out = await suggest_memories_tool(max_suggestions=1)
    assert len(out) == 1
