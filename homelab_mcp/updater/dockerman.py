"""Dockerman / single-container apply path.

Used for stacks managed by Unraid's Community Applications dockerman
plugin (or any bare docker container with no compose project label).
The compose pipeline (``updater.pipeline.run_pipeline``) can't handle
these because they have no ``stack_dir`` to ``docker compose up -d``
from. This module does:

  1. Snapshot the running container's full create-config.
  2. ``docker pull <image>:<new-tag>`` (or ``<image>@<digest>``).
  3. ``docker stop <name>`` + ``docker rm <name>``.
  4. ``docker run`` with the captured config + new image.
  5. Watchdog polls until healthy (or timeout).

Revert reverses the flow:

  1. ``docker stop <name>`` + ``docker rm <name>``.
  2. ``docker run`` with the captured config + old image (from snapshot).

This is more invasive than ``docker compose up -d`` (which preserves
container state) but it's the only option when there's no compose
file to re-apply.

Image-tag-swap fallback (your call: option 1b):
  If the old image sha isn't in the local cache (often the case
  because docker GC reclaims intermediate layers), we fall back to
  retagging the current image to a backup name and pulling the new
  one in its place. This avoids the destructive rm/run cycle when
  possible.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homelab_mcp.hosts.base import CommandResult, HostClient
from homelab_mcp.state import State

log = logging.getLogger(__name__)


# -- snapshot ---------------------------------------------------------------


@dataclass
class DockermanSnapshot:
    """Pre-update snapshot of a single dockerman-managed container.

    Captures everything needed to recreate the container with the OLD
    image after a bad apply, OR recreate it with the NEW image on a
    successful apply.

    Attributes:
        host:           the host the container is on
        container_name: the container name
        stack:          alias for the stack (often same as container_name)
        image_ref:      the original image reference (e.g. "jellyfin/jellyfin:latest")
        old_image_digest: RepoDigests[0] at snapshot time (sha256:...)
        old_image_id:   Config.Image (sha256:...) at snapshot time
        run_config:     the captured ``docker run`` config dict (env, ports,
                        volumes, network, command, labels, restart, etc.)
        compose_yaml:   empty for dockerman (no compose file exists)
        captured_at:    unix timestamp
    """

    host: str
    container_name: str
    stack: str
    image_ref: str
    old_image_digest: str | None
    old_image_id: str | None
    run_config: dict[str, Any]
    compose_yaml: str = ""
    captured_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "container_name": self.container_name,
            "stack": self.stack,
            "image_ref": self.image_ref,
            "old_image_digest": self.old_image_digest,
            "old_image_id": self.old_image_id,
            "run_config": self.run_config,
            "compose_yaml": self.compose_yaml,
            "captured_at": self.captured_at,
        }


def _run_config_from_inspect(info: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``docker inspect`` JSON into a ``docker run`` config dict.

    This is what we capture for recreation. We keep the most common
    fields; exotic stuff (cap-add, devices, etc.) falls through to
    ``extra_args`` so the operator can audit it.
    """
    cfg = info.get("Config") or {}
    host_cfg = info.get("HostConfig") or {}
    net_cfg = info.get("NetworkSettings") or {}

    env_list = cfg.get("Env") or []
    cmd_list = cfg.get("Cmd") or []
    entrypoint = cfg.get("Entrypoint") or []

    # v0.9.14-hermes-3: sanitize containers corrupted by an older updater
    # that placed --entrypoint inside Cmd.  Drop the leading
    # "--entrypoint", "<ep>" tokens so the recreated container gets a
    # clean Cmd.
    if isinstance(cmd_list, list) and len(cmd_list) >= 2 and cmd_list[0] == "--entrypoint":
        cmd_list = cmd_list[2:]
    elif isinstance(cmd_list, str) and cmd_list.startswith("--entrypoint "):
        parts = cmd_list.split(None, 2)
        cmd_list = parts[2] if len(parts) > 2 else ""

    # Published ports: dict of "container_port/proto" -> [{"HostIp":..,"HostPort":..}]
    # Capture FULL list of bindings (not just first) so multi-binding specs survive.
    port_bindings = host_cfg.get("PortBindings") or {}
    published: dict[str, list[dict[str, str]]] = {}
    for spec, bindings in port_bindings.items():
        if bindings:
            published[spec] = [
                {"HostIp": b.get("HostIp", "0.0.0.0"), "HostPort": str(b.get("HostPort", ""))}
                for b in bindings
            ]

    # Also capture NetworkSettings.Ports for post-apply verification.
    network_ports = net_cfg.get("Ports") or {}

    # Volume mounts: prefer HostConfig.Mounts, but fall back to HostConfig.Binds
    # for older / Unraid dockerman containers that still use the legacy binds API.
    mounts = list(host_cfg.get("Mounts") or [])
    binds = host_cfg.get("Binds") or []
    if binds:
        seen_targets = {m.get("Target") for m in mounts if m.get("Target")}
        for bind in binds:
            parts = bind.split(":")
            if len(parts) < 2:
                continue
            source, target = parts[0], parts[1]
            # Docker bind format: source:target[:mode]. Mode is a comma-separated list.
            options = parts[2].split(",") if len(parts) >= 3 else []
            normalized = [opt.strip().lower() for opt in options]
            read_only = "ro" in normalized or "readonly" in normalized
            propagation_options = [opt for opt in normalized if opt in {
                "private", "rprivate", "shared", "rshared", "slave", "rslave", "bind", "rbind"
            }]
            propagation = propagation_options[0] if propagation_options else None
            if target in seen_targets:
                # Mounts entry already exists; just backfill propagation if needed.
                for m in mounts:
                    if m.get("Target") == target and propagation and not (m.get("BindOptions") or {}).get("Propagation"):
                        m.setdefault("BindOptions", {})["Propagation"] = propagation
                continue
            seen_targets.add(target)
            mount_entry = {
                "Type": "bind",
                "Source": source,
                "Target": target,
                "ReadOnly": read_only,
            }
            if propagation:
                mount_entry["BindOptions"] = {"Propagation": propagation}
            mounts.append(mount_entry)

    # Network mode: prefer the explicit join, fall back to networks dict
    network_mode = host_cfg.get("NetworkMode") or ""
    networks = list((net_cfg.get("Networks") or {}).keys())

    return {
        "Env": env_list,
        "Cmd": cmd_list,
        "Entrypoint": entrypoint,
        "WorkingDir": cfg.get("WorkingDir") or "",
        "User": cfg.get("User") or "",
        "Labels": cfg.get("Labels") or {},
        "ExposedPorts": cfg.get("ExposedPorts") or {},
        "PortBindings": published,
        "NetworkPorts": network_ports,
        "Mounts": mounts,
        "NetworkMode": network_mode,
        "Networks": networks,
        "RestartPolicy": host_cfg.get("RestartPolicy") or {},
        "LogConfig": host_cfg.get("LogConfig") or {},
        "CapAdd": host_cfg.get("CapAdd") or [],
        "CapDrop": host_cfg.get("CapDrop") or [],
        "Privileged": host_cfg.get("Privileged", False),
        "ReadonlyRootfs": host_cfg.get("ReadonlyRootfs", False),
        "ExtraHosts": host_cfg.get("ExtraHosts") or [],
        "Dns": host_cfg.get("Dns") or [],
        "DnsSearch": host_cfg.get("DnsSearch") or [],
        "ShmSize": host_cfg.get("ShmSize", 0),
        # Anything we didn't enumerate — keep around for the operator.
        # We intentionally keep Binds and Mounts here so the safety check can
        # compare the live container state against the reconstructed run command.
        "_raw_host_config": {k: v for k, v in host_cfg.items()
                             if k not in {"PortBindings", "NetworkMode",
                                          "RestartPolicy", "LogConfig", "CapAdd",
                                          "CapDrop", "Privileged", "ReadonlyRootfs",
                                          "ExtraHosts", "Dns", "DnsSearch", "ShmSize"}},
    }


async def snapshot_dockerman_container(
    host: HostClient,
    *,
    container_name: str,
    stack: str | None = None,
) -> DockermanSnapshot | None:
    """Capture the full ``docker run`` config of a single container.

    Returns None if the container isn't running or inspect fails.
    """
    try:
        info = await host.inspect_container(container_name)
    except Exception as e:
        log.warning("dockerman snapshot: inspect %s failed: %s", container_name, e)
        return None

    state = (info.get("State") or {})
    if state.get("Status") not in ("running", "exited"):
        # Exited is OK — we may be reviving it. "created" / "dead" not OK.
        if state.get("Status") not in ("created",):
            log.warning("dockerman snapshot: %s state=%s; refusing",
                        container_name, state.get("Status"))
            return None

    cfg = (info.get("Config") or {})
    image_ref = cfg.get("Image") or ""

    repo_digests = info.get("RepoDigests") or []
    old_digest = None
    for d in repo_digests:
        if "@sha256:" in d:
            old_digest = d.split("@", 1)[1]
            break

    old_id = None
    if isinstance(image_ref, str) and image_ref.startswith("sha256:"):
        old_id = image_ref

    return DockermanSnapshot(
        host=host.name,
        container_name=container_name,
        stack=stack or container_name,
        image_ref=image_ref,
        old_image_digest=old_digest,
        old_image_id=old_id,
        run_config=_run_config_from_inspect(info),
        captured_at=time.time(),
    )


# -- run-config rebuild ------------------------------------------------------


def _run_config_to_args(run_cfg: dict[str, Any], new_image: str,
                        new_container_name: str) -> tuple[list[str], list[str]]:
    """Translate a run_config dict into ``docker run`` flags.

    Returns (positional_args, side_args):
      - positional_args: --name <name> <image> [cmd...]
      - side_args:      --env X=Y, --publish ..., --mount ..., etc.

    The caller is responsible for assembling ``docker run [side_args] [positional_args]``.
    """
    side: list[str] = []

    # --detach and --restart
    side.append("--detach")
    rp = run_cfg.get("RestartPolicy") or {}
    if rp.get("Name") and rp["Name"] != "no":
        side += ["--restart", f"{rp['Name']}:{rp.get('MaximumRetryCount', 0)}"]

    # --name is part of positional
    # --hostname, --domainname
    if run_cfg.get("Hostname"):
        side += ["--hostname", run_cfg["Hostname"]]
    if run_cfg.get("Domainname"):
        side += ["--domainname", run_cfg["Domainname"]]

    # --user
    if run_cfg.get("User"):
        side += ["--user", run_cfg["User"]]

    # --workdir
    if run_cfg.get("WorkingDir"):
        side += ["--workdir", run_cfg["WorkingDir"]]

    # --env (only file-safe entries; secrets get filtered upstream)
    for e in run_cfg.get("Env") or []:
        if "=" in e:
            side += ["--env", e]

    # --env-file (skip — we use --env inline)

    # --expose
    for spec in (run_cfg.get("ExposedPorts") or {}).keys():
        side += ["--expose", spec]

    # --publish
    for spec, bindings in (run_cfg.get("PortBindings") or {}).items():
        for binding in bindings:
            if binding.get("HostPort"):
                host_ip = binding.get("HostIp") or "0.0.0.0"
                side += ["--publish", f"{host_ip}:{binding['HostPort']}:{spec.split('/')[0]}"]

    # --mount
    for m in run_cfg.get("Mounts") or []:
        # m has Type, Source, Target, ReadOnly, BindOptions, etc.
        mount_spec = f"type={m.get('Type', 'volume')}"
        if m.get("Source"):
            mount_spec += f",source={m['Source']}"
        if m.get("Target"):
            mount_spec += f",destination={m['Target']}"
        if m.get("ReadOnly"):
            mount_spec += ",readonly"
        bind_opts = m.get("BindOptions") or {}
        if bind_opts.get("Propagation"):
            mount_spec += f",bind-propagation={bind_opts['Propagation']}"
        side += ["--mount", mount_spec]

    # --network
    nm = run_cfg.get("NetworkMode")
    if nm and nm not in ("default",):
        if nm.startswith("container:"):
            side += ["--network", nm]
        else:
            side += ["--network", nm]

    # --label
    for k, v in (run_cfg.get("Labels") or {}).items():
        side += ["--label", f"{k}={v}"]

    # --log-driver / --log-opt
    lc = run_cfg.get("LogConfig") or {}
    if lc.get("Type"):
        side += ["--log-driver", lc["Type"]]
        for k, v in (lc.get("Config") or {}).items():
            side += ["--log-opt", f"{k}={v}"]

    # --cap-add / --cap-drop
    for c in run_cfg.get("CapAdd") or []:
        side += ["--cap-add", c]
    for c in run_cfg.get("CapDrop") or []:
        side += ["--cap-drop", c]

    if run_cfg.get("Privileged"):
        side.append("--privileged")
    if run_cfg.get("ReadonlyRootfs"):
        side.append("--read-only")

    # --add-host
    for h in run_cfg.get("ExtraHosts") or []:
        side += ["--add-host", h]

    # --dns / --dns-search
    for d in run_cfg.get("Dns") or []:
        side += ["--dns", d]
    for s in run_cfg.get("DnsSearch") or []:
        side += ["--dns-search", s]

    if run_cfg.get("ShmSize"):
        side += ["--shm-size", str(run_cfg["ShmSize"])]

    # Positional: --name <new> [--entrypoint <ep>] <image> [cmd]
    positional: list[str] = ["--name", new_container_name]

    # --entrypoint must come BEFORE the image reference; otherwise docker
    # treats it as part of the container command and corrupts Cmd.
    if run_cfg.get("Entrypoint"):
        positional += ["--entrypoint", run_cfg["Entrypoint"][0] if isinstance(run_cfg["Entrypoint"], list) else run_cfg["Entrypoint"]]

    positional.append(new_image)

    # Sanitize Cmd one more time at build time, then append after image.
    cmd = run_cfg.get("Cmd") or []
    if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "--entrypoint":
        cmd = cmd[2:]
    elif isinstance(cmd, str) and cmd.startswith("--entrypoint "):
        parts = cmd.split(None, 2)
        cmd = parts[2] if len(parts) > 2 else ""
    if cmd:
        if isinstance(cmd, list):
            positional += cmd
        else:
            positional.append(cmd)

    return positional, side


def _shell_quote(s: str) -> str:
    """Minimal shell-quote for embedding into a docker run command."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "/=:.,_-+@" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _build_docker_run_cmd(new_image: str, snapshot: DockermanSnapshot,
                          new_container_name: str | None = None) -> str:
    """Build a complete ``docker run`` shell command string.

    Quoting is done via ``_shell_quote`` (safe enough for the values
    we control — env vars, paths, labels). The image string is also
    quoted defensively.
    """
    positional, side = _run_config_to_args(
        snapshot.run_config, new_image, new_container_name or snapshot.container_name,
    )
    parts = ["docker", "run"] + side + positional
    return " ".join(_shell_quote(p) for p in parts)


# -- apply ------------------------------------------------------------------


@dataclass
class DockermanApplyResult:
    ok: bool
    action: str  # "applied" | "rolled_back" | "rollback_failed" | "failed" | "tag_swapped" | "dry_run"
    new_image: str
    new_container_id: str | None = None
    error: str | None = None
    from_digest: str | None = None
    to_digest: str | None = None
    rollback: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "new_image": self.new_image,
            "new_container_id": self.new_container_id,
            "error": self.error,
            "from_digest": self.from_digest,
            "to_digest": self.to_digest,
            "rollback": self.rollback,
            "samples": self.samples,
        }


async def _pull_image(host: HostClient, image_ref: str) -> CommandResult:
    """Pull an image by reference (tag, digest, or both)."""
    log.info("dockerman: pulling %s", image_ref)
    return await host.run_command(
        f"docker pull {_shell_quote(image_ref)}", timeout=600.0,
    )


async def _is_image_cached(host: HostClient, image_ref: str) -> bool:
    """Check if the image is in the local cache."""
    r = await host.run_command(
        f"docker image inspect {_shell_quote(image_ref)} --format '{{{{.Id}}}}' 2>&1",
        timeout=10.0,
    )
    return r.ok and r.stdout.strip().startswith("sha256:")


async def _try_tag_swap(
    host: HostClient,
    *,
    snapshot: DockermanSnapshot,
    new_image_ref: str,
    new_to_digest: str | None,
) -> DockermanApplyResult:
    """Image-tag-swap fallback: keep old image as a backup tag, pull new, restart.

    Only viable if the OLD image is still in the local cache. If not,
    we return action="failed" and the caller falls back to rm/run.
    """
    if not snapshot.old_image_digest:
        return DockermanApplyResult(
            ok=False, action="failed",
            new_image=new_image_ref,
            error="tag_swap unavailable: no old digest in snapshot",
        )

    if not await _is_image_cached(host, snapshot.old_image_digest):
        return DockermanApplyResult(
            ok=False, action="failed",
            new_image=new_image_ref,
            error="tag_swap unavailable: old image not in local cache",
        )

    backup_tag = f"{snapshot.image_ref.split(':')[0]}:pre-update-{int(time.time())}"
    log.info("dockerman: tag_swap: backup tag=%s", backup_tag)

    r = await host.run_command(
        f"docker tag {_shell_quote(snapshot.old_image_digest)} {_shell_quote(backup_tag)}",
        timeout=10.0,
    )
    if not r.ok:
        return DockermanApplyResult(
            ok=False, action="failed",
            new_image=new_image_ref,
            error=f"tag_swap backup tag failed: {r.stderr[:300]}",
        )

    pull = await _pull_image(host, new_image_ref)
    if not pull.ok:
        # Roll back the tag swap
        await host.run_command(
            f"docker rmi {_shell_quote(backup_tag)} 2>/dev/null",
            timeout=10.0,
        )
        return DockermanApplyResult(
            ok=False, action="failed",
            new_image=new_image_ref,
            error=f"tag_swap pull failed: {pull.stderr[:300]}",
        )

    # Restart the running container — Docker's restart will pick up the
    # newly-pulled image at the same tag.
    restart = await host.run_command(
        f"docker restart {_shell_quote(snapshot.container_name)}", timeout=60.0,
    )
    if not restart.ok:
        return DockermanApplyResult(
            ok=False, action="failed",
            new_image=new_image_ref,
            error=f"tag_swap restart failed: {restart.stderr[:300]}",
        )

    return DockermanApplyResult(
        ok=True, action="tag_swapped", new_image=new_image_ref,
        from_digest=snapshot.old_image_digest,
        to_digest=new_to_digest,
    )


async def dockerman_apply_update(
    host: HostClient,
    *,
    container_name: str,
    new_image_ref: str,
    new_to_digest: str | None,
    dry_run: bool = False,
    use_tag_swap: bool = True,
    settle_seconds: int = 5,
) -> DockermanApplyResult:
    """Apply a new image to a single dockerman-managed container.

    Strategy:
      1. Snapshot the running container.
      2. (Optional) try tag-swap (faster, less disruptive). If old image
         isn't cached or swap fails, fall through to step 3.
      3. Stop + rm + run with captured config + new image.
      4. Return a structured result; caller wires watchdog + revert.
    """
    snap = await snapshot_dockerman_container(host, container_name=container_name)
    if snap is None:
        return DockermanApplyResult(
            ok=False, action="failed", new_image=new_image_ref,
            error=f"could not snapshot container {container_name}",
        )

    if dry_run:
        cmd_preview = _build_docker_run_cmd(new_image_ref, snap)
        return DockermanApplyResult(
            ok=True, action="dry_run", new_image=new_image_ref,
            from_digest=snap.old_image_digest,
            to_digest=new_to_digest,
            samples=[{
                "phase": "would_run",
                "cmd_preview": cmd_preview[:500],
                "old_image": snap.image_ref,
                "old_digest": snap.old_image_digest,
                "new_image": new_image_ref,
                "new_digest": new_to_digest,
            }],
        )

    # Try tag-swap first (cheaper, less disruptive)
    if use_tag_swap:
        swap = await _try_tag_swap(
            host, snapshot=snap,
            new_image_ref=new_image_ref, new_to_digest=new_to_digest,
        )
        if swap.ok:
            await asyncio.sleep(settle_seconds)
            return swap
        log.info("dockerman: tag_swap unavailable (%s); falling back to rm/run",
                 swap.error)

    # Safety checks before the destructive rm/run.
    # 1) Port bindings
    if not snap.run_config.get("PortBindings"):
        log.warning("dockerman: snapshot has no PortBindings; the recreated container will have no published ports")
        # We still proceed, because some containers legitimately have no published ports.

    # 2) Bind/volume mounts: fail fast if we are about to drop a bind that existed live.
    live_binds = set()
    try:
        raw_host_cfg = snap.run_config.get("_raw_host_config") or {}
        for b in (raw_host_cfg.get("Binds") or []):
            live_binds.add(b)
        for m in (raw_host_cfg.get("Mounts") or []):
            if m.get("Type") == "bind" and m.get("Source") and m.get("Target"):
                mode = "ro" if m.get("ReadOnly") else "rw"
                live_binds.add(f"{m['Source']}:{m['Target']}:{mode}")
    except Exception:
        pass
    if live_binds:
        reconstructed_mounts = set()
        for m in snap.run_config.get("Mounts") or []:
            if m.get("Type") == "bind" and m.get("Source") and m.get("Target"):
                mode = "ro" if m.get("ReadOnly") else "rw"
                propagation = (m.get("BindOptions") or {}).get("Propagation")
                sig = f"{m['Source']}:{m['Target']}:{mode}"
                if propagation:
                    sig += f",{propagation}"
                reconstructed_mounts.add(sig)
        # Normalize modes (legacy binds may omit :rw, docker treats as rw)
        missing = []
        for live_bind in live_binds:
            parts = live_bind.split(":")
            if len(parts) >= 3 and parts[2] == "":
                parts[2] = "rw"
            normalized = ":".join(parts)
            # live_bind may have propagation options in the mode field
            live_mode_and_opts = parts[2:] if len(parts) >= 3 else []
            live_rw = "rw" if (not live_mode_and_opts or live_mode_and_opts[0] == "rw") else live_mode_and_opts[0]
            if live_rw == "rw":
                alt = ":".join(parts[:2])
                if alt in reconstructed_mounts or normalized in reconstructed_mounts:
                    continue
            elif normalized in reconstructed_mounts:
                continue
            missing.append(live_bind)
        if missing:
            err = f"snapshot would drop live bind mounts: {missing}. refusing destructive apply."
            log.error("dockerman: %s", err)
            return DockermanApplyResult(
                ok=False, action="failed", new_image=new_image_ref,
                error=err,
                from_digest=snap.old_image_digest, to_digest=new_to_digest,
            )

    # Full rm + run path
    pull = await _pull_image(host, new_image_ref)
    if not pull.ok:
        return DockermanApplyResult(
            ok=False, action="failed", new_image=new_image_ref,
            error=f"docker pull failed: {pull.stderr[:300]}",
            from_digest=snap.old_image_digest,
            to_digest=new_to_digest,
        )

    stop = await host.run_command(
        f"docker stop {_shell_quote(snap.container_name)}", timeout=120.0,
    )
    if not stop.ok:
        return DockermanApplyResult(
            ok=False, action="failed", new_image=new_image_ref,
            error=f"docker stop failed: {stop.stderr[:300]}",
            from_digest=snap.old_image_digest, to_digest=new_to_digest,
        )

    rm = await host.run_command(
        f"docker rm {_shell_quote(snap.container_name)}", timeout=60.0,
    )
    if not rm.ok:
        # Restore from snapshot by recreating with old image
        log.error("dockerman: rm failed; recreating with old image to restore state")
        old_run = _build_docker_run_cmd(snap.image_ref, snap)
        await host.run_command(old_run, timeout=120.0)
        return DockermanApplyResult(
            ok=False, action="failed", new_image=new_image_ref,
            error=f"docker rm failed (reverted): {rm.stderr[:300]}",
            from_digest=snap.old_image_digest, to_digest=new_to_digest,
        )

    run_cmd = _build_docker_run_cmd(new_image_ref, snap)
    log.info("dockerman: running new container: %s", run_cmd[:200])
    run = await host.run_command(run_cmd, timeout=120.0)
    if not run.ok:
        # Try to restore from snapshot
        log.error("dockerman: run failed; recreating with old image")
        old_run = _build_docker_run_cmd(snap.image_ref, snap)
        await host.run_command(old_run, timeout=120.0)
        return DockermanApplyResult(
            ok=False, action="failed", new_image=new_image_ref,
            error=f"docker run failed (reverted): {run.stderr[:300]}",
            from_digest=snap.old_image_digest, to_digest=new_to_digest,
        )

    new_id = run.stdout.strip().splitlines()[-1].strip() if run.stdout else None
    await asyncio.sleep(settle_seconds)

    # Post-apply verification: inspect the new container and confirm PortBindings match snapshot.
    try:
        new_info = await host.inspect_container(snap.container_name)
        new_bindings = (new_info.get("HostConfig") or {}).get("PortBindings") or {}
        missing = []
        for spec, expected_bindings in (snap.run_config.get("PortBindings") or {}).items():
            actual = new_bindings.get(spec) or []
            actual_set = {(b.get("HostIp", "0.0.0.0"), str(b.get("HostPort", ""))) for b in actual}
            for exp in expected_bindings:
                key = (exp.get("HostIp", "0.0.0.0"), exp.get("HostPort", ""))
                if key not in actual_set:
                    missing.append(f"{spec} -> {key[0]}:{key[1]}")
        if missing:
            log.error("dockerman: port bindings missing after apply: %s; reverting", missing)
            revert_result = await dockerman_revert(host, snapshot=snap, reason=f"port bindings lost after apply: {missing}")
            return DockermanApplyResult(
                ok=False, action="rolled_back", new_image=new_image_ref,
                new_container_id=new_id,
                from_digest=snap.old_image_digest, to_digest=new_to_digest,
                rollback=revert_result,
                error=f"port bindings lost after apply: {missing}",
            )
    except Exception as e:
        log.warning("dockerman: could not verify post-apply port bindings: %s", e)

    return DockermanApplyResult(
        ok=True, action="applied", new_image=new_image_ref,
        new_container_id=new_id,
        from_digest=snap.old_image_digest, to_digest=new_to_digest,
    )


# -- revert -----------------------------------------------------------------


async def dockerman_revert(
    host: HostClient,
    *,
    snapshot: DockermanSnapshot,
    reason: str = "",
    new_container_name: str | None = None,
) -> dict[str, Any]:
    """Roll back to the captured snapshot state.

    Recreates the container with the old image. If the snapshot's
    image is no longer tagged, we use the old digest (pull first).
    """
    if not snapshot.image_ref and not snapshot.old_image_digest:
        return {"ok": False, "error": "snapshot has no image ref or digest"}

    old_ref = snapshot.image_ref
    if snapshot.old_image_digest and not old_ref.startswith("sha256:"):
        # Try to use the digest if the tag may have moved
        pull = await _pull_image(host, snapshot.old_image_digest)
        if pull.ok:
            old_ref = snapshot.old_image_digest

    # Stop + rm whatever's running (it may be the new image)
    stop = await host.run_command(
        f"docker stop {_shell_quote(snapshot.container_name)} 2>&1 || true",
        timeout=120.0,
    )
    rm = await host.run_command(
        f"docker rm {_shell_quote(snapshot.container_name)} 2>&1 || true",
        timeout=60.0,
    )

    run_cmd = _build_docker_run_cmd(
        old_ref, snapshot,
        new_container_name=new_container_name,
    )
    run = await host.run_command(run_cmd, timeout=120.0)
    if not run.ok:
        return {
            "ok": False,
            "error": f"revert run failed: {run.stderr[:400]}",
            "stop": stop.stdout[:200],
            "rm": rm.stdout[:200],
            "reason": reason,
        }

    return {
        "ok": True,
        "rolled_back_to": old_ref,
        "new_container_id": run.stdout.strip().splitlines()[-1] if run.stdout else None,
        "reason": reason,
    }


__all__ = [
    "DockermanSnapshot",
    "DockermanApplyResult",
    "snapshot_dockerman_container",
    "dockerman_apply_update",
    "dockerman_revert",
    "_build_docker_run_cmd",
]