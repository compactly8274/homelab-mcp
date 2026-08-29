
"""Tests for benchmark_diff / baseline tools (Phase 6)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homelab_mcp.tools import benchmark_diff


@pytest.fixture
def tmp_benchmark_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HOMELAB_MCP_BENCHMARK_DIR", d)
        yield Path(d)


@pytest.mark.asyncio
async def test_baseline_saves_file(tmp_benchmark_dir):
    with patch("homelab_mcp.tools.benchmark_diff.container_metrics_tool", new=AsyncMock(return_value={
        "ok": True,
        "metrics": {"host": "truenas", "containers": {"prowlarr": {"cpu_percent": 2.5}}},
    })):
        result = await benchmark_diff.benchmark_baseline_tool(
            host="truenas", container="prowlarr", label="before",
        )
    assert result["ok"] is True
    assert result["path"].startswith(str(tmp_benchmark_dir))
    assert Path(result["path"]).is_file()
    data = json.loads(Path(result["path"]).read_text())
    assert data["host"] == "truenas"
    assert data["container"] == "prowlarr"
    assert data["label"] == "before"
    assert data["metrics"]["containers"]["prowlarr"]["cpu_percent"] == 2.5


@pytest.mark.asyncio
async def test_baseline_with_load(tmp_benchmark_dir):
    with patch("homelab_mcp.tools.benchmark_diff.container_metrics_tool", new=AsyncMock(return_value={
        "ok": True,
        "metrics": {"host": "truenas", "containers": {}},
    })), patch("homelab_mcp.tools.benchmark_diff.benchmark_load_tool", new=AsyncMock(return_value={
        "ok": True, "requests": 10, "successful": 10,
    })):
        result = await benchmark_diff.benchmark_baseline_tool(
            host="truenas", container="prowlarr", label="with-load", load_url="http://prowlarr:9696",
            load_requests=10, load_concurrency=1,
        )
    assert result["ok"] is True
    data = json.loads(Path(result["path"]).read_text())
    assert data["load"]["requests"] == 10


@pytest.mark.asyncio
async def test_diff_from_baseline(tmp_benchmark_dir):
    baseline = {
        "created_at": "2026-08-29T00:00:00",
        "host": "truenas",
        "container": "prowlarr",
        "label": "default",
        "metrics": {
            "host": "truenas",
            "containers": {
                "prowlarr": {
                    "cpu_percent": 1.0,
                    "memory": {"usage_bytes": 100, "usage_percent": 10.0},
                    "network": {"rx_bytes": 50, "tx_bytes": 50},
                    "block_io": {"read_bytes": 10, "write_bytes": 10},
                }
            }
        }
    }
    d = tmp_benchmark_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / "truenas_prowlarr_default_2026-08-29T00_00_00.json"
    path.write_text(json.dumps(baseline))

    with patch("homelab_mcp.tools.benchmark_diff.container_metrics_tool", new=AsyncMock(return_value={
        "ok": True,
        "metrics": {
            "host": "truenas",
            "containers": {
                "prowlarr": {
                    "cpu_percent": 3.0,
                    "memory": {"usage_bytes": 200, "usage_percent": 20.0},
                    "network": {"rx_bytes": 150, "tx_bytes": 100},
                    "block_io": {"read_bytes": 30, "write_bytes": 20},
                }
            }
        }
    })):
        result = await benchmark_diff.benchmark_diff_tool(
            host="truenas", container="prowlarr", baseline_label="default",
        )
    assert result["ok"] is True
    delta = result["deltas"]["prowlarr"]
    assert delta["cpu_percent"]["delta"] == 2.0
    assert delta["memory_usage_percent"]["delta"] == 10.0
    assert delta["network_rx_bytes"]["delta"] == 100
    assert delta["network_tx_bytes"]["delta"] == 50
    assert delta["block_io_read_bytes"]["delta"] == 20
    assert delta["block_io_write_bytes"]["delta"] == 10


@pytest.mark.asyncio
async def test_diff_missing_baseline(tmp_benchmark_dir):
    with patch("homelab_mcp.tools.benchmark_diff.container_metrics_tool", new=AsyncMock(return_value={
        "ok": True, "metrics": {"host": "truenas", "containers": {}},
    })):
        result = await benchmark_diff.benchmark_diff_tool(
            host="truenas", container="prowlarr", baseline_label="nope",
        )
    assert result["ok"] is False
    assert "no baseline found" in result["error"]


@pytest.mark.asyncio
async def test_diff_explicit_path_not_found():
    result = await benchmark_diff.benchmark_diff_tool(
        host="truenas", container="prowlarr", baseline_path="/nonexistent.json",
    )
    assert result["ok"] is False
    assert "baseline not found" in result["error"]
