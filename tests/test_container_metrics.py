"""Tests for container_metrics_tool and host backends."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.tools.container_metrics import container_metrics_tool


def _fake_host(name: str):
    from types import SimpleNamespace
    h = SimpleNamespace()
    h.name = name
    return h


def _make_preflight(safe: bool = True, blockers: list[str] | None = None):
    m = MagicMock()
    m.preflight_check_tool = AsyncMock(
        return_value={
            "safe": safe,
            "blockers": blockers or [],
            "warnings": [],
            "info": [],
        }
    )
    return m


async def test_container_metrics_success():
    fake = _fake_host("truenas")
    fake.container_metrics = AsyncMock(
        return_value={
            "host": "truenas",
            "containers": {
                "prowlarr": {
                    "cpu_percent": 0.12,
                    "memory": {"usage_bytes": 180224000, "limit_bytes": 75601567744, "usage_percent": 0.24},
                    "network": {"rx_bytes": 42900000000, "tx_bytes": 119000000000},
                    "block_io": {"read_bytes": 144000000, "write_bytes": 0, "read_ios": 0, "write_ios": 0},
                    "pids": 30,
                    "raw": {},
                }
            },
            "sample_seconds": 1.0,
        }
    )

    with patch("homelab_mcp.tools.container_metrics.preflight", new=_make_preflight()), patch(
        "homelab_mcp.tools.container_metrics.get_host", return_value=fake
    ):
        r = await container_metrics_tool("truenas", "prowlarr")

    assert r["ok"] is True
    assert r["metrics"]["containers"]["prowlarr"]["cpu_percent"] == 0.12


async def test_container_metrics_preflight_blocked():
    with patch(
        "homelab_mcp.tools.container_metrics.preflight",
        new=_make_preflight(safe=False, blockers=["host unreachable"]),
    ):
        r = await container_metrics_tool("truenas", "prowlarr")

    assert r["ok"] is False
    assert "host unreachable" in r["preflight"]["blockers"]


async def test_container_metrics_backend_error():
    fake = _fake_host("truenas")
    fake.container_metrics = AsyncMock(return_value={"error": "docker not reachable", "host": "truenas"})

    with patch("homelab_mcp.tools.container_metrics.preflight", new=_make_preflight()), patch(
        "homelab_mcp.tools.container_metrics.get_host", return_value=fake
    ):
        r = await container_metrics_tool("truenas", "prowlarr")

    assert r["ok"] is False
    assert "docker not reachable" in r["error"]


def test_parse_remote_stats_line():
    from homelab_mcp.hosts.remote_ssh import _parse_remote_stats_line

    line = '{"BlockIO":"144MB / 0B","CPUPerc":"0.12%","Container":"prowlarr","ID":"fda0b77b30af","MemPerc":"0.24%","MemUsage":"172MiB / 70.4GiB","Name":"prowlarr","NetIO":"42.9GB / 119GB","PIDs":"30"}'
    parsed = _parse_remote_stats_line(line)
    assert parsed is not None
    assert parsed["cpu_percent"] == 0.12
    assert parsed["memory"]["usage_bytes"] == 172 * 1024 * 1024
    assert parsed["memory"]["limit_bytes"] == int(70.4 * 1024 * 1024 * 1024)
    assert parsed["network"]["rx_bytes"] == 42900000000
    assert parsed["network"]["tx_bytes"] == 119000000000
    assert parsed["block_io"]["read_bytes"] == 144000000
    assert parsed["pids"] == 30


def test_parse_remote_stats_line_bad_json():
    from homelab_mcp.hosts.remote_ssh import _parse_remote_stats_line

    assert _parse_remote_stats_line("") is None
    assert _parse_remote_stats_line("not json") is None


def test_normalize_docker_stats():
    from homelab_mcp.hosts.local_docker import _normalize_docker_stats

    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1000000000, "percpu_usage": [0] * 4},
            "system_cpu_usage": 80000000000,
            "online_cpus": 4,
            "throttling_data": {"periods": 30},
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 900000000},
            "system_cpu_usage": 70000000000,
        },
        "memory_stats": {"usage": 200000000, "limit": 800000000},
        "pids_stats": {"current": 30},
        "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}},
        "blkio_stats": {
            "io_service_bytes_recursive": [{"op": "read", "value": 3000}, {"op": "write", "value": 4000}],
            "io_serviced_recursive": [{"op": "read", "value": 5}, {"op": "write", "value": 6}],
        },
    }
    out = _normalize_docker_stats(stats)
    assert out["cpu_percent"] > 0
    assert out["memory"]["usage_bytes"] == 200000000
    assert out["memory"]["limit_bytes"] == 800000000
    assert out["network"]["rx_bytes"] == 1000
    assert out["block_io"]["read_bytes"] == 3000
    assert out["pids"] == 30
