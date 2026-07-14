"""The visibility-cron scheduler.

Runs :func:`scan_host` for every configured host on a fixed interval
(default 6h). Populates the ``pending_updates`` table but does not
apply any updates. The auto-apply pipeline reads from that table.

Restart-aware: the last-scan time is persisted to a file
(``state_dir/last_scan.txt``) so after a server restart the scheduler
waits ``interval - elapsed`` before its first scan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from homelab_mcp.hosts.base import HostClient
from homelab_mcp.state import State
from homelab_mcp.updater.scanner import scan_host

log = logging.getLogger(__name__)


# Default scan interval. Six hours matches the documented plan.
DEFAULT_INTERVAL_S = 6 * 60 * 60


class ScanScheduler:
    """Background scheduler that scans all hosts on a fixed interval."""

    def __init__(
        self,
        hosts: dict[str, HostClient],
        state: State,
        interval_seconds: int = DEFAULT_INTERVAL_S,
        state_dir: Path | None = None,
        run_immediately: bool = True,
    ) -> None:
        self.hosts = hosts
        self.state = state
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._state_dir = state_dir
        self._run_immediately = run_immediately

    def stop(self) -> None:
        self._stop.set()

    def _last_scan_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return Path(self._state_dir) / "last_scan.txt"

    def _read_last_scan(self) -> float | None:
        p = self._last_scan_path()
        if p is None or not p.exists():
            return None
        try:
            return float(p.read_text().strip())
        except (ValueError, OSError):
            return None

    def _write_last_scan(self, t: float) -> None:
        p = self._last_scan_path()
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(t))
        except OSError as e:
            log.warning("could not write last_scan.txt: %s", e)

    async def run(self) -> None:
        """Run the scan loop until stop() is called.

        On first run after a restart, waits for ``max(0, interval -
        elapsed)`` so the schedule isn't reset by uptime.
        """
        first_delay = 0
        if not self._run_immediately:
            first_delay = self.interval_seconds
        else:
            last = self._read_last_scan()
            if last is not None:
                elapsed = time.time() - last
                first_delay = max(0, int(self.interval_seconds - elapsed))
                if first_delay > 0:
                    log.info(
                        "scheduler: %ds since last scan, waiting %ds before first scan",
                        int(elapsed), first_delay,
                    )
        if first_delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=first_delay)
                return
            except TimeoutError:
                pass

        while not self._stop.is_set():
            try:
                await self._scan_all()
                self._write_last_scan(time.time())
            except Exception as e:
                log.exception("scan_all failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                return
            except TimeoutError:
                pass

    async def _scan_all(self) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        for name, host in self.hosts.items():
            try:
                rows = await scan_host(host, self.state)
                all_rows.extend(rows)
                if rows:
                    log.info("drift detected on %s: %d stacks", name, len(rows))
            except Exception as e:
                log.exception("scan_host failed for %s: %s", name, e)
        return all_rows
