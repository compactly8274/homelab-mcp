"""Tests for benchmark_load_tool."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homelab_mcp.tools.benchmark_load import _latency_summary, _percentile, benchmark_load_tool


@pytest.fixture
def mock_host_clients(monkeypatch):
    monkeypatch.setattr(
        "homelab_mcp.tools.benchmark_load.get_host",
        lambda name: None,
    )


async def test_empty_url(mock_host_clients):
    out = await benchmark_load_tool(url="")
    assert out["ok"] is False
    assert "url is required" in out["error"]


async def test_unknown_host():
    from homelab_mcp.server import _host_clients
    _host_clients.clear()
    out = await benchmark_load_tool(url="http://example.test", host="missing")
    assert out["ok"] is False
    assert "unknown host" in out["error"]


async def test_capping(mock_host_clients):
    with patch("homelab_mcp.tools.benchmark_load.http_probe_tool", new_callable=AsyncMock) as p:
        p.return_value = {"ok": True, "http_code": 200, "duration_ms": 12.0}
        out = await benchmark_load_tool(url="http://x", requests=100_000, concurrency=1_000, timeout=60)
        assert out["requests"] == 10_000
        assert out["ok"] is True
        assert p.call_count == 10_000


async def test_latency_percentiles(mock_host_clients):
    with patch("homelab_mcp.tools.benchmark_load.http_probe_tool", new_callable=AsyncMock) as p:
        # Return increasing latencies for deterministic percentiles (1..100 ms)
        latencies = [float(i) for i in range(1, 101)]
        p.side_effect = [{"ok": True, "http_code": 200, "duration_ms": ms} for ms in latencies]
        out = await benchmark_load_tool(url="http://x", requests=100, concurrency=100)
        assert out["successful"] == 100
        assert out["latency_ms"]["min"] == 1.0
        assert out["latency_ms"]["max"] == 100.0
        assert out["latency_ms"]["mean"] == 50.5
        assert out["latency_ms"]["p50"] == 50.5
        # Linear interpolation: index (n-1)*p
        assert out["latency_ms"]["p95"] == 95.05
        assert out["latency_ms"]["p99"] == 99.01
        assert out["status_counts"][200] == 100


async def test_error_aggregation(mock_host_clients):
    with patch("homelab_mcp.tools.benchmark_load.http_probe_tool", new_callable=AsyncMock) as p:
        responses = []
        for i in range(10):
            if i % 3 == 0:
                responses.append({"ok": False, "error": "timeout"})
            elif i % 3 == 1:
                responses.append({"ok": False, "stderr": "connection refused"})
            else:
                responses.append({"ok": True, "http_code": 500, "duration_ms": 5.0})
        p.side_effect = responses
        out = await benchmark_load_tool(url="http://x", requests=10, concurrency=10)
        assert out["successful"] == 3
        assert out["errors"] == 7
        assert out["error_messages"]["timeout"] == 4
        assert out["error_messages"]["connection refused"] == 3
        assert out["status_counts"][500] == 3


def test_latency_summary_empty():
    assert _latency_summary([]) == {
        "min": None, "mean": None, "p50": None, "p95": None, "p99": None, "max": None
    }


def test_percentile_edges():
    data = [10.0, 20.0, 30.0]
    assert _percentile(data, 0.0) == 10.0
    assert _percentile(data, 1.0) == 30.0
    assert _percentile(data, 0.5) == 20.0

