"""Tests for the apply pipeline (snapshot + apply + probe + rollback)."""

from __future__ import annotations

from pathlib import Path

from homelab_mcp.hosts.base import CommandResult
from homelab_mcp.state import State
from homelab_mcp.updater.apply import apply_update
from homelab_mcp.updater.pipeline import run_pipeline
from homelab_mcp.updater.rollback import rollback_stack
from homelab_mcp.updater.snapshot import (
    StackSnapshot,
    _container_config_digest,
    _container_manifest_digest,
    _resolve_stack_dir,
    snapshot_stack,
)

# -- _resolve_stack_dir ----------------------------------------------------


def test_resolve_stack_dir_uses_working_dir_first() -> None:
    """A working_dir from a compose label is authoritative."""
    assert _resolve_stack_dir("u", "radarr", working_dir="/srv/radarr") == "/srv/radarr"


def test_resolve_stack_dir_falls_back_to_compose_manager() -> None:
    """Without a label, CA-style compose manager paths are used."""
    assert _resolve_stack_dir("u", "radarr", compose_manager_root="/srv/ca") == "/srv/ca/radarr"


def test_resolve_stack_dir_falls_back_to_dockge() -> None:
    """Without CA, Dockge stack root is used."""
    assert _resolve_stack_dir("u", "radarr", dockge_stacks_root="/mnt/dockge") == "/mnt/dockge/radarr"


def test_resolve_stack_dir_returns_none_when_nothing_set() -> None:
    """If no resolver is configured, returns None."""
    assert _resolve_stack_dir("u", "radarr") is None


# -- _container_manifest_digest / _container_config_digest ------------------


def test_container_manifest_digest_prefers_repodiests() -> None:
    info = {"RepoDigests": ["img@sha256:" + "a" * 64], "Image": "sha256:" + "b" * 64}
    assert _container_manifest_digest(info) == "sha256:" + "a" * 64


def test_container_manifest_digest_falls_back_to_image() -> None:
    info = {"RepoDigests": [], "Image": "sha256:" + "b" * 64}
    assert _container_manifest_digest(info) == "sha256:" + "b" * 64


def test_container_config_digest_from_config_id() -> None:
    info = {"Config": {"Id": "sha256:" + "c" * 64}}
    assert _container_config_digest(info) == "sha256:" + "c" * 64


def test_container_config_digest_missing() -> None:
    assert _container_config_digest({"Config": {}}) is None


# -- snapshot_stack ---------------------------------------------------------


class _FakeHost:
    def __init__(self, name: str, containers: list, inspect_data: dict):
        self._name = name
        self._containers = containers
        self._inspect = inspect_data

    @property
    def name(self) -> str:
        return self._name

    async def list_containers(self, all: bool = True) -> list:
        return self._containers

    async def inspect_container(self, name: str) -> dict:
        return self._inspect


async def test_snapshot_stack_returns_none_if_not_running(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost("unraid", containers=[], inspect_data={})
    out = await snapshot_stack(host, state, stack="radarr")
    assert out is None


async def test_snapshot_stack_uses_compose_manager_root(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        "unraid",
        containers=[{
            "NAME": "radarr-1", "PROJECT": "radarr", "SERVICE": "radarr",
            "WORKDIR": "", "IMAGE": "ghcr.io/x/radarr:latest",
        }],
        inspect_data={
            "RepoDigests": ["ghcr.io/x/radarr@sha256:" + "a" * 64],
            "Image": "sha256:" + "a" * 64,
            "Config": {"Id": "sha256:" + "b" * 64},
        },
    )
    out = await snapshot_stack(
        host, state, stack="radarr", compose_manager_root="/srv/ca"
    )
    assert out is not None
    assert out.stack_dir == "/srv/ca/radarr"
    assert out.manifest_digest == "sha256:" + "a" * 64


# -- apply_update -----------------------------------------------------------


async def test_apply_update_fails_when_no_stack_dir(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost("u", [], {})
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="",
        manifest_digest="sha256:" + "a" * 64, config_digest=None,
        compose_hash="",
    )
    out = await apply_update(host, state, stack="x", snapshot=snap)
    assert out["ok"] is False
    assert "no stack_dir" in out["error"]


async def test_apply_update_succeeds(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    class _ApplyHost:
        def __init__(self, name):
            self._name = name
            self.pull_called = False
            self.up_called = False
            self._containers = [{
                "NAME": "x-1", "PROJECT": "x", "SERVICE": "x",
                "WORKDIR": "", "IMAGE": "x:latest",
            }]

        @property
        def name(self) -> str: return self._name

        async def list_containers(self, all: bool = True) -> list:
            return self._containers

        async def run_command(self, command, timeout=10.0):
            if command.startswith("cat "):
                return CommandResult(0, "services:\n  x:\n    image: foo\n", "", 5)
            return CommandResult(0, "", "", 0)

        async def compose_pull(self, stack_dir):
            self.pull_called = True
            return CommandResult(0, "Pulled", "", 1000)

        async def compose_up(self, stack_dir):
            self.up_called = True
            return CommandResult(0, "Up", "", 1000)

        async def inspect_container(self, name):
            return {
                "RepoDigests": ["x@sha256:" + "c" * 64],
                "Image": "sha256:" + "c" * 64,
                "State": {"Status": "running", "Health": {}},
            }

    h = _ApplyHost("u")
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="/srv/x",
        manifest_digest="sha256:" + "a" * 64, config_digest=None,
        compose_hash="",
    )
    out = await apply_update(h, state, stack="x", snapshot=snap, settle_seconds=0)
    assert out["ok"] is True
    assert h.pull_called
    assert h.up_called
    assert out["services"]["x"] is True


async def test_apply_update_rolls_back_on_probe_failure(tmp_path: Path) -> None:
    """A service probe failure after up -d is reported as ok=False."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    class _FailHost:
        def __init__(self, name):
            self._name = name
            self._containers = [{
                "NAME": "x-1", "PROJECT": "x", "SERVICE": "x",
                "WORKDIR": "", "IMAGE": "x:latest",
            }]

        @property
        def name(self) -> str: return self._name

        async def list_containers(self, all: bool = True) -> list:
            return self._containers

        async def run_command(self, command, timeout=10.0):
            if command.startswith("cat "):
                return CommandResult(0, "services:\n  x:\n    image: foo\n", "", 5)
            return CommandResult(0, "", "", 0)

        async def compose_pull(self, stack_dir):
            return CommandResult(0, "Pulled", "", 1000)

        async def compose_up(self, stack_dir):
            return CommandResult(0, "Up", "", 1000)

        async def inspect_container(self, name):
            return {
                "RepoDigests": ["x@sha256:" + "c" * 64],
                "State": {"Status": "exited", "Health": {}},
            }

    h = _FailHost("u")
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="/srv/x",
        manifest_digest="sha256:" + "a" * 64, config_digest=None,
        compose_hash="",
    )
    out = await apply_update(h, state, stack="x", snapshot=snap, settle_seconds=0)
    assert out["ok"] is False
    assert "x" in out["services"]


# -- rollback_stack ---------------------------------------------------------


async def test_rollback_requires_stack_dir() -> None:
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="",
        manifest_digest="sha256:" + "a" * 64, config_digest=None,
        compose_hash="",
    )

    class _Host:
        @property
        def name(self) -> str: return "u"
        async def run_command(self, *a, **k): raise AssertionError("not called")
        async def compose_up(self, *a, **k): raise AssertionError("not called")
    out = await rollback_stack(_Host(), snapshot=snap)
    assert out["ok"] is False
    assert "no stack_dir" in out["error"]


async def test_rollback_requires_manifest_digest() -> None:
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="/srv/x",
        manifest_digest=None, config_digest=None, compose_hash="",
        services={"x": "foo:latest"},
    )

    class _Host:
        @property
        def name(self) -> str: return "u"
        async def run_command(self, *a, **k): raise AssertionError("not called")
        async def compose_up(self, *a, **k): raise AssertionError("not called")
    out = await rollback_stack(_Host(), snapshot=snap)
    assert out["ok"] is False
    assert "no manifest_digest" in out["error"]


async def test_rollback_pulls_old_digest_then_compose_ups(tmp_path: Path) -> None:
    """Happy path: pull by digest, then compose up -d."""
    snap = StackSnapshot(
        host="u", stack="x", stack_dir="/srv/x",
        manifest_digest="sha256:" + "a" * 64, config_digest=None,
        compose_hash="",
        services={"x": "foo:latest"},
    )
    pulled: list[str] = []
    upped: list[str] = []

    class _Host:
        @property
        def name(self) -> str: return "u"

        async def run_command(self, command, timeout=10.0):
            pulled.append(command)
            return CommandResult(0, "", "", 100)

        async def compose_up(self, stack_dir, **kw):
            upped.append(stack_dir)
            return CommandResult(0, "Up", "", 100)

    out = await rollback_stack(_Host(), snapshot=snap, reason="test")
    assert out["ok"] is True
    assert pulled[0].startswith("docker pull foo:latest@sha256:")
    assert upped == ["/srv/x"]


# -- run_pipeline -----------------------------------------------------------


class _ApplyHost(_FakeHost):
    def __init__(self, name):
        self._name = name
        self.pull_called = False
        self.up_called = False

    async def run_command(self, command, timeout=10.0):
        if command.startswith("cat "):
            return CommandResult(0, "services:\n  x:\n    image: foo\n", "", 5)
        return CommandResult(0, "", "", 0)

    async def compose_pull(self, stack_dir):
        self.pull_called = True
        return CommandResult(0, "Pulled", "", 1000)

    async def compose_up(self, stack_dir):
        self.up_called = True
        return CommandResult(0, "Up", "", 1000)

    async def inspect_container(self, name):
        return {
            "RepoDigests": ["x@sha256:" + "c" * 64],
            "Image": "sha256:" + "c" * 64,
            "State": {"Status": "running", "Health": {}},
        }


async def test_run_pipeline_dry_run_does_not_pull(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    h = _ApplyHost("u")
    h._containers = [{
        "NAME": "x-1", "PROJECT": "x", "SERVICE": "x",
        "WORKDIR": "", "IMAGE": "x:latest",
    }]
    h._inspect = {
        "RepoDigests": ["x@sha256:" + "a" * 64],
        "Image": "sha256:" + "a" * 64,
        "State": {"Status": "running"},
    }
    out = await run_pipeline(
        h, state, stack="x", to_digest="sha256:" + "b" * 64,
        compose_manager_root="/srv/ca", dry_run=True,
    )
    assert out["action"] == "dry_run"
    assert out["ok"] is True
    assert h.pull_called is False
    assert h.up_called is False


async def test_run_pipeline_records_applied_status(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    h = _ApplyHost("u")
    h._containers = [{
        "NAME": "x-1", "PROJECT": "x", "SERVICE": "x",
        "WORKDIR": "", "IMAGE": "x:latest",
    }]
    h._inspect = {
        "RepoDigests": ["x@sha256:" + "a" * 64],
        "Image": "sha256:" + "a" * 64,
        "State": {"Status": "running"},
    }
    out = await run_pipeline(
        h, state, stack="x", to_digest="sha256:" + "b" * 64,
        compose_manager_root="/srv/ca",
    )
    assert out["action"] == "applied"
    assert out["ok"] is True
    history = await state.list_update_history("u", "x")
    assert len(history) == 1
    assert history[0]["status"] == "applied"


async def test_run_pipeline_rolls_back_on_failure(tmp_path: Path) -> None:
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    class _FailHost(_ApplyHost):
        async def inspect_container(self, name):
            return {
                "RepoDigests": ["x@sha256:" + "c" * 64],
                "State": {"Status": "exited"},
            }

    h = _FailHost("u")
    h._containers = [{
        "NAME": "x-1", "PROJECT": "x", "SERVICE": "x",
        "WORKDIR": "", "IMAGE": "x:latest",
    }]
    h._inspect = {
        "RepoDigests": ["x@sha256:" + "a" * 64],
        "Image": "sha256:" + "a" * 64,
        "State": {"Status": "running"},
    }
    out = await run_pipeline(
        h, state, stack="x", to_digest="sha256:" + "b" * 64,
        compose_manager_root="/srv/ca",
    )
    assert out["ok"] is False
    assert out["action"] == "rolled_back"
    history = await state.list_update_history("u", "x")
    assert history[0]["status"] == "rolled_back"
