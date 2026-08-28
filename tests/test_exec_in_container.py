"""Tests for exec_in_container_tool.

Covers:
- The strict allowlist (default-deny, no self-override).
- Rejection of path/binary/argument tricks.
- Preflight gate integration.
- Happy-path execution.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.tools.exec_in_container import (
    _is_allowlisted,
    exec_in_container_tool,
)

# --- allowlist unit tests ---------------------------------------------------

def test_allowlist_accepts_read_only_command() -> None:
    assert _is_allowlisted(["ls", "-la", "/app"]) is None


def test_allowlist_rejects_dangerous_binary() -> None:
    assert _is_allowlisted(["rm", "-rf", "/"]) is not None


def test_allowlist_rejects_path_binary() -> None:
    assert _is_allowlisted(["/bin/bash", "-c", "rm -rf /"]) is not None


def test_allowlist_rejects_shell_metacharacters_in_args() -> None:
    assert _is_allowlisted(["ls", "-la; rm -rf /"]) is not None


def test_allowlist_rejects_bash_word() -> None:
    assert _is_allowlisted(["bash"]) is not None


def test_allowlist_rejects_sh_dash_c() -> None:
    assert _is_allowlisted(["sh", "-c", "echo hi"]) is not None


def test_allowlist_rejects_empty_command() -> None:
    assert _is_allowlisted([]) is not None


def test_allowlist_rejects_unlisted_binary() -> None:
    assert _is_allowlisted(["unlisted_binary"]) is not None


def test_allowlist_allows_simple_database_read() -> None:
    assert _is_allowlisted(["sqlite3", "/data/db.sqlite", ".tables"]) is None


def test_allowlist_rejects_database_write_payload() -> None:
    assert _is_allowlisted(["sqlite3", "/data/db.sqlite", "DROP TABLE users"]) is not None


def test_allowlist_arg_pattern_enforced() -> None:
    assert _is_allowlisted(["redis-cli", "INFO"]) is None
    assert _is_allowlisted(["redis-cli", "FLUSHALL"]) is not None


def test_allowlist_no_args_binary_with_args_fails() -> None:
    assert _is_allowlisted(["uptime", "--version"]) is not None


# --- preflight integration tests -------------------------------------------


def _fake_host(name: str, containers: list[dict[str, Any]], inspect: dict[str, dict] | None = None) -> Any:
    h = MagicMock()
    h.name = name
    h.list_containers = AsyncMock(return_value=containers)
    inspect_map = inspect if inspect is not None else {}

    async def _inspect(name: str) -> dict[str, Any]:
        return inspect_map.get(
            name,
            {
                "Name": f"/{name}",
                "State": {
                    "Status": "running",
                    "RestartCount": 0,
                    "StartedAt": "2020-01-01T00:00:00Z",
                },
                "Mounts": [],
            },
        )

    h.inspect_container = AsyncMock(side_effect=_inspect)
    return h


async def test_exec_rejected_by_allowlist_before_preflight() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas", [{"NAME": "prowlarr", "PROJECT": "prowlarr"}])}

    with patch("homelab_mcp.tools.exec_in_container.get_host") as mock_host:
        mock_host.return_value = MagicMock()
        r = await exec_in_container_tool("truenas", "prowlarr", ["rm", "-rf", "/"])
    assert r["ok"] is False
    assert r["blocked"] is not None
    assert "allowlist" in r["stderr"].lower()
    assert mock_host.return_value.exec_in_container.call_count == 0


async def test_exec_blocked_when_preflight_fails() -> None:
    from homelab_mcp import server

    server._host_clients = {"truenas": _fake_host("truenas", [])}
    with patch("homelab_mcp.tools.exec_in_container.get_host") as mock_host:
        mock_host.return_value = MagicMock()
        r = await exec_in_container_tool("truenas", "prowlarr", ["ls", "/app"])
    print("DEBUG_R", r)
    assert r["ok"] is False
    assert r["preflight"] is not None
    assert r["preflight"]["safe"] is False
    assert mock_host.return_value.exec_in_container.call_count == 0


async def test_exec_succeeds_when_allowlisted_and_preflight_passes() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas", [{"NAME": "prowlarr", "PROJECT": "prowlarr"}])
    server._host_clients = {"truenas": fake}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = "app\n"
    result.stderr = ""
    result.duration_ms = 12

    mock_host = MagicMock()
    mock_host.exec_in_container = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.exec_in_container.get_host", return_value=mock_host):
        r = await exec_in_container_tool("truenas", "prowlarr", ["ls", "/app"])
    print("DEBUG_R", r)
    assert r["ok"] is True
    assert r["exit_code"] == 0
    assert r["blocked"] is None
    assert r["preflight"]["safe"] is True
    mock_host.exec_in_container.assert_awaited_once_with(
        "prowlarr", ["ls", "/app"], env=None, timeout=30.0, workdir=None
    )


async def test_exec_respects_require_approval_false() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas", [{"NAME": "prowlarr", "PROJECT": "prowlarr"}])
    server._host_clients = {"truenas": fake}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = ""
    result.stderr = ""
    result.duration_ms = 5

    mock_host = MagicMock()
    mock_host.exec_in_container = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.exec_in_container.get_host", return_value=mock_host):
        r = await exec_in_container_tool(
            "truenas", "prowlarr", ["ls", "/app"], require_approval=False
        )
    assert r["ok"] is True
    assert r["preflight"] is None


async def test_exec_clamps_timeout() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas", [{"NAME": "prowlarr", "PROJECT": "prowlarr"}])
    server._host_clients = {"truenas": fake}
    result = MagicMock()
    result.ok = True
    result.exit_code = 0
    result.stdout = ""
    result.stderr = ""
    result.duration_ms = 5

    mock_host = MagicMock()
    mock_host.exec_in_container = AsyncMock(return_value=result)

    with patch("homelab_mcp.tools.exec_in_container.get_host", return_value=mock_host):
        r = await exec_in_container_tool(
            "truenas", "prowlarr", ["ls", "/app"], timeout=600.0
        )
    assert r["ok"] is True
    # timeout should be clamped to _MAX_TIMEOUT_S = 300
    mock_host.exec_in_container.assert_awaited_once_with(
        "prowlarr", ["ls", "/app"], env=None, timeout=300.0, workdir=None
    )
