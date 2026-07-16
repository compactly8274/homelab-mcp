"""Tests for the apply_all_pending, get_update_history, dismiss_all_pending tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.tools.apply_all_pending import apply_all_pending_tool
from homelab_mcp.tools.dismiss_all_pending import dismiss_all_pending_tool
from homelab_mcp.tools.get_update_history import get_update_history_tool

# --- apply_all_pending --------------------------------------------------


async def test_apply_all_pending_with_no_rows() -> None:
    """Empty pending list → empty result, no errors."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.apply_all_pending.get_state", return_value=fake_state):
        result = await apply_all_pending_tool(host="truenas")
    assert result["processed"] == 0
    assert result["results"] == []
    assert "no pending updates" in result["message"]


async def test_apply_all_pending_processes_each_row_in_isolation() -> None:
    """A failure in one row doesn't stop the others from being processed."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:ccc",
         "latest_digest": "sha256:ddd", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
        {"host": "truenas", "stack": "jellyfin", "current_digest": "sha256:eee",
         "latest_digest": "sha256:fff", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])

    # Mock apply_update_tool to return varied outcomes keyed on the
    # real stack names from the test fixtures.
    outcomes = {
        "nextcloud": {"action": "applied", "verdict": {"risk": "SAFE"}},
        "immich":    {"action": "notified_breaking", "verdict": {"risk": "BREAKING"}},
        "jellyfin":  {"action": "failed", "error": "docker socket down"},
    }

    async def fake_apply(host: str, stack: str, force: bool = False, dry_run: bool = False) -> dict:
        return outcomes.get(stack, {"action": "failed", "error": "unknown"})

    with patch("homelab_mcp.tools.apply_all_pending.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_all_pending.apply_update_tool",
               side_effect=fake_apply):
        result = await apply_all_pending_tool(host="truenas")

    assert result["processed"] == 3
    assert result["applied"] == 1
    assert result["notified_breaking"] == 1
    assert result["failed"] == 1
    assert len(result["results"]) == 3


async def test_apply_all_pending_respects_max_rows() -> None:
    """The max_rows cap prevents runaway applies."""
    fake_state = MagicMock()
    # 100 pending rows
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": f"stack-{i}", "current_digest": "sha256:aaa",
         "latest_digest": f"sha256:bbb{i}", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"}
        for i in range(100)
    ])

    with patch("homelab_mcp.tools.apply_all_pending.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_all_pending.apply_update_tool",
               AsyncMock(return_value={"action": "applied", "verdict": {"risk": "SAFE"}})):
        result = await apply_all_pending_tool(host="truenas", max_rows=10)

    assert result["processed"] == 10


async def test_apply_all_pending_passes_force_through() -> None:
    """force=True at the bulk level is passed to each per-row apply."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    apply_mock = AsyncMock(return_value={"action": "applied", "verdict": {"risk": "BREAKING"}})

    with patch("homelab_mcp.tools.apply_all_pending.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_all_pending.apply_update_tool", apply_mock):
        await apply_all_pending_tool(host="truenas", force=True)

    # Check the call passed force=True
    call = apply_mock.call_args
    assert call.kwargs.get("force") is True or (len(call.args) > 2 and call.args[2] is True)


async def test_apply_all_pending_skips_rows_with_no_stack() -> None:
    """Rows with no stack name are skipped (defensive)."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    with patch("homelab_mcp.tools.apply_all_pending.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_all_pending.apply_update_tool",
               AsyncMock(return_value={"action": "applied"})):
        result = await apply_all_pending_tool(host="truenas")
    assert result["processed"] == 0  # row with no stack skipped


# --- get_update_history -------------------------------------------------


async def test_get_update_history_returns_state_rows() -> None:
    """The tool just forwards the result from state.list_update_history."""
    fake_state = MagicMock()
    expected = [
        {"id": 1, "from_digest": "sha256:aaa", "to_digest": "sha256:bbb",
         "status": "applied", "started_at": "2026-07-15T10:00:00Z",
         "finished_at": "2026-07-15T10:01:00Z", "rollback_to_digest": None,
         "reason": ""},
        {"id": 2, "from_digest": "sha256:ccc", "to_digest": "sha256:ddd",
         "status": "rolled_back", "started_at": "2026-07-14T10:00:00Z",
         "finished_at": "2026-07-14T10:01:00Z", "rollback_to_digest": "sha256:ccc",
         "reason": "healthcheck failed"},
    ]
    fake_state.list_update_history = AsyncMock(return_value=expected)
    with patch("homelab_mcp.tools.get_update_history.get_state", return_value=fake_state):
        result = await get_update_history_tool(host="truenas", stack="nextcloud")
    assert result == expected
    fake_state.list_update_history.assert_awaited_once_with(
        host="truenas", stack="nextcloud", limit=20
    )


async def test_get_update_history_caps_limit_at_200() -> None:
    """The limit is hard-capped at 200 to bound response size."""
    fake_state = MagicMock()
    fake_state.list_update_history = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.get_update_history.get_state", return_value=fake_state):
        await get_update_history_tool(host="truenas", stack="x", limit=9999)
    call = fake_state.list_update_history.call_args
    assert call.kwargs["limit"] == 200


async def test_get_update_history_clamps_limit_to_at_least_1() -> None:
    """A limit of 0 or negative is clamped to 1."""
    fake_state = MagicMock()
    fake_state.list_update_history = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.get_update_history.get_state", return_value=fake_state):
        await get_update_history_tool(host="truenas", stack="x", limit=0)
    call = fake_state.list_update_history.call_args
    assert call.kwargs["limit"] == 1


# --- dismiss_all_pending -----------------------------------------------


async def test_dismiss_all_pending_dismisses_every_row_for_host() -> None:
    """With stack=None, all rows for the host are dismissed."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "latest_digest": "sha256:bbb"},
        {"host": "truenas", "stack": "immich", "latest_digest": "sha256:ddd"},
        {"host": "truenas", "stack": "jellyfin", "latest_digest": "sha256:fff"},
    ])
    fake_state.mark_update_seen = AsyncMock(return_value=1)
    with patch("homelab_mcp.tools.dismiss_all_pending.get_state", return_value=fake_state):
        result = await dismiss_all_pending_tool(host="truenas")
    assert result["dismissed"] == 3
    assert len(result["rows"]) == 3
    assert result["stack"] is None
    assert fake_state.mark_update_seen.await_count == 3


async def test_dismiss_all_pending_filters_by_stack_when_given() -> None:
    """With stack='nextcloud', only matching rows are dismissed."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "latest_digest": "sha256:bbb"},
        {"host": "truenas", "stack": "immich", "latest_digest": "sha256:ddd"},
    ])
    fake_state.mark_update_seen = AsyncMock(return_value=1)
    with patch("homelab_mcp.tools.dismiss_all_pending.get_state", return_value=fake_state):
        result = await dismiss_all_pending_tool(host="truenas", stack="nextcloud")
    assert result["dismissed"] == 1
    assert len(result["rows"]) == 1
    assert result["rows"][0]["stack"] == "nextcloud"


async def test_dismiss_all_pending_handles_per_row_failures() -> None:
    """A mark_update_seen failure on one row doesn't stop the others."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "latest_digest": "sha256:bbb"},
        {"host": "truenas", "stack": "immich", "latest_digest": "sha256:ddd"},
    ])

    async def fake_mark(host: str, stack: str, latest_digest: str) -> int:
        if stack == "nextcloud":
            raise RuntimeError("db locked")
        return 1

    fake_state.mark_update_seen = AsyncMock(side_effect=fake_mark)
    with patch("homelab_mcp.tools.dismiss_all_pending.get_state", return_value=fake_state):
        result = await dismiss_all_pending_tool(host="truenas")
    # immich was dismissed; nextcloud was not (it raised)
    assert result["dismissed"] == 1
    assert len(result["rows"]) == 1
    assert result["rows"][0]["stack"] == "immich"


async def test_dismiss_all_pending_skips_zero_rowcount_returns() -> None:
    """If mark_update_seen returns 0 (no row matched), we don't count it."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "latest_digest": "sha256:bbb"},
    ])
    fake_state.mark_update_seen = AsyncMock(return_value=0)  # nothing deleted
    with patch("homelab_mcp.tools.dismiss_all_pending.get_state", return_value=fake_state):
        result = await dismiss_all_pending_tool(host="truenas")
    assert result["dismissed"] == 0
    assert result["rows"] == []


async def test_dismiss_all_pending_with_no_rows() -> None:
    """No pending rows → 0 dismissed, no error."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.dismiss_all_pending.get_state", return_value=fake_state):
        result = await dismiss_all_pending_tool(host="truenas")
    assert result["dismissed"] == 0
    assert result["rows"] == []
