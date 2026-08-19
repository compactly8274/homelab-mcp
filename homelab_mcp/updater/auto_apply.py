"""Auto-apply orchestrator (the cron entry point).

Runs the full pipeline for a single pending update:

  1. Inspect the running container to find its labels and stack dir.
  2. Fetch release notes for the new image.
  3. Classify via LLM (SAFE / CAUTION / BREAKING).
  4. Apply the configured policy (safe-and-caution vs safe-only).
  5. Run the apply pipeline (snapshot → pull → up → healthcheck).
  6. On success, dismiss the pending row.

v0.9.13 hermes patch: stack key resolution (same fix as
``tools/apply_update.py``).

The scanner (post-v0.9.13) writes ``pending_updates`` rows keyed by
the compose project label. The cron path previously assumed
``inputs.stack`` was a container NAME. This patch adds the same
``_resolve_container_for_stack`` helper so the cron path can find
the right container regardless of which key form the row uses.
"""

from __future__ import annotations
import os

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol

import aiosqlite

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.notifier import Notifier
from homelab_mcp.updater.release_notes import ReleaseNotes
from homelab_mcp.updater.risk import RiskVerdict

log = logging.getLogger(__name__)


async def _resolve_container_for_stack(
    host: HostClient,
    stack: str,
) -> str:
    """Resolve a ``stack`` key to a real container NAME on the host.

    Duplicated from ``tools/apply_update.py`` to avoid a circular
    import (tools depends on updater.auto_apply, not the other way
    around). Keep the two copies in sync.

    Resolution order:

      1. Any running container whose ``com.docker.compose.project``
         label equals ``stack``.
      2. Any running container whose ``com.docker.compose.service``
         label equals ``stack``.
      3. Any running container whose NAME equals ``stack``.
      4. iX-Systems TrueNAS Apps: the scanner writes the unprefixed
         app name (e.g. ``dockge``) while Docker sets the project
         label to ``ix-dockge``. Try the ``ix-`` prefixed and
         unprefixed variants of ``stack`` for all three match types.

    Returns the container NAME to use with ``inspect_container``.

    Raises ``KeyError`` if no matching container is found.
    """
    try:
        cs = await host.list_containers(all=True)
    except Exception as e:
        raise KeyError(f"list_containers failed: {e}") from e

    def _names(s: str) -> list[str]:
        names = [s]
        if s.startswith("ix-"):
            names.append(s[3:])
        else:
            names.append(f"ix-{s}")
        return names

    candidate_names = _names(stack)

    for key in ("PROJECT", "SERVICE", "NAME"):
        for name in candidate_names:
            for c in cs:
                if (c.get(key) or "").strip() == name:
                    container_name = (c.get("NAME") or "").strip()
                    if container_name:
                        return container_name

    raise KeyError(
        f"no container on host {host.name!r} matches stack key {stack!r} "
        f"(tried PROJECT, SERVICE, and NAME matches, including ix- variants)"
    )


async def _container_digest_and_health(
    host: HostClient,
    stack: str,
    to_digest: str | None = None,
) -> tuple[str | None, bool, str | None]:
    """Return (current_digest_sha, healthy, container_name_or_error).

    Digest is extracted from ``RepoDigests`` and stripped to the bare
    sha256 value so it can be compared to scanner/pending digests.
    Health is True when the container is running and (if it has a
    Docker healthcheck) the health status is ``healthy``.
    """
    try:
        container_name = await _resolve_container_for_stack(host, stack)
    except KeyError as e:
        return None, False, str(e)
    try:
        info = await host.inspect_container(container_name)
    except Exception as e:
        return None, False, f"inspect {container_name} failed: {e}"
    cfg = info.get("Config") or {}
    state = info.get("State") or {}
    health = state.get("Health") or {}
    health_status = health.get("Status", "").lower()
    running = state.get("Running", False)
    healthy = running and (not health_status or health_status == "healthy")

    repo_digests = info.get("RepoDigests") or []
    current_digest: str | None = None
    for rd in repo_digests:
        if isinstance(rd, str) and "@sha256:" in rd:
            current_digest = rd.split("@sha256:", 1)[1]
            break

    # Fallback #1: Config.Image may be pinned by digest.
    if not current_digest:
        cfg_image = cfg.get("Image") or ""
        if isinstance(cfg_image, str) and "@sha256:" in cfg_image:
            current_digest = cfg_image.split("@sha256:", 1)[1]

    # Fallback #2: for compose-managed containers Docker often leaves
    # RepoDigests empty on the container object. Inspect the local image
    # by its ID (info["Image"]) and read RepoDigests from there.
    if not current_digest:
        image_id = info.get("Image") or ""
        if isinstance(image_id, str) and image_id.startswith("sha256:"):
            try:
                img_r = await host.run_command(
                    f"docker image inspect {image_id}", timeout=15.0
                )
                if img_r.ok:
                    img_data = json.loads(img_r.stdout)
                    if isinstance(img_data, list) and img_data:
                        for rd in img_data[0].get("RepoDigests") or []:
                            if isinstance(rd, str) and "@sha256:" in rd:
                                current_digest = rd.split("@sha256:", 1)[1]
                                break
            except Exception as e:
                log.debug(
                    "reconcile: failed to inspect image %s for %s/%s: %s",
                    image_id, host.name, stack, e,
                )

    # When the caller supplied a target digest, prefer a match against
    # any available RepoDigest. Multi-arch images expose both the
    # manifest-list digest and the platform-specific digest; Docker often
    # runs the platform digest while pending rows store the manifest-list
    # digest. Treating them as equivalent lets reconciliation close out a
    # successful update even when the bare container digest differs.
    if to_digest and current_digest:
        norm_target = to_digest.split(":", 1)[-1]
        norm_current = current_digest.split(":", 1)[-1]
        if norm_current != norm_target:
            matched = False
            for rd in (info.get("RepoDigests") or []):
                if isinstance(rd, str) and "@sha256:" in rd:
                    if rd.split("@sha256:", 1)[1] == norm_target:
                        current_digest = norm_target
                        matched = True
                        break
            if not matched:
                image_id = info.get("Image") or ""
                if isinstance(image_id, str) and image_id.startswith("sha256:"):
                    try:
                        img_r = await host.run_command(
                            f"docker image inspect {image_id}", timeout=15.0
                        )
                        if img_r.ok:
                            img_data = json.loads(img_r.stdout)
                            if isinstance(img_data, list) and img_data:
                                for rd in img_data[0].get("RepoDigests") or []:
                                    if isinstance(rd, str) and "@sha256:" in rd:
                                        if rd.split("@sha256:", 1)[1] == norm_target:
                                            current_digest = norm_target
                                            break
                    except Exception as e:
                        log.debug(
                            "reconcile: image inspect for target digest failed for %s/%s: %s",
                            host.name, stack, e,
                        )

    return current_digest, healthy, container_name


async def _reconcile_in_progress_row(
    *,
    host: HostClient,
    state: State,
    stack: str,
    to_digest: str,
    image: str,
) -> dict[str, Any] | None:
    """Close out an interrupted apply if the container already updated.

    The smart pipeline records an ``update_history`` row with status
    ``in_progress`` before it pulls/restarts, then updates it to
    ``applied`` after the watchdog passes. If the cron or daemon is
    killed during the watchdog phase, the container may already be on
    the new digest while the database still says ``in_progress``. That
    stale state blocks future auto-apply cycles and can cause the
    same update to be attempted again.

    This helper checks the most recent ``in_progress`` row for
    ``(host, stack)``. If the live container is already running
    ``to_digest`` and is healthy, it marks that row ``applied`` and
    dismisses the matching pending row. If the container is not on
    the target digest or is unhealthy, it marks the stale row
    ``failed`` so a fresh apply can proceed.

    Returns a result dict when reconciliation happened (so the
    caller can skip applying), or ``None`` when no reconciliation
    was needed.
    """
    if not to_digest:
        return None
    db = await state._connect()
    try:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, to_digest, started_at FROM update_history
            WHERE host = ? AND stack = ? AND status = 'in_progress'
            ORDER BY datetime(started_at) DESC
            LIMIT 1
            """,
            (host.name, stack),
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        return None

    row_id = row["id"]
    row_to_digest = row["to_digest"]
    started_at_str = row["started_at"]
    # Only reconcile if the interrupted row was targeting the same
    # digest we are about to apply. A different digest means the
    # operator changed targets and a fresh apply is correct.
    if not row_to_digest:
        return None
    # Normalize digest values: some sources prefix with "sha256:",
    # others (like docker image inspect RepoDigests) use "@sha256:".
    norm_row_to = row_to_digest.split(":", 1)[-1] if ":" in row_to_digest else row_to_digest
    norm_target = to_digest.split(":", 1)[-1] if ":" in to_digest else to_digest
    if norm_row_to != norm_target:
        return None

    # If the in-progress row is recent, don't disturb it. The smart
    # pipeline is likely still running the watchdog for this apply.
    # Returning a skip result prevents the outer loop from spawning a
    # duplicate apply for the same pending row.
    recent_window_seconds = int(os.environ.get("HOMELAB_MCP_RECONCILE_RECENT_SEC", "300"))
    try:
        started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
    except Exception:
        started_dt = None
    now = datetime.now(UTC)
    now_str = now.isoformat().replace("+00:00", "Z")
    # If we can't parse the timestamp, assume it's recent. Duplicate
    # applies are far worse than waiting a bit longer for reconciliation.
    if started_dt is None:
        log.info(
            "reconcile: %s/%s in_progress row %s has unparseable started_at %r; "
            "treating as recent and skipping duplicate apply",
            host.name, stack, row_id, started_at_str,
        )
        return {
            "action": "in_progress",
            "skip": True,
            "reason": "existing apply still in progress (timestamp unparseable)",
            "to_digest": to_digest,
            "image": image,
        }
    age_seconds = (now - started_dt).total_seconds()
    if age_seconds < recent_window_seconds:
        log.info(
            "reconcile: %s/%s in_progress row %s is only %ds old; "
            "leaving it alone and skipping duplicate apply",
            host.name, stack, row_id, int(age_seconds),
        )
        return {
            "action": "in_progress",
            "skip": True,
            "reason": f"existing apply still in progress ({int(age_seconds)}s old)",
            "to_digest": to_digest,
            "image": image,
        }

    current_digest, healthy, detail = await _container_digest_and_health(host, stack, to_digest=to_digest)
    norm_current = current_digest.split(":", 1)[-1] if current_digest and ":" in current_digest else current_digest
    if norm_current == norm_target and healthy:
        log.info(
            "reconcile: %s/%s is already on target digest %s and healthy; "
            "closing stale in_progress row %s",
            host.name, stack, to_digest, row_id,
        )
        await state.update_update(
            row_id=row_id,
            status="applied",
            finished_at=now_str,
            reason=f"reconciled: container already on {to_digest} and healthy",
        )
        try:
            await state.mark_update_seen(host.name, stack, to_digest)
        except Exception as e:
            log.warning("reconcile: dismiss pending row failed: %s", e)
        return {
            "action": "applied",
            "reconciled": True,
            "reason": "container already on target digest after interrupted apply",
            "to_digest": to_digest,
            "image": image,
        }

    # Stale row exists but the container is NOT on the target digest or
    # is unhealthy. Mark the old row failed so it doesn't block a
    # fresh apply.
    log.warning(
        "reconcile: %s/%s has stale in_progress row %s but container "
        "digest=%s healthy=%s (detail: %s); marking row failed and retrying",
        host.name, stack, row_id, current_digest, healthy, detail,
    )
    await state.update_update(
        row_id=row_id,
        status="failed",
        finished_at=now_str,
        reason=(
            f"interrupted apply not reconciled: digest={current_digest} "
            f"healthy={healthy} detail={detail}"
        ),
    )
    return None


def resolve_stack_dir(
    *,
    inspect_data: dict[str, Any],
    container_labels: dict[str, str],
    compose_manager_root: str | None,
    dockge_stacks_root: str | None,
    ix_apps_root: str | None = None,
) -> str | None:
    """Pick the on-host absolute directory of a stack for ``docker compose up -d``.

    Order of attempts:

    1. The ``auto-update.stack-dir`` label (always wins if set).
    2. The ``com.docker.compose.project.working_dir`` label.
    3. If ``com.dockge.owner`` or ``auto-update.dockge=true`` is set,
       use ``<dockge_stacks_root>/<project>``.
    4. Otherwise ``<compose_manager_root>/<project>``.
    5. ix-Systems TrueNAS Apps fallback: look in
       ``<ix_apps_root>/<project>/versions/<latest>/templates/rendered/``
       and return that path. The compose file is named
       ``docker-compose.yaml`` (already supported by the apply pipeline).
    """
    override = container_labels.get("auto-update.stack-dir")
    if override:
        return override
    workdir = container_labels.get("com.docker.compose.project.working_dir")
    if workdir:
        return workdir
    project = container_labels.get("com.docker.compose.project")
    if not project:
        # No compose project label — try ix-apps fallback before giving up.
        # Useful for single-container stacks named after their ix-app
        # (e.g. "ix-clamav" but the running container is just "clamav-1").
        if ix_apps_root:
            ix_path = _resolve_ix_apps_stack_dir(ix_apps_root, stack_name_fallback=container_labels.get("auto-update.ix-app", ""))
            if ix_path:
                return ix_path
        return None
    use_dockge = bool(
        container_labels.get("com.dockge.owner")
        or container_labels.get("auto-update.dockge", "").lower() == "true"
    )
    if use_dockge and dockge_stacks_root:
        return f"{dockge_stacks_root.rstrip('/')}/{project}"
    if compose_manager_root:
        return f"{compose_manager_root.rstrip('/')}/{project}"
    # ix-Systems TrueNAS Apps fallback (e.g. ix-clamav -> /mnt/.ix-apps/app_configs/clamav/...)
    if ix_apps_root:
        ix_path = _resolve_ix_apps_stack_dir(ix_apps_root, project=project)
        if ix_path:
            return ix_path
    return None


def _resolve_ix_apps_stack_dir(ix_apps_root: str, project: str | None = None,
                              *, stack_name_fallback: str = "") -> str | None:
    """Resolve the rendered compose dir for an iX-Systems TrueNAS App.

    The iX-Systems App format on TrueNAS stores each app's rendered
    compose file at:
        <ix_apps_root>/<app_name>/versions/<version>/templates/rendered/docker-compose.yaml

    We pick the latest version directory (lexicographic sort of the
    version subdirs). ``project`` is preferred as the app name; if
    ``project`` is None (no compose project label), we fall back to
    ``stack_name_fallback`` which can be set via the
    ``auto-update.ix-app`` container label.

    Returns the absolute path to the rendered dir, or None if no
    matching app/version was found.
    """
    import os
    candidates: list[tuple[str, str]] = []  # (version, full_path)
    # Build the candidate app names: try the raw value, then the ix-
    # prefixed version stripped, then the other direction. This handles
    # both "ix-clamav" (in pending_updates) → "clamav" (app dir on disk)
    # and the reverse case.
    raw_names: list[str] = []
    for n in (project, stack_name_fallback):
        if not n:
            continue
        raw_names.append(n)
        if n.startswith("ix-"):
            raw_names.append(n[3:])
        else:
            raw_names.append("ix-" + n)
    # Dedup while preserving order
    seen: set[str] = set()
    names: list[str] = []
    for n in raw_names:
        if n not in seen:
            seen.add(n)
            names.append(n)
    for app_name in names:
        app_dir = f"{ix_apps_root.rstrip('/')}/{app_name}"
        if not os.path.isdir(app_dir):
            continue
        versions_dir = f"{app_dir}/versions"
        if not os.path.isdir(versions_dir):
            continue
        for v in sorted(os.listdir(versions_dir), reverse=True):
            rendered = f"{versions_dir}/{v}/templates/rendered"
            if os.path.isfile(f"{rendered}/docker-compose.yaml") or \
               os.path.isfile(f"{rendered}/compose.yaml"):
                candidates.append((v, rendered))
    if not candidates:
        return None
    # Sort by version string desc, then return the newest
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# -- dependency injection types --------------------------------------------


class _PipelineFn(Protocol):
    async def __call__(
        self,
        host: HostClient,
        state: State,
        *,
        stack: str,
        to_digest: str,
        image: str,
        container_name: str | None = None,
        compose_manager_root: str | None = None,
        dockge_stacks_root: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...


class _FetchFn(Protocol):
    async def __call__(self, image: str) -> ReleaseNotes | None: ...


class _ClassifyFn(Protocol):
    async def __call__(
        self,
        *,
        endpoint: str,
        model: str,
        notes_text: str,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> RiskVerdict: ...


# -- the orchestrator ------------------------------------------------------


@dataclass
class _Inputs:
    """Inputs to evaluate_and_act that aren't already parameters."""
    image: str
    stack: str
    to_digest: str
    compose_manager_root: str | None
    dockge_stacks_root: str | None
    ix_apps_root: str | None = None


def _pipeline_result_to_dict(result: Any) -> dict[str, Any]:
    """Convert a smart-pipeline dataclass or classic dict to a plain dict.

    run_smart_pipeline returns a SmartPipelineResult dataclass; the classic
    run_pipeline returns a plain dict. The dismiss logic below needs the
    uniform shape.
    """
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict") and callable(result.to_dict):
        return result.to_dict()
    if hasattr(result, "__dict__"):
        return dict(result.__dict__)
    return {"action": "?", "ok": False, "error": f"unrecognized result type: {type(result).__name__}"}


async def evaluate_and_act(
    *,
    host: HostClient,
    state: State,
    inputs: _Inputs,
    fetch_release_notes: _FetchFn,
    classify_release_notes: _ClassifyFn,
    run_pipeline: _PipelineFn,
    notifier: Notifier,
    policy: str = "safe-and-caution",
    llm_endpoint: str = "",
    llm_api_key: str = "",
    llm_model: str = "",
    llm_timeout: float = 30.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Decide and act on a single pending update.

    Returns a result dict with ``action`` (``applied``, ``notified_breaking``,
    ``notified_caution``, ``skipped_no_notes``, ``skipped``) and the
    intermediate details for diagnostics.
    """
    # 1. Inspect the running container to find its labels and stack dir.
    # The scanner (post-v0.9.13) writes rows keyed by compose project
    # label, so resolve ``inputs.stack`` to a real container NAME
    # before calling inspect. Falls back to direct inspect on KeyError
    # (legacy v0.9.12 behavior) for any row that still has a
    # container-NAME key.
    try:
        container_name = await _resolve_container_for_stack(host, inputs.stack)
    except KeyError as e:
        log.warning(
            "resolve_container_for_stack %s failed, falling back to "
            "direct inspect: %s",
            inputs.stack, e,
        )
        container_name = inputs.stack
    try:
        inspect = await host.inspect_container(container_name)
    except Exception as e:
        log.warning("inspect %s on %s failed: %s", container_name, host.name, e)
        inspect = {}
    cfg = (inspect.get("Config") or {})
    raw_labels = cfg.get("Labels") or {}
    if not isinstance(raw_labels, dict):
        raw_labels = {}
    labels = {str(k): str(v) for k, v in raw_labels.items()}
    # Resolve the project key: prefer the compose label. For iX-Systems
    # TrueNAS Apps the label is already "ix-<app>" (e.g. "ix-dockge"), which
    # matches the existing Docker project; do NOT strip the prefix or we create
    # a duplicate stack on the same host ports.
    project = labels.get("com.docker.compose.project", inputs.stack)
    stack_dir = resolve_stack_dir(
        inspect_data=inspect,
        container_labels=labels,
        compose_manager_root=inputs.compose_manager_root,
        dockge_stacks_root=inputs.dockge_stacks_root,
        ix_apps_root=inputs.ix_apps_root,
    )

    # 2. Fetch release notes for the new image.
    notes = await fetch_release_notes(inputs.image)
    if notes is None:
        verdict = RiskVerdict(
            risk="CAUTION", summary="no release notes fetched"
        )
    else:
        try:
            verdict = await classify_release_notes(
                endpoint=llm_endpoint, model=llm_model,
                notes_text=notes.text,
                api_key=llm_api_key, timeout=llm_timeout,
            )
        except Exception as e:
            log.warning("classifier raised: %s", e)
            verdict = RiskVerdict(risk="CAUTION", summary=f"classifier raised: {e}")

    # 3. Apply the policy. For BREAKING or (safe-only + CAUTION), notify only.
    if verdict.risk == "BREAKING":
        await notifier.notify_breaking(
            host.name, project, inputs.image, verdict.summary,
        )
        # Only dismiss the pending row on a real run.  smart_apply_tool calls
        # evaluate_and_act with dry_run=True just to get the verdict; if we
        # dismiss here, the subsequent real apply sees no pending row and
        # returns no_pending_update without ever updating the container.
        if not dry_run:
            try:
                await state.mark_update_seen(host.name, inputs.stack, inputs.to_digest)
            except Exception as e:
                log.warning("dismiss pending after BREAKING notify failed: %s", e)
        return {
            "action": "notified_breaking",
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
        }

    if verdict.risk == "CAUTION" and policy == "safe-only":
        await notifier.notify_caution(
            host.name, project, inputs.image, verdict.summary,
        )
        if not dry_run:
            try:
                await state.mark_update_seen(host.name, inputs.stack, inputs.to_digest)
            except Exception as e:
                log.warning("dismiss pending after CAUTION notify failed: %s", e)
        return {
            "action": "notified_caution",
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
        }

    if dry_run:
        return {
            "action": "dry_run",
            "would_apply": True,
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
            "to_digest": inputs.to_digest,
            "image": inputs.image,
            "policy": policy,
            "dry_run": True,
        }

    # 4. Apply. Before running the pipeline, reconcile any stale
    # in_progress row left by a previous interrupted run. If the container
    # already updated successfully but the daemon died before recording
    # the result, we close out that row and skip the redundant apply.
    if not inputs.to_digest:
        return {
            "action": "skipped",
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
            "skip_reason": "no to_digest supplied",
        }

    reconciled = await _reconcile_in_progress_row(
        host=host,
        state=state,
        stack=inputs.stack,
        to_digest=inputs.to_digest,
        image=inputs.image,
    )
    if reconciled:
        # If reconciliation says an existing apply is still running,
        # don't dismiss the pending row; just report skip and move on.
        if not reconciled.get("skip"):
            try:
                await state.mark_update_seen(host.name, inputs.stack, inputs.to_digest)
            except Exception as e:
                log.warning("dismiss pending after reconcile failed: %s", e)
        reconciled["verdict"] = verdict.to_dict()
        reconciled["notes_source"] = notes.source if notes else ""
        reconciled["stack_dir"] = stack_dir
        return reconciled

    try:
        # Pass image= so that run_smart_pipeline (which requires it) works
        # when injected. The classic run_pipeline accepts **image** since
        # Phase 7c — it just ignores it. See patches/pipeline.py.
        apply_result_raw = await run_pipeline(
            host, state,
            stack=project, to_digest=inputs.to_digest,
            image=inputs.image,
            
            compose_manager_root=inputs.compose_manager_root,
            dockge_stacks_root=inputs.dockge_stacks_root,
        )
    except Exception as e:
        log.exception("apply failed for %s/%s: %s", host.name, project, e)
        return {
            "action": "failed",
            "verdict": verdict.to_dict(),
            "error": str(e),
        }

    apply_result = _pipeline_result_to_dict(apply_result_raw)

    # 5. Dismiss the pending row on a successful apply.
    # Only dismiss for REAL apply outcomes. Dry-runs return ok=True too
    # (the pipeline just returns a plan), and we want dry-runs to NOT
    # clear pending rows so we can re-run them for visibility.
    # Real-apply actions are: "applied" (compose), "tag_swapped" (dockerman
    # tag-swap path). "rolled_back" / "rollback_failed" also count: the
    # pipeline tried to apply and either reverted or failed — either way
    # the operator must intervene before this row can be cleared, so we
    # DON'T auto-dismiss those (they stay in the queue until manual action).
    if (apply_result.get("ok")
            and apply_result.get("action") in ("applied", "tag_swapped")):
        # v0.9.13 hermes patch: dismiss by inputs.stack (the row key)
        # not by ``project``. The scanner writes the row keyed by
        # inputs.stack, so the dismiss must match. Before v0.9.13
        # the scanner wrote by container NAME and this dismiss used
        # ``project`` (the compose label), so the keys never matched
        # and the row persisted forever. Switching to inputs.stack
        # is the correct fix.
        try:
            await state.mark_update_seen(host.name, inputs.stack, inputs.to_digest)
        except Exception as e:
            log.warning("dismiss pending failed: %s", e)

    return {
        "action": (apply_result.get("action")
                   if apply_result.get("action")
                   else ("applied" if apply_result.get("ok") else "apply_failed")),
        "verdict": verdict.to_dict(),
        "notes_source": notes.source if notes else "",
        "stack_dir": stack_dir,
        "apply_result": apply_result,
    }

