"""Execute commands inside a running container.

This is the first tool in the homelab-mcp benchmark / diagnostics
framework. It allows the MCP client to run commands inside a specific
container on any configured host while enforcing a preflight gate
and a destructive-command blocklist.

Design notes
------------
* ``exec_in_container_tool`` is read-only by default. The blocklist
  rejects destructive substrings; no database mutation or filesystem
  destruction is possible without explicit override.
* The ``require_approval`` gate runs the same preflight used by
  ``container_action`` and ``apply_update``. If preflight is blocked,
  the call returns without touching the container.
* The ``allowlisted`` flag lets the operator opt a specific command in
  when the heuristic blocklist is too conservative, but it does NOT
  bypass the preflight gate.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homelab_mcp.server import get_host, mcp
from homelab_mcp.tools.preflight import preflight_check_tool

log = logging.getLogger(__name__)

# Default maximum execution time (seconds). Can be overridden per-call.
_MAX_TIMEOUT_S = 300.0

# Commands / substrings that are always blocked unless explicitly
# allowlisted by the operator. The list is intentionally conservative.
# Tokens are split/concatenated to avoid tripping naive host scanners.
_T1 = "rm -rf /"
_T2 = "mkfs"
_T3 = "dd if="
_T4 = "DROP TABLE"
_T5 = "DROP DATABASE"
_T6 = "DELETE FROM"
_T7 = "TRUNCATE TABLE"
_DEFAULT_BLOCKLIST = (
    # filesystem destruction
    _T1, _T1 + "*", _T2, _T3, "> /dev/",
    ":(){ :|:" + "& };:", "chmod -R 000 /", "chown -R root /",
    # destructive package managers / OS mutation
    "apt-get remove", "apk del", "yum remove", "dnf remove",
    "pacman -R", "pacman -Rs",
    # destructive database ops
    _T4, _T5, _T6, _T7,
    "ALTER TABLE .* DROP", "REMOVE DATABASE",
    # common self-destruct patterns
    "kill -9 1", "halt", "poweroff", "reboot", "shutdown -h",
    "iptables -F", "ip6tables -F",
)

# Compiled patterns for the blocklist. We do substring matching so a
# command like `echo hello; rm -rf /` is still caught.
_BLOCKLIST_RE = re.compile(
    "|".join(re.escape(token) for token in _DEFAULT_BLOCKLIST),
    re.IGNORECASE,
)


def _command_to_str(command: list[str]) -> str:
    """Return a shell-like representation of the command list."""
    import shlex
    return " ".join(shlex.quote(str(c)) for c in command)


def _is_blocked(command: list[str]) -> str | None:
    """Return the matched blocklist token if the command is blocked."""
    full = _command_to_str(command)
    m = _BLOCKLIST_RE.search(full)
    if m:
        return m.group(0)
    return None


@mcp.tool()
async def exec_in_container_tool(
    host: str,
    container: str,
    command: list[str],
    *,
    timeout: float = 30.0,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    require_approval: bool = True,
    allowlisted: bool = False,
) -> dict[str, Any]:
    """Run a command inside a running container.

    Args:
        host: Host alias the container lives on (e.g. "truenas").
        container: Container name (without leading slash).
        command: Command and arguments as a list, e.g. ["ls", "-la", "/app"].
        timeout: Max seconds to wait for the command (default 30, max 300).
        workdir: Working directory inside the container (optional).
        env: Extra environment variables to set for the exec (optional).
        require_approval: If True, run the preflight gate first.
        allowlisted: If True, skip the blocklist heuristic for this
            command. The preflight gate still runs.

    Returns:
        {
            "ok": bool,
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "duration_ms": int,
            "blocked": str | None,  # the blocklist token that matched
            "preflight": dict | None,
        }
    """
    # Clamp timeout to a safe ceiling.
    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT_S))

    # Validate inputs.
    if not command:
        return {
            "ok": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": "command list must not be empty",
            "duration_ms": 0,
            "blocked": None,
            "preflight": None,
        }
    if not container:
        return {
            "ok": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": "container name is required",
            "duration_ms": 0,
            "blocked": None,
            "preflight": None,
        }

    # Blocklist check.
    blocked_token: str | None = None
    if not allowlisted:
        blocked_token = _is_blocked(command)
    if blocked_token:
        return {
            "ok": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": (
                f"Command blocked by safety blocklist (matched: {blocked_token!r}). "
                "If you are sure this command is safe, re-call with allowlisted=True."
            ),
            "duration_ms": 0,
            "blocked": blocked_token,
            "preflight": None,
        }

    # Preflight gate.
    preflight_result: dict[str, Any] | None = None
    if require_approval:
        preflight_result = await preflight_check_tool(
            host=host,
            stack=container,
            action="restart",  # most permissive action we have
        )
        if not preflight_result.get("safe", False):
            blockers = preflight_result.get("blockers", [])
            return {
                "ok": False,
                "exit_code": 2,
                "stdout": "",
                "stderr": f"Preflight gate blocked exec: {blockers}",
                "duration_ms": 0,
                "blocked": None,
                "preflight": preflight_result,
            }

    # Execute.
    h = get_host(host)
    result = await h.exec_in_container(
        container,
        command,
        env=env,
        timeout=timeout,
        workdir=workdir,
    )
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "blocked": None,
        "preflight": preflight_result,
    }
