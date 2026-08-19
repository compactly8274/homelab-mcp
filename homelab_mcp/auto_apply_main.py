"""``python -m homelab_mcp.auto_apply_main`` entry point.

The cron entry point. Runs one full cycle of:

1. Read all pending_updates rows (optionally filtered to one host).
2. For each row, fetch release notes → classify → apply or notify.
3. Print a one-line summary to stderr (so cron can capture it in mail).
4. Exit 0 on success, non-zero on a fatal error (per-row exceptions
   are isolated and logged but do not affect the exit code).
"""

from __future__ import annotations

import argparse
import asyncio
import aiosqlite
import logging
from datetime import datetime, timedelta, timezone
import os
import sys
from pathlib import Path
from typing import Any

from homelab_mcp.config import Settings
from homelab_mcp.server import build_hosts
from homelab_mcp.state import State
from homelab_mcp.updater.auto_apply import (
    _Inputs,
    _resolve_container_for_stack,
    evaluate_and_act,
)
from homelab_mcp.updater.notifier import MultiNotifier, NtfyNotifier
from homelab_mcp.updater.pipeline import run_pipeline as _default_run_pipeline
from homelab_mcp.updater.release_notes import fetch_release_notes as _default_fetch
from homelab_mcp.updater.risk import classify_release_notes as _default_classify

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line flags."""
    p = argparse.ArgumentParser(
        prog="python -m homelab_mcp.auto_apply_main",
        description=(
            "Run one auto-apply cycle: fetch release notes, classify via LLM, "
            "apply SAFE+CAUTION updates, notify on BREAKING."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="never apply; just classify + log")
    p.add_argument("--host", type=str, default=None, help="only process this host (default: all configured)")
    p.add_argument("--per-row-timeout", type=float, default=120.0, help="seconds per pending row (default 120)")
    p.add_argument("--max-rows", type=int, default=0, help="limit number of rows processed per run (0 = unlimited)")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    return p.parse_args(argv)


async def _resolve_image(host, stack: str) -> str:
    """Look up the image ref of the container that implements this stack."""
    try:
        container_name = await _resolve_container_for_stack(host, stack)
    except Exception:
        return ""
    try:
        inspect = await host.inspect_container(container_name)
    except Exception:
        return ""
    return (inspect.get("Config") or {}).get("Image", "") or ""


async def run_one_cycle(
    *,
    hosts: dict[str, Any],
    state: State,
    dry_run: bool,
    host_filter: str | None,
    per_row_timeout: float,
    fetch_release_notes,
    classify_release_notes,
    run_pipeline,
    notifier,
    compose_manager_root: str | None,
    dockge_stacks_root: str | None,
    policy: str = "safe-and-caution",
    llm_endpoint: str = "",
    llm_api_key: str = "",
    llm_model: str = "",
    llm_timeout: float = 30.0,
    max_rows: int = 0,
) -> list[dict[str, Any]]:
    """Run the cycle. Returns a list of per-row result dicts.

    Per-row exceptions are caught and logged so one bad row does not
    stop the rest of the cycle.
    """
    rows = await state.list_pending_updates(host=host_filter)
    if max_rows and max_rows > 0:
        rows = rows[:max_rows]
    out: list[dict[str, Any]] = []
    for row in rows:
        host_name = row["host"]
        stack = row["stack"]
        host = hosts.get(host_name)
        if host is None:
            log.warning("pending row for %s but no host client; skipping", host_name)
            out.append({"host": host_name, "stack": stack, "action": "no_host_client"})
            continue
        image = await _resolve_image(host, stack)
        if not image:
            log.warning("pending row for %s/%s: couldn't resolve image", host_name, stack)
            out.append({"host": host_name, "stack": stack, "action": "no_image"})
            continue

        inputs = _Inputs(
            image=image,
            stack=stack,
            to_digest=row.get("latest_digest", ""),
            compose_manager_root=compose_manager_root,
            dockge_stacks_root=dockge_stacks_root,
        )
        try:
            result = await asyncio.wait_for(
                evaluate_and_act(
                    host=host, state=state, inputs=inputs,
                    fetch_release_notes=fetch_release_notes,
                    classify_release_notes=classify_release_notes,
                    run_pipeline=run_pipeline if not dry_run else _dry_run_pipeline,
                    notifier=notifier,
                    policy=policy,
                    llm_endpoint=llm_endpoint,
                    llm_api_key=llm_api_key,
                    llm_model=llm_model,
                    llm_timeout=llm_timeout,
                ),
                timeout=per_row_timeout,
            )
            out.append({
                "host": host_name, "stack": stack,
                "action": result.get("action", "?"),
                "verdict": result.get("verdict", {}),
            })
        except TimeoutError:
            log.error("pending row %s/%s: timeout after %.1fs", host_name, stack, per_row_timeout)
            out.append({"host": host_name, "stack": stack, "action": "timeout"})
        except Exception as e:
            log.exception("pending row %s/%s raised: %s", host_name, stack, e)
            out.append({"host": host_name, "stack": stack, "action": "exception", "error": str(e)})
    return out


async def _dry_run_pipeline(*args, **kwargs):
    """A no-op pipeline that records the would-be apply but doesn't touch docker."""
    return {"ok": True, "action": "dry_run", **kwargs}


def summarize(rows: list[dict[str, Any]]) -> str:
    """Format a one-line summary suitable for cron mail."""
    if not rows:
        return "[homelab-mcp] auto-apply: 0 pending updates"
    counts: dict[str, int] = {}
    for r in rows:
        a = r.get("action", "?")
        counts[a] = counts.get(a, 0) + 1
    parts = [f"{a}={n}" for a, n in sorted(counts.items())]
    return "[homelab-mcp] auto-apply: " + " ".join(parts) + f" (total {len(rows)})"


def _build_notifier(settings: Settings) -> MultiNotifier:
    """Build a MultiNotifier from the Settings.

    Includes every backend whose required env vars are set. ntfy,
    Discord, and Pushover all share the same Notifier protocol so the
    auto-apply pipeline doesn't care which one is configured.
    """
    notifiers = []
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
            device=settings.pushover_device or None,
            sound=settings.pushover_sound,
        ))
    return MultiNotifier(notifiers)




async def _reconcile_orphan_in_progress_rows(state: State, hosts: dict[str, Any]) -> int:
    """Mark stale in_progress rows as failed if no auto_apply_main is running."""
    try:
        db_path = getattr(state, "db_path", getattr(state, "_db_path", None))
        if db_path is None:
            return 0
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, host, stack, started_at FROM update_history WHERE status = 'in_progress'"
            )
            rows = await cursor.fetchall()
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
            fixed = 0
            for row in rows:
                started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                if started > cutoff:
                    continue
                host_name = row["host"]
                host = hosts.get(host_name)
                live_process = False
                if host is not None:
                    try:
                        r = await host.run_command('pgrep -f "auto_apply_main.py" > /dev/null 2>&1 && echo yes || echo no')
                        live_process = r.ok and "yes" in (r.stdout or "")
                    except Exception:
                        live_process = True
                if not live_process:
                    await db.execute(
                        "UPDATE update_history SET status='failed', finished_at=?, reason=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(),
                         "startup reconciliation: orphan in_progress row", row["id"])
                    )
                    fixed += 1
                    log.warning("reconciled orphan in_progress row id=%s %s/%s", row["id"], host_name, row["stack"])
            await db.commit()
            return fixed
    except Exception as e:
        log.warning("startup reconciliation failed: %s", e)
        return 0

def main(argv: list[str] | None = None) -> int:
    """Sync entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = Settings()
    dry_run = args.dry_run or bool(os.getenv("HOMELAB_MCP_AUTO_APPLY_DRY_RUN"))

    hosts = build_hosts(settings)
    state = State(db_path=settings.state_dir / "state.db")

    notifier = _build_notifier(settings)

    asyncio.run(_reconcile_orphan_in_progress_rows(state, hosts))

    rows = asyncio.run(run_one_cycle(
        hosts=hosts, state=state,
        dry_run=dry_run,
        host_filter=args.host,
        per_row_timeout=args.per_row_timeout,
        fetch_release_notes=_default_fetch,
        classify_release_notes=_default_classify,
        run_pipeline=_default_run_pipeline,
        notifier=notifier,
        compose_manager_root=_default_compose_manager_root(),
        dockge_stacks_root=settings.dockge_stacks_root,
        policy=settings.auto_apply_policy,
        llm_endpoint=settings.llm_endpoint,
        llm_api_key=settings.llm_api_key,
        llm_model=settings.llm_model,
        llm_timeout=settings.llm_timeout,
        max_rows=args.max_rows,
    ))

    summary = summarize(rows)
    print(summary, file=sys.stderr)
    return 0


def _default_compose_manager_root() -> str:
    """Best-effort CA compose.manager root for the local host."""
    for candidate in (
        "/boot/config/plugins/compose.manager/projects",
        "/mnt/Apps/compose.manager/projects",
    ):
        if Path(candidate).is_dir():
            return candidate
    return "/boot/config/plugins/compose.manager/projects"


if __name__ == "__main__":
    raise SystemExit(main())
