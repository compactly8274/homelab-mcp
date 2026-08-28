"""Tests for the image-drift scanner."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from homelab_mcp.state import State
from homelab_mcp.updater.registry import RegistryResult
from homelab_mcp.updater.scanner import _local_digest_from_inspect, scan_host

# -- _local_digest_from_inspect --------------------------------------------


def test_local_digest_prefers_repodiests() -> None:
    """RepoDigests manifest digest wins over Image config digest."""
    inspect = {
        "RepoDigests": ["ghcr.io/owner/img@sha256:" + "a" * 64],
        "Image": "sha256:" + "b" * 64,
    }
    assert _local_digest_from_inspect(inspect) == "sha256:" + "a" * 64


def test_local_digest_falls_back_to_image() -> None:
    """When RepoDigests is empty, Image (config digest) is used."""
    inspect = {
        "RepoDigests": [],
        "Image": "sha256:" + "b" * 64,
    }
    assert _local_digest_from_inspect(inspect) == "sha256:" + "b" * 64


def test_local_digest_returns_none_when_both_missing() -> None:
    """A fresh container with no digest at all returns None."""
    assert _local_digest_from_inspect({}) is None
    assert _local_digest_from_inspect({"Image": "not-a-sha256"}) is None


# -- scan_host: happy path --------------------------------------------------


class _FakeHost:
    def __init__(self, *, name: str, containers: list, inspect_data: dict):
        self._name = name
        self._containers = containers
        self._inspect_data = inspect_data

    @property
    def name(self) -> str:
        return self._name

    async def list_containers(self, all: bool = True) -> list[dict[str, Any]]:
        return self._containers

    async def inspect_container(self, name: str) -> dict[str, Any]:
        return self._inspect_data

    async def run_command(self, cmd: str, timeout: float = 30.0) -> Any:
        # Pretend every compose-file candidate exists so project-keyed
        # stacks are not silently skipped.
        class _Result:
            ok = True
            stdout = "found"
            stderr = ""
            returncode = 0
        return _Result()


async def test_scan_host_records_drift_to_state(tmp_path: Path) -> None:
    """A drifted container is recorded as a pending_update."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        name="unraid",
        containers=[{"NAME": "radarr", "IMAGE": "ghcr.io/owner/img:latest", "STATE": "running", "PROJECT": "radarr"}],
        inspect_data={
            "RepoDigests": ["ghcr.io/owner/img@sha256:" + "a" * 64],
            "Image": "sha256:" + "a" * 64,
        },
    )

    remote_digest = "sha256:" + "b" * 64
    with patch(
        "homelab_mcp.updater.scanner.fetch_remote_digest",
        AsyncMockReturn(RegistryResult(kind="ok", digest=remote_digest)),
    ):
        # Replace with an async function
        async def _fake(*args, **kwargs):
            return RegistryResult(kind="ok", digest=remote_digest)
        with patch("homelab_mcp.updater.scanner.fetch_remote_digest", _fake):
            rows = await scan_host(host, state)

    assert len(rows) == 1
    assert rows[0]["container"] == "radarr"
    assert rows[0]["remote_digest"] == remote_digest

    pending = await state.list_pending_updates()
    assert len(pending) == 1


async def test_scan_host_skips_when_digests_match(tmp_path: Path) -> None:
    """No drift, no row written."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        name="unraid",
        containers=[{"NAME": "sonarr", "IMAGE": "ghcr.io/owner/img:latest", "STATE": "running", "PROJECT": "sonarr"}],
        inspect_data={
            "RepoDigests": ["ghcr.io/owner/img@sha256:" + "a" * 64],
            "Image": "sha256:" + "a" * 64,
        },
    )

    async def _fake(*args, **kwargs):
        return RegistryResult(kind="ok", digest="sha256:" + "a" * 64)
    with patch("homelab_mcp.updater.scanner.fetch_remote_digest", _fake):
        rows = await scan_host(host, state)

    assert rows == []
    assert await state.list_pending_updates() == []


async def test_scan_host_skips_transient_registry_errors(tmp_path: Path) -> None:
    """Transient errors don't record a pending_update (the next scan will retry)."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        name="unraid",
        containers=[{"NAME": "qbittorrent", "IMAGE": "x:latest", "STATE": "running", "PROJECT": "qbittorrent"}],
        inspect_data={"RepoDigests": ["x@sha256:" + "a" * 64]},
    )

    async def _fake(*args, **kwargs):
        return RegistryResult(kind="transient_error", detail="timeout")
    with patch("homelab_mcp.updater.scanner.fetch_remote_digest", _fake):
        rows = await scan_host(host, state)

    assert rows == []
    assert await state.list_pending_updates() == []


async def test_scan_host_skips_not_found_registry(tmp_path: Path) -> None:
    """An image deleted upstream is not flagged as drift."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        name="unraid",
        containers=[{"NAME": "x", "IMAGE": "missing:latest", "STATE": "running", "PROJECT": "x"}],
        inspect_data={"RepoDigests": ["missing@sha256:" + "a" * 64]},
    )

    async def _fake(*args, **kwargs):
        return RegistryResult(kind="not_found")
    with patch("homelab_mcp.updater.scanner.fetch_remote_digest", _fake):
        rows = await scan_host(host, state)

    assert rows == []


async def test_scan_host_ignores_non_running_containers(tmp_path: Path) -> None:
    """Stopped containers are not scanned."""
    state = State(db_path=tmp_path / "state.db")
    await state.init_db()
    host = _FakeHost(
        name="unraid",
        containers=[{"NAME": "x", "IMAGE": "x:latest", "STATE": "exited", "PROJECT": "x"}],
        inspect_data={"RepoDigests": []},
    )
    async def _fake(*args, **kwargs):
        return RegistryResult(kind="ok", digest="sha256:abc")
    with patch("homelab_mcp.updater.scanner.fetch_remote_digest", _fake):
        rows = await scan_host(host, state)
    assert rows == []


# -- AsyncMockReturn helper ------------------------------------------------


class AsyncMockReturn:
    """Async replacement for a patched function (returns a fixed value)."""

    def __init__(self, value: Any):
        self._value = value

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._value
