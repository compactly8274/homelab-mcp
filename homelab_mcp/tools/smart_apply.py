"""smart_apply_tool: a unified MCP entrypoint that runs the full
5-stage apply pipeline (Classify → Migrate → Snapshot → Apply → Watch).

For Phase 3 (2026-07-29) this calls :func:`run_smart_pipeline`
directly — the new orchestrator that handles compose + dockerman
stacks, runs LLM-classified migration steps before the pull, and
watches the result before declaring success. On any failure the
pipeline reverts via :func:`~homelab_mcp.updater.revert.revert`.

The tool is the canonical entry point for new code paths;
``apply_update_tool`` stays for backwards compat (compose-only,
no migration executor, no watchdog).
"""
from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.config import Settings
from homelab_mcp.server import get_host, get_state, mcp
from homelab_mcp.updater.auto_apply import _Inputs, evaluate_and_act
from homelab_mcp.updater.migration import MigrationStep
from homelab_mcp.updater.notifier import Notifier
from homelab_mcp.updater.release_notes import fetch_release_notes
from homelab_mcp.updater.risk import classify_release_notes
from homelab_mcp.updater.smart_pipeline import run_smart_pipeline, smart_pipeline_to_dict

log = logging.getLogger(__name__)


def _build_notifier_from_settings(settings: Settings) -> Notifier:
    """Mirror ``tools.apply_update._build_notifier_from_settings``.

    Imported here as a copy to avoid the circular import.
    """
    from homelab_mcp.updater.notifier import (
        ConsoleNotifier,
        MultiNotifier,
        NtfyNotifier,
    )
    notifiers: list[Notifier] = []
    if settings.ntfy_topic:
        notifiers.append(NtfyNotifier(
            base_url=settings.ntfy_url,
            topic=settings.ntfy_topic,
            priority=settings.ntfy_priority,
        ))
    if settings.discord_webhook_url:
        from homelab_mcp.updater.discord import DiscordNotifier
        notifiers.append(DiscordNotifier(
            webhook_url=settings.discord_webhook_url,
            username=settings.discord_username,
        ))
    if settings.pushover_app_token and settings.pushover_user_key:
        from homelab_mcp.updater.pushover import PushoverNotifier
        notifiers.append(PushoverNotifier(
            app_token=settings.pushover_app_token,
            user_key=settings.pushover_user_key,
            device=settings.pushover_device,
            sound=settings.pushover_sound,
        ))
    if not notifiers:
        notifiers.append(ConsoleNotifier())
    return MultiNotifier(notifiers)


@mcp.tool()
async def smart_apply_tool(
    host: str,
    stack: str,
    force: bool = False,
    dry_run: bool = False,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Smart auto-apply: classify, migrate, snapshot, apply, watch + revert.

    Full pipeline:

    1. **Classify** — fetch release notes + LLM verdict.
    2. **Migrate** — if BREAKING with structured_steps, run them
       on the target host BEFORE the pull. Abort on any failure.
    3. **Snapshot** — capture pre-state (compose: working_dir +
       manifest; dockerman: full run_config).
    4. **Apply** — pull + restart (compose: ``docker compose up -d``;
       dockerman: ``docker pull`` + ``docker run``).
    5. **Watch** — if HOMELAB_MCP_WATCHDOG_ENABLED=1, poll health
       until ok or timeout. Revert on failure.

    On any failure after the snapshot, the pipeline reverts via
    :func:`~homelab_mcp.updater.revert.revert`.

    Parameters
    ----------
    host : str
        The host alias (must be in HOMELAB_MCP_HOSTS).
    stack : str
        The stack name (e.g. "nextcloud", "PeaNUT").
    force : bool
        Apply even when LLM classifies BREAKING.
    dry_run : bool
        Plan + classify without applying.
    require_approval : bool
        Run preflight gate first; refuse on blockers.

    Returns
    -------
    dict
        Same envelope as ``apply_update_tool`` plus:

        - ``snapshot_kind``: "compose" or "dockerman"
        - ``migration``: per-step outcome from the migration executor
        - ``watchdog``: post-apply probe results
    """
    settings = Settings()
    state = get_state()
    host_client = get_host(host)
    notifier = _build_notifier_from_settings(settings)

    # 1. Look up the pending row
    pending = await state.list_pending_updates(host=host)
    rows = [r for r in pending if r.get("stack") == stack]
    if not rows:
        return {
            "action": "no_pending_update",
            "host": host, "stack": stack,
            "message": f"no pending update for {host}/{stack}",
        }
    row = rows[0]
    to_digest = row["latest_digest"]
    current_digest = row["current_digest"]

    # v0.9.13 hermes patch: resolve stack→container (post-v0.9.13
    # scanner writes rows keyed by compose project label, not NAME).
    try:
        from homelab_mcp.tools.apply_update import _resolve_container_for_stack
        container_name = await _resolve_container_for_stack(host_client, stack)
    except KeyError as e:
        return {
            "action": "failed", "host": host, "stack": stack,
            "error": f"resolve_container_for_stack failed: {e}",
        }
    try:
        inspect = await host_client.inspect_container(container_name)
    except Exception as e:
        return {
            "action": "failed", "host": host, "stack": stack,
            "error": f"inspect_container({container_name}) failed: {e}",
        }
    cfg = (inspect.get("Config") or {})
    image = cfg.get("Image") or ""
    if not image:
        return {
            "action": "failed", "host": host, "stack": stack,
            "error": "could not determine image from container inspect",
        }

    # 2. Pre-flight gate (unless dry_run or force)
    if require_approval and not dry_run:
        try:
            from homelab_mcp.tools.preflight import preflight_check_tool
            verdict = await preflight_check_tool(
                host=host, stack=stack, action="apply_update",
            )
            if not verdict["safe"]:
                return {
                    "action": "blocked",
                    "host": host, "stack": stack, "image": image,
                    "current_digest": current_digest, "to_digest": to_digest,
                    "preflight": verdict,
                    "message": f"preflight refused: {len(verdict['blockers'])} blocker(s)",
                }
        except Exception as e:
            log.warning("preflight_check failed: %s", e)

    # 3. Fetch release notes + classify (via the existing evaluate_and_act,
    #    but skipping its apply step — we want the verdict + migration steps,
    #    not the apply itself).
    inputs = _Inputs(
        image=image, stack=stack, to_digest=to_digest,
        compose_manager_root=None,
        dockge_stacks_root=settings.dockge_stacks_root,
    )

    # 4. Call evaluate_and_act ONLY to get the verdict + migration_steps,
    #    bypassing the apply (use a no-op pipeline). Then call run_smart_pipeline.
    async def _no_op_pipeline(*args, **kwargs):
        return {"ok": True, "action": "dry_run", "from_digest": None, "to_digest": None}

    try:
        classification = await evaluate_and_act(
            host=host_client, state=state, inputs=inputs,
            fetch_release_notes=fetch_release_notes,
            classify_release_notes=classify_release_notes,
            run_pipeline=_no_op_pipeline,
            notifier=notifier,
            policy="safe-and-caution" if force else settings.auto_apply_policy,
            llm_endpoint=settings.llm_endpoint,
            llm_api_key=settings.llm_api_key,
            llm_model=settings.llm_model,
            llm_timeout=float(settings.llm_timeout),
            dry_run=True,
        )
    except Exception as e:
        log.exception("smart_apply_tool classification failed: %s", e)
        return {
            "action": "failed", "host": host, "stack": stack,
            "error": f"classification failed: {e}",
        }

    verdict = classification.get("verdict", {})
    risk = verdict.get("risk", "CAUTION")

    # BREAKING with safe-only policy and not force → notify, don't apply
    if risk == "BREAKING" and not force:
        # The evaluate_and_act already notified in the no-op path? No — it
        # didn't because we passed dry_run=True. So notify here.
        try:
            from homelab_mcp.updater.auto_apply import _notify_breaking
            from homelab_mcp.updater.release_notes import fetch_release_notes as _fr
            notes_obj = await _fr(image)
            await _notify_breaking(notifier, image=image, stack=stack,
                                   verdict=verdict, notes=notes_obj)
        except Exception as e:
            log.warning("notify_breaking: %s", e)
        return {
            "action": "notified_breaking",
            "host": host, "stack": stack, "image": image,
            "verdict": verdict,
            "to_digest": to_digest,
            "message": "BREAKING update; force=True to apply",
        }

    # 5. Convert structured_steps → MigrationStep list
    raw_steps = verdict.get("structured_steps") or []
    migration_steps: list[MigrationStep] = []
    for entry in raw_steps:
        if isinstance(entry, dict):
            migration_steps.append(MigrationStep.from_dict(entry))

    # 6. Run the 5-stage smart pipeline
    try:
        result = await run_smart_pipeline(
            host_client, state,
            stack=stack, to_digest=to_digest, image=image,
            container_name=stack,
            migration_steps=migration_steps or None,
            dry_run=dry_run,
        )
    except Exception as e:
        log.exception("smart_pipeline raised: %s", e)
        return {
            "action": "failed", "host": host, "stack": stack,
            "error": f"smart_pipeline raised: {e}",
        }

    payload = smart_pipeline_to_dict(result)
    payload["host"] = host
    payload["stack"] = stack
    payload["image"] = image
    payload["current_digest"] = current_digest
    payload["to_digest"] = to_digest
    payload["verdict"] = verdict
    payload["forced"] = force
    payload["dry_run"] = dry_run

    if result.ok and not dry_run:
        try:
            await state.mark_update_seen(host, stack, to_digest)
        except Exception as e:
            log.warning("dismiss pending after apply: %s", e)

    return payload
