"""Tests for the auto-apply cron entry point."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homelab_mcp.auto_apply_main import (
    parse_args,
    run_one_cycle,
    summarize,
)

# -- parse_args ------------------------------------------------------------


def test_parse_args_dry_run() -> None:
    args = parse_args(["--dry-run"])
    assert args.dry_run is True
    assert args.host is None
    assert args.verbose is False


def test_parse_args_host_filter() -> None:
    args = parse_args(["--host", "unraid", "--per-row-timeout", "60"])
    assert args.host == "unraid"
    assert args.per_row_timeout == 60.0


def test_parse_args_verbose() -> None:
    args = parse_args(["--verbose", "--dry-run"])
    assert args.verbose is True
    assert args.dry_run is True


# -- run_one_cycle ---------------------------------------------------------


class _FakeHost:
    def __init__(self, name):
        self._name = name

    @property
    def name(self) -> str:
        return self._name


class _FakeState:
    def __init__(self, rows):
        self._rows = rows
        self.dismissed: list[tuple] = []

    async def list_pending_updates(self, host=None):
        if host is None:
            return list(self._rows)
        return [r for r in self._rows if r["host"] == host]

    async def mark_update_seen(self, host, stack, latest_digest):
        self.dismissed.append((host, stack, latest_digest))
        return 1


async def test_run_one_cycle_empty_db() -> None:
    """An empty DB yields an empty result list."""
    state = _FakeState([])
    hosts = {"unraid": _FakeHost("unraid")}
    out = await run_one_cycle(
        hosts=hosts, state=state,  # type: ignore[arg-type]
        dry_run=True, host_filter=None,
        per_row_timeout=10.0,
        fetch_release_notes=AsyncMock(),
        classify_release_notes=AsyncMock(),
        run_pipeline=AsyncMock(),
        notifier=MagicMock(notify=AsyncMock()),
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    assert out == []


async def test_run_one_cycle_host_filter() -> None:
    """With a host filter, only rows for that host are processed."""
    state = _FakeState([
        {"host": "unraid", "stack": "x1", "current_digest": "a", "latest_digest": "b"},
        {"host": "truenas", "stack": "x2", "current_digest": "c", "latest_digest": "d"},
    ])

    class _LocalHost:
        @property
        def name(self) -> str:
            return "unraid"

        async def inspect_container(self, name: str) -> dict:
            return {
                "Config": {
                    "Image": "img:latest",
                    "Labels": {"com.docker.compose.project": name},
                },
            }

    class _RemoteHost:
        @property
        def name(self) -> str:
            return "truenas"

        async def inspect_container(self, name: str) -> dict:
            return {
                "Config": {
                    "Image": "img:latest",
                    "Labels": {"com.docker.compose.project": name},
                },
            }

    hosts = {"unraid": _LocalHost(), "truenas": _RemoteHost()}
    out = await run_one_cycle(
        hosts=hosts, state=state,  # type: ignore[arg-type]
        dry_run=True, host_filter="unraid",
        per_row_timeout=10.0,
        fetch_release_notes=AsyncMock(return_value=None),
        classify_release_notes=AsyncMock(),
        run_pipeline=AsyncMock(),
        notifier=MagicMock(notify=AsyncMock()),
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    # Only x1 (unraid) was processed
    assert len(out) == 1
    assert out[0]["stack"] == "x1"


async def test_run_one_cycle_per_row_exception_isolation() -> None:
    """One row's exception does not stop the rest of the cycle."""
    state = _FakeState([
        {"host": "unraid", "stack": "x1", "current_digest": "a", "latest_digest": "b"},
        {"host": "unraid", "stack": "x2", "current_digest": "c", "latest_digest": "d"},
    ])

    class _LocalHost:
        @property
        def name(self) -> str:
            return "unraid"

        async def inspect_container(self, name: str) -> dict:
            return {
                "Config": {
                    "Image": "ghcr.io/owner/img:latest",
                    "Labels": {"com.docker.compose.project": name},
                },
                "RepoDigests": [],
            }

    hosts = {"unraid": _LocalHost()}

    call_count = [0]
    async def _pipeline(host, state, **kw):
        call_count[0] += 1
        # x1 has digest 'b'*64; x2 has 'd'*64
        if kw.get("to_digest", "").endswith("b" * 64):
            raise RuntimeError("simulated failure on x1")
        return {"ok": True, "action": "applied"}

    out = await run_one_cycle(
        hosts=hosts, state=state,  # type: ignore[arg-type]
        dry_run=False, host_filter=None,
        per_row_timeout=10.0,
        fetch_release_notes=AsyncMock(return_value=None),
        classify_release_notes=AsyncMock(),
        run_pipeline=_pipeline,
        notifier=MagicMock(notify=AsyncMock()),
        compose_manager_root="/srv/ca",
        dockge_stacks_root="/srv/dockge",
    )
    assert call_count[0] == 2
    assert len(out) == 2
    # x1 failed, x2 succeeded and was dismissed
    x2 = next(r for r in out if r["stack"] == "x2")
    assert x2["action"] == "applied"


# -- summarize -------------------------------------------------------------


def test_summarize_zero() -> None:
    out = summarize([])
    assert "0" in out


def test_summarize_mixed() -> None:
    rows = [
        {"action": "applied"},
        {"action": "applied"},
        {"action": "rolled_back"},
        {"action": "skipped_no_notes"},
        {"action": "notified_breaking"},
    ]
    out = summarize(rows)
    assert "applied=2" in out
    assert "rolled_back=1" in out
    assert "notified_breaking=1" in out
