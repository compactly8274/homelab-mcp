"""Read-only health checks: NFS, DNS, VPN."""

from __future__ import annotations

import logging
import re
from typing import Any

from homelab_mcp.server import mcp

log = logging.getLogger(__name__)


@mcp.tool()
async def check_nfs_shares_tool(host: str) -> list[dict[str, str]]:
    """List mounted NFS exports on the given host.

    Returns one dict per mount: ``device``, ``mountpoint``, ``fstype``.
    Runs ``mount`` on the host and parses the output.
    """
    from homelab_mcp.server import get_host
    h = get_host(host)
    cmd = "mount | grep -E '^[^ ]+ on .* type nfs' || true"
    try:
        r = await h.run_command(cmd, timeout=10.0)
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]
    rows: list[dict[str, str]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Linux mount format: "device on mountpoint type fstype (opts)"
        m = re.match(r"^(\S+) on (\S+) type (\S+)\s+\(([^)]*)\)", line)
        if m:
            rows.append({
                "device": m.group(1),
                "mountpoint": m.group(2),
                "fstype": m.group(3),
            })
    return rows


@mcp.tool()
async def check_dns_tool(host: str, names: list[str]) -> dict[str, list[str]]:
    """Resolve each name on the given host. Returns name → list of IPs.

    Uses the host's resolver (so it sees the host's /etc/hosts and
    DNS config). Empty list = name did not resolve.
    """
    from homelab_mcp.server import get_host
    h = get_host(host)
    out: dict[str, list[str]] = {}
    for name in names:
        try:
            r = await h.run_command(
                f"getent ahosts {name} 2>/dev/null || true", timeout=5.0
            )
        except Exception as e:
            out[name] = [f"<error: {e}>"]
            continue
        ips: list[str] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            # getent ahosts output: "name IP" or just "IP"
            parts = line.split()
            if not parts:
                continue
            # Last field is the IP
            ip = parts[-1]
            if ip not in ips:
                ips.append(ip)
        out[name] = ips
    return out


@mcp.tool()
async def check_vpn_health_tool(host: str) -> dict[str, Any]:
    """Check gluetun health on the given host.

    Returns ``running`` (bool), the recent log excerpt, and the
    default port-forwarding state if available.
    """
    from homelab_mcp.server import get_host
    h = get_host(host)
    try:
        containers = await h.list_containers(all=False)
    except Exception as e:
        return {"running": False, "error": f"{type(e).__name__}: {e}"}
    gluetun = next((c for c in containers if "gluetun" in (c.get("NAME") or "").lower()), None)
    if not gluetun:
        return {"running": False, "found": False}
    try:
        logs = await h.container_logs(gluetun["NAME"], tail=50)
    except Exception as e:
        return {"running": True, "found": True, "log_error": f"{type(e).__name__}: {e}"}
    return {
        "running": True,
        "found": True,
        "container": gluetun["NAME"],
        "log_excerpt": logs[-2000:],  # tail of the last ~50 lines
    }
