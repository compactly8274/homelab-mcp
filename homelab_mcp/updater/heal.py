"""Auto-heal: detect and recover from broken containers.

The pipeline:

1. Probe the container. If it's already healthy, return ``ok=True, action=already_healthy``.
2. If unhealthy, record the failure mode (state, restart count, health
   status, last logs line). The failure mode drives the recovery choice.
3. Action 1: ``docker restart <name>``. This is the most common fix for
   transient issues (process crash, leaked fds, bad initial state).
   Wait ``settle_seconds``, re-probe.
4. If still unhealthy AND a snapshot is supplied, try rollback (pull
   the old image by digest + ``docker compose up -d``). This is the
   safety net for "I updated this and it broke" scenarios.
5. If still unhealthy, return ``ok=False`` and the caller (cron or HTTP
   handler) is expected to notify via ntfy. We do NOT loop: a
   persistently broken container needs a human, and infinite restart
   loops are how you DOS a host.

This is intentionally conservative: a single restart attempt + a single
rollback attempt, then give up and surface the problem. False positives
on healthy containers (transient network blip during a probe) are
acceptable; false negatives on broken containers are not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.updater.snapshot import StackSnapshot, stack_dir_of

log = logging.getLogger(__name__)


@dataclass
class HealOutcome:
    """Result of a heal attempt. The MCP/tool layer reads ``.to_dict()``."""

    ok: bool
    action: str  # already_healthy | restarted | rolled_back | needs_human | failed
    name: str
    host: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    actions_taken: list[str] = field(default_factory=list)
    error: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "name": self.name,
            "host": self.host,
            "before": self.before,
            "after": self.after,
            "actions_taken": self.actions_taken,
            "error": self.error,
            "notes": self.notes,
        }


# Failure modes that warrant a heal attempt. Maps the docker ``State``
# string to a short human-readable label.
_UNHEALTHY_STATES = {"restarting", "exited", "dead", "paused", "created"}
_UNHEALTHY_HEALTH = {"unhealthy", "starting"}


async def _inspect(host: HostClient, name: str) -> dict[str, Any] | None:
    try:
        return await host.inspect_container(name)
    except Exception as e:
        log.debug("heal: inspect %s failed: %s", name, e)
        return None


def _classify(info: dict[str, Any]) -> dict[str, Any]:
    """Pull the four signals we care about from a docker inspect blob."""
    state = (info.get("State") or {})
    health = (state.get("Health") or {})
    return {
        "status": state.get("Status", ""),
        "running": state.get("Running", False),
        "restarting": state.get("Restarting", False),
        "pid": state.get("Pid", 0),
        "exit_code": state.get("ExitCode", 0),
        "error": state.get("Error", ""),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "health_status": health.get("Status", ""),
        "health_failing_streak": health.get("FailingStreak", 0),
        "restart_count": (info.get("RestartCount") or
                          (info.get("State") or {}).get("RestartCount") or 0),
    }


def _is_unhealthy(snap: dict[str, Any]) -> bool:
    """Return True iff the container is in a state worth trying to heal."""
    if snap["status"] in _UNHEALTHY_STATES:
        return True
    return bool(
        snap["status"] == "running"
        and snap["health_status"] in _UNHEALTHY_HEALTH
    )


async def _probe(
    host: HostClient, name: str, *, settle_seconds: int = 10
) -> dict[str, Any] | None:
    """Re-inspect after a settle window. Returns the classify-snapshot or
    None on inspect error."""
    await asyncio.sleep(settle_seconds)
    info = await _inspect(host, name)
    return _classify(info) if info else None


async def heal_container(
    host: HostClient,
    name: str,
    *,
    snapshot: StackSnapshot | None = None,
    settle_seconds: int = 10,
) -> HealOutcome:
    """Try to recover a single container. See module docstring for the
    full strategy. Returns a :class:`HealOutcome` describing what
    happened; the caller decides whether to notify.
    """
    outcome = HealOutcome(ok=False, action="failed", name=name, host=host.name)

    info = await _inspect(host, name)
    if info is None:
        outcome.error = f"could not inspect container {name!r}"
        return outcome

    outcome.before = _classify(info)

    if not _is_unhealthy(outcome.before):
        outcome.ok = True
        outcome.action = "already_healthy"
        outcome.notes = f"status={outcome.before['status']!r}, health={outcome.before['health_status']!r}"
        return outcome

    # Action 1: docker restart.
    log.info("heal: %s/%s is unhealthy (%s) — attempting restart",
             host.name, name, outcome.before["status"])
    restart = await host.run_command(
        f"docker restart {name}", timeout=60.0
    )
    outcome.actions_taken.append("docker restart")
    if not restart.ok:
        outcome.error = f"docker restart failed: {restart.stderr[:300]}"
        outcome.notes = "restart command failed; container may not be restartable"
        return outcome

    after_restart = await _probe(host, name, settle_seconds=settle_seconds)
    if after_restart is None:
        outcome.error = "could not re-inspect after restart"
        return outcome
    outcome.after = after_restart

    if not _is_unhealthy(after_restart):
        outcome.ok = True
        outcome.action = "restarted"
        outcome.notes = (
            f"restart fixed it: status went {outcome.before['status']!r} -> "
            f"{after_restart['status']!r}, health {outcome.before['health_status']!r} -> "
            f"{after_restart['health_status']!r}"
        )
        return outcome

    # Action 2: rollback from snapshot if we have one.
    if snapshot is None:
        outcome.action = "needs_human"
        outcome.error = (
            f"still unhealthy after restart: status={after_restart['status']!r}, "
            f"health={after_restart['health_status']!r}. "
            f"No snapshot supplied; cannot roll back. Investigate manually."
        )
        return outcome

    stack_dir = stack_dir_of(snapshot)
    if not stack_dir or not snapshot.manifest_digest:
        outcome.action = "needs_human"
        outcome.error = (
            "still unhealthy after restart. Snapshot lacks stack_dir/manifest_digest; "
            "cannot roll back. Investigate manually."
        )
        return outcome

    log.info("heal: %s/%s still unhealthy — rolling back to %s",
             host.name, name, snapshot.manifest_digest[:19])
    outcome.actions_taken.append("rollback")
    # Local import: avoid a circular dependency at module load.
    from homelab_mcp.updater.rollback import rollback_stack
    rb = await rollback_stack(
        host, snapshot=snapshot,
        reason=f"auto-heal: container unhealthy after restart ({after_restart['status']!r})",
    )
    if rb.get("ok"):
        outcome.ok = True
        outcome.action = "rolled_back"
        outcome.notes = (
            f"rolled back to {snapshot.manifest_digest[:19]}; container "
            f"should now be back to the previously-known-good state"
        )
        return outcome

    outcome.action = "needs_human"
    outcome.error = (
        f"still unhealthy after restart, and rollback failed: {rb.get('error', '?')}. "
        f"Manual intervention required."
    )
    return outcome


async def scan_and_heal(
    host: HostClient,
    *,
    snapshot_provider: Any = None,
    settle_seconds: int = 10,
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """Scan a host for unhealthy containers and attempt to heal each one.

    ``snapshot_provider`` is an optional async callable ``(stack_name) -> StackSnapshot|None``
    that the heal pipeline uses to fetch a snapshot for rollback. If
    None, only the restart path is exercised.

    Returns a summary dict with the host name, the list of heal
    outcomes, and aggregate counts.
    """
    cs = await host.list_containers(all=True)
    # Pre-classify; only attempt heal on the bad ones.
    candidates: list[str] = []
    for c in cs:
        nm = c.get("NAME", "")
        if not nm:
            continue
        info = await _inspect(host, nm)
        if info is None:
            continue
        snap = _classify(info)
        if _is_unhealthy(snap):
            candidates.append(nm)

    log.info("heal: %s found %d unhealthy of %d total",
             host.name, len(candidates), len(cs))

    sem = asyncio.Semaphore(max_concurrent)
    outcomes: list[HealOutcome] = []

    async def _one(nm: str) -> HealOutcome:
        async with sem:
            snap = None
            if snapshot_provider is not None:
                try:
                    snap = await snapshot_provider(nm)
                except Exception as e:
                    log.debug("heal: snapshot_provider(%s) raised: %s", nm, e)
                    snap = None
            return await heal_container(
                host, nm, snapshot=snap, settle_seconds=settle_seconds,
            )

    results = await asyncio.gather(*[_one(n) for n in candidates])
    outcomes.extend(results)

    return {
        "host": host.name,
        "scanned": len(cs),
        "unhealthy_found": len(candidates),
        "healed": sum(1 for o in outcomes if o.ok),
        "needs_human": sum(1 for o in outcomes if o.action == "needs_human"),
        "outcomes": [o.to_dict() for o in outcomes],
    }
