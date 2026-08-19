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


# -- v0.9.11: permanent-error classification -------------------------------


def test_is_permanent_apply_error_matches_known_substrings() -> None:
    """The classifier should match the strings that come out of
    apply.py:line 104 and the host.compose_pull fallback path."""
    from homelab_mcp.updater.pipeline import _is_permanent_apply_error
    assert _is_permanent_apply_error(
        "no stack_dir resolved for x on u; cannot apply"
    ) is True
    assert _is_permanent_apply_error(
        "docker compose pull failed (exit=2): stack dir does not exist: /srv/x"
    ) is True
    assert _is_permanent_apply_error(
        "no such file or directory: '/srv/x/compose.yaml'"
    ) is True
    # Transient: should NOT match
    assert _is_permanent_apply_error(
        "docker compose pull failed (exit=1): pull access denied for foo/bar"
    ) is False
    assert _is_permanent_apply_error(
        "docker compose up -d failed (exit=1): port already in use"
    ) is False
    assert _is_permanent_apply_error("") is False


async def test_run_pipeline_marks_permanent_error_as_failed(
    tmp_path: Path,
) -> None:
    """A missing stack dir during pull triggers the permanent path:
    the history row goes to ``failed`` (not ``rolled_back``) and the
    pipeline returns ``action=failed, permanent=True`` without trying
    to roll back (which would also fail)."""
    from homelab_mcp.updater.pipeline import _PERMANENT_APPLY_ERRORS

    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    class _MissingStackDirHost(_ApplyHost):
        async def compose_pull(self, stack_dir):
            # Simulate the real error string from local_docker.py
            # _run_compose line 258: "stack dir does not exist: ..."
            return CommandResult(
                2, "",
                f"stack dir does not exist: {stack_dir}",
                0,
            )

        async def compose_up(self, stack_dir):  # pragma: no cover
            raise AssertionError(
                "compose_up should not be called on a permanent failure"
            )

    h = _MissingStackDirHost("u")
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
    # Pipeline result
    assert out["ok"] is False
    assert out["action"] == "failed"
    assert out.get("permanent") is True
    assert "stack dir does not exist" in out["apply"]["error"]
    # History row
    history = await state.list_update_history("u", "x")
    assert len(history) == 1
    assert history[0]["status"] == "failed"
    assert "permanent" in history[0]["reason"]
    # Sanity: classifier returns True for this error string
    assert _PERMANENT_APPLY_ERRORS  # not empty
    from homelab_mcp.updater.pipeline import _is_permanent_apply_error
    assert _is_permanent_apply_error(out["apply"]["error"]) is True


# -- v0.9.11: orphaned in_progress sweep ------------------------------------


async def test_sweep_orphaned_in_progress_recovers_old_rows(
    tmp_path: Path,
) -> None:
    """Rows with status='in_progress' older than the threshold are
    marked 'rolled_back'. Recent rows are left alone."""
    from datetime import UTC, datetime, timedelta

    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    now = datetime.now(UTC)
    old_iso = (now - timedelta(seconds=1200)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    recent_iso = (now - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"

    # Insert an old in_progress row directly (simulating a daemon
    # that was killed before v0.9.11).
    db = await state._connect()
    try:
        await db.execute(
            """
            INSERT INTO update_history
                (host, stack, from_digest, to_digest,
                 status, started_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("u", "x", "sha256:aaa", "sha256:bbb",
             "in_progress", old_iso, "old apply"),
        )
        # And a recent in_progress row that should NOT be touched
        # (the threshold is 600s by default; 30s is well under).
        await db.execute(
            """
            INSERT INTO update_history
                (host, stack, from_digest, to_digest,
                 status, started_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("u", "y", "sha256:ccc", "sha256:ddd",
             "in_progress", recent_iso, "in flight"),
        )
        await db.commit()
    finally:
        await db.close()

    swept = await state.sweep_orphaned_in_progress(max_age_seconds=600)
    assert len(swept) == 1  # only the old one

    # The old row should now be 'rolled_back' with a reason.
    history = await state.list_update_history("u", "x")
    assert history[0]["status"] == "rolled_back"
    assert "orphaned by daemon restart" in history[0]["reason"]

    # The recent row should be untouched.
    history_y = await state.list_update_history("u", "y")
    assert history_y[0]["status"] == "in_progress"


async def test_sweep_orphaned_in_progress_is_idempotent(
    tmp_path: Path,
) -> None:
    """A second call after the first one is a no-op."""
    from datetime import UTC, datetime, timedelta

    state = State(db_path=tmp_path / "state.db")
    await state.init_db()

    old_iso = (datetime.now(UTC) - timedelta(seconds=7201)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    db = await state._connect()
    try:
        await db.execute(
            """
            INSERT INTO update_history
                (host, stack, from_digest, to_digest,
                 status, started_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("u", "x", "sha256:aaa", "sha256:bbb",
             "in_progress", old_iso, ""),
        )
        await db.commit()
    finally:
        await db.close()

    first = await state.sweep_orphaned_in_progress()
    assert len(first) == 1
    second = await state.sweep_orphaned_in_progress()
    assert second == []


async def test_init_db_runs_sweep_on_every_startup(tmp_path: Path) -> None:
    """init_db should call sweep_orphaned_in_progress even when the
    schema is already at v1 (the post-migration branch is the only
    place that runs the sweep; this test ensures the every-startup
    branch also runs it)."""
    from datetime import UTC, datetime, timedelta

    state = State(db_path=tmp_path / "state.db")
    # First init: pre-migration; bump user_version manually so the
    # v0.9.10 migration path is skipped.
    await state.init_db()
    db = await state._connect()
    try:
        await db.execute("PRAGMA user_version = 1")
        # Insert an old in_progress row (older than the default 7200s sweep
        # threshold so the every-startup sweep will recover it).
        old_iso = (datetime.now(UTC) - timedelta(seconds=7201)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        await db.execute(
            """
            INSERT INTO update_history
                (host, stack, from_digest, to_digest,
                 status, started_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("u", "x", "sha256:aaa", "sha256:bbb",
             "in_progress", old_iso, ""),
        )
        await db.commit()
    finally:
        await db.close()

    # Second init_db: user_version is already 1, so the migration
    # branch is skipped, but the every-startup sweep should still
    # fire.
    state2 = State(db_path=tmp_path / "state.db")
    await state2.init_db()
    history = await state2.list_update_history("u", "x")
    assert history[0]["status"] == "rolled_back"
    assert "orphaned by daemon restart" in history[0]["reason"]
