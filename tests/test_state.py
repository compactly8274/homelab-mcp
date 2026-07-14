"""Tests for the async SQLite state layer."""

from pathlib import Path

import pytest

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
