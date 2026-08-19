"""Image-drift scanner.

Walks a host's running containers, compares each one's local image
digest to the registry's current digest, and records any drift to the
state layer's ``pending_updates`` table.

The scanner does NOT apply updates. The apply pipeline reads from
``pending_updates`` after a risk-classification step.

Stack key selection (the v0.9.13 hermes-patched behavior):

The pending_updates row is keyed by a stable stack identity that the
apply pipeline can resolve AND dismiss by. The dismiss side of the
pipeline uses the compose project label (``com.docker.compose.project``)
as the stack identity, so the scanner writes the same key:

  1. ``com.docker.compose.project`` (compose stacks) — the canonical key.
  2. ``com.docker.compose.service`` (compose services in rare single-service
     stacks where the project label is missing) — fallback.
  3. Container NAME (dockerman, bare Community Applications, non-compose) —
     last-resort fallback. Matches legacy v0.9.12 behavior.

This fixes the bug where scanner wrote the container NAME and the dismiss
path used the project label — the keys never matched, so flags
re-populated after every apply and the dashboard never cleared.

Also performs a one-shot legacy-key self-heal: when a new project-keyed
row is written, any old container-NAME-keyed row for a container in that
project is dismissed. Old scanner keys (``immich_server``) → new key
(``immich``) is a no-op from the operator's perspective after the next
scan tick.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import logging
import shlex
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.registry import fetch_remote_digest, parse_image_ref, RegistryResult

log = logging.getLogger(__name__)


def _local_digest_from_inspect(inspect: dict[str, Any]) -> str | None:
    """Return the local manifest digest of the image, if we can find one.

    For multi-arch images, ``RepoDigests`` may contain BOTH the index
    manifest digest and the platform-specific manifest digest.
    ``fetch_remote_digest`` returns the platform digest; Docker may run
    from the platform digest. Returning the first digest caused
    permanent false-positive drift, so we now return a digest "set"
    string (space-separated) and the caller checks membership.

    Falls back to the config digest if no RepoDigests are present.
    """
    digests = inspect.get("RepoDigests") or []
    found = []
    for d in digests:
        if "@sha256:" in d:
            found.append(d.split("@", 1)[1])
    if found:
        return " ".join(found)
    img = inspect.get("Image")
    if isinstance(img, str) and img.startswith("sha256:"):
        return img
    return None


async def _resolve_local_platform_digest(host: HostClient, image_ref: str, fallback_id: str | None) -> str | None:
    """Resolve the linux/amd64 manifest digest from the local image.

    When RepoDigests is empty or only contains the manifest-list index
    digest, we must inspect the local image manifest directly to find the
    platform-specific digest that matches what the registry returns.
    """
    for ref in (image_ref, fallback_id):
        if not ref:
            continue
        r = await host.run_command(
            f"docker image inspect {shlex.quote(ref)} --format '{{{{json .RepoDigests}}}}'",
            timeout=15.0,
        )
        if not r.ok:
            continue
        try:
            data = json.loads(r.stdout or "[]")
        except Exception:
            continue
        # If RepoDigests has a platform-specific digest (longer list or
        # only one digest on single-arch), return the first one. We
        # already did a broader scan above; this is a final fallback.
        for d in data:
            if "@sha256:" in d:
                return d.split("@", 1)[1]
    return None


def _ignored_patterns() -> set[str]:
    """Parse HOMELAB_MCP_AUTO_APPLY_IGNORE env var into a set of patterns."""
    raw = (os.environ.get("HOMELAB_MCP_AUTO_APPLY_IGNORE") or "").strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            return {str(s).strip() for s in json.loads(raw) if str(s).strip()}
        except Exception:
            return {s.strip() for s in raw.strip("[]").replace('"', "").split(",") if s.strip()}
    return {s.strip() for s in raw.split(",") if s.strip()}


def _is_ignored(stack: str, host: str, patterns: set[str]) -> bool:
    """True if stack/host matches an ignore pattern (supports wildcards)."""
    if not patterns:
        return False
    candidate = f"{host}/{stack}"
    return any(fnmatch.fnmatch(candidate, pat) or fnmatch.fnmatch(stack, pat) for pat in patterns)


def _stack_key_for_container(c: dict[str, Any]) -> tuple[str, str]:
    """Pick the canonical stack key for one container, plus the source.

    Returns ``(stack_key, source)`` where ``source`` is one of
    ``"project"``, ``"service"``, or ``"name"`` — useful for logging
    and for the legacy-key self-heal that only runs when source
    is ``"project"``.

    The container's flat-dict shape comes from
    ``HostClient.list_containers()`` which already populates
    ``PROJECT`` and ``SERVICE`` from the compose labels.
    """
    project = (c.get("PROJECT") or "").strip()
    if project:
        return project, "project"
    service = (c.get("SERVICE") or "").strip()
    if service:
        return service, "service"
    name = (c.get("NAME") or "").strip()
    if name:
        return name, "name"
    return "", "name"


# Common Compose project directories used by the apply pipeline.  Keep this
# list in sync with whatever the updater uses to resolve stack_dir.  If the
# scanner picks a project label whose compose directory is gone, the apply
# pipeline will fail every time; skipping it here prevents zombie pending rows.
_COMPOSE_PROJECT_SEARCH_PATHS: tuple[str, ...] = (
    "/boot/config/plugins/compose.manager/projects/{project}/compose.yml",
    "/boot/config/plugins/compose.manager/projects/{project}/docker-compose.yml",
    "/app/projects/{project}/compose.yml",
    "/app/projects/{project}/docker-compose.yml",
    "/mnt/Data/appdata/dockge/stacks/{project}/compose.yml",
    "/mnt/Data/appdata/dockge/stacks/{project}/docker-compose.yml",
    "/opt/stacks/{project}/compose.yml",
    "/opt/stacks/{project}/docker-compose.yml",
    "/mnt/.ix-apps/app_configs/{project}/versions/*/templates/rendered/compose.yml",
    "/mnt/.ix-apps/app_configs/{project}/versions/*/templates/rendered/docker-compose.yml",
)


async def _compose_project_exists(host: Any, project: str) -> bool:
    """True if a Compose project has at least one candidate compose file."""
    for tpl in _COMPOSE_PROJECT_SEARCH_PATHS:
        candidate = tpl.format(project=project)
        # The ix-apps path contains a glob; use ls instead of test -f.
        if "*" in candidate:
            r = await host.run_command(
                f"ls {shlex.quote(candidate)} >/dev/null 2>&1",
                timeout=10.0,
            )
        else:
            r = await host.run_command(
                f"test -f {shlex.quote(candidate)}",
                timeout=10.0,
            )
        if r.ok:
            return True
    return False


async def _dismiss_legacy_keys_for_project(
    state: State,
    host: str,
    host_client: HostClient,
    project: str,
    new_stack: str,
) -> int:
    """Dismiss any pending rows keyed by container NAME for containers
    in the given project that are NOT the new stack key itself.

    Lists the host's running containers, finds every container whose
    compose project label matches ``project``, builds a set of their
    container NAMEs, and dismisses any pending row whose stack key is
    in that set (and is not the new project key).

    Called whenever the scanner writes a new project-keyed row, to
    self-heal the dashboard after the first scan post-upgrade.

    Returns the number of legacy rows dismissed.
    """
    try:
        running = await host_client.list_containers(all=True)
    except Exception as e:
        log.warning("self-heal: list_containers failed: %s", e)
        return 0

    # Build the set of container NAMEs in this project. These are the
    # candidates for legacy v0.9.12-style row keys.
    names_in_project = {
        (c.get("NAME") or "").strip()
        for c in running
        if (c.get("PROJECT") or "").strip() == project
        and (c.get("NAME") or "").strip()
    }
    if not names_in_project:
        return 0

    try:
        rows = await state.list_pending_updates(host=host)
    except Exception as e:
        log.warning("self-heal: list_pending_updates failed: %s", e)
        return 0

    dismissed = 0
    for row in rows:
        old_stack = (row.get("stack") or "").strip()
        latest = (row.get("latest_digest") or "").strip()
        if old_stack in names_in_project and old_stack != new_stack:
            try:
                deleted = await state.mark_update_seen(host, old_stack, latest)
            except Exception as e:
                log.warning(
                    "self-heal: dismiss %s/%s failed: %s",
                    host, old_stack, e,
                )
                continue
            if deleted:
                log.info(
                    "self-heal: dismissed legacy pending row host=%s old_stack=%s "
                    "(superseded by project=%s)",
                    host, old_stack, project,
                )
                dismissed += 1
    return dismissed


async def scan_host(
    host: HostClient,
    state: State,
    *,
    concurrency: int = 4,
    enable_legacy_self_heal: bool = True,
) -> list[dict[str, Any]]:
    """Scan all running containers on ``host`` for image drift.

    Returns a list of drift rows that were written, with keys:
    ``container``, ``image``, ``local_digest``, ``remote_digest``,
    ``host``, ``stack`` (the new project-keyed identity).

    If ``enable_legacy_self_heal`` is True (the default), any legacy
    container-NAME-keyed pending rows for the same project are
    dismissed after a new project-keyed row is written. This is the
    one-shot cleanup that brings the dashboard to a clean state
    after the first scan post-upgrade; subsequent scans are no-ops.
    """
    containers = await host.list_containers(all=False)  # running only
    # Defense in depth: filter on STATE too.
    containers = [c for c in containers if c.get("STATE") == "running"]
    written: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)

    async def _local_manifest_digest(image_ref: str, fallback_id: str | None) -> str | None:
        """Return the local manifest digest set for ``image_ref``.

        Container inspect ``RepoDigests`` is sometimes empty (e.g. after a
        force-recreate from a pinned tag). Fall back to inspecting the
        image by its ref or ID and reading its ``RepoDigests``.

        Multi-arch images have both the manifest-list index digest and the
        platform-specific digest in ``RepoDigests``. Returning only the
        first digest caused false positives when the registry resolved to
        the other one, so we now return all digests as a space-separated
        set (matching ``_local_digest_from_inspect``).
        """
        for ref in (image_ref, fallback_id):
            if not ref:
                continue
            r = await host.run_command(
                f"docker image inspect {shlex.quote(ref)} --format '{{{{json .RepoDigests}}}}'",
                timeout=15.0,
            )
            if not r.ok:
                continue
            try:
                data = json.loads(r.stdout or "[]")
            except Exception:
                continue
            found = [d.split("@", 1)[1] for d in data if "@sha256:" in d]
            if found:
                return " ".join(found)
        return None

    async def _scan_one(c: dict[str, Any]) -> dict[str, Any] | None:
        name = c.get("NAME")
        image = c.get("IMAGE")
        if not name or not image:
            return None
        stack_key, key_source = _stack_key_for_container(c)
        if not stack_key:
            return None
        # Default behavior: only auto-track compose-project stacks. Single
        # Community-Apps / dockerman containers require a different update
        # workflow (template update, not compose pull), so tracking them here
        # creates a permanent backlog of pending rows that the apply pipeline
        # cannot satisfy.
        if key_source != "project" and not os.environ.get(
            "HOMELAB_MCP_SCANNER_INCLUDE_NON_COMPOSE"
        ):
            log.debug(
                "scanner: skipping non-project container %s/%s (source=%s); "
                "set HOMELAB_MCP_SCANNER_INCLUDE_NON_COMPOSE=1 to track",
                host.name, name, key_source,
            )
            return None
        if _is_ignored(stack_key, host.name, _ignored_patterns()):
            log.debug("scanner: ignoring stack %s/%s per AUTO_APPLY_IGNORE", host.name, stack_key)
            return None
        # Skip project-keyed stacks whose compose directory no longer exists.
        # Without this, a running container whose project was removed creates
        # a pending row that the apply pipeline can never satisfy.
        if key_source == "project":
            try:
                exists = await _compose_project_exists(host, stack_key)
            except Exception as e:
                log.warning("scanner: project-exists check %s/%s failed: %s", host.name, stack_key, e)
                exists = True  # fail open: let the row be written and the apply pipeline handle it
            if not exists:
                log.info(
                    "scanner: skipping dead project %s/%s (no compose file found)",
                    host.name, stack_key,
                )
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
            # If the local digest came from the container's config digest
            # (RepoDigests was empty), try to resolve the real manifest digest
            # from the local image. Comparing config digest to remote manifest
            # digest causes permanent false-positive drift.
            repo_digests = inspect.get("RepoDigests") or []
            if not repo_digests:
                manifest_local = await _local_manifest_digest(
                    inspect.get("Config", {}).get("Image", image),
                    inspect.get("Image"),
                )
                if manifest_local:
                    local = manifest_local
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
            # Prefer the platform-specific digest from local RepoDigests when
            # the registry returned a platform digest. This eliminates false
            # positives caused by comparing a manifest-list index digest locally
            # to a platform manifest digest remotely.
            local_set = (local or "").split()
            if remote not in local_set:
                # Try to resolve platform digest from local image manifest.
                platform_local = await _resolve_local_platform_digest(
                    host,
                    inspect.get("Config", {}).get("Image", image),
                    inspect.get("Image"),
                )
                if platform_local:
                    local_set = [platform_local]
                    local = platform_local
            if remote in (local or "").split():
                return None
            current_digest = (local or "").split()[0] if local else None
            await state.record_pending_update(
                host=host.name, stack=stack_key,
                current_digest=current_digest, latest_digest=remote,
            )
            # Self-heal: if we just wrote a project-keyed row, dismiss
            # any legacy container-NAME-keyed row for the same project
            # so the dashboard doesn't show duplicates.
            if enable_legacy_self_heal and key_source == "project":
                try:
                    await _dismiss_legacy_keys_for_project(
                        state, host.name, host, stack_key, stack_key,
                    )
                except Exception as e:
                    log.warning(
                        "self-heal %s/%s failed: %s",
                        host.name, stack_key, e,
                    )
            return {
                "host": host.name, "container": name, "image": image,
                "stack": stack_key, "stack_key_source": key_source,
                "local_digest": local, "remote_digest": remote,
            }

    results = await asyncio.gather(*[_scan_one(c) for c in containers])
    written.extend(r for r in results if r is not None)
    return written