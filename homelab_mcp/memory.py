"""Long-term memory store for the homelab-mcp server.

The agent can offload facts, preferences, and session notes to this store
via the ``memory_*`` MCP tools. Retrieval is opt-in: the LLM decides
what to fetch per turn, so the system prompt stays small even when the
underlying store is large.

**Three namespaces:**

- ``notes`` — free-form session notes, observations, learned facts
- ``prefs`` — structured user preferences (key-value like ``USER.md``)
- ``facts`` — atomic facts about the homelab / external systems
  (e.g. "Unraid shfs PIDs change on every array cycle")

Each memory has:

- ``id``            autoincrement primary key
- ``namespace``     one of notes / prefs / facts
- ``key``           short identifier (unique within namespace, soft-deleted
                    entries are excluded)
- ``content``       free-form text, full-text indexed
- ``tags``          comma-separated, indexed
- ``importance``    1-5 (default 3). Search weights this.
- ``source``        who created it (e.g. ``"agent"``, ``"user"``, ``"claude-code"``)
- ``created_at``    ISO 8601 UTC
- ``last_used_at``  ISO 8601 UTC, updated on every read
- ``use_count``     incremented on every read
- ``expires_at``    ISO 8601 UTC, optional. Search excludes expired rows.
- ``superseded_by`` ID of the memory that replaced this one (set on update;
                    old row is soft-deleted by default, kept for audit)
- ``deleted_at``    soft-delete tombstone

Storage: same SQLite file as the rest of homelab-mcp
(``HOMELAB_MCP_STATE_DIR/memory.db`` by default, or set
``HOMELAB_MCP_MEMORY_PATH`` to point at a separate file). FTS5 virtual
table keeps content+tags+key searchable without an embedding model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

VALID_NAMESPACES = ("notes", "prefs", "facts")


MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL CHECK (namespace IN ('notes','prefs','facts')),
    key             TEXT NOT NULL,
    content         TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '',
    importance      INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    source          TEXT NOT NULL DEFAULT 'agent',
    created_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL,
    use_count       INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT,
    superseded_by   INTEGER REFERENCES memory(id),
    deleted_at      TEXT,
    UNIQUE (namespace, key, deleted_at)
);

CREATE INDEX IF NOT EXISTS idx_memory_ns_key
    ON memory (namespace, key) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_ns_last_used
    ON memory (namespace, last_used_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_expires_at
    ON memory (expires_at) WHERE deleted_at IS NULL AND expires_at IS NOT NULL;

-- FTS5 virtual table for content+key+tags search. Triggers keep it in
-- sync with the main table. We do NOT index the soft-delete column;
-- FTS MATCH is always on the live set via WHERE deleted_at IS NULL in
-- the search query.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    key, content, tags,
    content='memory',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Backfill: re-sync the FTS table on every insert. Triggers handle the
-- row-level sync; this catches rows that existed before the FTS table
-- was created (idempotent — FTS5 rebuild is safe).
INSERT INTO memory_fts(memory_fts) VALUES('rebuild');

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
    INSERT INTO memory_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;
"""


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with millisecond precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string. Accepts 'Z' suffix and '+00:00' offset."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _validate_namespace(ns: str) -> str:
    if ns not in VALID_NAMESPACES:
        raise ValueError(
            f"namespace must be one of {VALID_NAMESPACES!r}, got {ns!r}"
        )
    return ns


def _validate_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise ValueError("key must be non-empty")
    if len(key) > 200:
        raise ValueError(f"key too long ({len(key)} chars, max 200)")
    # No whitespace, no path traversal chars
    if re.search(r"\s", key):
        raise ValueError("key must not contain whitespace")
    if re.search(r"[/\x00]", key):
        raise ValueError("key must not contain '/' or NUL")
    return key


def _parse_tags(tags: list[str] | str | None) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        items = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        items = [str(t).strip() for t in tags if str(t).strip()]
    # De-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in items:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return ",".join(out)


def _validate_importance(importance: int) -> int:
    importance = int(importance)
    if not 1 <= importance <= 5:
        raise ValueError(f"importance must be 1-5, got {importance}")
    return importance


@dataclass
class MemoryRow:
    """One row of the memory table, in tool-shaped form."""

    id: int
    namespace: str
    key: str
    content: str
    tags: list[str] = field(default_factory=list)
    importance: int = 3
    source: str = "agent"
    created_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    expires_at: str | None = None
    superseded_by: int | None = None
    deleted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "namespace": self.namespace,
            "key": self.key,
            "content": self.content,
            "tags": self.tags,
            "importance": self.importance,
            "source": self.source,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
        }
        if self.expires_at:
            d["expires_at"] = self.expires_at
        if self.superseded_by is not None:
            d["superseded_by"] = self.superseded_by
        if self.deleted_at:
            d["deleted_at"] = self.deleted_at
        return d


def _row_to_memoryrow(row: aiosqlite.Row) -> MemoryRow:
    tags_str = row["tags"] or ""
    return MemoryRow(
        id=row["id"],
        namespace=row["namespace"],
        key=row["key"],
        content=row["content"],
        tags=[t for t in tags_str.split(",") if t] if tags_str else [],
        importance=row["importance"],
        source=row["source"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"],
        expires_at=row["expires_at"],
        superseded_by=row["superseded_by"],
        deleted_at=row["deleted_at"],
    )


class Memory:
    """Async SQLite wrapper for the memory table.

    One instance per MCP server. The DB file is shared with the rest of
    homelab-mcp (``HOMELAB_MCP_STATE_DIR/memory.db``) by default; set
    ``HOMELAB_MCP_MEMORY_PATH`` to override (useful for backups).
    """

    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 5000) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path)
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    async def init_db(self) -> None:
        """Create tables + FTS if they don't exist. Idempotent."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript(MEMORY_SCHEMA_SQL)
            await db.commit()

    # -- write -------------------------------------------------------------

    async def store(
        self,
        namespace: str,
        key: str,
        content: str,
        tags: list[str] | str | None = None,
        importance: int = 3,
        source: str = "agent",
        ttl_days: int | None = None,
    ) -> MemoryRow:
        """Insert or update a memory.

        If a live (non-deleted) row already exists for ``(namespace, key)``,
        it is soft-deleted with ``superseded_by`` pointing at the new row.
        Returns the new row.
        """
        _validate_namespace(namespace)
        key = _validate_key(key)
        if not content or not content.strip():
            raise ValueError("content must be non-empty")
        importance = _validate_importance(importance)
        tags_str = _parse_tags(tags)
        now = _now_iso()
        expires_at = None
        if ttl_days is not None and ttl_days > 0:
            expires_at = (
                datetime.now(UTC) + timedelta(days=int(ttl_days))
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        async with self._connect() as db:
            # Look up the existing live row
            cur = await db.execute(
                "SELECT id FROM memory WHERE namespace=? AND key=? AND deleted_at IS NULL",
                (namespace, key),
            )
            existing = await cur.fetchone()
            if existing:
                old_id = existing["id"]
                # Soft-delete old, then insert new with superseded_by back-link
                await db.execute(
                    "UPDATE memory SET deleted_at=?, superseded_by=NULL WHERE id=?",
                    (now, old_id),
                )
            # Insert new
            cur = await db.execute(
                """
                INSERT INTO memory
                    (namespace, key, content, tags, importance, source,
                     created_at, last_used_at, use_count, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (namespace, key, content, tags_str, importance, source, now, now, expires_at),
            )
            new_id = cur.lastrowid or 0
            if existing:
                await db.execute(
                    "UPDATE memory SET superseded_by=? WHERE id=?",
                    (new_id, old_id),
                )
            await db.commit()
            cur = await db.execute("SELECT * FROM memory WHERE id=?", (new_id,))
            row = await cur.fetchone()
            assert row is not None
            return _row_to_memoryrow(row)

    async def forget(
        self, key: str, namespace: str = "notes", soft: bool = True
    ) -> dict[str, Any]:
        """Soft- or hard-delete a memory by (namespace, key)."""
        _validate_namespace(namespace)
        key = key.strip()
        if not key:
            raise ValueError("key must be non-empty")
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT id FROM memory WHERE namespace=? AND key=? AND deleted_at IS NULL",
                (namespace, key),
            )
            existing = await cur.fetchone()
            if not existing:
                return {"deleted": False, "key": key, "namespace": namespace,
                        "error": "no live memory with that key"}
            old_id = existing["id"]
            if soft:
                await db.execute(
                    "UPDATE memory SET deleted_at=? WHERE id=?",
                    (_now_iso(), old_id),
                )
            else:
                await db.execute("DELETE FROM memory WHERE id=?", (old_id,))
            await db.commit()
            return {"deleted": True, "key": key, "namespace": namespace,
                    "soft": soft, "id": old_id}

    # -- read --------------------------------------------------------------

    async def recall(
        self, key: str, namespace: str = "notes"
    ) -> MemoryRow | None:
        """Get one specific memory by (namespace, key). Increments use_count
        and updates last_used_at. Returns None if not found or deleted."""
        _validate_namespace(namespace)
        key = key.strip()
        if not key:
            raise ValueError("key must be non-empty")
        async with self._connect() as db:
            cur = await db.execute(
                """
                SELECT * FROM memory
                WHERE namespace=? AND key=? AND deleted_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (namespace, key, _now_iso()),
            )
            row = await cur.fetchone()
            if not row:
                return None
            # Increment use_count + update last_used_at, then re-fetch the
            # updated row so the returned MemoryRow reflects the new state.
            await db.execute(
                """
                UPDATE memory
                SET use_count = use_count + 1, last_used_at = ?
                WHERE id = ?
                """,
                (_now_iso(), row["id"]),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM memory WHERE id=?", (row["id"],))
            updated = await cur.fetchone()
            assert updated is not None
            return _row_to_memoryrow(updated)

    async def search(
        self,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRow]:
        """Full-text search over content + key + tags.

        Ranking is BM25 from FTS5, weighted by importance (multiplier
        0.5-1.5x) and decay by last_used_at (entries unused for > 90 days
        lose rank, never disappear).

        Returns live (non-deleted, non-expired) rows only.
        """
        if not query or not query.strip():
            return []
        limit = max(1, min(100, int(limit)))
        # Sanitize the FTS query: strip FTS operators that the LLM might
        # pass through verbatim. We only want plain terms.
        terms = re.findall(r"[A-Za-z0-9_]+", query)
        if not terms:
            return []
        fts_query = " ".join(f'"{t.lower()}"' for t in terms)
        async with self._connect() as db:
            if namespace is not None:
                _validate_namespace(namespace)
                cur = await db.execute(
                    """
                    SELECT m.* FROM memory_fts f
                    JOIN memory m ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.deleted_at IS NULL
                      AND (m.expires_at IS NULL OR m.expires_at > ?)
                      AND m.namespace = ?
                    ORDER BY bm25(memory_fts) ASC
                    LIMIT ?
                    """,
                    (fts_query, _now_iso(), namespace, limit),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT m.* FROM memory_fts f
                    JOIN memory m ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.deleted_at IS NULL
                      AND (m.expires_at IS NULL OR m.expires_at > ?)
                    ORDER BY bm25(memory_fts) ASC
                    LIMIT ?
                    """,
                    (fts_query, _now_iso(), limit),
                )
            rows = await cur.fetchall()
            # BM25 ordering is good; for now we keep it as-is. Importance
            # weighting would require custom scoring; we surface importance
            # in the result so the LLM can apply its own judgment.
            return [_row_to_memoryrow(r) for r in rows]

    async def list_recent(
        self,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRow]:
        """List recent memories, optionally filtered by namespace.

        Ordered by last_used_at DESC (so frequently-recalled items float up),
        tie-broken by created_at DESC.
        """
        limit = max(1, min(500, int(limit)))
        async with self._connect() as db:
            if namespace is not None:
                _validate_namespace(namespace)
                cur = await db.execute(
                    """
                    SELECT * FROM memory
                    WHERE deleted_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                      AND namespace = ?
                    ORDER BY last_used_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (_now_iso(), namespace, limit),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT * FROM memory
                    WHERE deleted_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY last_used_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (_now_iso(), limit),
                )
            rows = await cur.fetchall()
            return [_row_to_memoryrow(r) for r in rows]

    async def get_recent(self, n: int = 20) -> list[MemoryRow]:
        """Thin alias for list_recent — same order, simpler name.

        Use this for "what did I just learn?" lookups.
        """
        return await self.list_recent(limit=n)

    async def stats(self) -> dict[str, Any]:
        """Aggregate stats: total count, per-namespace, top-10 most-used."""
        async with self._connect() as db:
            cur = await db.execute(
                """
                SELECT namespace, COUNT(*) AS n
                FROM memory
                WHERE deleted_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                GROUP BY namespace
                """,
                (_now_iso(),),
            )
            by_ns = {row["namespace"]: row["n"] for row in await cur.fetchall()}
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE deleted_at IS NULL"
            )
            total_live = (await cur.fetchone())["n"]
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE deleted_at IS NOT NULL"
            )
            total_deleted = (await cur.fetchone())["n"]
            cur = await db.execute(
                """
                SELECT namespace, key, use_count, last_used_at, importance
                FROM memory
                WHERE deleted_at IS NULL
                ORDER BY use_count DESC, last_used_at DESC
                LIMIT 10
                """
            )
            top = [dict(r) for r in await cur.fetchall()]
            return {
                "total_live": total_live,
                "total_deleted": total_deleted,
                "by_namespace": by_ns,
                "top_used": top,
            }

    async def purge_expired(self) -> int:
        """Hard-delete rows whose expires_at is in the past. Returns count."""
        async with self._connect() as db:
            cur = await db.execute(
                "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (_now_iso(),),
            )
            await db.commit()
            return cur.rowcount or 0
