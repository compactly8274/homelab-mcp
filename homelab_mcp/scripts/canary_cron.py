#!/usr/bin/env python3
"""Recurring cron job: scan + test-apply a small set of "low-risk"
stacks every 6 hours, then notify via ntfy on the result.

This is the "canary" cron — the 3 stacks chosen here are the
canary set:

- PlexAutoLanguages: low-risk, CAUTION-classified already, no
  user-traffic interruption.
- dockwatch: utility, no user traffic.
- homelab-mcp: canary — if a bad apply breaks it, you'll see
  the failure fast (and the WebUI will go down, which is
  itself a signal).

The cron is opt-in: it ONLY runs if the user has set the env
var HOMELAB_MCP_CANARY_CRON=1 on the deployed daemon. Default
off, so a fresh deploy does not silently start applying
updates.

Usage:
    HOMELAB_MCP_CANARY_CRON=1 \\
        python -m homelab_mcp.scripts.canary_cron \\
        --config /path/to/homelab-mcp.env

Scheduling:
    Designed to be run from the system cron (or the hermes
    scheduler) every 6 hours. Each invocation:
      1. Reads HOMELAB_MCP_HOSTS (JSON list) and HOMELAB_MCP_STATE_DIR
      2. For each (host, stack) in CANARY_STACKS:
         a. trigger_scan_tool(host)
         b. list_pending_updates_tool(host) — find this stack
         c. apply_update_tool(host, stack, dry_run=True) — verify safe
         d. If verdict.safe and would_apply: apply_update_tool(dry_run=False)
      3. Send ONE ntfy summary message with the per-stack results.

The script uses the homelab-mcp settings + state machinery so
it goes through the same code paths as the WebUI's /api/apply
endpoint. No special-cased "cron" path that could drift from
the user-facing path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("homelab_mcp.canary_cron")


# Canary set: the 3 stacks the user picked in the WebUI scope Q.
#
# IMPORTANT: do NOT include the running homelab-mcp daemon stack here.
# apply_update_tool for this stack runs `docker compose up -d` on the
# homelab-mcp container itself, which restarts the daemon and kills the
# gateway's MCP session every 6 hours. The canary is for *other* stacks
# so we can detect bad updates without taking down the watcher.
# (Defence in depth: even if a future change re-adds the daemon stack,
# the self-protection check in _run_canary() will refuse to apply.)
CANARY_STACKS: list[tuple[str, str]] = [
    # (host, stack) pairs. Order matters: most isolated first.
    ("truenas", "PlexAutoLanguages"),
    ("truenas", "dockwatch"),
    # homelab-mcp removed 2026-07-24: see defence-in-depth note above.
]


def _load_env(path: Path) -> dict[str, str]:
    """Tiny .env loader. We don't want to depend on python-dotenv
    in a script that's loaded from cron."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _setup_settings(env: dict[str, str]) -> Any:
    """Build a Settings instance with env-var overrides from the loaded .env file."""
    for k, v in env.items():
        if k.startswith("HOMELAB_MCP_") and v:
            os.environ.setdefault(k, v)
    from homelab_mcp.config import Settings
    return Settings()


async def _run_canary() -> dict[str, Any]:
    """The main loop. Returns a summary dict suitable for ntfy."""
    from homelab_mcp import server
    from homelab_mcp import state as state_mod

    env_path = Path(os.environ.get("HOMELAB_MCP_ENV_FILE",
                                   "/mnt/Data/appdata/dockge/stacks/homelab-mcp/.env"))
    env = _load_env(env_path)
    settings = _setup_settings(env)

    db_path = Path(os.environ.get("HOMELAB_MCP_STATE_DIR", "/data")) / "state.db"
    st = state_mod.State(db_path=str(db_path))
    await st.init_db()
    server._state = st
    server.init_hosts(server.build_hosts(settings), st)

    # Lazy-import the tools AFTER init so they see the populated _host_clients.
    from homelab_mcp.tools.apply_update import _build_notifier_from_settings, apply_update_tool
    from homelab_mcp.tools.updates import (
        list_pending_updates_tool,
        trigger_scan_tool,
    )

    # Step 1: trigger scans on each host that has a canary stack.
    hosts_with_canaries = {h for h, _ in CANARY_STACKS}
    scan_results: dict[str, Any] = {}
    for host in hosts_with_canaries:
        try:
            r = await trigger_scan_tool(host=host)
            scan_results[host] = r
            log.info("scan %s: %d drift rows", host, len(r))
        except Exception as e:
            log.warning("scan %s failed: %s", host, e)
            scan_results[host] = {"error": str(e)}

    # Step 2: for each canary stack, find its pending row, dry-run,
    # then apply if safe+would_apply.
    per_stack: list[dict[str, Any]] = []
    for host, stack in CANARY_STACKS:
        # Defence in depth: refuse to ever apply to the stack that is
        # running the homelab-mcp daemon itself. compose_up on that
        # stack would restart the daemon, killing the gateway's MCP
        # session. The canary is supposed to exercise *other* stacks.
        # (See bug: every-6-hour MCP outage 2026-07-22 -> 2026-07-24.)
        # Read HOMELAB_MCP_LOCAL_HOST_ALIAS directly from the env so we
        # don't construct a full Settings (which would require a complete
        # env: HOMELAB_MCP_HOSTS, HOMELAB_MCP_SSH_CONFIG, etc., and would
        # raise in tests or partial envs).
        _self_alias = os.environ.get(
            "HOMELAB_MCP_LOCAL_HOST_ALIAS", "unraid"
        ).lower()
        _self_stack = os.environ.get("HOMELAB_MCP_SELF_STACK", "homelab-mcp")
        if host.lower() == _self_alias and stack == _self_stack:
            log.warning(
                "canary: refusing to apply to self (%s/%s); this stack IS the daemon. "
                "Remove it from CANARY_STACKS or set HOMELAB_MCP_SELF_STACK to override.",
                host, stack,
            )
            per_stack.append({
                "host": host, "stack": stack,
                "outcome": "refused_self",
                "reason": "stack is the running daemon; self-protection",
            })
            continue
        try:
            pendings = await list_pending_updates_tool(host=host)
            row = next((p for p in pendings if p.get("stack") == stack), None)
            if not row:
                per_stack.append({
                    "host": host, "stack": stack,
                    "outcome": "no_pending",
                })
                continue
            # Dry-run first. require_approval=False so we don't
            # block on the preflight gate (the gate is the
            # production safety net; the canary cron intentionally
            # bypasses it because the stacks are pre-vetted).
            dry = await apply_update_tool(
                host=host, stack=stack, dry_run=True, require_approval=False,
            )
            verdict = dry.get("verdict", {})
            if not dry.get("would_apply"):
                per_stack.append({
                    "host": host, "stack": stack,
                    "outcome": "would_not_apply",
                    "risk": verdict.get("risk"),
                    "summary": (verdict.get("summary") or "")[:120],
                })
                continue
            # Apply for real.
            apply_result = await apply_update_tool(
                host=host, stack=stack, dry_run=False, require_approval=False,
            )
            per_stack.append({
                "host": host, "stack": stack,
                "outcome": apply_result.get("action"),
                "risk": verdict.get("risk"),
                "summary": (verdict.get("summary") or "")[:120],
                "apply_error": apply_result.get("error"),
            })
        except Exception as e:
            log.exception("canary %s/%s failed", host, stack)
            per_stack.append({
                "host": host, "stack": stack,
                "outcome": "exception",
                "error": str(e),
            })

    summary = {
        "ok": True,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scans": {h: len(r) if isinstance(r, list) else r.get("error")
                  for h, r in scan_results.items()},
        "stacks": per_stack,
    }

    # Step 3: send a single ntfy summary.
    try:
        notifier = _build_notifier_from_settings(settings)
        # Compact ntfy body
        lines = [f"homelab-mcp canary @ {summary['ts']}", ""]
        for s in per_stack:
            lines.append(
                f"  {s['host']}/{s['stack']}: {s['outcome']} "
                f"({s.get('risk', '?')})"
            )
        await notifier.notify(
            "\n".join(lines),
            title="homelab-mcp canary",
            tags=["robot", "homelab-mcp"],
            priority="default",
        )
    except Exception as e:
        log.warning("ntfy summary failed: %s", e)
        summary["notify_error"] = str(e)

    return summary


def main() -> int:
    if os.environ.get("HOMELAB_MCP_CANARY_CRON") != "1":
        sys.stderr.write(
            "ERROR: HOMELAB_MCP_CANARY_CRON is not set to '1'.\n"
            "Set the env var to acknowledge that this cron will "
            "auto-apply updates to the canary stacks.\n"
        )
        # Use sys.exit so the cron job records a non-zero exit
        # in the system log. argparse calls sys.exit(2) on parse
        # errors so this is consistent.
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/mnt/Data/appdata/dockge/stacks/homelab-mcp/.env"),
        help=(
            "Path to the homelab-mcp .env file. The Docker image's "
            "/data/.env is the state dir, not the env file (the env "
            "file is mounted into Dockge's stacks dir, not the state "
            "volume). On a typical TrueNAS deployment this is the "
            "DOCKGE_STACKS_ROOT/<stack-name>/.env path."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the summary as JSON to stdout (in addition to logs)",
    )
    args = parser.parse_args()
    os.environ["HOMELAB_MCP_ENV_FILE"] = str(args.config)

    summary = asyncio.run(_run_canary())
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
