"""Tests for the async SQLite state layer."""

from pathlib import Path

from homelab_mcp.state import State, UpdateRow


async def test_state_init_creates_tables(tmp_path: Path) -> None:
    """init_db creates the three expected tables."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    # A second call must be idempotent
    await state.init_db()


async def test_upsert_stack_insert_then_update(tmp_path: Path) -> None:
    """Upserting a stack twice: first creates, second updates."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.upsert_stack(
        host="unraid", name="radarr",
        path="/srv/radarr", managed_by="compose.manager",
        current_image_digest="sha256:aaa",
    )
    await state.upsert_stack(
        host="unraid", name="radarr",
        current_image_digest="sha256:bbb",  # different digest
    )


async def test_record_pending_update_idempotent(tmp_path: Path) -> None:
    """Recording the same (host, stack, latest_digest) twice doesn't duplicate."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    rows = await state.list_pending_updates()
    assert len(rows) == 1


async def test_mark_update_seen_removes_row(tmp_path: Path) -> None:
    """mark_update_seen deletes a pending row and returns the count."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    deleted = await state.mark_update_seen("unraid", "radarr", "sha256:bbb")
    assert deleted == 1
    assert await state.list_pending_updates() == []


async def test_record_and_update_history(tmp_path: Path) -> None:
    """The full lifecycle: record → update status → read back."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    row_id = await state.record_update(
        host="unraid", stack="radarr",
        from_digest="sha256:aaa", to_digest="sha256:bbb",
        status="in_progress",
    )
    await state.update_update(row_id=row_id, status="applied")

    rows = await state.list_update_history("unraid", "radarr")
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["from_digest"] == "sha256:aaa"


async def test_last_known_good_returns_most_recent_applied(tmp_path: Path) -> None:
    """last_known_good returns the most recent applied to_digest."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_update(
        host="unraid", stack="radarr",
        from_digest="sha256:aaa", to_digest="sha256:bbb",
        status="applied",
    )
    await state.record_update(
        host="unraid", stack="radarr",
        from_digest="sha256:bbb", to_digest="sha256:ccc",
        status="applied",
    )
    lkg = await state.last_known_good("unraid", "radarr")
    assert lkg == "sha256:ccc"


async def test_update_row_dataclass() -> None:
    """UpdateRow is a typed dataclass."""
    r = UpdateRow(
        id=1, host="unraid", stack="radarr",
        from_digest="sha256:a", to_digest="sha256:b",
        status="applied", started_at="2026-07-09T00:00:00Z",
        finished_at="2026-07-09T00:01:00Z",
        rollback_to_digest=None, reason="",
        manifest_digest="sha256:b", config_digest="sha256:c",
    )
    assert r.id == 1
    assert r.from_digest == "sha256:a"
    assert r.manifest_digest == "sha256:b"
    assert r.config_digest == "sha256:c"
    assert r.status == "applied"


async def test_record_pending_update_replaces_on_latest_change(
    tmp_path: Path,
) -> None:
    """v0.9.10: re-recording the same (host, stack) with a different
    latest_digest replaces the existing row in place. Pre-v0.9.10
    this would have created a second row.
    """
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:bbb", latest_digest="sha256:ccc",
    )
    rows = await state.list_pending_updates()
    assert len(rows) == 1
    assert rows[0]["current_digest"] == "sha256:bbb"
    assert rows[0]["latest_digest"] == "sha256:ccc"


async def test_record_pending_update_preserves_first_seen(
    tmp_path: Path,
) -> None:
    """v0.9.10: re-recording the same (host, stack) keeps the
    original first_seen_at; only last_seen_at is bumped.
    """
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    rows = await state.list_pending_updates()
    first_seen_initial = rows[0]["first_seen_at"]
    last_seen_initial = rows[0]["last_seen_at"]

    # The next record (different latest_digest) should bump
    # last_seen_at but preserve first_seen_at.
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:bbb", latest_digest="sha256:ccc",
    )
    rows = await state.list_pending_updates()
    assert rows[0]["first_seen_at"] == first_seen_initial
    assert rows[0]["last_seen_at"] >= last_seen_initial


async def test_migrate_pending_updates_dedup_collapses_and_backfills(
    tmp_path: Path,
) -> None:
    """v0.9.10 migration: simulates the pre-fix bug by inserting
    three rows for the same (host, stack) (A→B, B→C, C→D), then
    runs the migration. Expects: one pending row remaining
    (C→D, the newest state), two transitions backfilled into
    update_history (B→C, C→D) with status='drift_observed'.
    """
    state = State(db_path=tmp_path / "state.db")
    # Bypass init_db's auto-migration so we can simulate the
    # pre-migration state directly. We do this by inserting into
    # the table that the OLD schema would have created.
    db = await state._connect()
    try:
        await db.executescript("""
            CREATE TABLE pending_updates_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                stack TEXT NOT NULL,
                current_digest TEXT NOT NULL,
                latest_digest TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (host, stack, latest_digest)
            );
            CREATE TABLE update_history (
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
        """)
        # Insert three drift rows that simulate the bug: each one is
        # a separate (host, stack, latest_digest) tuple.
        await db.execute(
            """
            INSERT INTO pending_updates_old
                (host, stack, current_digest, latest_digest,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("unraid", "radarr", "sha256:aaa", "sha256:bbb",
             "2026-07-20T00:00:00Z", "2026-07-20T00:00:00Z"),
        )
        await db.execute(
            """
            INSERT INTO pending_updates_old
                (host, stack, current_digest, latest_digest,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("unraid", "radarr", "sha256:bbb", "sha256:ccc",
             "2026-07-21T00:00:00Z", "2026-07-21T00:00:00Z"),
        )
        await db.execute(
            """
            INSERT INTO pending_updates_old
                (host, stack, current_digest, latest_digest,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("unraid", "radarr", "sha256:ccc", "sha256:ddd",
             "2026-07-22T00:00:00Z", "2026-07-22T00:00:00Z"),
        )
        # And a single-row stack (should be left alone except for
        # the dedup table swap).
        await db.execute(
            """
            INSERT INTO pending_updates_old
                (host, stack, current_digest, latest_digest,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("unraid", "sonarr", "sha256:111", "sha256:222",
             "2026-07-22T00:00:00Z", "2026-07-22T00:00:00Z"),
        )
        await db.commit()
    finally:
        await db.close()

    # Rename the old table to the live name so migrate_pending_updates_dedup
    # can read it. The migration does its own swap at the end.
    db = await state._connect()
    try:
        await db.execute("ALTER TABLE pending_updates_old RENAME TO pending_updates")
        await db.commit()
    finally:
        await db.close()

    # Run the migration.
    result = await state.migrate_pending_updates_dedup()

    assert result["rows_before"] == 4
    assert result["stacks_collapsed"] == 2
    assert result["transitions_backfilled"] == 2  # radarr had 3 rows -> 2 transitions

    # The remaining pending rows: one per stack.
    rows = await state.list_pending_updates()
    assert len(rows) == 2
    radarr = next(r for r in rows if r["stack"] == "radarr")
    sonarr = next(r for r in rows if r["stack"] == "sonarr")
    assert radarr["current_digest"] == "sha256:ccc"
    assert radarr["latest_digest"] == "sha256:ddd"
    assert radarr["first_seen_at"] == "2026-07-20T00:00:00Z"  # earliest preserved
    assert sonarr["latest_digest"] == "sha256:222"

    # update_history has the two backfilled transitions. The
    # list_update_history query orders DESC by started_at, so the
    # most recent transition comes first.
    history = await state.list_update_history("unraid", "radarr")
    drift_rows = [h for h in history if h["status"] == "drift_observed"]
    assert len(drift_rows) == 2
    # Build a dict keyed by to_digest so the assertions are
    # order-independent — there are only two transitions and
    # we know their digests from the test setup.
    by_to = {h["to_digest"]: h for h in drift_rows}
    assert by_to["sha256:ccc"]["from_digest"] == "sha256:bbb"
    assert by_to["sha256:ddd"]["from_digest"] == "sha256:ccc"


async def test_migrate_pending_updates_dedup_is_idempotent(
    tmp_path: Path,
) -> None:
    """Running the migration twice is a no-op the second time.

    The init_db path uses PRAGMA user_version to gate the migration
    call, but the underlying function should also be safe to call
    manually because some operators invoke it from cron / one-off
    scripts during a rollback. Calling it on a post-migration DB
    (where every (host, stack) already has exactly one row) should
    report 0 rows collapsed and 0 transitions backfilled.
    """
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:aaa", latest_digest="sha256:bbb",
    )
    result = await state.migrate_pending_updates_dedup()
    assert result["rows_before"] == 1
    assert result["stacks_collapsed"] == 1
    assert result["transitions_backfilled"] == 0
    # Second call: no work to do.
    result2 = await state.migrate_pending_updates_dedup()
    assert result2["rows_before"] == 1
    assert result2["stacks_collapsed"] == 1
    assert result2["transitions_backfilled"] == 0
