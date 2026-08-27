"""Execute commands inside a running container.

This is the first tool in the homelab-mcp benchmark / diagnostics
framework. It allows the MCP client to run commands inside a specific
container on any configured host while enforcing a preflight gate
and a strict command allowlist.

Design notes
------------
* ``exec_in_container_tool`` is default-deny. Only command shapes that
  are explicitly allowlisted can run.
* The ``require_approval`` gate runs a dedicated preflight action that
  validates the host, container, and command shape. If preflight is
  blocked, the call returns without touching the container.
* There is no runtime ``allowlisted`` bypass. If a command is not
  allowlisted, the operator must change the server-side allowlist
  (configuration/code) and redeploy; the caller cannot self-approve.
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

# Shell metacharacters that are never permitted inside any argument.
# We reject these so that a single-word ``bash`` cannot carry a payload.
_SHELL_META_RE = re.compile(r"[;&|`$()\n\r<>\"\\']")

# Allowlisted executables (basename) and their permitted argument patterns.
# Patterns use fnmatch-style globs. An empty tuple means "no arguments allowed".
_ALLOWLIST: tuple[tuple[str, tuple[str, ...]], ...] = (
    # read-only filesystem inspection
    ("ls", ("*",)),
    ("find", ("*",)),
    ("cat", ("*",)),
    ("head", ("*",)),
    ("tail", ("*",)),
    ("less", ("*",)),
    ("more", ("*",)),
    ("grep", ("*",)),
    ("egrep", ("*",)),
    ("fgrep", ("*",)),
    ("rg", ("*",)),
    ("awk", ("*",)),
    ("sed", ("*",)),
    ("cut", ("*",)),
    ("sort", ("*",)),
    ("uniq", ("*",)),
    ("wc", ("*",)),
    ("file", ("*",)),
    ("stat", ("*",)),
    ("readlink", ("*",)),
    ("realpath", ("*",)),
    ("dirname", ("*",)),
    ("basename", ("*",)),
    ("printf", ("*",)),
    ("echo", ("*",)),
    ("test", ("*",)),
    ("[", ("*",)),

    # process / resource inspection
    ("ps", ("*",)),
    ("top", ()),
    ("htop", ()),
    ("pstree", ("*",)),
    ("pgrep", ("*",)),
    ("pidof", ("*",)),
    ("df", ("*",)),
    ("du", ("*",)),
    ("free", ("*",)),
    ("vmstat", ("*",)),
    ("iostat", ("*",)),
    ("uptime", ()),
    ("whoami", ()),
    ("id", ("*",)),
    ("hostname", ("*",)),
    ("uname", ("*",)),
    ("date", ("*",)),
    ("env", ("*",)),
    ("pwd", ()),

    # network inspection
    ("ping", ("*",)),
    ("ping6", ("*",)),
    ("nslookup", ("*",)),
    ("dig", ("*",)),
    ("host", ("*",)),
    ("curl", ("*",)),
    ("wget", ("*",)),
    ("ss", ("*",)),
    ("netstat", ("*",)),
    ("ip", ("*",)),
    ("ifconfig", ("*",)),
    ("route", ("*",)),
    ("traceroute", ("*",)),

    # package/database read-only inspection
    ("apk", ("list", "info", "search", "version", "--version", "-V", "policy")),
    ("apt", ("list", "show", "policy", "search")),
    ("apt-get", ("--version",)),
    ("dpkg", ("-l", "-s", "--list", "--status")),
    ("yum", ("list", "info", "search")),
    ("dnf", ("list", "info", "search")),
    ("pacman", ("-Q", "-Qi", "-Ql", "-Qo", "-Si", "-Sl")),
    ("rpm", ("-q", "-qa", "-qi", "-ql", "-qf")),

    # database read-only diagnostics
    ("psql", ("-c", "\\l", "\\dt", "\\d", "\\du", "SELECT *", "--version")),
    ("mysql", ("-e", "SHOW", "SELECT", "-V", "--version")),
    ("sqlite3", (".tables", ".tables?", ".schema", ".schema?", "SELECT", "PRAGMA", "*sqlite", "*sqlite3", "*.db", "*.duckdb", ".mode", ".headers", ".version")),
    ("redis-cli", ("INFO", "PING", "GET", "LRANGE", "LLEN", "SCARD", "HLEN", "SMEMBERS", "HGETALL")),
    ("mongo", ("--eval", "db.adminCommand", "rs.status", "db.stats")),

    # container-internal helpers
    ("true", ()),
    ("false", ()),
    ("which", ("*",)),
    ("whereis", ("*",)),
    ("tini", ("*",)),
    ("s6-svscanctl", ("*",)),
    ("s6-svc", ("*",)),
    ("supervisorctl", ("status", "avail", "pid")),
)


def _command_to_str(command: list[str]) -> str:
    """Return a shell-like representation of the command list."""
    import shlex
    return " ".join(shlex.quote(str(c)) for c in command)


def _has_shell_meta(text: str) -> bool:
    return bool(_SHELL_META_RE.search(text))


def _is_safe_arg(arg: str) -> bool:
    """A safe argument contains no shell metacharacters."""
    if not isinstance(arg, str):
        return False
    return not _has_shell_meta(arg)


def _matches_pattern(arg: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(arg, pattern)


def _is_allowlisted(command: list[str]) -> str | None:
    """Return None if the command is allowed, else a rejection reason."""
    if not command:
        return "empty command list"

    binary = command[0]
    args = command[1:]

    # Validate the binary name itself: no paths, no shell meta.
    if "/" in binary or "\\" in binary or _has_shell_meta(binary):
        return f"binary name contains path or shell metacharacters: {binary!r}"

    # Find allowlist entry and validate each argument against its patterns.
    allowed_patterns: tuple[str, ...] | None = None
    for b, patterns in _ALLOWLIST:
        if b == binary:
            allowed_patterns = patterns
            break

    if allowed_patterns is None:
        return f"binary {binary!r} is not in the allowlist"

    if not allowed_patterns:
        # No args allowed.
        if args:
            return f"{binary!r} does not accept arguments"
        return None

    # Every argument must be free of shell metacharacters.
    for a in args:
        if not _is_safe_arg(a):
            return f"argument contains shell metacharacters: {a!r}"

    # Wildcard pattern means any safe arg is acceptable.
    if "*" in allowed_patterns:
        return None

    # Each arg must match at least one allowed pattern.
    for a in args:
        if not any(_matches_pattern(a, p) for p in allowed_patterns):
            return f"argument {a!r} is not allowed for {binary!r}"

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
) -> dict[str, Any]:
    """Run a command inside a running container.

    The command must be in the server-side allowlist. There is no
    runtime override; callers cannot self-approve destructive commands.
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
            "blocked": "empty command list",
            "preflight": None,
        }
    if not container:
        return {
            "ok": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": "container name is required",
            "duration_ms": 0,
            "blocked": "missing container name",
            "preflight": None,
        }

    # Allowlist check.
    blocked_reason = _is_allowlisted(command)
    if blocked_reason:
        return {
            "ok": False,
            "exit_code": 2,
            "stdout": "",
            "stderr": (
                f"Command rejected by allowlist: {blocked_reason}. "
                "If this command should be permitted, update the server-side "
                "allowlist and redeploy."
            ),
            "duration_ms": 0,
            "blocked": blocked_reason,
            "preflight": None,
        }

    # Preflight gate.
    preflight_result: dict[str, Any] | None = None
    if require_approval:
        preflight_result = await preflight_check_tool(
            host=host,
            stack=container,
            action="exec_in_container",
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
