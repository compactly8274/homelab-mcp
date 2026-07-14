"""Tests for the FastMCP server skeleton."""

from __future__ import annotations

from fastmcp import FastMCP

from homelab_mcp.server import mcp


def test_mcp_is_a_fastmcp_instance() -> None:
    """The mcp singleton is a FastMCP instance with the right name."""
    assert isinstance(mcp, FastMCP)
    assert mcp.name == "homelab-mcp"


def test_mcp_has_tools_registered() -> None:
    """Phase 1 wires the read-only tools into the server."""
    import homelab_mcp.tools.events
    import homelab_mcp.tools.health
    import homelab_mcp.tools.stacks
    import homelab_mcp.tools.updates  # noqa: F401
    tools = mcp._tool_manager._tools
    expected = {
        "list_stacks_tool",
        "stack_status_tool",
        "recent_events_tool",
        "get_logs_tool",
        "check_nfs_shares_tool",
        "check_dns_tool",
        "check_vpn_health_tool",
        "list_pending_updates_tool",
        "trigger_scan_tool",
        "pending_update_dismiss_tool",
    }
    registered = set(tools)
    assert expected.issubset(registered), f"missing: {expected - registered}"


def test_local_host_alias_default() -> None:
    """Without an explicit setting, the local host is 'unraid'."""
    from homelab_mcp.server import _local_host_alias
    class _S: pass
    s = _S()
    assert _local_host_alias(s) == "unraid"


def test_local_host_alias_explicit() -> None:
    """HOMELAB_MCP_LOCAL_HOST_ALIAS='truenas' returns 'truenas'."""
    from homelab_mcp.server import _local_host_alias
    class _S: pass
    s = _S()
    s.local_host_alias = "truenas"
    assert _local_host_alias(s) == "truenas"


def test_build_hosts_local_and_remote() -> None:
    """build_hosts wires up LocalDocker for the local alias, RemoteSSH for others."""
    from unittest.mock import patch

    from homelab_mcp.hosts.local_docker import LocalDocker
    from homelab_mcp.hosts.remote_ssh import RemoteSSH
    from homelab_mcp.server import build_hosts

    class _S:
        hosts = ["truenas", "unraid"]
        ssh_config = "/dev/null"
        local_host_alias = "truenas"

    with patch.object(RemoteSSH, "__init__", lambda self, **kw: None):
        hosts = build_hosts(_S())
    assert isinstance(hosts["truenas"], LocalDocker)
    assert isinstance(hosts["unraid"], RemoteSSH)
