"""The homelab-mcp server: FastMCP app singleton + entry point + tool wiring.

The ``mcp`` singleton is exported so tool modules can register
themselves with ``@mcp.tool()`` at import time. ``build_hosts``
constructs the HostClient mapping from the Settings.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.hosts.local_docker import LocalDocker
from homelab_mcp.hosts.remote_ssh import RemoteSSH

# Build the FastMCP singleton. host/port are applied in __main__.main().
mcp = FastMCP(
    name="homelab-mcp",
    instructions=(
        "homelab-mcp exposes read-only diagnostics for the homelab "
        "(list_stacks, stack_status, recent_events, get_logs, check_nfs_shares, "
        "check_dns, check_vpn_health) and a visibility-only drift scanner "
        "(trigger_scan, list_pending_updates, pending_update_dismiss). "
        "Read-only tools are safe to call any time. The scan tools only "
        "surface image-drift; they do not apply updates. Apply/rollback "
        "are out of scope for this server."
    ),
)

# Host clients are populated at startup. ``None`` until ``init_hosts()`` runs.
_host_clients: dict[str, HostClient] = {}
_state: Any = None


def _local_host_alias(settings: Any) -> str:
    """Return the alias of the host the daemon is running on.

    Resolution order:
    1. ``HOMELAB_MCP_LOCAL_HOST_ALIAS`` env var.
    2. The literal ``"unraid"`` (legacy default).
    """
    explicit = getattr(settings, "local_host_alias", None)
    if explicit:
        return str(explicit)
    return "unraid"


def build_hosts(settings: Any) -> dict[str, HostClient]:
    """Build a host-clients mapping from the Settings.

    The host whose alias matches :func:`_local_host_alias` gets a
    :class:`LocalDocker` (talks to the local docker socket). Every
    other host gets a :class:`RemoteSSH` that connects via the SSH
    config.

    On TrueNAS, set ``HOMELAB_MCP_LOCAL_HOST_ALIAS=truenas`` so the
    running daemon uses LocalDocker for the TrueNAS socket and
    RemoteSSH for Unraid and any other downstream hosts.
    """
    out: dict[str, HostClient] = {}
    local = _local_host_alias(settings).lower()
    for alias in settings.hosts:
        if alias.lower() == local:
            out[alias] = LocalDocker(
                name=alias, socket_url="unix:///var/run/docker.sock"
            )
        else:
            out[alias] = RemoteSSH(
                name=alias,
                ssh_alias=alias,
                ssh_config_path=settings.ssh_config,
                verify_config=True,
            )
    return out


def init_hosts(hosts: dict[str, HostClient], state: Any) -> None:
    """Set the host clients and state singleton. Called from __main__."""
    global _host_clients, _state
    _host_clients = hosts
    _state = state
    # Eagerly import the tool modules so their @mcp.tool() decorators run.
    import homelab_mcp.tools.apply_all_pending
    import homelab_mcp.tools.apply_update
    import homelab_mcp.tools.arr
    import homelab_mcp.tools.auto_heal
    import homelab_mcp.tools.container_action
    import homelab_mcp.tools.dashboard
    import homelab_mcp.tools.dismiss_all_pending
    import homelab_mcp.tools.events
    import homelab_mcp.tools.db_restore
    import homelab_mcp.tools.db_snapshot
    import homelab_mcp.tools.exec_in_container
    import homelab_mcp.tools.get_update_history
    import homelab_mcp.tools.http_probe
    import homelab_mcp.tools.health
    import homelab_mcp.tools.memory
    import homelab_mcp.tools.notifier_status
    import homelab_mcp.tools.ollama
    import homelab_mcp.tools.plex
    import homelab_mcp.tools.preflight
    import homelab_mcp.tools.recipes
    import homelab_mcp.tools.searxng
    import homelab_mcp.tools.stacks
    import homelab_mcp.tools.suggest
    import homelab_mcp.tools.updates  # noqa: F401


def get_host(name: str) -> HostClient:
    """Look up a host client. Raises KeyError if not configured."""
    if name not in _host_clients:
        available = sorted(_host_clients.keys())
        raise KeyError(
            f"host {name!r} is not configured. "
            f"Available hosts: {available}. "
            f"Add it to HOMELAB_MCP_HOSTS (JSON list) and restart."
        )
    return _host_clients[name]


def get_state() -> Any:
    """Return the State singleton."""
    if _state is None:
        raise RuntimeError("state not initialized; call init_hosts() first")
    return _state


__all__ = ["build_hosts", "get_host", "get_state", "init_hosts", "mcp"]
