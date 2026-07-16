"""apply_update_tool: the MCP-callable smart-update entry point.

This is what the project was named for. An AI agent (or a curl
call to the MCP protocol) can invoke this to actually run the
release-notes → LLM classify → policy gate → apply → healthcheck →
rollback pipeline for a single host/stack.

Wires up the same machinery the auto-apply cron uses
(``updater.auto_apply.evaluate_and_act``) but exposes it as a
synchronous-style MCP tool — i.e. one tool call does the whole
pipeline for one stack and returns a structured result.
"""

from __future__ import annotations

import logging
from typing import Any

from homelab_mcp.config import Settings
from homelab_mcp.server import get_host, get_state, mcp
from homelab_mcp.updater.auto_apply import (
    _Inputs,
    evaluate_and_act,
)
from homelab_mcp.updater.notifier import Notifier
from homelab_mcp.updater.pipeline import run_pipeline
from homelab_mcp.updater.release_notes import fetch_release_notes
from homelab_mcp.updater.risk import classify_release_notes

log = logging.getLogger(__name__)


def _build_notifier_from_settings(settings: Settings) -> Notifier:
    """Construct a Notifier covering all configured backends.

    Mirrors the construction in ``auto_apply_main._build_notifier``
    so the MCP tool's notify behavior matches the cron's.
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
        # Always have at least a console notifier so the tool works in dev
        notifiers.append(ConsoleNotifier())
    return MultiNotifier(notifiers)


@mcp.tool()
async def apply_update_tool(
    host: str,
    stack: str,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply the latest pending update for a (host, stack).

    This is the smart-update entry point. It runs the full
    pipeline for one stack:

    1. Look up the pending update row (current_digest → latest_digest)
    2. Fetch release notes for the new image
    3. Send notes to the LLM for SAFE / CAUTION / BREAKING classification
    4. Apply the configured policy:
       - ``safe-and-caution`` (default): apply SAFE and CAUTION
       - ``safe-only``: apply SAFE only, notify on CAUTION + BREAKING
    5. If the policy says apply: snapshot, pull, up -d, healthcheck,
       rollback on failure
    6. If BREAKING (or safe-only + CAUTION): notify instead of applying
    7. On a successful apply, dismiss the pending row

    Parameters
    ----------
    host : str
        The host alias (must be in HOMELAB_MCP_HOSTS).
    stack : str
        The stack name (e.g. "nextcloud", "immich"). This is the
        compose project name, not the container name.
    force : bool, default False
        If True, override the policy: apply even when the LLM
        classified the update as BREAKING. **Use with care.**
        This bypasses the safe-and-caution / safe-only policy but
        does NOT bypass healthcheck + rollback — those still run.
    dry_run : bool, default False
        If True, return the LLM's risk classification and the plan
        (would_apply, to_digest, stack_dir, notes_source) WITHOUT
        snapshotting, pulling, or restarting anything. Safe to call
        on any stack. Combine with ``force=False`` to preview what
        the policy would do.

    Returns
    -------
    dict
        Result with the following keys:
        - action: one of "applied", "notified_breaking",
          "notified_caution", "no_pending_update",
          "skipped_no_notes", "skipped", "failed"
        - verdict: the LLM's classification (risk, summary,
          migration_steps, compose_changes, env_changes)
        - apply_result: only when action is "applied" or
          "apply_failed"; details from the pipeline (snapshot
          digest, probe result, rollback digest if any)
        - error: present if action is "failed"
    """
    state = get_state()
    host_client = get_host(host)

    # 1. Find the pending row. The most-recent row for (host, stack) wins.
    pending = await state.list_pending_updates(host=host)
    rows = [r for r in pending if r.get("stack") == stack]
    if not rows:
        return {
            "action": "no_pending_update",
            "host": host,
            "stack": stack,
            "message": (
                f"no pending update for {host}/{stack}. "
                f"Call trigger_scan_tool first to populate pending updates."
            ),
        }
    row = rows[0]  # list_pending_updates orders by stack
    to_digest = row["latest_digest"]
    current_digest = row["current_digest"]

    # We need the image string, which is what the registry / release-notes
    # fetcher wants. The pending_updates row doesn't store the image ref
    # itself (just digests), so we look it up from the running container.
    try:
        inspect = await host_client.inspect_container(stack)
    except Exception as e:
        return {
            "action": "failed",
            "host": host,
            "stack": stack,
            "error": f"inspect_container failed: {e}",
        }
    cfg = (inspect.get("Config") or {})
    image = cfg.get("Image") or ""
    if not image:
        return {
            "action": "failed",
            "host": host,
            "stack": stack,
            "error": "could not determine image from container inspect",
        }

    # 2. Build the inputs that the orchestrator needs.
    settings = Settings()
    inputs = _Inputs(
        image=image,
        stack=stack,
        to_digest=to_digest,
        compose_manager_root=None,  # CA compose.manager not used; Dockge path is read from labels
        dockge_stacks_root=settings.dockge_stacks_root,
    )

    # 3. Build the notifier.
    notifier = _build_notifier_from_settings(settings)

    # 4. Run the pipeline. If force=True, temporarily flip the policy to
    #    "safe-and-caution" so the BREAKING branch is skipped. The
    #    pipeline itself still healthchecks + rolls back on failure.
    effective_policy = "safe-and-caution" if force else settings.auto_apply_policy

    try:
        result = await evaluate_and_act(
            host=host_client,
            state=state,
            inputs=inputs,
            fetch_release_notes=fetch_release_notes,
            classify_release_notes=classify_release_notes,
            run_pipeline=run_pipeline,
            notifier=notifier,
            policy=effective_policy,
            llm_endpoint=settings.llm_endpoint,
            llm_api_key=settings.llm_api_key,
            llm_model=settings.llm_model,
            llm_timeout=float(settings.llm_timeout),
            dry_run=dry_run,
        )
    except Exception as e:
        log.exception("apply_update_tool raised: %s", e)
        return {
            "action": "failed",
            "host": host,
            "stack": stack,
            "error": str(e),
        }

    # Enrich the result with the host/stack context.
    result["host"] = host
    result["stack"] = stack
    result["image"] = image
    result["current_digest"] = current_digest
    result["to_digest"] = to_digest
    result["policy"] = effective_policy
    result["forced"] = force
    result["dry_run"] = dry_run
    return result
