"""Tests for http_probe_tool.

Covers:
- Happy-path HTTP probe using curl.
- Timeout/method parsing.
- Preflight gate bypass (http_probe is read-only).
- Error propagation from the host backend.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_mcp.tools.http_probe import http_probe_tool


def _fake_host(name: str) -> Any:
    h = MagicMock()
    h.name = name
    return h


async def test_http_probe_parses_curl_output() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "200\t0.123\t1450\ttext/html; charset=utf-8\thttps://example.com/\t\n"
    result.stderr = ""
    result.duration_ms = 123

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("https://example.com/", host="truenas")

    assert r["ok"] is True
    assert r["http_code"] == 200
    assert r["time_total_seconds"] == 0.123
    assert r["size_download_bytes"] == 1450
    assert r["content_type"] == "text/html; charset=utf-8"
    assert r["url_effective"] == "https://example.com/"

    called_cmd = mock_host.run_command.call_args[0][0]
    assert "https://example.com/" in called_cmd
    assert "curl" in called_cmd


async def test_http_probe_non_2xx_is_ok_but_reports_status() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "500\t0.045\t120\ttext/plain; charset=utf-8\thttps://example.com/error\t\n"
    result.stderr = ""
    result.duration_ms = 45

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("https://example.com/error", host="truenas")

    assert r["ok"] is True
    assert r["http_code"] == 500


async def test_http_probe_curl_failure() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = False
    result.exit_code = 28
    result.stdout = "000\t0.000\t0\t\thttp://down/\t\n"
    result.stderr = "Connection timed out"
    result.duration_ms = 5000

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("http://down/", timeout=5.0, host="truenas")

    assert r["ok"] is False
    assert "timed out" in r["stderr"].lower() or "curl" in (r.get("error") or "").lower()


async def test_http_probe_unknown_host() -> None:
    from homelab_mcp import server

    server._host_clients = {}
    r = await http_probe_tool("http://x/", host="missing")
    assert r["ok"] is False
    assert "unknown host" in r["error"].lower()


async def test_http_probe_method_and_timeout_passed_to_curl() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "204\t0.010\t0\t\thttps://api/\t\n"
    result.stderr = ""
    result.duration_ms = 10

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("https://api/", method="POST", timeout=15.0, host="truenas")

    called_cmd = mock_host.run_command.call_args[0][0]
    assert "-X POST" in called_cmd
    assert "-m 15.0" in called_cmd or "-m 15" in called_cmd
    assert r["ok"] is True


async def test_http_probe_preflight_not_required() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "200\t0.001\t5\t\thttp://x/\t\n"
    result.stderr = ""
    result.duration_ms = 1

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("http://x/", host="truenas")

    assert r.get("preflight") is None


async def test_http_probe_malformed_curl_output() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas")}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "garbage"
    result.stderr = ""
    result.duration_ms = 0

    mock_host = MagicMock()
    mock_host.run_command = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.http_probe.get_host", return_value=mock_host):
        r = await http_probe_tool("http://x/", host="truenas")

    assert r["ok"] is False
