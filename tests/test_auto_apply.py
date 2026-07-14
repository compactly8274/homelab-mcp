"""Tests for the auto-apply orchestrator.

The orchestrator's job is to:

1. Inspect the running container to find labels and stack dir.
2. Fetch release notes for the new image.
3. Send the notes to the LLM classifier.
4. Apply policy: SAFE → apply; CAUTION → apply (default) or notify
   (safe-only); BREAKING → always notify.
5. Dismiss the pending row on a successful apply.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homelab_mcp.state import State
from homelab_mcp.updater.auto_apply import (
    _Inputs,
    evaluate_and_act,
    resolve_stack_dir,
)
from homelab_mcp.updater.release_notes import ReleaseNotes
from homelab_mcp.updater.risk import RiskVerdict

# -- resolve_stack_dir -----------------------------------------------------


def test_resolve_stack_dir_label_override() -> None:
    """A label override wins over compose and CA roots."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={"auto-update.stack-dir": "/custom/path"},
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    assert out == "/custom/path"


def test_resolve_stack_dir_compose_working_dir() -> None:
    """A compose working_dir from labels is used when no override is set."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={"com.docker.compose.project.working_dir": "/srv/app"},
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    assert out == "/srv/app"


def test_resolve_stack_dir_ca_compose_manager() -> None:
    """Falls back to <compose_manager_root>/<project>."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={"com.docker.compose.project": "radarr"},
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    assert out == "/srv/ca/radarr"


def test_resolve_stack_dir_dockge_owner_label() -> None:
    """A com.dockge.owner label switches to Dockge resolution."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={
            "com.docker.compose.project": "qbittorrent",
            "com.dockge.owner": "homelab",
        },
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/mnt/dockge/stacks",
    )
    assert out == "/mnt/dockge/stacks/qbittorrent"


def test_resolve_stack_dir_dockge_optin_label() -> None:
    """An auto-update.dockge=true label switches to Dockge resolution."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={
            "com.docker.compose.project": "qbittorrent",
            "auto-update.dockge": "true",
        },
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/mnt/dockge/stacks",
    )
    assert out == "/mnt/dockge/stacks/qbittorrent"


def test_resolve_stack_dir_no_project_returns_none() -> None:
    """Without a project name we cannot derive a path."""
    out = resolve_stack_dir(
        inspect_data={},
        container_labels={},
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/mnt/dockge/stacks",
    )
    assert out is None


# -- evaluate_and_act ------------------------------------------------------


class _FakeHost:
    def __init__(self, name, inspect_data):
        self._name = name
        self._inspect_data = inspect_data

    @property
    def name(self) -> str:
        return self._name

    async def inspect_container(self, name: str) -> dict:
        return self._inspect_data


def _make_inputs(stack: str = "radarr") -> _Inputs:
    return _Inputs(
        image="ghcr.io/owner/img:latest",
        stack=stack,
        to_digest="sha256:" + "b" * 64,
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )


async def test_safe_verdict_is_applied() -> None:
    """SAFE → apply via the pipeline; pending row dismissed."""
    state = State(db_path=Path("/tmp/auto_apply_safe.db"))
    await state.init_db()
    await state.record_pending_update(
        host="unraid", stack="radarr",
        current_digest="sha256:" + "a" * 64,
        latest_digest="sha256:" + "b" * 64,
    )
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "ghcr.io/owner/img:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": ["x@sha256:" + "b" * 64],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v1", source="github_release")),
        classify_release_notes=AsyncMock(return_value=RiskVerdict(risk="SAFE", summary="bug fix")),
        run_pipeline=pipeline, notifier=notifier,
    )
    assert out["action"] == "applied"
    pipeline.assert_awaited_once()
    assert pipeline.call_args.kwargs["stack"] == "radarr"
    assert pipeline.call_args.kwargs["to_digest"] == "sha256:" + "b" * 64
    notifier.notify.assert_not_called()


async def test_caution_verdict_applied_under_default_policy() -> None:
    """CAUTION → apply under 'safe-and-caution'."""
    state = State(db_path=Path("/tmp/auto_apply_caution_default.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v2", source="github_release")),
        classify_release_notes=AsyncMock(return_value=RiskVerdict(risk="CAUTION", summary="careful")),
        run_pipeline=pipeline, notifier=notifier,
        policy="safe-and-caution",
    )
    assert out["action"] == "applied"
    pipeline.assert_awaited_once()
    notifier.notify.assert_not_called()


async def test_caution_verdict_notified_under_safe_only() -> None:
    """CAUTION → notify under 'safe-only'."""
    state = State(db_path=Path("/tmp/auto_apply_caution_safe.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v2", source="github_release")),
        classify_release_notes=AsyncMock(return_value=RiskVerdict(risk="CAUTION", summary="careful")),
        run_pipeline=pipeline, notifier=notifier,
        policy="safe-only",
    )
    assert out["action"] == "notified_caution"
    pipeline.assert_not_awaited()
    notifier.notify.assert_awaited_once()


async def test_breaking_verdict_never_applies() -> None:
    """BREAKING → notify, never apply, regardless of policy."""
    state = State(db_path=Path("/tmp/auto_apply_breaking.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v3", source="github_release")),
        classify_release_notes=AsyncMock(return_value=RiskVerdict(
            risk="BREAKING", summary="drops v1 api",
            migration_steps=["rebuild clients"],
        )),
        run_pipeline=pipeline, notifier=notifier,
    )
    assert out["action"] == "notified_breaking"
    pipeline.assert_not_awaited()
    notifier.notify.assert_awaited_once()


async def test_no_release_notes_treated_as_caution_default() -> None:
    """Without notes, the verdict is CAUTION; default policy applies."""
    state = State(db_path=Path("/tmp/auto_apply_no_notes.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=None),
        classify_release_notes=AsyncMock(),
        run_pipeline=pipeline, notifier=notifier,
    )
    # No notes → CAUTION → apply under default policy
    assert out["action"] == "applied"


async def test_no_release_notes_notified_under_safe_only() -> None:
    """No notes + safe-only = notify."""
    state = State(db_path=Path("/tmp/auto_apply_no_notes_safe.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock()
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=None),
        classify_release_notes=AsyncMock(),
        run_pipeline=pipeline, notifier=notifier,
        policy="safe-only",
    )
    assert out["action"] == "notified_caution"
    pipeline.assert_not_awaited()
    notifier.notify.assert_awaited_once()


async def test_classifier_raising_falls_back_to_caution() -> None:
    """A classifier exception → CAUTION (apply under default)."""
    state = State(db_path=Path("/tmp/auto_apply_classifier_exc.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    pipeline = AsyncMock(return_value={"ok": True, "action": "applied"})

    async def _raise(**kw):
        raise RuntimeError("LLM down")

    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v1", source="github_release")),
        classify_release_notes=_raise,
        run_pipeline=pipeline, notifier=MagicMock(notify=AsyncMock()),
    )
    # classifier raised → CAUTION → apply under default
    assert out["action"] == "applied"
    assert "classifier raised" in out["verdict"]["summary"]


async def test_apply_failure_returns_action_failed() -> None:
    """A pipeline exception propagates as 'failed' action."""
    state = State(db_path=Path("/tmp/auto_apply_failed.db"))
    await state.init_db()
    host = _FakeHost("unraid", {
        "Config": {
            "Image": "x:latest",
            "Labels": {"com.docker.compose.project": "radarr"},
        },
        "RepoDigests": [],
    })
    async def _boom(*a, **kw):
        raise RuntimeError("docker daemon down")
    out = await evaluate_and_act(
        host=host, state=state, inputs=_make_inputs(),
        fetch_release_notes=AsyncMock(return_value=ReleaseNotes(text="x", tag="v1", source="github_release")),
        classify_release_notes=AsyncMock(return_value=RiskVerdict(risk="SAFE", summary="ok")),
        run_pipeline=_boom, notifier=MagicMock(notify=AsyncMock()),
    )
    assert out["action"] == "failed"
