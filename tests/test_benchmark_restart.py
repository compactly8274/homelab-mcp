
"""Tests for benchmark_restart_tool (Phase 5)."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_mcp.tools.benchmark_restart import benchmark_restart_tool


@pytest.fixture
def mock_host():
    h = MagicMock()
    h.name = "truenas"
    h.list_containers = AsyncMock(return_value=[
        {"NAME": "prowlarr", "STATE": "running", "PROJECT": "", "STATUS": "Up 2 hours"},
    ])
    h.container_action = AsyncMock(return_value=MagicMock(exit_code=0, stdout="", stderr="", duration_ms=123))
    h.container_metrics = AsyncMock(return_value={
        "host": "truenas",
        "containers": {"prowlarr": {"cpu_percent": 1.2}},
    })
    return h


@pytest.mark.asyncio
async def test_restart_success_with_probe_url(mock_host):
    with patch("homelab_mcp.tools.benchmark_restart.get_host", return_value=mock_host), \
         patch("homelab_mcp.tools.benchmark_restart.container_action_tool", new=AsyncMock(return_value={
             "action": "applied", "host": "truenas", "target": "prowlarr", "kind": "container",
             "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 123,
             "preflight": {"safe": True},
         })), \
         patch("homelab_mcp.tools.benchmark_restart.container_metrics_tool", new=AsyncMock(return_value={
             "ok": True, "metrics": {"host": "truenas", "containers": {"prowlarr": {"cpu_percent": 1.2}}},
         })), \
         patch("homelab_mcp.tools.benchmark_restart.http_probe_tool", new=AsyncMock(return_value={
             "ok": True, "http_code": 200,
         })):
        result = await benchmark_restart_tool(
            host="truenas",
            target="prowlarr",
            probe_url="http://prowlarr:9696",
            max_wait_after_restart=5.0,
        )
    assert result["ok"] is True
    assert result["host"] == "truenas"
    assert result["target"] == "prowlarr"
    assert result["settle_ms"] is not None
    assert result["downtime_ms"] is not None
    assert result["error"] is None
    assert result["pre_metrics"] is not None
    assert result["post_metrics"] is not None


@pytest.mark.asyncio
async def test_restart_success_without_probe_url(mock_host):
    with patch("homelab_mcp.tools.benchmark_restart.get_host", return_value=mock_host), \
         patch("homelab_mcp.tools.benchmark_restart.container_action_tool", new=AsyncMock(return_value={
             "action": "applied", "host": "truenas", "target": "prowlarr", "kind": "container",
             "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 123,
             "preflight": {"safe": True},
         })), \
         patch("homelab_mcp.tools.benchmark_restart.container_metrics_tool", new=AsyncMock(return_value={
             "ok": True, "metrics": {"host": "truenas", "containers": {"prowlarr": {"cpu_percent": 1.2}}},
         })):
        result = await benchmark_restart_tool(
            host="truenas",
            target="prowlarr",
            max_wait_after_restart=5.0,
        )
    assert result["ok"] is True
    assert result["healthy_at"] is not None
    assert result["error"] is None


@pytest.mark.asyncio
async def test_restart_blocked_by_preflight(mock_host):
    with patch("homelab_mcp.tools.benchmark_restart.get_host", return_value=mock_host), \
         patch("homelab_mcp.tools.benchmark_restart.container_action_tool", new=AsyncMock(return_value={
             "action": "blocked",
             "host": "truenas",
             "target": "prowlarr",
             "requested_action": "restart",
             "preflight": {"safe": False, "blockers": ["in restart loop"]},
             "message": "preflight refused",
         })), \
         patch("homelab_mcp.tools.benchmark_restart.container_metrics_tool", new=AsyncMock(return_value={
             "ok": True, "metrics": {"host": "truenas", "containers": {}},
         })):
        result = await benchmark_restart_tool(
            host="truenas",
            target="prowlarr",
            max_wait_after_restart=2.0,
        )
    assert result["ok"] is False
    assert result["restart"]["action"] == "blocked"
    assert result["error"] == "preflight refused"
    assert result["healthy_at"] is None


@pytest.mark.asyncio
async def test_restart_unknown_target():
    h = MagicMock()
    h.name = "truenas"
    h.list_containers = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.benchmark_restart.get_host", return_value=h):
        result = await benchmark_restart_tool(host="truenas", target="missing")
    assert result["ok"] is False
    assert "neither a container nor a stack" in result["error"]


@pytest.mark.asyncio
async def test_restart_unknown_host():
    with patch("homelab_mcp.tools.benchmark_restart.get_host", side_effect=KeyError("nope")):
        result = await benchmark_restart_tool(host="badhost", target="prowlarr")
    assert result["ok"] is False
    assert "unknown host" in result["error"]


@pytest.mark.asyncio
async def test_restart_unhealthy_probe_timeout(mock_host):
    with patch("homelab_mcp.tools.benchmark_restart.get_host", return_value=mock_host), \
         patch("homelab_mcp.tools.benchmark_restart.container_action_tool", new=AsyncMock(return_value={
             "action": "applied", "host": "truenas", "target": "prowlarr", "kind": "container",
             "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 123,
             "preflight": {"safe": True},
         })), \
         patch("homelab_mcp.tools.benchmark_restart.container_metrics_tool", new=AsyncMock(return_value={
             "ok": True, "metrics": {"host": "truenas", "containers": {"prowlarr": {"cpu_percent": 1.2}}},
         })), \
         patch("homelab_mcp.tools.benchmark_restart.http_probe_tool", new=AsyncMock(return_value={
             "ok": False, "http_code": None, "error": "timeout",
         })), \
         patch("homelab_mcp.tools.benchmark_restart.time.sleep", return_value=None):
        result = await benchmark_restart_tool(
            host="truenas",
            target="prowlarr",
            probe_url="http://prowlarr:9696",
            max_wait_after_restart=0.5,
            probe_interval=0.1,
        )
    assert result["ok"] is False
    assert result["healthy_at"] is None
    assert "did not become healthy" in result["error"]
