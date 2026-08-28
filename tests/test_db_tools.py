"""Tests for db_snapshot_tool and db_restore_tool.

Covers:
- Snapshot dumps DB content to host path.
- Restore validates snapshot and writes it back into container.
- Preflight gate integration for restore.
- Snapshot requires approval (database read).
- Failure paths: missing container, unreadable snapshot, invalid SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.tools.db_restore import db_restore_tool
from homelab_mcp.tools.db_snapshot import db_snapshot_tool


@dataclass
class _CmdResult:
    ok: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


def _fake_host(name: str) -> Any:
    h = MagicMock()
    h.name = name
    h.read_file = AsyncMock()
    h.write_file = AsyncMock()
    h.copy_to_container = AsyncMock()
    h.run_command = AsyncMock()
    h.exec_in_container = AsyncMock()
    return h


async def test_snapshot_success() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}

    exec_result = _CmdResult(stdout="PRAGMA foreign_keys=OFF;\nCREATE TABLE users (id INT);\n")
    fake.exec_in_container = AsyncMock(return_value=exec_result)

    write_result = _CmdResult()
    fake.write_file = AsyncMock(return_value=write_result)

    with patch("homelab_mcp.tools.db_snapshot.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_snapshot.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_snapshot_tool("truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql")

    assert r["ok"] is True
    assert r["snapshot_size_bytes"] > 0
    assert r["preflight"]["safe"] is True
    fake.exec_in_container.assert_awaited_once_with(
        "prowlarr", ["sqlite3", "/data/db.sqlite", ".dump"], timeout=60.0
    )
    fake.write_file.assert_awaited_once_with("/backups/db.sql", exec_result.stdout)


async def test_snapshot_preflight_blocked() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}

    with patch("homelab_mcp.tools.db_snapshot.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_snapshot.preflight_check_tool",
        new=AsyncMock(return_value={"safe": False, "blockers": ["not running"]}),
    ):
        r = await db_snapshot_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "not running" in r["error"]
    fake.exec_in_container.assert_not_awaited()


async def test_snapshot_unknown_host() -> None:
    from homelab_mcp import server

    server._host_clients = {}
    r = await db_snapshot_tool("missing", "prowlarr", "/data/db.sqlite", "/backups/db.sql")
    assert r["ok"] is False
    err = r["error"]
    if isinstance(err, list):
        err = "; ".join(str(x) for x in err)
    assert "unknown host" in err.lower()


async def test_snapshot_rejects_bad_db_path() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}

    with patch("homelab_mcp.tools.db_snapshot.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_snapshot.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_snapshot_tool("truenas", "prowlarr", "../../etc/passwd", "/backups/db.sql")

    assert r["ok"] is False
    assert "allowlist" in r["error"].lower()
    fake.exec_in_container.assert_not_awaited()


async def test_restore_success() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}

    fake.read_file = AsyncMock(return_value=_CmdResult(stdout="PRAGMA foreign_keys=OFF;\nCREATE TABLE users (id INT);\n"))

    write_result = _CmdResult()
    fake.write_file = AsyncMock(return_value=write_result)

    copy_result = _CmdResult()
    fake.copy_to_container = AsyncMock(return_value=copy_result)

    exec_result = _CmdResult()
    fake.exec_in_container = AsyncMock(return_value=exec_result)

    fake.run_command = AsyncMock(return_value=_CmdResult())

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is True
    assert r["preflight"]["safe"] is True
    sqlite_calls = [c for c in fake.exec_in_container.call_args_list if c[0][1][0] == "sqlite3"]
    assert len(sqlite_calls) == 1
    called = sqlite_calls[0]
    assert called[0][0] == "prowlarr"
    assert called[0][1][0] == "sqlite3"
    assert called[0][1][1] == "/data/db.sqlite"
    assert called[0][1][2].startswith(".read ")



async def test_restore_rejects_invalid_snapshot() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}
    fake.read_file = AsyncMock(return_value=_CmdResult(stdout="hello world"))

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "does not look like a SQLite dump" in r["error"]


async def test_restore_preflight_blocked() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}
    fake.read_file = AsyncMock(return_value=_CmdResult(stdout="PRAGMA foreign_keys=OFF;\n"))

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": False, "blockers": ["not running"]}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "not running" in r["error"]
    fake.write_file.assert_not_awaited()


async def test_restore_host_write_failure() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}
    fake.read_file = AsyncMock(return_value=_CmdResult(stdout="PRAGMA foreign_keys=OFF;\nCREATE TABLE t (id INT);\n"))

    write_result = _CmdResult(ok=False, stderr="permission denied")
    fake.write_file = AsyncMock(return_value=write_result)

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "permission denied" in r["error"]


async def test_restore_container_copy_failure() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}
    fake.read_file = AsyncMock(return_value=_CmdResult(stdout="PRAGMA foreign_keys=OFF;\nCREATE TABLE t (id INT);\n"))

    write_result = _CmdResult()
    fake.write_file = AsyncMock(return_value=write_result)

    copy_result = _CmdResult(ok=False, stderr="no such container")
    fake.copy_to_container = AsyncMock(return_value=copy_result)

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "/data/db.sqlite", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "no such container" in r["error"]


async def test_restore_rejects_bad_db_path() -> None:
    from homelab_mcp import server

    fake = _fake_host("truenas")
    server._host_clients = {"truenas": fake}

    with patch("homelab_mcp.tools.db_restore.get_host", return_value=fake), patch(
        "homelab_mcp.tools.db_restore.preflight_check_tool",
        new=AsyncMock(return_value={"safe": True, "blockers": [], "warnings": []}),
    ):
        r = await db_restore_tool(
            "truenas", "prowlarr", "../../etc/passwd", "/backups/db.sql"
        )

    assert r["ok"] is False
    assert "allowlist" in r["error"].lower()
