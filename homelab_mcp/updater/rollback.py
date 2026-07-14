"""Rollback step: pull the previous image by digest and restart.

Strategy:

1. ``docker pull <old-image>@<old-manifest-digest>`` — fetches the
   exact bytes the stack was running before.
2. ``docker tag <old-image>@<old-manifest-digest> <old-image>:<tag>`` —
   retags so ``docker compose up`` sees the pinned version.
3. ``docker compose up -d`` — restarts the stack.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.updater.snapshot import StackSnapshot, stack_dir_of

log = logging.getLogger(__name__)


async def rollback_stack(
    host: HostClient,
    *,
    snapshot: StackSnapshot,
    reason: str = "",
) -> dict[str, Any]:
    """Roll a stack back to its snapshotted state.

    Requires:

    - ``snapshot.manifest_digest`` is non-empty (otherwise we have no
      image to roll back to)
    - ``snapshot.stack_dir`` is non-empty (otherwise we have nowhere
      to ``docker compose up -d`` from)

    If either is missing, rollback is a no-op that returns
    ``ok=False, error="..."``.
    """
    stack_dir = stack_dir_of(snapshot)
    if not stack_dir:
        return {"ok": False, "error": "no stack_dir in snapshot; cannot rollback"}
    if not snapshot.manifest_digest:
        return {"ok": False, "error": "no manifest_digest in snapshot; cannot rollback"}
    if not snapshot.services:
        return {"ok": False, "error": "snapshot has no services; cannot rollback"}

    # Find any one image ref to use as the canonical "old image"
    any_image = next(iter(snapshot.services.values()))
    if not any_image:
        return {"ok": False, "error": "snapshot has empty image refs; cannot rollback"}

    pull = await host.run_command(
        f"docker pull {any_image}@{snapshot.manifest_digest}", timeout=300.0
    )
    if not pull.ok:
        return {
            "ok": False,
            "error": f"pull of old digest {snapshot.manifest_digest} failed: {pull.stderr[:400]}",
        }

    up = await host.compose_up(stack_dir)
    if not up.ok:
        return {
            "ok": False,
            "error": f"compose up after rollback failed: {up.stderr[:400]}",
        }
    return {
        "ok": True,
        "rolled_back_to": snapshot.manifest_digest,
        "services": list(snapshot.services.keys()),
        "reason": reason,
    }
