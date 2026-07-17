"""Tests for the preflight gate integration in apply_update_tool.

The gate is the enforcement that turns preflight from "advisory"
into "blocking". A call to apply_update_tool with
require_approval=True (the default) must check preflight first
and return {action: "blocked"} if there are any blockers.

These tests use the real preflight module (no mocking of the
gate itself) to ensure the integration works end-to-end.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


def _settings(**kwargs: Any) -> Any:
    s = MagicMock()
    s.ntfy_topic = ""
    s.ntfy_url = "http://ntfy"
    s.ntfy_priority = "default"
    s.pushover_app_token = ""
    s.pushover_user_key = ""
    s.pushover_device = ""
    s.pushover_sound = "default"
    s.auto_apply_policy = "safe-and-caution"
    s.dockge_stacks_root = "/stacks"
    s.llm_endpoint = ""
    s.llm_api_key = ""
    s.llm_model = ""
    s.llm_timeout = 30.0
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _pending_row(stack: str = "immich") -> dict[str, Any]:
    return {
        "host": "truenas",
        "stack": stack,
        "current_digest": "sha256:aaa",
        "latest_digest": "sha256:bbb",
        "first_seen_at": "2026-07-15T00:00:00Z",
        "last_seen_at": "2026-07-15T00:00:00Z",
    }


def _healthy_host_with_stack(stack: str = "immich") -> Any:
    """A host client that returns a healthy, single-container stack."""
    h = MagicMock()
    h.name = "truenas"
    h.inspect_container = AsyncMock(return_value={
        "Name": f"/{stack}",
        "Config": {
            "Image": f"ghcr.io/compactly8274/{stack}:release",
            "Labels": {"com.docker.compose.project": stack},
        },
        "State": {
            "Status": "running",
            "RestartCount": 0,
            "StartedAt": "2026-07-15T00:00:00Z",
        },
        "Mounts": [],
    })
    h.list_containers = AsyncMock(return_value=[
        {"NAME": stack, "PROJECT": stack, "STATE": "running", "IMAGE": "x", "ID": "1"},
    ])
    return h


async def test_apply_blocked_by_preflight_when_stack_not_found() -> None:
    """require_approval=True + non-existent stack -> blocked, not applied."""
    from homelab_mcp import server
    from homelab_mcp.tools.apply_update import apply_update_tool

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[_pending_row("ghost_stack")])
    fake_host = _healthy_host_with_stack("ghost_stack")
    # Override: no containers matching ghost_stack
    fake_host.list_containers = AsyncMock(return_value=[])
    # Populate the server's host_clients so preflight can find the host
    server._host_clients = {"truenas": fake_host}

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="ghost_stack")

    assert result["action"] == "blocked"
    assert "preflight" in result
    assert result["preflight"]["safe"] is False
    assert any("not found" in b.lower() or "no container" in b.lower() for b in result["preflight"]["blockers"])


async def test_apply_runs_when_preflight_safe() -> None:
    """require_approval=True + clean stack -> proceeds to evaluate_and_act."""
    from homelab_mcp import server
    from homelab_mcp.tools.apply_update import apply_update_tool

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[_pending_row("plex")])
    fake_state.last_known_good = AsyncMock(return_value="sha256:aaa")
    fake_host = _healthy_host_with_stack("plex")
    server._host_clients = {"truenas": fake_host}
    fake_orchestrator_result = {
        "action": "applied",
        "verdict": {"risk": "SAFE", "summary": "ok"},
        "apply_result": {"ok": True},
    }
    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.preflight.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(return_value=fake_orchestrator_result)) as mock_eval, \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="plex")

    # The orchestrator was called -> gate let it through
    assert mock_eval.called
    assert result["action"] == "applied"


async def test_apply_require_approval_false_skips_gate() -> None:
    """require_approval=False bypasses the preflight check entirely."""
    from homelab_mcp.tools.apply_update import apply_update_tool

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[_pending_row("ghost")])
    # Return NO containers so preflight WOULD block
    fake_host = _healthy_host_with_stack("ghost")
    fake_host.list_containers = AsyncMock(return_value=[])

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(return_value={"action": "applied", "verdict": {"risk": "SAFE"}})) as mock_eval, \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(
            host="truenas", stack="ghost", require_approval=False
        )

    assert mock_eval.called  # gate was skipped
    assert result["action"] == "applied"


async def test_apply_dry_run_skips_preflight_gate() -> None:
    """dry_run=True is a read-only preview; the gate should not block it."""
    from homelab_mcp.tools.apply_update import apply_update_tool

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[_pending_row("ghost")])
    fake_host = _healthy_host_with_stack("ghost")
    fake_host.list_containers = AsyncMock(return_value=[])  # preflight would block

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(return_value={
                   "action": "dry_run",
                   "verdict": {"risk": "SAFE"},
                   "would_apply": True,
               })) as mock_eval, \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(
            host="truenas", stack="ghost", dry_run=True, require_approval=True
        )

    assert mock_eval.called
    assert result["action"] == "dry_run"
    assert result["dry_run"] is True
