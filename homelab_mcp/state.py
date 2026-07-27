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

import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

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
    -- v0.9.10: one row per (host, stack). Previous schema was UNIQUE
    -- (host, stack, latest_digest), which accumulated one row per
    -- upstream rebuild (the apply pipeline only reads rows[0] so the
    -- rest were dead weight, bloating the dashboard count and hiding
    -- the real "which stacks are drifting" signal).
    UNIQUE (host, stack)
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
        # Normalize to pathlib.Path so callers can pass either
        # str or Path. init_db() does self._db_path.parent.mkdir(...)
        # which would fail on a plain str.
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    async def _connect(self):
        db = await aiosqlite.connect(self._db_path)
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return db

    async def init_db(self) -> None:
        """Create tables if they don't exist. Idempotent.

        Also runs the v0.9.10 ``pending_updates`` dedup migration if
        it hasn't been applied yet, and the v0.9.11 orphaned
        ``in_progress`` sweep on every startup (cheap, idempotent).
        The migration is gated on a user-version PRAGMA so it runs
        exactly once per database file even if init_db() is called
        multiple times.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await self._connect()
        try:
            await db.executescript(SCHEMA_SQL)
            # Check user_version: 0 means pre-migration schema is in
            # place; 1 means the dedup migration has been applied.
            cur = await db.execute("PRAGMA user_version")
            row = await cur.fetchone()
            version = int(row[0]) if row else 0
            if version < 1:
                # The migration does its own commit/rollback. We
                # commit the CREATE TABLE work above before we start
                # so the table the migration reads is the just-created
                # one (in case the user is on a very old DB that
                # doesn't have it yet).
                await db.commit()
                await db.close()
                result = await self.migrate_pending_updates_dedup()
                # Bump the user_version so we don't run the
                # migration again. We open a fresh connection so the
                # PRAGMA + UPDATE is committed atomically.
                db = await self._connect()
                await db.execute("PRAGMA user_version = 1")
                await db.commit()
                log.info("pending_updates dedup migration applied: %s", result)
                # v0.9.11: also sweep any orphaned in_progress rows
                # the moment the migration completes (in case the
                # daemon was restarted mid-apply in the past and the
                # rows predate this fix). Subsequent startups will
                # re-sweep on their own.
                swept = await self.sweep_orphaned_in_progress()
                if swept:
                    log.info(
                        "swept %d orphaned in_progress rows after migration",
                        len(swept),
                    )
            else:
                await db.commit()
            # v0.9.11: on every startup (cheap, idempotent), recover
            # any in_progress history rows left over from a daemon
            # crash. This is the per-startup half of the fix; the
            # migration branch above also runs it once for the case
            # where the migration itself just upgraded a database
            # that was full of orphans.
            swept = await self.sweep_orphaned_in_progress()
            if swept:
                log.info(
                    "swept %d orphaned in_progress rows at startup",
                    len(swept),
                )
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
        """Upsert a single pending row per (host, stack).

        v0.9.10: replaced the per-(host, stack, latest_digest) upsert
        with a per-(host, stack) upsert. ``current_digest`` and
        ``latest_digest`` are overwritten on conflict, ``first_seen_at``
        is preserved (it's the original detection time for this stack's
        drift), and ``last_seen_at`` is bumped to now. The apply
        pipeline already assumes "one row per stack" (``rows[0]``);
        this change makes the schema match the code's assumption.
        """
        now = _now_iso()
        db = await self._connect()
        try:
            cur = await db.execute(
                """
                INSERT INTO pending_updates
                    (host, stack, current_digest, latest_digest, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (host, stack) DO UPDATE SET
                    current_digest = excluded.current_digest,
                    latest_digest = excluded.latest_digest,
                    last_seen_at  = excluded.last_seen_at
                """,
                (host, stack, current_digest, latest_digest, now, now),
            )
            await db.commit()
            return cur.lastrowid or 0
        finally:
            await db.close()

    async def migrate_pending_updates_dedup(self) -> dict:
        """One-time migration for the v0.9.10 schema change.

        Three things happen here, in this order, all inside a single
        transaction so a crash mid-migration is recoverable on the
        next startup (the migration is idempotent):

        1. **Backfill the transition history** into ``update_history``
           with ``status='drift_observed'``. For each (host, stack)
           with N rows, write N-1 transition rows so the
           A→B→C→D chain isn't lost. Each transition uses the
           corresponding row's ``last_seen_at`` as the
           ``started_at`` so the timing is preserved.
        2. **Collapse to one row per (host, stack)**, keeping the row
           with the newest ``latest_digest`` (i.e. the most recent
           known state). ``first_seen_at`` is preserved from the row
           that the original scanner wrote when the stack first
           showed drift.
        3. **Drop the old auto-index** SQLite created for the previous
           unique constraint (``sqlite_autoindex_pending_updates_1``)
           and re-create the new index on ``(host, stack)`` so future
           scans stay fast.

        Returns a small dict with the migration's effect so callers
        can log it. The keys are ``stacks_collapsed``,
        ``transitions_backfilled``, and ``rows_before``.
        """
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            # Snapshot the current state for the effect-report
            cur = await db.execute("SELECT COUNT(*) FROM pending_updates")
            rows_before = int((await cur.fetchone())[0])

            # 1) Backfill transitions into update_history.
            #
            # For each (host, stack) with >1 row, sort by last_seen_at
            # ASC and emit one update_history row per consecutive pair.
            # The original pair (A, B) became (A, B) in pending; the
            # next observation (B, C) becomes a (B, C) transition.
            # status='drift_observed' distinguishes scanner-detected
            # drift from operator-applied updates.
            cur = await db.execute(
                """
                SELECT host, stack, current_digest, latest_digest,
                       first_seen_at, last_seen_at
                FROM pending_updates
                ORDER BY host, stack, last_seen_at ASC, id ASC
                """
            )
            grouped: dict[tuple[str, str], list[aiosqlite.Row]] = {}
            for r in await cur.fetchall():
                key = (r[0], r[1])  # (host, stack)
                grouped.setdefault(key, []).append(r)

            transitions_backfilled = 0
            for _key, rows_list in grouped.items():
                if len(rows_list) < 2:
                    continue
                for i in range(len(rows_list) - 1):
                    prev = rows_list[i]
                    cur_row = rows_list[i + 1]
                    # The transition we observed: local moved from
                    # prev.latest_digest to cur_row.latest_digest. The
                    # from_digest should be the previous observed
                    # latest, the to_digest the current observed
                    # latest. We use the cur_row's last_seen_at as
                    # the transition timestamp.
                    await db.execute(
                        """
                        INSERT INTO update_history
                            (host, stack, from_digest, to_digest,
                             status, started_at, finished_at, reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cur_row[0],         # host
                            cur_row[1],         # stack
                            prev[3],            # from_digest = prev latest
                            cur_row[3],         # to_digest = cur latest
                            "drift_observed",
                            cur_row[5],         # started_at = cur last_seen_at
                            cur_row[5],         # finished_at = same
                            "v0.9.10 pending_updates dedup backfill",
                        ),
                    )
                    transitions_backfilled += 1

            # 2) Collapse to one row per (host, stack), keeping the
            # newest by last_seen_at. We do this by recreating the
            # table rather than wrestling with the old unique index.
            await db.execute(
                """
                CREATE TABLE pending_updates_dedup (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host TEXT NOT NULL,
                    stack TEXT NOT NULL,
                    current_digest TEXT NOT NULL,
                    latest_digest TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE (host, stack)
                )
                """
            )
            await db.execute(
                """
                INSERT INTO pending_updates_dedup
                    (host, stack, current_digest, latest_digest,
                     first_seen_at, last_seen_at)
                SELECT host, stack, current_digest, latest_digest,
                       MIN(first_seen_at), MAX(last_seen_at)
                FROM pending_updates
                GROUP BY host, stack
                """
            )
            # SQLite names the auto-index after the original table
            # name, not the new one. Drop the old table, then the new
            # one becomes pending_updates. This is the standard
            # SQLite "rename and replace" pattern.
            await db.execute("DROP TABLE pending_updates")
            await db.execute("ALTER TABLE pending_updates_dedup RENAME TO pending_updates")

            # 3) Drop the old auto-index if it still exists (it
            # travels with the dropped table in most cases, but be
            # defensive in case the user has an older SQLite).
            with suppress(Exception):
                await db.execute(
                    "DROP INDEX IF EXISTS sqlite_autoindex_pending_updates_1"
                )

            await db.commit()
            stacks_collapsed = len(grouped)
            return {
                "rows_before": rows_before,
                "stacks_collapsed": stacks_collapsed,
                "transitions_backfilled": transitions_backfilled,
            }
        except Exception:
            await db.rollback()
            raise
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

    async def summary(self) -> dict:
        """Aggregate counts for /status endpoint.

        Returns the number of tracked stacks, the number of pending
        update rows, the timestamp of the last scan (if any), and
        the timestamp of the last completed update (if any). All
        queries are best-effort: if a table is missing, we report
        0 / null for that field.

        Fix 2026-07-18: ``last_scan_ts`` used to read from ``stacks``,
        but the scanner only writes to ``pending_updates`` (the ``stacks``
        table stays empty in normal operation). Fall back to
        ``pending_updates.last_seen_at`` so the field actually populates.
        """
        out: dict = {}

        async def _scalar(sql: str, params: tuple = ()) -> int | None:
            db = await self._connect()
            try:
                cur = await db.execute(sql, params)
                row = await cur.fetchone()
                return row[0] if row else None
            finally:
                await db.close()

        try:
            n = await _scalar("SELECT COUNT(*) FROM stacks")
            out["stacks"] = int(n or 0)
        except Exception:
            out["stacks"] = 0

        try:
            n = await _scalar("SELECT COUNT(*) FROM pending_updates")
            out["pending_updates"] = int(n or 0)
        except Exception:
            out["pending_updates"] = 0

        # Prefer pending_updates.last_seen_at (populated by every scan)
        # over stacks.last_checked_at (which the scanner does not write
        # in normal operation). If both are null, leave the field null.
        try:
            ts = await _scalar(
                "SELECT MAX(last_seen_at) FROM pending_updates"
            )
            if ts is None:
                ts = await _scalar(
                    "SELECT MAX(last_checked_at) FROM stacks"
                )
            out["last_scan_ts"] = ts
        except Exception:
            out["last_scan_ts"] = None

        try:
            out["last_applied_update_ts"] = await _scalar(
                "SELECT MAX(finished_at) FROM update_history "
                "WHERE status = 'applied'"
            )
        except Exception:
            out["last_applied_update_ts"] = None

        return out

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

    async def sweep_orphaned_in_progress(
        self, max_age_seconds: int = 600
    ) -> list[int]:
        """Recover apply rows left in 'in_progress' by a daemon crash.

        Background: the apply pipeline writes ``update_history`` rows
        with ``status='in_progress'`` at the start of an apply, then
        updates them to ``applied`` or ``rolled_back`` when the apply
        finishes. If the daemon is killed mid-apply (OOM, host
        restart, force-recreate) the row is left dangling. The next
        scan will re-add a pending row for the same drift, but the
        ``in_progress`` history row blocks the operator from seeing
        the actual current state of that stack.

        This sweep runs at startup (called from ``init_db`` after the
        v0.9.10 migration) and marks any ``in_progress`` row older
        than ``max_age_seconds`` as ``rolled_back`` with a clear
        reason. The default 600s is conservative: even the slowest
        legitimate apply finishes in well under 60s; anything still
        ``in_progress`` after 10 minutes is orphaned.

        Returns the list of ``row_id`` values that were swept, so
        callers can log them. Idempotent — a second call is a no-op
        because the rows are no longer ``in_progress``.
        """
        cutoff = _now_iso()  # naive: a real cutoff is "now - max_age_seconds"
        # SQLite doesn't have date arithmetic in stock builds, and
        # ISO 8601 strings sort lexically the same way they sort
        # chronologically (when they're in the same format with
        # consistent width). So a string-comparison cutoff is correct
        # here. We just need to subtract max_age_seconds.
        from datetime import timedelta
        cutoff_dt = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        db = await self._connect()
        try:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT id, host, stack, from_digest, to_digest, started_at
                FROM update_history
                WHERE status = 'in_progress'
                  AND started_at < ?
                ORDER BY id ASC
                """,
                (cutoff,),
            )
            orphans = [dict(r) for r in await cur.fetchall()]
            if not orphans:
                return []
            now = _now_iso()
            for row in orphans:
                await db.execute(
                    """
                    UPDATE update_history
                    SET status = 'rolled_back',
                        finished_at = ?,
                        reason = COALESCE(reason, '') ||
                                 ' | orphaned by daemon restart, auto-recovered at ' || ?
                    WHERE id = ?
                    """,
                    (now, now, row["id"]),
                )
            await db.commit()
            log.info(
                "swept %d orphaned in_progress rows: %s",
                len(orphans), [r["id"] for r in orphans],
            )
            return [r["id"] for r in orphans]
        finally:
            await db.close()
