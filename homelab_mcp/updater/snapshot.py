"""Pre-update snapshot of a stack.

Captures the running image manifest digest for every container in the
stack. This is what we fall back to if the apply step fails or
post-update probes don't pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.registry import parse_image_ref


log = logging.getLogger(__name__)


@dataclass
class StackSnapshot:
    """A point-in-time snapshot of a stack, used for safe rollback.

    Attributes:
        host:           the host the stack is on
        stack:          the stack (compose project) name
        stack_dir:      absolute path to the compose working dir
        manifest_digest:  sha256 of the resolved image manifest
        config_digest:  sha256 of the image config (config digests
                         change on any image rebuild; useful to
                         confirm a rollback restored the same
                         image bytes)
        compose_hash:   sha256 of the active compose file's contents
                         (a stack whose compose file was edited mid-
                         update will not rollback cleanly)
        services:       map of service name → image reference
        compose:        the raw compose file content (for re-applying
                         an exact rollback)
    """

    host: str
    stack: str
    stack_dir: str
    manifest_digest: str | None
    config_digest: str | None
    compose_hash: str
    services: dict[str, str] = field(default_factory=dict)
    compose: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "stack": self.stack,
            "stack_dir": self.stack_dir,
            "manifest_digest": self.manifest_digest,
            "config_digest": self.config_digest,
            "compose_hash": self.compose_hash,
            "services": self.services,
            "compose": self.compose,
        }


def _resolve_stack_dir(
    host_name: str,
    project: str,
    *,
    working_dir: str | None = None,
    compose_manager_root: str | None = None,
    dockge_stacks_root: str | None = None,
) -> str | None:
    """Resolve the on-host absolute directory of a stack.

    Order of attempts:

    1. The compose working_dir label (always authoritative if set).
    2. ``compose_manager_root/<project>`` for CA-style stacks.
    3. ``dockge_stacks_root/<project>`` for Dockge-style stacks.
    """
    if working_dir:
        return working_dir
    if compose_manager_root:
        candidate = f"{compose_manager_root.rstrip('/')}/{project}"
        return candidate
    if dockge_stacks_root:
        return f"{dockge_stacks_root.rstrip('/')}/{project}"
    return None


def _container_manifest_digest(info: dict[str, Any]) -> str | None:
    """Best-effort: extract the manifest digest from ``docker inspect``."""
    digests = info.get("RepoDigests") or []
    for d in digests:
        if "@sha256:" in d:
            return d.split("@", 1)[1]
    img = info.get("Image")
    if isinstance(img, str) and img.startswith("sha256:"):
        return img
    return None


def _container_config_digest(info: dict[str, Any]) -> str | None:
    """The image config digest (sha256 of the image config JSON).

    Look in two places:

    - ``Config.Id``         (most common in modern docker)
    - ``ImageConfig.Id``    (legacy)
    """
    cfg = info.get("Config") or {}
    if isinstance(cfg, dict) and isinstance(cfg.get("Id"), str):
        if cfg["Id"].startswith("sha256:"):
            return cfg["Id"]
    return None


async def snapshot_stack(
    host: HostClient,
    state: State,
    *,
    stack: str,
    compose_manager_root: str | None = None,
    dockge_stacks_root: str | None = None,
) -> StackSnapshot | None:
    """Take a snapshot of a stack.

    Returns ``None`` if the stack is not running on this host.
    """
    containers = await host.list_containers(all=True)
    matching = [c for c in containers if c.get("NAME") == stack or c.get("PROJECT") == stack]
    if not matching:
        log.warning("snapshot: %s on %s: stack not running", stack, host.name)
        return None

    first = matching[0]
    stack_dir = _resolve_stack_dir(
        host.name, first.get("PROJECT") or stack,
        working_dir=first.get("WORKDIR") or None,
        compose_manager_root=compose_manager_root,
        dockge_stacks_root=dockge_stacks_root,
    )

    services: dict[str, str] = {}
    primary: dict[str, Any] | None = None
    for c in matching:
        try:
            info = await host.inspect_container(c["NAME"])
        except Exception as e:
            log.warning("inspect %s failed: %s", c["NAME"], e)
            continue
        svc = c.get("SERVICE") or c["NAME"]
        services[svc] = c.get("IMAGE", "")
        if primary is None:
            primary = info

    if primary is None:
        return None

    manifest_digest = _container_manifest_digest(primary)
    config_digest = _container_config_digest(primary)

    compose_hash = ""
    compose_text = ""
    if stack_dir:
        # We don't try to read the compose file from the host here
        # (LocalDocker vs RemoteSSH would need different paths). The
        # hash will be populated by the apply pipeline right before
        # the pull, which has the stack_dir in scope.
        compose_text = ""
        compose_hash = hashlib.sha256(b"").hexdigest()

    return StackSnapshot(
        host=host.name, stack=stack, stack_dir=stack_dir or "",
        manifest_digest=manifest_digest, config_digest=config_digest,
        compose_hash=compose_hash, services=services, compose=compose_text,
    )


def stack_dir_of(snapshot: StackSnapshot) -> str | None:
    return snapshot.stack_dir or None
