"""Image-drift scanner.

Walks a host's running containers, compares each one's local image
digest to the registry's current digest, and records any drift to the
state layer's ``pending_updates`` table.

The scanner does NOT apply updates. The apply pipeline reads from
``pending_updates`` after a risk-classification step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.registry import fetch_remote_digest, parse_image_ref


log = logging.getLogger(__name__)


def _local_digest_from_inspect(inspect: dict[str, Any]) -> str | None:
    """Return the local manifest digest of the image, if we can find one.

    Two fields matter:

    1. ``RepoDigests`` — a list of ``<repo>@sha256:<hex>`` strings. This
       is the **manifest digest** of the pulled image.
    2. ``Image`` — the sha256 of the image's config JSON. This is a
       **config digest**, which is computed from the image metadata
       and is therefore NOT content-addressed the same way.

    We prefer the manifest digest; fall back to config digest if absent.
    Returns None when neither is present.
    """
    digests = inspect.get("RepoDigests") or []
    for d in digests:
        if "@sha256:" in d:
            return d.split("@", 1)[1]
    img = inspect.get("Image")
    if isinstance(img, str) and img.startswith("sha256:"):
        return img
    return None


async def scan_host(
    host: HostClient,
    state: State,
    *,
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Scan all running containers on ``host`` for image drift.

    Returns a list of drift rows that were written, with keys:
    ``container``, ``image``, ``local_digest``, ``remote_digest``,
    ``host``.
    """
    containers = await host.list_containers(all=False)  # running only
    # Defense in depth: filter on STATE too.
    containers = [c for c in containers if c.get("STATE") == "running"]
    written: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)

    async def _scan_one(c: dict[str, Any]) -> dict[str, Any] | None:
        name = c.get("NAME")
        image = c.get("IMAGE")
        if not name or not image:
            return None
        async with sem:
            try:
                inspect = await host.inspect_container(name)
            except (KeyError, Exception) as e:
                log.warning("inspect %s on %s failed: %s", name, host.name, e)
                return None
            local = _local_digest_from_inspect(inspect)
            if not local:
                log.debug("container %s: no local digest in inspect", name)
                return None
            try:
                ref = parse_image_ref(image)
            except Exception as e:
                log.warning("parse %r: %s", image, e)
                return None
            remote_result = await fetch_remote_digest(ref)
            if remote_result.kind != "ok":
                log.debug("container %s: registry %s (%s)",
                          name, remote_result.kind, remote_result.detail[:200])
                return None
            remote = remote_result.digest
            if remote == local:
                return None
            await state.record_pending_update(
                host=host.name, stack=name,
                current_digest=local, latest_digest=remote,
            )
            return {
                "host": host.name, "container": name, "image": image,
                "local_digest": local, "remote_digest": remote,
            }

    results = await asyncio.gather(*[_scan_one(c) for c in containers])
    written.extend(r for r in results if r is not None)
    return written
