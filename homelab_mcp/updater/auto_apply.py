"""The auto-apply orchestrator.

Wires up:

1. :func:`homelab_mcp.updater.release_notes.fetch_release_notes` — fetch
   release notes for the **new** version.
2. :func:`homelab_mcp.updater.risk.classify_release_notes` — get a
   risk verdict (SAFE / CAUTION / BREAKING).
3. Apply policy: SAFE and CAUTION are applied under the default
   ``safe-and-caution`` policy; BREAKING is always notified and never
   auto-applied; under ``safe-only`` only SAFE is applied.
4. On a successful apply, dismiss the pending row.
5. On BREAKING (and on CAUTION under safe-only), send a notification
   with the LLM's summary and migration steps.

The orchestrator exposes a single function, :func:`evaluate_and_act`,
plus a small pure helper, :func:`resolve_stack_dir`. Tests can pass
fakes for ``fetch_release_notes``, ``classify_release_notes``, and
``run_pipeline``; the cron entry point passes the real ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.notifier import Notifier
from homelab_mcp.updater.release_notes import ReleaseNotes
from homelab_mcp.updater.risk import RiskVerdict

log = logging.getLogger(__name__)


# -- stack-dir resolution --------------------------------------------------


def resolve_stack_dir(
    *,
    inspect_data: dict[str, Any],
    container_labels: dict[str, str],
    compose_manager_root: str | None,
    dockge_stacks_root: str | None,
) -> str | None:
    """Pick the on-host absolute directory of a stack for ``docker compose up -d``.

    Order of attempts:

    1. The ``auto-update.stack-dir`` label (always wins if set).
    2. The ``com.docker.compose.project.working_dir`` label.
    3. If ``com.dockge.owner`` or ``auto-update.dockge=true`` is set,
       use ``<dockge_stacks_root>/<project>``.
    4. Otherwise ``<compose_manager_root>/<project>``.
    """
    override = container_labels.get("auto-update.stack-dir")
    if override:
        return override
    workdir = container_labels.get("com.docker.compose.project.working_dir")
    if workdir:
        return workdir
    project = container_labels.get("com.docker.compose.project")
    if not project:
        return None
    use_dockge = bool(
        container_labels.get("com.dockge.owner")
        or container_labels.get("auto-update.dockge", "").lower() == "true"
    )
    if use_dockge and dockge_stacks_root:
        return f"{dockge_stacks_root.rstrip('/')}/{project}"
    if compose_manager_root:
        return f"{compose_manager_root.rstrip('/')}/{project}"
    return None


# -- dependency injection types --------------------------------------------


class _PipelineFn(Protocol):
    async def __call__(
        self,
        host: HostClient,
        state: State,
        *,
        stack: str,
        to_digest: str,
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
    try:
        inspect = await host.inspect_container(inputs.stack)
    except Exception as e:
        log.warning("inspect %s on %s failed: %s", inputs.stack, host.name, e)
        inspect = {}
    cfg = (inspect.get("Config") or {})
    raw_labels = cfg.get("Labels") or {}
    if not isinstance(raw_labels, dict):
        raw_labels = {}
    labels = {str(k): str(v) for k, v in raw_labels.items()}
    project = labels.get("com.docker.compose.project", inputs.stack)
    stack_dir = resolve_stack_dir(
        inspect_data=inspect,
        container_labels=labels,
        compose_manager_root=inputs.compose_manager_root,
        dockge_stacks_root=inputs.dockge_stacks_root,
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

    # 3. Apply the policy.
    if verdict.risk == "BREAKING":
        if dry_run:
            # Don't even notify in dry_run — return the plan only.
            return {
                "action": "dry_run",
                "would_apply": False,
                "verdict": verdict.to_dict(),
                "notes_source": notes.source if notes else "",
                "stack_dir": stack_dir,
                "to_digest": inputs.to_digest,
                "image": inputs.image,
                "policy": policy,
                "dry_run": True,
            }
        await _notify_breaking(notifier, image=inputs.image, stack=project, verdict=verdict, notes=notes)
        # BREAKING was notified but never applied. Dismiss the
        # pending row so the canary cron doesn't re-notify the
        # same drift every 6h. (The user already saw it; the
        # apply_status column on the notification tells them what
        # to do next, and a new pending row will appear if a
        # newer upstream digest is published.)
        try:
            await state.mark_update_seen(host.name, project, inputs.to_digest)
        except Exception as e:
            log.warning("dismiss pending after BREAKING notify failed: %s", e)
        return {
            "action": "notified_breaking",
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
        }

    if verdict.risk == "SAFE":
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
    else:  # CAUTION
        if policy == "safe-only":
            if dry_run:
                return {
                    "action": "dry_run",
                    "would_apply": False,
                    "verdict": verdict.to_dict(),
                    "notes_source": notes.source if notes else "",
                    "stack_dir": stack_dir,
                    "to_digest": inputs.to_digest,
                    "image": inputs.image,
                    "policy": policy,
                    "dry_run": True,
                }
            await _notify_caution(
                notifier, image=inputs.image, stack=project,
                verdict=verdict, notes=notes,
            )
            # CAUTION under safe-only was notified but never
            # applied. Dismiss the pending row so the canary cron
            # doesn't re-notify the same drift every 6h. (If the
            # user wants the apply, they can do it manually via
            # the WebUI with force=True.)
            try:
                await state.mark_update_seen(host.name, project, inputs.to_digest)
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

    # 4. Apply.
    if not inputs.to_digest:
        return {
            "action": "skipped",
            "verdict": verdict.to_dict(),
            "notes_source": notes.source if notes else "",
            "stack_dir": stack_dir,
            "skip_reason": "no to_digest supplied",
        }

    try:
        apply_result = await run_pipeline(
            host, state,
            stack=project, to_digest=inputs.to_digest,
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

    # 5. Dismiss the pending row on a successful apply.
    if apply_result.get("ok"):
        try:
            await state.mark_update_seen(host.name, project, inputs.to_digest)
        except Exception as e:
            log.warning("dismiss pending failed: %s", e)

    return {
        "action": "applied" if apply_result.get("ok") else "apply_failed",
        "verdict": verdict.to_dict(),
        "notes_source": notes.source if notes else "",
        "stack_dir": stack_dir,
        "apply_result": apply_result,
    }


async def _notify_breaking(
    notifier: Notifier, *, image: str, stack: str,
    verdict: RiskVerdict, notes: ReleaseNotes | None,
) -> None:
    body_lines = [
        f"**{verdict.risk}** for {image} on stack {stack}",
        "",
        f"Summary: {verdict.summary}",
    ]
    if verdict.migration_steps:
        body_lines += ["", "Migration steps:"]
        body_lines += [f"- {s}" for s in verdict.migration_steps]
    if verdict.compose_changes:
        body_lines += ["", "Compose changes:"]
        body_lines += [f"- {s}" for s in verdict.compose_changes]
    if notes and notes.tag:
        body_lines += ["", f"Tag: {notes.tag}"]
    await notifier.notify(
        "\n".join(body_lines),
        title=f"BREAKING: {image}",
        tags=["warning", "homelab-mcp"],
        priority="high",
    )


async def _notify_caution(
    notifier: Notifier, *, image: str, stack: str,
    verdict: RiskVerdict, notes: ReleaseNotes | None,
) -> None:
    body_lines = [
        f"**{verdict.risk}** for {image} on stack {stack} (safe-only policy)",
        "",
        f"Summary: {verdict.summary}",
    ]
    if verdict.migration_steps:
        body_lines += ["", "Migration steps:"]
        body_lines += [f"- {s}" for s in verdict.migration_steps]
    if notes and notes.tag:
        body_lines += ["", f"Tag: {notes.tag}"]
    await notifier.notify(
        "\n".join(body_lines),
        title=f"CAUTION: {image}",
        tags=["warning", "homelab-mcp"],
        priority="default",
    )
