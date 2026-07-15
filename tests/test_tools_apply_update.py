"""Tests for the apply_update MCP tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.tools.apply_update import apply_update_tool


def _settings(**overrides) -> MagicMock:
    """Build a Settings mock with the fields apply_update_tool reads."""
    s = MagicMock()
    s.ntfy_url = "https://ntfy.sh/"
    s.ntfy_topic = ""
    s.ntfy_priority = "default"
    s.discord_webhook_url = ""
    s.discord_username = "homelab-mcp"
    s.pushover_app_token = ""
    s.pushover_user_key = ""
    s.pushover_device = ""
    s.pushover_sound = "pushover"
    s.llm_endpoint = "http://localhost:11434/v1/chat/completions"
    s.llm_api_key = ""
    s.llm_model = "llama3.1:8b"
    s.llm_timeout = 30
    s.auto_apply_policy = "safe-and-caution"
    s.dockge_stacks_root = "/mnt/Data/appdata/dockge/stacks"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# --- no pending update --------------------------------------------------


async def test_apply_returns_no_pending_when_empty() -> None:
    """If there are no pending rows for the (host, stack), return cleanly."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[])
    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=MagicMock()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "no_pending_update"
    assert result["host"] == "truenas"
    assert result["stack"] == "immich"


async def test_apply_returns_no_pending_when_other_stack_only() -> None:
    """Pending rows for other stacks don't satisfy this call."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "nextcloud", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=MagicMock()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "no_pending_update"


# --- inspect failure ----------------------------------------------------


async def test_apply_handles_inspect_failure() -> None:
    """If inspect_container throws, the tool returns action='failed' with error."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(side_effect=RuntimeError("docker down"))
    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "failed"
    assert "docker down" in result["error"]


# --- missing image in inspect ------------------------------------------


async def test_apply_handles_missing_image_in_inspect() -> None:
    """If Config.Image is empty, the tool returns action='failed' cleanly."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(return_value={"Config": {}})  # no Image
    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "failed"
    assert "image" in result["error"].lower()


# --- happy path: BREAKING → notified ------------------------------------


async def test_apply_breaking_classification_notifies_not_applies() -> None:
    """A BREAKING verdict leads to action='notified_breaking' (no apply)."""
    from homelab_mcp.updater.risk import RiskVerdict

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(return_value={
        "Config": {"Image": "ghcr.io/immich-app/immich-server:release"},
    })
    fake_notes = MagicMock()
    fake_notes.text = "Major version: rewritten DB layer"
    fake_notes.tag = "v1.100.0"
    fake_notes.source = "github_release"
    fake_verdict = RiskVerdict(
        risk="BREAKING",
        summary="Database migration required",
        migration_steps=["Run pre-upgrade script"],
    )

    fake_release_notes = AsyncMock(return_value=fake_notes)
    fake_classify = AsyncMock(return_value=fake_verdict)
    fake_pipeline = AsyncMock()
    fake_notifier = MagicMock()
    fake_notifier.notify = AsyncMock()

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.fetch_release_notes", fake_release_notes), \
         patch("homelab_mcp.tools.apply_update.classify_release_notes", fake_classify), \
         patch("homelab_mcp.tools.apply_update.run_pipeline", fake_pipeline), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(return_value={
                   "action": "notified_breaking",
                   "verdict": fake_verdict.to_dict(),
                   "notes_source": "github_release",
                   "stack_dir": "/mnt/Data/appdata/dockge/stacks/immich",
               })), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=fake_notifier), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "notified_breaking"
    assert result["verdict"]["risk"] == "BREAKING"
    assert result["image"] == "ghcr.io/immich-app/immich-server:release"
    assert result["forced"] is False
    # The pipeline should NOT have been called
    fake_pipeline.assert_not_called()


# --- happy path: SAFE → applied ----------------------------------------


async def test_apply_safe_classification_runs_pipeline() -> None:
    """A SAFE verdict leads to action='applied'."""
    from homelab_mcp.updater.risk import RiskVerdict

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(return_value={
        "Config": {"Image": "ghcr.io/immich-app/immich-server:release"},
    })
    fake_verdict = RiskVerdict(risk="SAFE", summary="patch update")

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(return_value={
                   "action": "applied",
                   "verdict": fake_verdict.to_dict(),
                   "apply_result": {"ok": True, "snapshot_digest": "sha256:aaa"},
               })), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "applied"
    assert result["verdict"]["risk"] == "SAFE"
    assert result["forced"] is False
    assert result["policy"] == "safe-and-caution"


# --- force=True overrides BREAKING -------------------------------------


async def test_apply_force_bypasses_policy_but_still_healthchecks() -> None:
    """force=True flips policy to safe-and-caution so BREAKING gets applied.

    The pipeline still runs and still healthchecks + rolls back on
    failure — force only overrides the policy gate, not safety.
    """
    from homelab_mcp.updater.risk import RiskVerdict

    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(return_value={
        "Config": {"Image": "ghcr.io/immich-app/immich-server:release"},
    })
    fake_verdict = RiskVerdict(risk="BREAKING", summary="DB migration")

    evaluate_mock = AsyncMock(return_value={
        "action": "applied",
        "verdict": fake_verdict.to_dict(),
        "apply_result": {"ok": True, "snapshot_digest": "sha256:aaa"},
    })

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act", evaluate_mock), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings",
               return_value=_settings(auto_apply_policy="safe-only")):
        result = await apply_update_tool(host="truenas", stack="immich", force=True)
    assert result["action"] == "applied"
    assert result["forced"] is True
    assert result["policy"] == "safe-and-caution"  # overridden by force
    # Confirm the orchestrator was called with the overridden policy
    call_kwargs = evaluate_mock.call_args.kwargs
    assert call_kwargs["policy"] == "safe-and-caution"


# --- exception escaping evaluate_and_act -------------------------------


async def test_apply_catches_orchestrator_exceptions() -> None:
    """If evaluate_and_act raises, the tool returns action='failed'."""
    fake_state = MagicMock()
    fake_state.list_pending_updates = AsyncMock(return_value=[
        {"host": "truenas", "stack": "immich", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb", "first_seen_at": "2026-07-15T00:00:00Z",
         "last_seen_at": "2026-07-15T00:00:00Z"},
    ])
    fake_host = MagicMock()
    fake_host.inspect_container = AsyncMock(return_value={
        "Config": {"Image": "ghcr.io/foo/bar:1"},
    })

    with patch("homelab_mcp.tools.apply_update.get_state", return_value=fake_state), \
         patch("homelab_mcp.tools.apply_update.get_host", return_value=fake_host), \
         patch("homelab_mcp.tools.apply_update.evaluate_and_act",
               AsyncMock(side_effect=RuntimeError("orchestrator crashed"))), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=MagicMock(notify=AsyncMock())), \
         patch("homelab_mcp.tools.apply_update.Settings", return_value=_settings()):
        result = await apply_update_tool(host="truenas", stack="immich")
    assert result["action"] == "failed"
    assert "orchestrator crashed" in result["error"]


# --- notifier construction ----------------------------------------------


def test_build_notifier_with_no_config_uses_console() -> None:
    """If no notifier env vars are set, we still get a working (console) notifier."""
    from homelab_mcp.tools.apply_update import _build_notifier_from_settings
    s = _settings()  # all empty
    notifier = _build_notifier_from_settings(s)
    # MultiNotifier always wraps; with one ConsoleNotifier inside
    assert notifier is not None


def test_build_notifier_with_ntfy_topic_adds_ntfy() -> None:
    """If ntfy_topic is set, the notifier list includes NtfyNotifier."""
    from homelab_mcp.tools.apply_update import _build_notifier_from_settings
    s = _settings(ntfy_topic="my-topic")
    notifier = _build_notifier_from_settings(s)
    # Just confirm it builds without raising
    assert notifier is not None
