from __future__ import annotations

import time
from typing import Any

from homelab_mcp.hosts.base import CommandResult
from homelab_mcp.server import get_host
from homelab_mcp.tools.preflight import preflight_check_tool

_MAX_TIMEOUT_S = 300.0

_DUMP_SCRIPT = r"""
import sqlite3, sys
db_path = sys.argv[1]
con = sqlite3.connect(db_path)
for line in con.iterdump():
    print(line)
con.close()
"""


def _normalize_error(e: Any) -> str:
    if isinstance(e, list):
        e = "; ".join(str(x) for x in e)
    return str(e)


def _safe_db_path(db_path: str) -> bool:
    """Only allow absolute paths inside typical container data dirs."""
    if not db_path.startswith("/"):
        return False
    allowed_prefixes = (
        "/config/", "/data/", "/app/", "/var/lib/",
        "/usr/lib/", "/opt/", "/home/", "/root/",
    )
    forbidden = ("..", ";", "|", ">", "<", "`", "$", "(")
    if any(c in db_path for c in forbidden):
        return False
    return any(db_path.startswith(p) for p in allowed_prefixes)


async def db_snapshot_tool(
    host: str,
    container: str,
    db_path: str,
    snapshot_path: str,
    timeout: float = 60.0,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Dump a SQLite database from a container to a host path.

    The dump is produced by running Python's stdlib ``sqlite3.iterdump()``
    inside the container. The resulting SQL is compatible with the SQLite
    CLI ``.read`` command and with ``sqlite3.Connection.executescript()`` so
    it can be restored on any system with a SQLite-compatible engine.
    """
    if not all([host, container, db_path, snapshot_path]):
        return {"ok": False, "error": "host, container, db_path, and snapshot_path are required"}

    if not _safe_db_path(db_path):
        return {"ok": False, "error": f"db_path rejected by allowlist: {db_path!r}"}

    if not snapshot_path.startswith("/") or ".." in snapshot_path:
        return {"ok": False, "error": f"snapshot_path must be absolute and canonical: {snapshot_path!r}"}

    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT_S))

    preflight: dict[str, Any] | None = None
    if require_approval:
        preflight = await preflight_check_tool(host, container, "db_snapshot")
        if not preflight.get("safe"):
            blockers = preflight.get("blockers") or []
            warnings = preflight.get("warnings") or []
            return {
                "ok": False,
                "preflight": preflight,
                "error": _normalize_error(blockers or warnings or ["preflight blocked"]),
            }

    h = get_host(host)
    if h is None:
        return {"ok": False, "preflight": preflight, "error": f"unknown host: {host}"}

    cmd = ["python3", "-c", _DUMP_SCRIPT, db_path]
    t0 = time.monotonic()
    r: CommandResult = await h.exec_in_container(container, cmd, timeout=timeout)
    if not r.ok:
        return {
            "ok": False,
            "preflight": preflight,
            "exit_code": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "error": f"sqlite3 dump failed: {r.stderr.strip() or 'unknown error'}",
            "duration_ms": r.duration_ms,
        }

    # Basic sanity check: iterdump always wraps its output in a transaction,
    # even for an empty database or one with only virtual (e.g. FTS) tables,
    # so CREATE TABLE alone is too strict a signal for "valid dump".
    stdout_upper = r.stdout.upper()
    if "BEGIN TRANSACTION" not in stdout_upper or "COMMIT" not in stdout_upper:
        return {
            "ok": False,
            "preflight": preflight,
            "error": "snapshot does not look like a valid SQLite dump",
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    write_r = await h.write_file(snapshot_path, r.stdout)
    if not write_r.ok:
        return {
            "ok": False,
            "preflight": preflight,
            "error": f"failed to write snapshot: {write_r.stderr}",
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    return {
        "ok": True,
        "host": host,
        "container": container,
        "db_path": db_path,
        "snapshot_path": snapshot_path,
        "snapshot_size_bytes": len(r.stdout.encode("utf-8")),
        "exit_code": r.exit_code,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "preflight": preflight,
    }
