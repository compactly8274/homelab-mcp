"""The apply step: pull a new image and ``docker compose up -d``."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from homelab_mcp.hosts.base import CommandResult, HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.snapshot import StackSnapshot, stack_dir_of

log = logging.getLogger(__name__)


def _read_remote_compose(host: HostClient, stack_dir: str) -> tuple[str, str]:
    """Read the compose file from the host, return (text, sha256).

    Best-effort: the file path is ``<stack_dir>/compose.yaml`` or
    ``<stack_dir>/docker-compose.yaml``. Returns ("", sha256("")) if
    the host can't be reached or the file doesn't exist.
    """
    import asyncio
    for filename in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        try:
            r = asyncio.get_event_loop().run_until_complete(
                host.run_command(f"cat {stack_dir.rstrip('/')}/{filename}", timeout=10.0)
            ) if asyncio.get_event_loop().is_running() else None
        except Exception:
            r = None
        if r is None:
            # Outside an event loop: do it the proper async way
            r = _sync_run(host, f"cat {stack_dir.rstrip('/')}/{filename}", 10.0)
        if r and r.ok and r.stdout.strip():
            return r.stdout, hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    return "", hashlib.sha256(b"").hexdigest()


def _sync_run(host: HostClient, cmd: str, timeout: float) -> CommandResult | None:
    """Run a command synchronously (only used at compose-hash time)."""
    import asyncio
    try:
        return asyncio.run(host.run_command(cmd, timeout=timeout))
    except Exception:
        return None


async def _async_read_compose(host: HostClient, stack_dir: str) -> tuple[str, str]:
    """Async helper: read compose file, return (text, sha256)."""
    for filename in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        path = f"{stack_dir.rstrip('/')}/{filename}"
        try:
            r = await host.run_command(f"cat {path}", timeout=10.0)
        except Exception as e:
            log.debug("read compose %s failed: %s", path, e)
            r = CommandResult(exit_code=1, stdout="", stderr=str(e), duration_ms=0)
        if r.ok and r.stdout.strip():
            return r.stdout, hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    return "", hashlib.sha256(b"").hexdigest()


async def _probe_container(host: HostClient, name: str, *, settle_s: int = 5) -> bool:
    """Check that a container is running and (if it has a healthcheck) healthy.

    Returns True iff the container is running and:
    - has no healthcheck, OR
    - has a passing healthcheck, OR
    - healthcheck is starting within settle_s seconds (Plex warm-up)
    """
    time.sleep(settle_s)
    try:
        info = await host.inspect_container(name)
    except Exception as e:
        log.warning("probe: inspect %s failed: %s", name, e)
        return False
    state = (info.get("State") or {})
    if state.get("Status") != "running":
        return False
    health = state.get("Health") or {}
    if not health:
        return True
    h_status = health.get("Status")
    return h_status in ("healthy", "starting")


async def apply_update(
    host: HostClient,
    state: State,
    *,
    stack: str,
    snapshot: StackSnapshot,
    settle_seconds: int = 5,
) -> dict[str, Any]:
    """Pull the new image, restart the stack, and probe.

    Returns a structured result with the from/to digests, the new
    compose hash, and the post-update probe status of every service.
    """
    stack_dir = stack_dir_of(snapshot)
    if not stack_dir:
        return {
            "ok": False,
            "error": f"no stack_dir resolved for {stack} on {host.name}; cannot apply",
        }

    _compose_text, compose_hash = await _async_read_compose(host, stack_dir)
    pull = await host.compose_pull(stack_dir)
    if not pull.ok:
        return {
            "ok": False,
            "error": f"docker compose pull failed (exit={pull.exit_code}): {pull.stderr[:400]}",
            "from_digest": snapshot.manifest_digest,
            "compose_hash": compose_hash,
        }
    up = await host.compose_up(stack_dir)
    if not up.ok:
        return {
            "ok": False,
            "error": f"docker compose up -d failed (exit={up.exit_code}): {up.stderr[:400]}",
            "from_digest": snapshot.manifest_digest,
            "compose_hash": compose_hash,
        }

    # Find every service in the new stack
    cs = await host.list_containers(all=True)
    matching = [c for c in cs if c.get("PROJECT") == snapshot.stack or c.get("NAME") == snapshot.stack]
    if not matching:
        return {
            "ok": False,
            "error": f"after up, no containers found for stack {stack}",
            "from_digest": snapshot.manifest_digest,
            "compose_hash": compose_hash,
        }

    services_ok: dict[str, bool] = {}
    new_digests: dict[str, str | None] = {}
    for c in matching:
        svc = c.get("SERVICE") or c["NAME"]
        try:
            info = await host.inspect_container(c["NAME"])
        except Exception as e:
            log.warning("post-apply inspect %s failed: %s", c["NAME"], e)
            services_ok[svc] = False
            continue
        new_digests[svc] = (info.get("RepoDigests") or [None])[0]
        services_ok[svc] = await _probe_container(host, c["NAME"], settle_s=settle_seconds)

    all_ok = all(services_ok.values())
    return {
        "ok": all_ok,
        "from_digest": snapshot.manifest_digest,
        "services": services_ok,
        "new_digests": new_digests,
        "compose_hash": compose_hash,
        "stack_dir": stack_dir,
    }
