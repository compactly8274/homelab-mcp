"""Tests for the canary cron (v0.9.0, v0.9.9: dropped self-stack).

The cron orchestrates trigger_scan + list_pending + apply_update
for a small set of "canary" stacks. We mock every tool call
and verify:
- 2 stacks in CANARY_STACKS (the user's pick — PlexAutoLanguages, dockwatch)
  [v0.9.9: removed "homelab-mcp" because apply_update on the running daemon's
  own stack would restart the daemon and kill the gateway's MCP session
  every 6h. See canary_cron.py CANARY_STACKS docstring.]
- scans run on each unique host
- dry-run before real apply
- self-protection: if someone re-adds the daemon's own stack, the run loop
  refuses to apply (defence in depth)
- ntfy summary sent on completion
- HOMELAB_MCP_CANARY_CRON env var guards execution (default off)
- main() exits 2 when the env var is missing
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_canary_stacks_contains_2_pairs_and_excludes_self() -> None:
    from homelab_mcp.scripts.canary_cron import CANARY_STACKS
    # v0.9.9: removed the homelab-mcp self-stack from the canary set.
    # 2 stacks on truenas remain: PlexAutoLanguages, dockwatch.
    assert len(CANARY_STACKS) == 2
    stack_names = [s for _, s in CANARY_STACKS]
    assert "PlexAutoLanguages" in stack_names
    assert "dockwatch" in stack_names
    # The daemon's own stack MUST NOT be in the canary set — apply_update
    # on it would restart the running container and kill the gateway's MCP
    # session. (Defence-in-depth check: the run loop also refuses, but
    # keeping the list clean is the primary control.)
    assert "homelab-mcp" not in stack_names
    for host, _ in CANARY_STACKS:
        assert host == "truenas"


def test_run_loop_refuses_to_apply_to_self_stack(monkeypatch, tmp_path) -> None:
    """Defence-in-depth: even if the canary list is reconfigured to include
    the daemon's own stack, the run loop must refuse to apply.
    """
    import asyncio
    monkeypatch.setenv("HOMELAB_MCP_CANARY_CRON", "1")
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_MCP_SELF_STACK", "homelab-mcp")
    monkeypatch.setattr("homelab_mcp.scripts.canary_cron.CANARY_STACKS",
                        [("truenas", "homelab-mcp")])

    from homelab_mcp.scripts import canary_cron

    # Make apply_update_tool FAIL the test loudly if it gets called —
    # the self-protection should prevent any apply.
    apply_called = {"n": 0}

    async def fake_apply(*_a, **_kw):
        apply_called["n"] += 1
        return {"action": "APPLIED", "would_apply": True}

    with patch("homelab_mcp.tools.updates.trigger_scan_tool",
               AsyncMock(return_value=[])), \
         patch("homelab_mcp.tools.updates.list_pending_updates_tool",
               AsyncMock(return_value=[{"stack": "homelab-mcp"}])), \
         patch("homelab_mcp.tools.apply_update.apply_update_tool", fake_apply), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               MagicMock(return_value=MagicMock(notify=AsyncMock()))):
        summary = asyncio.run(canary_cron._run_canary())

    # Self-protection blocked the apply.
    assert apply_called["n"] == 0
    assert summary["stacks"][0]["outcome"] == "refused_self"
    assert "self-protection" in summary["stacks"][0]["reason"]


def test_main_refuses_when_env_var_not_set(monkeypatch) -> None:
    monkeypatch.delenv("HOMELAB_MCP_CANARY_CRON", raising=False)
    from homelab_mcp.scripts.canary_cron import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_load_env_skips_comments_and_blanks(tmp_path) -> None:
    from homelab_mcp.scripts.canary_cron import _load_env
    p = tmp_path / "test.env"
    p.write_text(
        "# comment\n"
        "\n"
        "FOO=bar\n"
        "QUOTED=\"baz qux\"\n"
        "SINGLE='one'\n"
    )
    env = _load_env(p)
    assert env["FOO"] == "bar"
    assert env["QUOTED"] == "baz qux"
    assert env["SINGLE"] == "one"


def test_run_canary_no_pendings_calls_notify(monkeypatch, tmp_path) -> None:
    """All 2 canary stacks have no pending updates: still send a ntfy summary."""
    import asyncio
    monkeypatch.setenv("HOMELAB_MCP_CANARY_CRON", "1")
    monkeypatch.setenv("HOMELAB_MCP_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("HOMELAB_MCP_NTFY_URL", "https://ntfy.sh")
    # Use tmp_path for the state DB so we don't try to write to /data
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))

    from homelab_mcp.scripts import canary_cron

    # Make all tool calls return safe no-op results.
    fake_scan = AsyncMock(return_value=[])
    fake_pending = AsyncMock(return_value=[])  # nothing pending
    fake_apply = AsyncMock(return_value={
        "action": "no_pending_update",
    })
    fake_notifier = MagicMock()
    fake_notifier.notify = AsyncMock()

    with patch("homelab_mcp.tools.updates.trigger_scan_tool", fake_scan), \
         patch("homelab_mcp.tools.updates.list_pending_updates_tool", fake_pending), \
         patch("homelab_mcp.tools.apply_update.apply_update_tool", fake_apply), \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=fake_notifier):
        summary = asyncio.run(canary_cron._run_canary())

    # Both canary stacks reported no_pending
    assert len(summary["stacks"]) == 2
    for s in summary["stacks"]:
        assert s["outcome"] == "no_pending"
    # ntfy was sent
    assert fake_notifier.notify.called
    # The body mentions both canary stacks (and does NOT mention homelab-mcp,
    # since it's not in the canary set anymore)
    body = fake_notifier.notify.call_args.args[0]
    assert "PlexAutoLanguages" in body
    assert "dockwatch" in body
    assert "homelab-mcp" not in body


def test_run_canary_would_not_apply_stops_before_real_apply(monkeypatch, tmp_path) -> None:
    """dry_run returns would_apply=False -> script does not call apply with dry_run=False."""
    import asyncio
    monkeypatch.setenv("HOMELAB_MCP_CANARY_CRON", "1")
    monkeypatch.setenv("HOMELAB_MCP_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))

    from homelab_mcp.scripts import canary_cron

    fake_scan = AsyncMock(return_value=[])
    fake_pending = AsyncMock(return_value=[
        {"stack": "PlexAutoLanguages", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb"},
    ])
    # dry-run says would_apply=False (e.g. BREAKING -> safe-only policy says no)
    fake_apply = AsyncMock(return_value={
        "action": "notified_breaking",
        "verdict": {"risk": "BREAKING", "summary": "big change"},
        "would_apply": False,
    })
    fake_notifier = MagicMock(notify=AsyncMock())

    with patch("homelab_mcp.tools.updates.trigger_scan_tool", fake_scan), \
         patch("homelab_mcp.tools.updates.list_pending_updates_tool", fake_pending), \
         patch("homelab_mcp.tools.apply_update.apply_update_tool", fake_apply) as m_apply, \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=fake_notifier):
        summary = asyncio.run(canary_cron._run_canary())

    # Only the dry_run call was made; no real apply
    assert m_apply.call_count == 1
    assert m_apply.call_args.kwargs["dry_run"] is True
    # The PlexAutoLanguages row got the would_not_apply outcome
    pl = next(s for s in summary["stacks"] if s["stack"] == "PlexAutoLanguages")
    assert pl["outcome"] == "would_not_apply"
    assert pl["risk"] == "BREAKING"


def test_run_canary_happy_path_calls_real_apply(monkeypatch, tmp_path) -> None:
    """dry_run returns would_apply=True, safe -> real apply is called."""
    import asyncio
    monkeypatch.setenv("HOMELAB_MCP_CANARY_CRON", "1")
    monkeypatch.setenv("HOMELAB_MCP_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))

    from homelab_mcp.scripts import canary_cron

    fake_scan = AsyncMock(return_value=[])
    fake_pending = AsyncMock(return_value=[
        {"stack": "PlexAutoLanguages", "current_digest": "sha256:aaa",
         "latest_digest": "sha256:bbb"},
    ])

    # First call (dry_run) -> would_apply=True. Second call (real) -> applied.
    fake_apply = AsyncMock(side_effect=[
        {"action": "dry_run", "verdict": {"risk": "CAUTION", "summary": "ok"},
         "would_apply": True},
        {"action": "applied", "verdict": {"risk": "CAUTION"}},
    ])
    fake_notifier = MagicMock(notify=AsyncMock())

    with patch("homelab_mcp.tools.updates.trigger_scan_tool", fake_scan), \
         patch("homelab_mcp.tools.updates.list_pending_updates_tool", fake_pending), \
         patch("homelab_mcp.tools.apply_update.apply_update_tool", fake_apply) as m_apply, \
         patch("homelab_mcp.tools.apply_update._build_notifier_from_settings",
               return_value=fake_notifier):
        summary = asyncio.run(canary_cron._run_canary())

    # dry_run then real apply
    assert m_apply.call_count == 2
    assert m_apply.call_args_list[0].kwargs["dry_run"] is True
    assert m_apply.call_args_list[1].kwargs["dry_run"] is False
    pl = next(s for s in summary["stacks"] if s["stack"] == "PlexAutoLanguages")
    assert pl["outcome"] == "applied"
    assert pl["risk"] == "CAUTION"
