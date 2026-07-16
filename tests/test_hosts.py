"""Tests for the host backends (LocalDocker + RemoteSSH)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch as _patch

import pytest

from homelab_mcp.hosts.base import CommandResult
from homelab_mcp.hosts.local_docker import LocalDocker
from homelab_mcp.hosts.remote_ssh import RemoteSSH

# -- LocalDocker ------------------------------------------------------------


def test_local_docker_requires_name() -> None:
    """An empty name is rejected."""
    with pytest.raises(ValueError, match="non-empty name"):
        LocalDocker(name="")


def test_local_docker_lazy_connection() -> None:
    """The docker client is created on first use, not at construction.

    We don't actually connect in this test (no docker socket in the
    sandbox); we just verify the constructor doesn't ping the socket.
    """
    h = LocalDocker(name="unraid")
    assert h._client is None  # constructor does not connect
    assert h.name == "unraid"


# -- RemoteSSH --------------------------------------------------------------


def test_remote_ssh_requires_name_alias_config() -> None:
    """Empty name/alias/config are all rejected."""
    with pytest.raises(ValueError, match="non-empty name"):
        RemoteSSH(name="", ssh_alias="x", ssh_config_path="/dev/null")
    with pytest.raises(ValueError, match="ssh_alias"):
        RemoteSSH(name="x", ssh_alias="", ssh_config_path="/dev/null")
    with pytest.raises(ValueError, match="ssh_config_path"):
        RemoteSSH(name="x", ssh_alias="x", ssh_config_path="")


def test_remote_ssh_verifies_config_path_when_enabled() -> None:
    """verify_config=True raises FileNotFoundError on a missing config."""
    with pytest.raises(FileNotFoundError, match="ssh config not found"):
        RemoteSSH(name="x", ssh_alias="x", ssh_config_path="/nonexistent/config")


def test_remote_ssh_skips_config_verification_when_disabled() -> None:
    """verify_config=False lets a missing config pass at construction."""
    h = RemoteSSH(
        name="x", ssh_alias="x",
        ssh_config_path="/nonexistent/config",
        verify_config=False,
    )
    assert h.name == "x"


def test_remote_ssh_name_property() -> None:
    """The .name property returns the configured alias."""
    h = RemoteSSH(
        name="truenas", ssh_alias="truenas",
        ssh_config_path="/dev/null", verify_config=False,
    )
    assert h.name == "truenas"


# -- CommandResult ---------------------------------------------------------


def test_command_result_ok_when_exit_zero() -> None:
    r = CommandResult(exit_code=0, stdout="ok", stderr="", duration_ms=10)
    assert r.ok is True


def test_command_result_not_ok_when_nonzero() -> None:
    r = CommandResult(exit_code=1, stdout="", stderr="fail", duration_ms=10)
    assert r.ok is False


# -- HostClient protocol ----------------------------------------------------


def test_local_docker_satisfies_hostclient_protocol() -> None:
    """LocalDocker is structurally a HostClient."""
    h = LocalDocker(name="x")
    from homelab_mcp.hosts.base import HostClient
    # Protocol's @runtime_checkable does structural checking
    assert isinstance(h, HostClient)


def test_remote_ssh_connect_uses_asyncssh_config_kwarg() -> None:
    """Regression: asyncssh>=2.18 moved config_path off connect(); we use config=[...].

    Pins the connect-call shape so a future asyncssh upgrade (or accidental
    re-introduction of `config_path=`) is caught.
    """
    captured: dict = {}

    class _FakeConn:
        is_closed = False

        def close(self) -> None:
            self.is_closed = True

        async def wait_closed(self) -> None:
            return None

    async def _fake_connect(host, *args, **kwargs):
        captured["host"] = host
        captured["kwargs"] = kwargs
        return _FakeConn()

    h = RemoteSSH(
        name="unraid", ssh_alias="unraid",
        ssh_config_path="/dev/null", verify_config=False,
    )
    with _patch("homelab_mcp.hosts.remote_ssh.asyncssh.connect", _fake_connect):
        asyncio.run(h._connect())  # type: ignore[attr-defined]

    assert captured["host"] == "unraid"
    assert "config" in captured["kwargs"], (
        f"asyncssh.connect must be called with config=; got kwargs={captured['kwargs']}"
    )
    assert captured["kwargs"]["config"] == ["/dev/null"]
    assert "config_path" not in captured["kwargs"], (
        "config_path= is unsupported by asyncssh>=2.18; use config=[...]"
    )


def test_remote_ssh_satisfies_hostclient_protocol() -> None:
    """RemoteSSH is structurally a HostClient."""
    h = RemoteSSH(name="x", ssh_alias="x", ssh_config_path="/dev/null", verify_config=False)
    from homelab_mcp.hosts.base import HostClient
    assert isinstance(h, HostClient)
