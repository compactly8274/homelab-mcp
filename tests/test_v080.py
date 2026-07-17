"""Tests for v0.8.0: container_action_tool + notifier_status_tool.

The preflight gate integration in container_action_tool mirrors
the apply_update_tool gate from v0.7.0. These tests cover:
- container_action_tool: unknown host, unsupported action,
  preflight block (require_approval=True), preflight pass
  (require_approval=False), single-container success, stack
  fan-out.
- notifier_status_tool: detects configured backends, surfaces
  missing env hints, runs self-test when requested.

Plus a regression check for the notifier status when no env is set.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------- container_action_tool ----------------


def _healthy_host_with(container_name: str = "plex", project: str = "plex") -> Any:
    h = MagicMock()
    h.name = "truenas"
    h.list_containers = AsyncMock(return_value=[
        {"NAME": container_name, "PROJECT": project, "STATE": "running",
         "IMAGE": "img", "ID": "abc"},
    ])
    h.container_action = AsyncMock(return_value=MagicMock(
        exit_code=0, stdout="", stderr="", duration_ms=42,
    ))
    return h


def _host_with_stack(stack: str = "immich", members: int = 2) -> Any:
    h = MagicMock()
    h.name = "truenas"
    h.list_containers = AsyncMock(return_value=[
        {"NAME": f"{stack}_{i}", "PROJECT": stack, "STATE": "running",
         "IMAGE": "img", "ID": f"id{i}"}
        for i in range(members)
    ])
    h.container_action = AsyncMock(return_value=MagicMock(
        exit_code=0, stdout="", stderr="", duration_ms=10,
    ))
    return h


async def test_container_action_unknown_host() -> None:
    from homelab_mcp.tools.container_action import container_action_tool
    with patch("homelab_mcp.tools.container_action.get_host",
               side_effect=KeyError("unknown host 'nope'")):
        result = await container_action_tool(host="nope", target="plex", action="restart")
    assert result["action"] == "failed"
    assert "unknown host" in result["error"]


async def test_container_action_unsupported_action() -> None:
    from homelab_mcp.tools.container_action import container_action_tool
    h = _healthy_host_with()
    with patch("homelab_mcp.tools.container_action.get_host", return_value=h):
        result = await container_action_tool(host="truenas", target="plex", action="drop")
    assert result["action"] == "failed"
    assert "unsupported action" in result["error"]


async def test_container_action_blocked_by_preflight() -> None:
    """require_approval=True + preflight blocker = blocked, no side effect."""
    from homelab_mcp import server
    from homelab_mcp.tools.container_action import container_action_tool

    # No matching container or stack: preflight should block.
    h = MagicMock()
    h.name = "truenas"
    h.list_containers = AsyncMock(return_value=[])
    server._host_clients = {"truenas": h}

    with patch("homelab_mcp.tools.container_action.get_host", return_value=h), \
         patch("homelab_mcp.tools.preflight.get_state", return_value=MagicMock()):
        result = await container_action_tool(
            host="truenas", target="ghost", action="stop", require_approval=True
        )

    assert result["action"] == "blocked"
    assert "preflight" in result
    assert not h.container_action.called


async def test_container_action_require_approval_false_bypasses_gate() -> None:
    from homelab_mcp import server
    from homelab_mcp.tools.container_action import container_action_tool

    h = _healthy_host_with(container_name="plex", project="plex")
    server._host_clients = {"truenas": h}

    with patch("homelab_mcp.tools.container_action.get_host", return_value=h), \
         patch("homelab_mcp.tools.preflight.get_state", return_value=MagicMock()):
        result = await container_action_tool(
            host="truenas", target="plex", action="restart", require_approval=False
        )

    # Gate was bypassed; the action ran on the single container.
    assert result["action"] == "applied"
    assert result["kind"] == "container"
    assert h.container_action.called


async def test_container_action_stack_fan_out() -> None:
    """When target is a stack (not a container name), all members are acted on."""
    from homelab_mcp import server
    from homelab_mcp.tools.container_action import container_action_tool

    h = _host_with_stack(stack="immich", members=3)
    server._host_clients = {"truenas": h}

    # Override so the target is NOT a container name
    h.list_containers = AsyncMock(return_value=[
        {"NAME": f"immich_{i}", "PROJECT": "immich", "STATE": "running",
         "IMAGE": "img", "ID": f"id{i}"}
        for i in range(3)
    ])
    # Pre-populate server's host list so preflight doesn't block on unknown host
    with patch("homelab_mcp.tools.container_action.get_host", return_value=h), \
         patch("homelab_mcp.tools.preflight.get_state", return_value=MagicMock()):
        result = await container_action_tool(
            host="truenas", target="immich", action="stop", require_approval=False
        )

    assert result["action"] == "applied"
    assert result["kind"] == "stack"
    assert result["member_count"] == 3
    assert h.container_action.call_count == 3


# ---------------- notifier_status_tool ----------------


def _settings_with(ntfy_topic: str = "", **kwargs: Any) -> Any:
    s = MagicMock()
    s.ntfy_topic = ntfy_topic
    s.ntfy_url = "http://ntfy"
    s.ntfy_priority = "default"
    s.pushover_app_token = ""
    s.pushover_user_key = ""
    s.pushover_device = ""
    s.pushover_sound = "default"
    s.discord_webhook_url = ""
    s.webhook_url = ""
    s.webhook_secret = ""
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


async def test_notifier_status_detects_ntfy() -> None:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    with patch("homelab_mcp.tools.notifier_status.Settings",
               return_value=_settings_with(ntfy_topic="my-topic")):
        r = await notifier_status_tool()
    assert r["configured_count"] == 1
    assert r["configured"][0]["backend"] == "ntfy"
    assert r["configured"][0]["topic"] == "my-topic"
    assert r["healthy"] is True


async def test_notifier_status_detects_pushover() -> None:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    with patch("homelab_mcp.tools.notifier_status.Settings",
               return_value=_settings_with(
                   pushover_app_token="tok", pushover_user_key="user",
                   pushover_device="iphone", pushover_sound="magic",
               )):
        r = await notifier_status_tool()
    assert r["configured_count"] == 1
    assert r["configured"][0]["backend"] == "pushover"
    assert r["configured"][0]["device"] == "iphone"


async def test_notifier_status_no_backends() -> None:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    with patch("homelab_mcp.tools.notifier_status.Settings",
               return_value=_settings_with()):
        r = await notifier_status_tool()
    assert r["configured_count"] == 0
    assert r["healthy"] is False
    assert "HOMELAB_MCP_NTFY_TOPIC" in r["missing_env_hints"]


async def test_notifier_status_test_notify_runs_notify() -> None:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    fake_notifier = MagicMock()
    fake_notifier.notify = AsyncMock()
    # The tool does the import inside the function, so we can't
    # patch the module-level name. Patch sys.modules[...] so
    # `from homelab_mcp.tools.apply_update import _build_notifier...`
    # returns our fake. Easier: stub the function the way it
    # appears in apply_update.__dict__.
    import homelab_mcp.tools.apply_update as au
    real = au._build_notifier_from_settings
    au._build_notifier_from_settings = lambda s: fake_notifier
    try:
        with patch("homelab_mcp.tools.notifier_status.Settings",
                   return_value=_settings_with(ntfy_topic="my-topic")):
            r = await notifier_status_tool(test_notify=True)
    finally:
        au._build_notifier_from_settings = real
    assert r["healthy"] is True
    assert fake_notifier.notify.called
    assert "self-test" in fake_notifier.notify.call_args.args[0]
    assert r["test_results"][0]["backend"] == "ntfy"
    assert r["test_results"][0]["ok"] is True


async def test_notifier_status_test_notify_skipped_when_none_configured() -> None:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    with patch("homelab_mcp.tools.notifier_status.Settings",
               return_value=_settings_with()):
        r = await notifier_status_tool(test_notify=True)
    assert r["test_results"] == []
    assert "silent no-op" in r["test_skipped_reason"]
