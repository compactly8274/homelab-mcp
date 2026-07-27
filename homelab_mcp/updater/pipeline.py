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

v0.9.11: the pipeline distinguishes between *transient* apply
failures (network blip, image registry hiccup — rollback and
classify as ``rolled_back`` so the canary cron retries next cycle)
and *permanent* failures (stack dir doesn't exist, no stack_dir
resolved — retrying won't help, so we skip the rollback attempt
and mark the history row ``failed``). Permanent failures used to
cycle every 6h and inflate the rolled_back count without ever
notifying the operator.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.apply import apply_update
from homelab_mcp.updater.rollback import rollback_stack
from homelab_mcp.updater.snapshot import snapshot_stack

log = logging.getLogger(__name__)


# Substrings that indicate the apply error is permanent (config error
# rather than a transient pull/up hiccup). Matches on a substring of
# the error message from apply.py:line 104 ("no stack_dir resolved")
# or the stderr from host.compose_pull ("stack dir does not exist")
# which is the same root cause.
_PERMANENT_APPLY_ERRORS: tuple[str, ...] = (
    "no stack_dir resolved",
    "stack dir does not exist",
    "no such file or directory",  # compose_pull on a missing dir
)


def _is_permanent_apply_error(error: str) -> bool:
    """True iff the apply error indicates a config problem the canary
    cron will keep hitting forever (missing stack dir, etc.)."""
    if not error:
        return False
    e = error.lower()
    return any(needle in e for needle in _PERMANENT_APPLY_ERRORS)


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
        apply_error = apply_result.get("error", "?")
        if _is_permanent_apply_error(apply_error):
            # Permanent error (e.g. stack dir doesn't exist). Skip
            # the rollback attempt — there's nothing to roll back
            # to because the stack was never deployable in the
            # first place — and mark the history row as ``failed``
            # so the canary cron and the dashboard can distinguish
            # "this will never work" from "this just had a bad
            # apply". The pending row stays in the queue; the
            # operator must either create the stack dir or dismiss
            # the row manually. Without this distinction, the cron
            # would retry the same apply every 6h forever.
            log.error(
                "apply %s/%s permanent failure: %s — marking failed, "
                "NOT attempting rollback (would also fail)",
                host.name, stack, apply_error,
            )
            await state.update_update(
                row_id=row_id, status="failed",
                reason=f"permanent: {apply_error}",
                rollback_to_digest=None,
            )
            return {
                "ok": False,
                "action": "failed",
                "row_id": row_id,
                "from_digest": snap.manifest_digest,
                "to_digest": to_digest,
                "apply": apply_result,
                "permanent": True,
            }
        log.warning(
            "apply %s/%s failed: %s — rolling back",
            host.name, stack, apply_error,
        )
        rb = await rollback_stack(host, snapshot=snap, reason=apply_error)
        await state.update_update(
            row_id=row_id, status="rolled_back",
            reason=apply_error,
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
