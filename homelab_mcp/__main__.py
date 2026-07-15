"""``python -m homelab_mcp`` entry point.

Loads Settings, configures the FastMCP server's host/port, initializes
the state DB + host clients, and starts the visibility-cron
scheduler as a background task. Runs SSE transport. SIGINT/SIGTERM
trigger a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading

from homelab_mcp.config import Settings
from homelab_mcp.http_routes import run_sse_with_health
from homelab_mcp.server import build_hosts, get_state, init_hosts, mcp
from homelab_mcp.state import State
from homelab_mcp.updater.scheduler import ScanScheduler


async def _build_lifecycle():
    settings = Settings()
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = settings.port

    hosts = build_hosts(settings)
    state = State(db_path=settings.state_dir / "state.db")
    await state.init_db()
    init_hosts(hosts, state)

    print(
        f"[homelab-mcp] starting on 0.0.0.0:{settings.port} "
        f"(hosts={settings.hosts}, state_dir={settings.state_dir}, "
        f"scan_interval={settings.poll_interval}s, poll_enabled={settings.poll_enabled})",
        file=sys.stderr,
    )

    scheduler: ScanScheduler | None = None
    if settings.poll_enabled:
        scheduler = ScanScheduler(
            hosts=hosts, state=state, interval_seconds=settings.poll_interval,
            state_dir=settings.state_dir,
        )
        print(
            f"[homelab-mcp] visibility scheduler will start "
            f"(interval={settings.poll_interval}s, state_dir={settings.state_dir})",
            file=sys.stderr,
        )
    else:
        print(
            "[homelab-mcp] visibility scheduler disabled (HOMELAB_MCP_POLL_ENABLED=false)",
            file=sys.stderr,
        )
    return scheduler, hosts, state, settings


def main() -> int:
    """Sync entry point. Returns process exit code."""
    scheduler, _hosts, _state, settings = asyncio.new_event_loop().run_until_complete(_build_lifecycle())

    # Run the scheduler as a background task on the loop that mcp.run() creates.
    shutdown_started = threading.Event()

    def _shutdown(_signum, _frame):
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        print("[homelab-mcp] caught signal, shutting down", file=sys.stderr)
        if scheduler is not None:
            scheduler.stop()
        sys.exit(0)

    if threading.current_thread() is threading.main_thread:
        try:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except ValueError as e:
            print(f"[homelab-mcp] signal handlers not installed: {e}", file=sys.stderr)

    # Start the scheduler in the current event loop if there is one.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running() and scheduler is not None:
            task = loop.create_task(scheduler.run())
            _ = task
    except RuntimeError:
        pass

    try:
        run_sse_with_health(
            mcp, host="0.0.0.0", port=settings.port, get_state=get_state,
        )
    finally:
        if scheduler is not None:
            scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
