"""SQLite state layer for the homelab-mcp server.

Three tables:

- ``stacks`` — discovered stacks per host (host, name, path, current_image_digest,
  last_checked_at)
- ``update_history`` — every update we attempted (id, host, stack,
  from_digest, to_digest, status, started_at, finished_at, rollback_to_digest,
  reason)
- ``pending_updates`` — image-drift rows from the visibility scanner
  (host, stack, current_digest, latest_digest, first_seen_at, last_seen_at)

All access is async (aiosqlite). The DB connection is opened per call to
support multiple writers safely; every connection sets
``PRAGMA busy_timeout`` so a long-running writer doesn't block a
concurrent reader with ``OperationalError: database is locked``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stacks (
    host TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT,
    managed_by TEXT,
    current_image_digest TEXT,
    last_known_good_image_digest TEXT,
    last_known_good_compose_hash TEXT,
    last_checked_at TEXT,
    PRIMARY KEY (host, name)
);

CREATE TABLE IF NOT EXISTS update_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    stack TEXT NOT NULL,
    from_digest TEXT NOT NULL,
    to_digest TEXT NOT NULL,
    manifest_digest TEXT,
    config_digest TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rollback_to_digest TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_update_history_host_stack
    ON update_history (host, stack, started_at DESC);

CREATE TABLE IF NOT EXISTS pending_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    stack TEXT NOT NULL,
    current_digest TEXT NOT NULL,
    latest_digest TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (host, stack, latest_digest)
);
"""


@dataclass
class UpdateRow:
    """One row of update_history."""

    id: int
    host: str
    stack: str
    from_digest: str
    to_digest: str
    status: str
    started_at: str
    finished_at: str | None
    rollback_to_digest: str | None
    manifest_digest: str | None = None
    config_digest: str | None = None
    reason: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class State:
    """Async SQLite wrapper for the homelab-mcp server."""

    def __init__(self, db_path, busy_timeout_ms: int = 5000) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    async def _connect(self):
        db = await aiosqlite.connect(self._db_path)
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return db

    async def init_db(self) -> None:
        """Create tables if they don't exist. Idempotent."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await self._connect()
        try:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        finally:
            await db.close()

    # -- stacks ------------------------------------------------------------

    async def upsert_stack(
        self,
        host: str,
        name: str,
        path: str | None = None,
        managed_by: str | None = None,
        current_image_digest: str | None = None,
        last_known_good_compose_hash: str | None = None,
    ) -> None:
        now = _now_iso()
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO stacks (host, name, path, managed_by, current_image_digest,
                                    last_known_good_compose_hash, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (host, name) DO UPDATE SET
                    path = COALESCE(excluded.path, path),
                    managed_by = COALESCE(excluded.managed_by, managed_by),
                    current_image_digest = COALESCE(excluded.current_image_digest, current_image_digest),
                    last_known_good_compose_hash = COALESCE(
                        excluded.last_known_good_compose_hash,
                        last_known_good_compose_hash
                    ),
                    last_checked_at = excluded.last_checked_at
                """,
                (host, name, path, managed_by, current_image_digest,
                 last_known_good_compose_hash, now),
            )
            await db.commit()
        finally:
            await db.close()

    # -- update_history ----------------------------------------------------

    async def record_update(
        self,
        host: str,
        stack: str,
        from_digest: str,
        to_digest: str,
        status: str,
        reason: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
        rollback_to_digest: str | None = None,
        manifest_digest: str | None = None,
        config_digest: str | None = None,
    ) -> int:
        if started_at is None:
            started_at = _now_iso()
        db = await self._connect()
        try:
            cur = await db.execute(
                """
                INSERT INTO update_history
                    (host, stack, from_digest, to_digest, manifest_digest, config_digest,
                     status, started_at, finished_at, rollback_to_digest, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (host, stack, from_digest, to_digest, manifest_digest, config_digest,
                 status, started_at, finished_at, rollback_to_digest, reason),
            )
            await db.commit()
            return cur.lastrowid or 0
        finally:
            await db.close()

    async def update_update(
        self,
        row_id: int,
        status: str,
        finished_at: str | None = None,
        reason: str | None = None,
        rollback_to_digest: str | None = None,
    ) -> None:
        if finished_at is None:
            finished_at = _now_iso()
        db = await self._connect()
        try:
            await db.execute(
                """
                UPDATE update_history
                SET status = ?,
                    finished_at = ?,
                    reason = COALESCE(?, reason),
                    rollback_to_digest = COALESCE(?, rollback_to_digest)
                WHERE id = ?
                """,
                (status, finished_at, reason, rollback_to_digest, row_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def last_known_good(self, host: str, stack: str) -> str | None:
        db = await self._connect()
        try:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT to_digest FROM update_history
                WHERE host = ? AND stack = ? AND status = 'applied'
                ORDER BY datetime(started_at) DESC
                LIMIT 1
                """,
                (host, stack),
            )
            row = await cur.fetchone()
            return row["to_digest"] if row else None
        finally:
            await db.close()

    async def list_update_history(
        self, host: str, stack: str, limit: int = 50
    ) -> list[dict]:
        db = await self._connect()
        try:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM update_history
                WHERE host = ? AND stack = ?
                ORDER BY datetime(started_at) DESC
                LIMIT ?
                """,
                (host, stack, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    # -- pending_updates ---------------------------------------------------

    async def record_pending_update(
        self,
        host: str,
        stack: str,
        current_digest: str,
        latest_digest: str,
    ) -> int:
        now = _now_iso()
        db = await self._connect()
        try:
            cur = await db.execute(
                """
                INSERT INTO pending_updates
                    (host, stack, current_digest, latest_digest, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (host, stack, latest_digest) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (host, stack, current_digest, latest_digest, now, now),
            )
            await db.commit()
            return cur.lastrowid or 0
        finally:
            await db.close()

    async def list_pending_updates(self, host: str | None = None) -> list[dict]:
        db = await self._connect()
        try:
            db.row_factory = aiosqlite.Row
            if host is None:
                cur = await db.execute(
                    "SELECT * FROM pending_updates ORDER BY host, stack"
                )
            else:
                cur = await db.execute(
                    "SELECT * FROM pending_updates WHERE host = ? ORDER BY stack",
                    (host,),
                )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def mark_update_seen(
        self, host: str, stack: str, latest_digest: str
    ) -> int:
        """Delete the pending row for (host, stack, latest_digest). Returns rows deleted."""
        db = await self._connect()
        try:
            cur = await db.execute(
                "DELETE FROM pending_updates WHERE host = ? AND stack = ? AND latest_digest = ?",
                (host, stack, latest_digest),
            )
            await db.commit()
            return cur.rowcount
        finally:
            await db.close()
