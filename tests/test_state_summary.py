"""Tests for the State.summary() method."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from homelab_mcp.state import State


@pytest.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        s = State(db_path=db_path)
        await s.init_db()
        yield s


async def test_summary_on_empty_db(state: State) -> None:
    out = await state.summary()
    assert out["stacks"] == 0
    assert out["pending_updates"] == 0
    assert out["last_scan_ts"] is None
    assert out["last_applied_update_ts"] is None


async def test_summary_counts_stacks(state: State) -> None:
    await state.upsert_stack("truenas", "immich", path="/srv/immich")
    await state.upsert_stack("truenas", "nextcloud", path="/srv/nextcloud")
    await state.upsert_stack("unraid", "plex", path="/mnt/user/appdata/plex")
    out = await state.summary()
    assert out["stacks"] == 3


async def test_summary_counts_pending_updates(state: State) -> None:
    await state.record_pending_update("truenas", "immich", "sha256:aaa", "sha256:bbb")
    await state.record_pending_update("truenas", "nextcloud", "sha256:ccc", "sha256:ddd")
    await state.record_pending_update("unraid", "plex", "sha256:eee", "sha256:fff")
    out = await state.summary()
    assert out["pending_updates"] == 3


async def test_summary_tracks_last_scan_ts(state: State) -> None:
    await state.upsert_stack("truenas", "immich", path="/srv/immich")
    out = await state.summary()
    assert out["last_scan_ts"] is not None
    assert "T" in out["last_scan_ts"]


async def test_summary_tracks_last_applied_update_ts(state: State) -> None:
    await state.record_update(
        host="truenas", stack="immich",
        from_digest="sha256:aaa", to_digest="sha256:bbb",
        status="applied", finished_at="2026-07-15T12:00:00Z",
    )
    out = await state.summary()
    assert out["last_applied_update_ts"] == "2026-07-15T12:00:00Z"


async def test_summary_ignores_failed_updates(state: State) -> None:
    await state.record_update(
        host="truenas", stack="immich",
        from_digest="sha256:aaa", to_digest="sha256:bbb",
        status="failed", finished_at="2026-07-15T11:00:00Z",
    )
    out = await state.summary()
    assert out["last_applied_update_ts"] is None


async def test_summary_ignores_rolled_back_updates(state: State) -> None:
    await state.record_update(
        host="truenas", stack="immich",
        from_digest="sha256:aaa", to_digest="sha256:bbb",
        status="rolled_back", finished_at="2026-07-15T11:00:00Z",
    )
    out = await state.summary()
    assert out["last_applied_update_ts"] is None
