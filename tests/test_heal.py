"""Unit tests for updater/heal.py."""

from unittest.mock import AsyncMock

import pytest

from homelab_mcp.updater.heal import HealOutcome, _classify, _is_unhealthy


def test_classify_healthy_running():
    info = {"State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}}, "RestartCount": 0}
    snap = _classify(info)
    assert snap["status"] == "running"
    assert snap["health_status"] == "healthy"
    assert snap["restart_count"] == 0
    assert not _is_unhealthy(snap)


def test_classify_exited_is_unhealthy():
    info = {"State": {"Status": "exited", "Running": False, "ExitCode": 1, "Health": {}}, "RestartCount": 5}
    snap = _classify(info)
    assert snap["status"] == "exited"
    assert snap["restart_count"] == 5
    assert _is_unhealthy(snap)


def test_classify_unhealthy_health_is_unhealthy():
    info = {"State": {"Status": "running", "Running": True, "Health": {"Status": "unhealthy", "FailingStreak": 3}}, "RestartCount": 1}
    snap = _classify(info)
    assert snap["status"] == "running"
    assert snap["health_status"] == "unhealthy"
    assert _is_unhealthy(snap)


def test_classify_starting_health_is_unhealthy():
    # "starting" health means docker is still running the check; we
    # include it in the heal list so we can re-probe after a settle.
    info = {"State": {"Status": "running", "Running": True, "Health": {"Status": "starting"}}, "RestartCount": 0}
    assert _is_unhealthy(_classify(info))


def test_classify_restarting_is_unhealthy():
    info = {"State": {"Status": "restarting", "Running": True, "Health": {}}, "RestartCount": 12}
    assert _is_unhealthy(_classify(info))


def test_classify_no_health_block_is_healthy():
    # A container with no healthcheck and status=running is "healthy"
    # for heal purposes (we have no signal to act on).
    info = {"State": {"Status": "running", "Running": True, "Health": {}}, "RestartCount": 0}
    assert not _is_unhealthy(_classify(info))


def test_heal_outcome_to_dict():
    o = HealOutcome(ok=True, action="restarted", name="x", host="h")
    o.actions_taken.append("docker restart")
    d = o.to_dict()
    assert d["ok"] is True
    assert d["action"] == "restarted"
    assert d["actions_taken"] == ["docker restart"]


@pytest.mark.asyncio
async def test_heal_already_healthy_returns_immediately():
    """Healthy container: one inspect, no restart, returns already_healthy."""
    from homelab_mcp.updater.heal import heal_container

    host = AsyncMock()
    host.name = "truenas"
    host.inspect_container = AsyncMock(return_value={
        "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
        "RestartCount": 0,
    })
    outcome = await heal_container(host, "healthy-app", settle_seconds=0)
    assert outcome.ok is True
    assert outcome.action == "already_healthy"
    # No restart attempted on a healthy container
    host.run_command.assert_not_called()


@pytest.mark.asyncio
async def test_heal_restart_fixes_broken_container():
    from homelab_mcp.updater.heal import heal_container

    host = AsyncMock()
    host.name = "truenas"
    # First inspect: unhealthy. After restart: healthy.
    host.inspect_container = AsyncMock(side_effect=[
        {"State": {"Status": "exited", "Running": False, "ExitCode": 1, "Health": {}}, "RestartCount": 3},
        {"State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}}, "RestartCount": 3},
    ])
    host.run_command = AsyncMock(return_value=type("R", (), {"ok": True, "stderr": "", "stdout": ""})())

    outcome = await heal_container(host, "broken-app", settle_seconds=0)
    assert outcome.ok is True
    assert outcome.action == "restarted"
    assert "docker restart" in outcome.actions_taken
    assert host.run_command.call_count == 1


@pytest.mark.asyncio
async def test_heal_needs_human_when_no_snapshot():
    """Restart fixes nothing + no snapshot = needs_human."""
    from homelab_mcp.updater.heal import heal_container

    host = AsyncMock()
    host.name = "truenas"
    # Both inspects come back unhealthy
    inspect_response = {
        "State": {"Status": "exited", "Running": False, "ExitCode": 137, "Health": {}},
        "RestartCount": 5,
    }
    host.inspect_container = AsyncMock(return_value=inspect_response)
    host.run_command = AsyncMock(return_value=type("R", (), {"ok": True, "stderr": "", "stdout": ""})())

    outcome = await heal_container(host, "really-broken", snapshot=None, settle_seconds=0)
    assert outcome.ok is False
    assert outcome.action == "needs_human"
    assert "restart" in outcome.error.lower() or "manual" in outcome.error.lower()


@pytest.mark.asyncio
async def test_heal_rollback_when_restart_doesnt_fix():
    from homelab_mcp.updater.heal import heal_container
    from homelab_mcp.updater.snapshot import StackSnapshot

    snap = StackSnapshot(
        host="truenas",
        stack="myapp",
        stack_dir="/data/stacks/myapp",
        manifest_digest="sha256:olddigest",
        config_digest="sha256:oldconfig",
        compose_hash="abc123",
        services={"myapp": "ghcr.io/me/myapp:latest"},
    )

    host = AsyncMock()
    host.name = "truenas"
    broken = {"State": {"Status": "exited", "Running": False, "ExitCode": 1, "Health": {}}, "RestartCount": 5}
    host.inspect_container = AsyncMock(return_value=broken)
    host.run_command = AsyncMock(side_effect=[
        # docker restart
        type("R", (), {"ok": True, "stderr": "", "stdout": ""})(),
        # rollback: pull old image
        type("R", (), {"ok": True, "stderr": "", "stdout": ""})(),
        # rollback: compose up
        type("R", (), {"ok": True, "stderr": "", "stdout": ""})(),
    ])
    host.compose_up = AsyncMock(return_value=type("R", (), {"ok": True, "stderr": "", "stdout": ""})())

    outcome = await heal_container(host, "broken", snapshot=snap, settle_seconds=0)
    assert outcome.ok is True
    assert outcome.action == "rolled_back"
    assert "rollback" in outcome.actions_taken
