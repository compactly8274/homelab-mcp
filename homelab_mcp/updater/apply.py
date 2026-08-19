"""The apply step: pull a new image and ``docker compose up -d``."""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import time
from pathlib import Path
from typing import Any

from homelab_mcp.hosts.base import CommandResult, HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.snapshot import StackSnapshot, stack_dir_of

log = logging.getLogger(__name__)


def _digest_in_repo_digests(to_digest: str | None, repo_digests: list[str] | None) -> bool:
    """Return True if to_digest matches any entry in RepoDigests.

    Multi-arch images expose both an index manifest digest and a
    platform-specific manifest digest in RepoDigests. Comparing only
    the first entry causes false-positive mismatch loops on amd64
    hosts where the platform digest is listed first.
    """
    if not to_digest or not repo_digests:
        return False
    to = to_digest.split(":")[-1]
    for d in repo_digests:
        if not d or "@sha256:" not in d:
            continue
        digest = d.split("@", 1)[1]
        if digest == to or digest.endswith(to) or to.endswith(digest):
            return True
    return False


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


def _image_has_tag(image: str) -> bool:
    """Return True if the last path component already has a tag or digest."""
    last = image.rsplit("/", 1)[-1]
    return ":" in last or "@" in last


def _tag_for(image: str) -> str:
    """Return the image ref to retag to so compose sees the new digest.

    If the image has no tag (e.g. `ghcr.io/cross-seed/cross-seed`),
    docker resolves it to `:latest` at runtime, so we retag to
    `...:latest`. If it already has a tag or digest, return as-is.
    """
    if not image or _image_has_tag(image):
        return image
    return f"{image}:latest"


async def _local_image_id(host: HostClient, tag: str) -> str | None:
    """Return the image ID that ``tag`` currently resolves to on the host."""
    r = await host.run_command(f"docker inspect --format '{{{{.Id}}}}' {tag}", timeout=15.0)
    if r.ok and r.stdout.strip():
        return r.stdout.strip()
    return None


def _timeout_for_stack_dir(stack_dir: str, default: float = 300.0) -> float:
    """Read per-stack timeout from /data/long_apply.yaml if available."""
    try:
        import yaml
        with open("/data/long_apply.yaml") as f:
            data = yaml.safe_load(f) or {}
        stack = Path(stack_dir).name
        return float(data.get("stacks", {}).get(stack, default))
    except Exception:
        return default



async def _wait_for_quiet_compose_state(
    host: HostClient,
    stack_dir: str,
    stack: str,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Wait until no compose container for this project is stuck removing/created/dead.

    Docker compose force-recreate races with the daemon finishing the removal
    of the old container.  If we detect any container in a transitional state
    (`Created`, `Dead`, `Removing`) we force-remove it and poll until the project
    is quiet or the timeout expires.  Returns a status dict; does not raise.
    """
    project = stack
    deadline = time.monotonic() + timeout_s
    last_status = "unknown"
    removed_names: list[str] = []

    while time.monotonic() < deadline:
        # Ask compose for every container in this project.
        r = await host.run_command(
            "bash -c " + shlex.quote(
                f"cd {stack_dir.rstrip('/')} && docker compose -p {project} ps -a --format json"
            ),
            timeout=20.0,
        )
        if not r.ok or not r.stdout.strip():
            # compose ps failed (e.g. project has no containers yet); that's quiet enough.
            return {"ok": True, "status": "empty", "removed": removed_names}

        # Docker compose ps can emit either a JSON array or one JSON object per line.
        entries: list[dict] = []
        try:
            data = json.loads(r.stdout.strip())
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                entries = [data]
        except json.JSONDecodeError:
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        bad = []
        for e in entries:
            state = (e.get("State") or e.get("state") or "").lower()
            health = (e.get("Health") or e.get("health") or "").lower()
            name = e.get("Name") or e.get("name") or ""
            if state in ("created", "dead", "removing") or health == "removing":
                bad.append(name)

        if not bad:
            return {"ok": True, "status": "quiet", "removed": removed_names}

        last_status = f"bad={bad}"
        for name in bad:
            if name:
                rm = await host.run_command(f"docker rm -f {name}", timeout=20.0)
                if rm.ok and name not in removed_names:
                    removed_names.append(name)

        time.sleep(2.0)

    log.warning("compose state for %s still not quiet after %.0fs: %s", stack, timeout_s, last_status)
    return {"ok": False, "status": last_status, "removed": removed_names}

async def _pin_image_to_digest(
    host: HostClient,
    image: str,
    to_digest: str,
) -> dict[str, Any]:
    """Pull the exact digest and retag the local image ref to point at it.

    This makes `docker compose up -d` recreate the container when the
    compose file uses an unpinned tag, without editing the compose file.
    Returns a dict with ok, stdout, stderr.
    """
    digest = to_digest
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:"):]

    # v0.9.14-hermes-2: strip any tag from the image before pulling by
    # digest.  Docker treats "repo:tag@sha256:digest" as a tag-qualified
    # reference; if the registry no longer serves that exact tag→digest
    # mapping, the pull fails with "manifest unknown".  A bare
    # "repo@sha256:digest" always resolves the manifest directly.
    pull_base = image
    if ":" in (image.rsplit("/", 1)[-1]):
        pull_base = image.rsplit(":", 1)[0]
    pull_ref = f"{pull_base}@sha256:{digest}"
    target = _tag_for(image)

    log.info("pin %s to digest %s (target tag %s)", image, to_digest, target)

    pull = await host.run_command(f"docker pull {pull_ref}", timeout=300.0)
    if not pull.ok:
        stderr = (pull.stderr or "").lower()
        # v0.9.15-hermes: many registries (Docker Hub, GHCR, Greenbone, etc.)
        # return an index digest from the Docker-Content-Digest header that
        # cannot be pulled directly ("manifest unknown" / "not found").
        # Fall back to pulling by tag and applying the current tag digest.
        # The stored to_digest is still used for classification/reporting.
        if "manifest unknown" in stderr or "not found" in stderr or "no such manifest" in stderr:
            log.warning(
                "digest pull %s failed with manifest-unknown; falling back to tag pull %s",
                pull_ref, target,
            )
            tag_pull = await host.run_command(f"docker pull {target}", timeout=300.0)
            if not tag_pull.ok:
                return {
                    "ok": False,
                    "error": (
                        f"digest pull {pull_ref} failed (manifest unknown); "
                        f"tag pull {target} also failed (exit={tag_pull.exit_code}): "
                        f"{tag_pull.stderr[:400]}"
                    ),
                }
            # Use the digest the tag actually resolved to locally, and
            # retag it to the canonical target. No digest check needed:
            # pulling by tag is the source of truth for :latest users.
            resolved = await _local_image_id(host, target)
            tag = await host.run_command(
                f"docker tag {resolved or target} {target}", timeout=30.0,
            )
            if not tag.ok:
                return {
                    "ok": False,
                    "error": f"docker tag {resolved or target} {target} failed (exit={tag.exit_code}): {tag.stderr[:400]}",
                }
            return {
                "ok": True,
                "target": target,
                "fallback": "tag_pull",
                "pull_stdout": tag_pull.stdout[:200],
            }
        return {
            "ok": False,
            "error": f"docker pull {pull_ref} failed (exit={pull.exit_code}): {pull.stderr[:400]}",
        }

    tag = await host.run_command(f"docker tag {pull_ref} {target}", timeout=30.0)
    if not tag.ok:
        return {
            "ok": False,
            "error": f"docker tag {pull_ref} {target} failed (exit={tag.exit_code}): {tag.stderr[:400]}",
        }

    return {"ok": True, "target": target, "pull_stdout": pull.stdout[:200]}


async def apply_update(
    host: HostClient,
    state: State,
    *,
    stack: str,
    snapshot: StackSnapshot,
    settle_seconds: int = 5,
    to_digest: str | None = None,
    image: str | None = None,
) -> dict[str, Any]:
    """Pull the new image, restart the stack, and probe.

    Returns a structured result with the from/to digests, the new
    compose hash, and the post-update probe status of every service.

    v0.9.14-hermes: if ``image`` and ``to_digest`` are provided, the
    exact digest is pulled and the local image tag is retagged to it
    before ``docker compose up -d``. This ensures the running
    container actually moves to the new digest for compose files that
    use unpinned tags (e.g. `image: ghcr.io/cross-seed/cross-seed`).
    Without this, compose may report success without recreating the
    container, and the pending row reappears on the next drift scan.
    """
    stack_dir = stack_dir_of(snapshot)
    if not stack_dir:
        return {
            "ok": False,
            "error": f"no stack_dir resolved for {stack} on {host.name}; cannot apply",
        }

    project = stack
    _compose_text, compose_hash = await _async_read_compose(host, stack_dir)

    # Detect multi-image stacks before we try to pin a single digest.
    # Pinning one image digest on a stack with multiple distinct images
    # (e.g. greenbone, ollama) pins the wrong service and causes endless
    # re-drift.  For these we pull and restart the whole compose project.
    cs = await host.list_containers(all=True)
    matching = [c for c in cs if c.get("PROJECT") == snapshot.stack or c.get("NAME") == snapshot.stack]
    distinct_images = sorted({c.get("IMAGE", "") for c in matching if c.get("IMAGE")})
    multi_image = len(distinct_images) > 1

    if multi_image:
        log.info(
            "multi-image stack %s (%d images); using compose pull+up for all services: %s",
            stack, len(distinct_images), distinct_images,
        )
        quiet = await _wait_for_quiet_compose_state(host, stack_dir, snapshot.stack)
        if not quiet["ok"]:
            log.warning("pre-flight compose quiet check failed for %s: %s", snapshot.stack, quiet)
        pull_cmd = f"cd {shlex.quote(stack_dir.rstrip('/'))} && docker compose -p {shlex.quote(project)} pull"
        pull = await host.run_command(
            f"bash -c {shlex.quote(pull_cmd)}",
            timeout=_timeout_for_stack_dir(stack_dir),
        )
        if not pull or not pull.ok:
            return {
                "ok": False,
                "error": f"compose pull failed (exit={pull.exit_code if pull else 'none'}): {pull.stderr[:400] if pull else ''}",
                "from_digest": snapshot.manifest_digest,
                "compose_hash": compose_hash,
            }
        up_cmd = f"cd {shlex.quote(stack_dir.rstrip('/'))} && docker compose -p {shlex.quote(project)} up -d"
        up = await host.run_command(
            f"bash -c {shlex.quote(up_cmd)}",
            timeout=_timeout_for_stack_dir(stack_dir),
        )
        if not up or not up.ok:
            return {
                "ok": False,
                "error": f"compose up failed (exit={up.exit_code if up else 'none'}): {up.stderr[:400] if up else ''}",
                "from_digest": snapshot.manifest_digest,
                "compose_hash": compose_hash,
            }
        # Re-list and probe every service; skip the single-digest checks.
        cs = await host.list_containers(all=True)
        matching = [c for c in cs if c.get("PROJECT") == snapshot.stack or c.get("NAME") == snapshot.stack]
        if not matching:
            return {
                "ok": False,
                "error": f"no containers found after compose up for multi-image stack {stack}",
                "from_digest": snapshot.manifest_digest,
                "compose_hash": compose_hash,
            }
        services_ok: dict[str, bool] = {}
        for c in matching:
            svc = c.get("SERVICE") or c["NAME"]
            info = await host.inspect_container(c["NAME"])
            state = (info.get("State") or {})
            status = state.get("Status")
            cfg = info.get("HostConfig", {})
            restart_policy = (cfg.get("RestartPolicy") or {}).get("Name", "")
            # Skip completed one-shot sidecars (RestartPolicy=no, exited cleanly)
            # Skip containers that have already completed successfully
            # (common one-shot permission/utility sidecars in ix-apps stacks)
            if status == "exited" and state.get("ExitCode") == 0:
                log.info("multi-image %s: skipping completed one-shot container %s", stack, c["NAME"])
                services_ok[svc] = True
                continue
            services_ok[svc] = await _probe_container(host, c["NAME"], settle_s=settle_seconds)
        return {
            "ok": all(services_ok.values()),
            "from_digest": snapshot.manifest_digest,
            "services": services_ok,
            "compose_hash": compose_hash,
            "stack_dir": stack_dir,
            "pin": None,
        }

    pin_result = None
    if image and to_digest:
        pin_result = await _pin_image_to_digest(host, image, to_digest)
        if not pin_result.get("ok"):
            return {
                "ok": False,
                "error": pin_result.get("error", "image pin failed"),
                "from_digest": snapshot.manifest_digest,
                "compose_hash": compose_hash,
            }

    # Find every service in the new stack before we run compose commands.
    cs = await host.list_containers(all=True)
    matching = [c for c in cs if c.get("PROJECT") == snapshot.stack or c.get("NAME") == snapshot.stack]

    # Identify which service(s) in the compose project use the image we are
    # updating. When pinning an unpinned tag we must force-recreate only those
    # services; a blanket `--force-recreate` would restart every service in a
    # multi-container stack unnecessarily.
    services_to_recreate: list[str] = []
    for c in matching:
        c_image = c.get("IMAGE") or ""
        if c_image == image or (image and c_image.startswith(image)):
            svc = c.get("SERVICE") or c["NAME"]
            if svc and svc not in services_to_recreate:
                services_to_recreate.append(svc)

    # Wait until the project's containers are in a stable state before we ask
    # compose to recreate anything.  This prevents the recurring
    # "removal already in progress" race on force-recreate.
    quiet = await _wait_for_quiet_compose_state(host, stack_dir, snapshot.stack)
    if not quiet["ok"]:
        log.warning("pre-flight compose quiet check failed for %s: %s", snapshot.stack, quiet)

    pull = None
    up = None
    if pin_result:
        # The exact digest is already pulled and tagged locally. Running
        # `docker compose pull` would fetch the registry's floating tag
        # (often still the old digest) and overwrite our pin. Use
        # `up -d --pull never` plus force-recreate only for the affected
        # service(s) so the running container actually moves to the new digest.
        recreate_args = ""
        if services_to_recreate:
            recreate_args = " --force-recreate --no-deps " + " ".join(shlex.quote(s) for s in services_to_recreate)
            log.info("pin path: force-recreate services %s", services_to_recreate)
        cmd = f"cd {shlex.quote(stack_dir.rstrip('/'))} && docker compose -p {shlex.quote(project)} up -d --pull never{recreate_args}"
        log.info("skipping compose pull because image was pinned; using %s", cmd)
        up = await host.run_command(
            f"bash -c {shlex.quote(cmd)}",
            timeout=_timeout_for_stack_dir(stack_dir),
        )
    else:
        pull = await host.compose_pull(stack_dir)
        if not pull.ok:
            return {
                "ok": False,
                "error": f"docker compose pull failed (exit={pull.exit_code}): {pull.stderr[:400]}",
                "from_digest": snapshot.manifest_digest,
                "compose_hash": compose_hash,
                "pin": pin_result,
            }
        up = await host.compose_up(stack_dir)
    if not up or not up.ok:
        return {
            "ok": False,
            "error": f"docker compose up -d failed (exit={up.exit_code}): {up.stderr[:400]}",
            "from_digest": snapshot.manifest_digest,
            "compose_hash": compose_hash,
            "pin": pin_result,
        }

    # Re-list containers after the apply so we inspect the recreated ones.
    cs = await host.list_containers(all=True)
    matching = [c for c in cs if c.get("PROJECT") == snapshot.stack or c.get("NAME") == snapshot.stack]
    if not matching:
        return {
            "ok": False,
            "error": f"after up, no containers found for stack {stack}",
            "from_digest": snapshot.manifest_digest,
            "compose_hash": compose_hash,
            "pin": pin_result,
        }

    services_ok: dict[str, bool] = {}
    new_digests: dict[str, str | None] = {}
    new_image_ids: dict[str, str | None] = {}
    digest_mismatch = False
    for c in matching:
        svc = c.get("SERVICE") or c["NAME"]
        try:
            info = await host.inspect_container(c["NAME"])
        except Exception as e:
            log.warning("post-apply inspect %s failed: %s", c["NAME"], e)
            services_ok[svc] = False
            continue
        repo_digest = (info.get("RepoDigests") or [None])[0]
        image_id = info.get("Image")
        new_digests[svc] = repo_digest
        new_image_ids[svc] = image_id
        services_ok[svc] = await _probe_container(host, c["NAME"], settle_s=settle_seconds)
        if to_digest and info.get("RepoDigests") and not _digest_in_repo_digests(to_digest, info.get("RepoDigests")):
            digest_mismatch = True
            log.warning(
                "post-apply digest mismatch for %s/%s: got %s expected %s (RepoDigests=%s)",
                stack, svc, repo_digest, to_digest, info.get("RepoDigests"),
            )

    # When RepoDigests is empty (common with pinned-then-tagged images),
    # fall back to comparing each service's container image ID with the local
    # image ID of that service's own resolved tag. The previous global
    # comparison caused false-positive mismatch loops on multi-service stacks.
    if to_digest and not digest_mismatch:
        for c in matching:
            svc = c.get("SERVICE") or c.get("NAME")
            if svc not in new_image_ids:
                continue
            cid = new_image_ids[svc]
            if not cid:
                continue
            service_image = c.get("IMAGE") or image
            if not service_image:
                continue
            target_tag = _tag_for(service_image)
            local_id = await _local_image_id(host, target_tag)
            if local_id and cid != local_id and not cid.endswith(local_id) and not local_id.endswith(cid):
                digest_mismatch = True
                log.warning(
                    "post-apply image ID mismatch for %s/%s: container=%s local_tag=%s (image=%s)",
                    stack, svc, cid, local_id, service_image,
                )

    all_ok = all(services_ok.values())
    # If every service passed its probe but the digest still does not
    # match, force-recreate the stack. This handles compose files with
    # unpinned tags where `docker compose up -d` silently skips
    # recreation because the service spec string hasn't changed.
    if all_ok and digest_mismatch:
        log.warning("digest mismatch for %s after up -d; retrying with --force-recreate", stack)
        await _wait_for_quiet_compose_state(host, stack_dir, snapshot.stack)
        cmd = f"cd {shlex.quote(stack_dir.rstrip('/'))} && docker compose -p {shlex.quote(project)} up -d --force-recreate"
        recreate = await host.run_command(
            f"bash -c {shlex.quote(cmd)}",
            timeout=_timeout_for_stack_dir(stack_dir),
        )
        if not recreate.ok:
            return {
                "ok": False,
                "error": f"post-apply digest mismatch; force-recreate failed (exit={recreate.exit_code}): {recreate.stderr[:400]}",
                "from_digest": snapshot.manifest_digest,
                "services": services_ok,
                "new_digests": new_digests,
                "compose_hash": compose_hash,
                "stack_dir": stack_dir,
                "pin": pin_result,
            }
        # Re-probe after force-recreate
        services_ok = {}
        new_digests = {}
        digest_mismatch = False
        for c in matching:
            svc = c.get("SERVICE") or c["NAME"]
            try:
                info = await host.inspect_container(c["NAME"])
            except Exception as e:
                log.warning("post-recreate inspect %s failed: %s", c["NAME"], e)
                services_ok[svc] = False
                new_digests[svc] = None
                continue
            repo_digest = (info.get("RepoDigests") or [None])[0]
            new_digests[svc] = repo_digest
            services_ok[svc] = await _probe_container(host, c["NAME"], settle_s=settle_seconds)
            if to_digest and info.get("RepoDigests") and not _digest_in_repo_digests(to_digest, info.get("RepoDigests")):
                digest_mismatch = True
                log.warning(
                    "post-recreate digest mismatch for %s/%s: got %s expected %s (RepoDigests=%s)",
                    stack, svc, repo_digest, to_digest, info.get("RepoDigests"),
                )
        all_ok = all(services_ok.values())
        if all_ok and digest_mismatch:
            return {
                "ok": False,
                "error": f"post-recreate digest mismatch: expected {to_digest}, got {new_digests}",
                "from_digest": snapshot.manifest_digest,
                "services": services_ok,
                "new_digests": new_digests,
                "compose_hash": compose_hash,
                "stack_dir": stack_dir,
                "pin": pin_result,
            }

    return {
        "ok": all_ok,
        "from_digest": snapshot.manifest_digest,
        "services": services_ok,
        "new_digests": new_digests,
        "compose_hash": compose_hash,
        "stack_dir": stack_dir,
        "pin": pin_result,
    }
