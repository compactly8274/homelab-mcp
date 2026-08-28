from __future__ import annotations

import shlex
import time
from typing import Any

from homelab_mcp.hosts.base import CommandResult
from homelab_mcp.server import get_host
from homelab_mcp.tools.preflight import preflight_check_tool

_MAX_TIMEOUT_S = 300.0
_RESTORE_STAGING_DIR = "/tmp"


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


async def db_restore_tool(
    host: str,
    container: str,
    db_path: str,
    snapshot_path: str,
    timeout: float = 120.0,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Restore a SQLite database from a host snapshot into a container.

    The snapshot SQL is copied into the container and applied with
    ``sqlite3 <db_path> ".read /tmp/...sql"``. The container's original
    database file is left in place but overwritten by the restore SQL.
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
        preflight = await preflight_check_tool(host, container, "db_restore")
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

    read_r = await h.read_file(snapshot_path)
    if not read_r.ok:
        return {"ok": False, "preflight": preflight, "error": f"failed to read snapshot: {read_r.stderr}"}

    sql = read_r.stdout
    if "PRAGMA" not in sql.upper() and "CREATE TABLE" not in sql.upper():
        return {
            "ok": False,
            "preflight": preflight,
            "error": "snapshot does not look like a SQLite dump",
        }

    staging = f"{_RESTORE_STAGING_DIR}/homelab-mcp-restore-{int(time.time())}.sql"
    write_r = await h.write_file(staging, sql)
    if not write_r.ok:
        return {
            "ok": False,
            "preflight": preflight,
            "error": f"failed to stage restore SQL on host: {write_r.stderr}",
        }

    copy_r = await h.copy_to_container(container, staging, staging)
    if not copy_r.ok:
        return {
            "ok": False,
            "preflight": preflight,
            "error": f"failed to copy restore SQL into container: {copy_r.stderr}",
        }

    try:
        cmd = ["sqlite3", db_path, f".read {staging}"]
        t0 = time.monotonic()
        r: CommandResult = await h.exec_in_container(container, cmd, timeout=timeout)
        return {
            "ok": r.ok,
            "host": host,
            "container": container,
            "db_path": db_path,
            "snapshot_path": snapshot_path,
            "staging_path": staging,
            "exit_code": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "preflight": preflight,
        }
    finally:
        # Best-effort cleanup of staging files on both sides.
        await h.run_command(f"rm -f {shlex.quote(staging)}", timeout=10.0)
        await h.exec_in_container(container, ["rm", "-f", staging], timeout=10.0)
