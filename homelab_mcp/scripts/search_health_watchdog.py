#!/usr/bin/env python3
"""Search + metrics health watchdog for homelab-mcp.

Checks every 5 minutes (when invoked from cron):
  1. homelab-mcp daemon health endpoint
  2. SearXNG health endpoint
  3. quick_search round-trips through Tavily/SearXNG
  4. container_metrics_tool round-trips for a known container

Notifies via ntfy on any failure.

Usage:
    HOMELAB_MCP_SEARCH_HEALTH_WATCHDOG=1 \
        python -m homelab_mcp.scripts.search_health_watchdog \
        --config /mnt/Data/appdata/dockge/stacks/homelab-mcp/.env

Cron (TrueNAS host, root):
    */5 * * * * cd /mnt/Data/appdata/homelab-mcp/src && \
        HOMELAB_MCP_SEARCH_HEALTH_WATCHDOG=1 \
        /usr/local/bin/python3 -m homelab_mcp.scripts.search_health_watchdog
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("homelab_mcp.search_health_watchdog")

SEARXNG_URL = os.environ.get("HOMELAB_MCP_SEARXNG_URL", "http://192.168.1.7:8080")
MCP_URL = os.environ.get("HOMELAB_MCP_URL", "http://127.0.0.1:18790")
# Container to sample for metrics (local docker on truenas)
METRICS_TARGET = ("truenas", "homelab-mcp")


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _setup_settings(env: dict[str, str]) -> Any:
    for k, v in env.items():
        if k.startswith("HOMELAB_MCP_") and v:
            os.environ.setdefault(k, v)
    from homelab_mcp.config import Settings
    return Settings()


def _http_get_ok(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status == 200, f"status={resp.status} body={body[:80]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _run_watchdog() -> dict[str, Any]:
    from homelab_mcp import server
    from homelab_mcp import state as state_mod
    from homelab_mcp.tools.apply_update import _build_notifier_from_settings
    from homelab_mcp.tools.container_metrics import container_metrics_tool
    from homelab_mcp.tools.searxng import quick_search as quick_search_tool

    env_path = Path(os.environ.get(
        "HOMELAB_MCP_ENV_FILE",
        "/mnt/Data/appdata/dockge/stacks/homelab-mcp/.env",
    ))
    env = _load_env(env_path)
    settings = _setup_settings(env)

    db_path = Path(os.environ.get("HOMELAB_MCP_STATE_DIR", "/data")) / "state.db"
    st = state_mod.State(db_path=str(db_path))
    await st.init_db()
    server._state = st
    server.init_hosts(server.build_hosts(settings), st)

    checks: list[dict[str, Any]] = []

    # 1. MCP daemon health
    ok, detail = _http_get_ok(f"{MCP_URL}/health", timeout=5.0)
    checks.append({"name": "mcp_health", "ok": ok, "detail": detail})

    # 2. SearXNG health
    ok, detail = _http_get_ok(f"{SEARXNG_URL}/healthz", timeout=10.0)
    checks.append({"name": "searxng_health", "ok": ok, "detail": detail})

    # 3. Search round-trip (Tavily primary; if no key, SearXNG fallback)
    try:
        search_result = await quick_search_tool(
            query="homelab docker monitoring best practices",
            limit=3,
        )
        search_ok = "error" not in search_result and len(search_result.get("results", [])) > 0
        checks.append({
            "name": "quick_search",
            "ok": search_ok,
            "detail": {
                "source": search_result.get("source"),
                "count": len(search_result.get("results", [])),
                "error": search_result.get("error"),
            },
        })
    except Exception as e:
        checks.append({"name": "quick_search", "ok": False, "detail": f"{type(e).__name__}: {e}"})


    # 5. SearXNG fallback round-trip (explicit engines, independent of Tavily)
    try:
        from homelab_mcp.tools.searxng import searxng_search
        searx_result = await searxng_search(
            query="homelab docker monitoring best practices",
            engines="bing,yandex",
            language="en",
            limit=5,
        )
        searx_ok = "error" not in searx_result and len(searx_result.get("results", [])) > 0
        checks.append({
            "name": "searxng_fallback",
            "ok": searx_ok,
            "detail": {
                "count": len(searx_result.get("results", [])),
                "error": searx_result.get("error"),
            },
        })
    except Exception as e:
        checks.append({"name": "searxng_fallback", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    # 4. Container metrics round-trip
    host, container = METRICS_TARGET
    try:
        metrics = await container_metrics_tool(host=host, container=container, sample_seconds=1.0)
        checks.append({
            "name": "container_metrics",
            "ok": metrics.get("ok", False),
            "detail": metrics.get("error") or f"got {len(metrics.get('metrics', {}))} entries",
        })
    except Exception as e:
        checks.append({"name": "container_metrics", "ok": False, "detail": f"{type(e).__name__}: {e}"})

    failed = [c for c in checks if not c["ok"]]
    summary = {
        "ok": len(failed) == 0,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
    }

    # Notify only on failure (or optionally always with a heartbeat).
    try:
        notifier = _build_notifier_from_settings(settings)
        if failed:
            lines = [f"search/metrics watchdog @ {summary['ts']}", ""]
            for c in failed:
                lines.append(f"  FAIL: {c['name']} -> {c['detail']}")
            await notifier.notify(
                "\n".join(lines),
                title="homelab-mcp search/metrics alert",
                tags=["rotating_light", "homelab-mcp"],
                priority="high",
            )
        else:
            # heartbeat every run would be noisy; send a daily OK summary?
            # For now, no OK notification to avoid spam.
            pass
    except Exception as e:
        log.warning("ntfy notify failed: %s", e)
        summary["notify_error"] = str(e)

    return summary


def main() -> int:
    if os.environ.get("HOMELAB_MCP_SEARCH_HEALTH_WATCHDOG") != "1":
        sys.stderr.write(
            "ERROR: HOMELAB_MCP_SEARCH_HEALTH_WATCHDOG is not set to '1'.\n"
        )
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Search/metrics health watchdog")
    parser.add_argument("--config", type=Path, default=None, help="Path to .env file")
    args = parser.parse_args()
    if args.config:
        os.environ["HOMELAB_MCP_ENV_FILE"] = str(args.config)

    try:
        summary = asyncio.run(_run_watchdog())
    except Exception as e:
        log.exception("watchdog run failed")
        sys.stderr.write(f"watchdog run failed: {e}\n")
        return 1

    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
