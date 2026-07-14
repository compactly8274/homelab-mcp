"""Apply + auto-rollback orchestrator.

The pipeline is:

1. :func:`snapshot_stack` — capture the from state.
2. :func:`apply_update`  — pull the new image, up -d, probe.
3. If anything in step 2 fails (pull exit code, up exit code, or any
   service probe fails), call :func:`rollback_stack` and record the
   rollback in the state layer.
4. If step 2 succeeds, write the to-state to the state layer and
   return the result to the caller.

The pipeline accepts a ``dry_run`` flag. In dry-run mode we still
take a snapshot and run probes against the current state, but the
``docker compose pull`` and ``docker compose up -d`` are not
executed. This is the safe pre-flight mode.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.apply import apply_update
from homelab_mcp.updater.rollback import rollback_stack
from homelab_mcp.updater.snapshot import StackSnapshot, snapshot_stack


log = logging.getLogger(__name__)


async def run_pipeline(
    host: HostClient,
    state: State,
    *,
    stack: str,
    to_digest: str,
    compose_manager_root: str | None = None,
    dockge_stacks_root: str | None = None,
    dry_run: bool = False,
    settle_seconds: int = 5,
) -> dict[str, Any]:
    """Apply ``to_digest`` to ``stack`` on ``host``. Roll back on any failure.

    Returns a dict with: ``ok``, ``action`` (applied/dry_run/rolled_back/
    failed), ``from_digest``, ``to_digest``, ``snapshot``, ``apply``,
    ``rollback`` (only on failure).
    """
    snap = await snapshot_stack(
        host, state, stack=stack,
        compose_manager_root=compose_manager_root,
        dockge_stacks_root=dockge_stacks_root,
    )
    if snap is None:
        return {
            "ok": False,
            "action": "failed",
            "error": f"stack {stack!r} not running on host {host.name}",
        }

    if dry_run:
        return {
            "ok": True,
            "action": "dry_run",
            "from_digest": snap.manifest_digest,
            "to_digest": to_digest,
            "stack_dir": snap.stack_dir,
            "services": list(snap.services.keys()),
        }

    row_id = await state.record_update(
        host=host.name, stack=stack,
        from_digest=snap.manifest_digest or "unknown",
        to_digest=to_digest,
        status="in_progress",
        reason=f"pipeline {host.name}/{stack}",
    )

    apply_result = await apply_update(
        host, state, stack=stack, snapshot=snap, settle_seconds=settle_seconds,
    )

    if not apply_result.get("ok"):
        log.warning(
            "apply %s/%s failed: %s — rolling back",
            host.name, stack, apply_result.get("error", "?"),
        )
        rb = await rollback_stack(host, snapshot=snap, reason=apply_result.get("error", ""))
        await state.update_update(
            row_id=row_id, status="rolled_back",
            reason=apply_result.get("error", ""),
            rollback_to_digest=snap.manifest_digest,
        )
        return {
            "ok": False,
            "action": "rolled_back" if rb.get("ok") else "rollback_failed",
            "row_id": row_id,
            "from_digest": snap.manifest_digest,
            "to_digest": to_digest,
            "apply": apply_result,
            "rollback": rb,
        }

    await state.update_update(row_id=row_id, status="applied", reason="ok")
    return {
        "ok": True,
        "action": "applied",
        "row_id": row_id,
        "from_digest": snap.manifest_digest,
        "to_digest": to_digest,
        "snapshot": snap.to_dict(),
        "apply": apply_result,
    }
