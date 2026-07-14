"""Tests for the config module."""

from pathlib import Path

import pytest

from homelab_mcp.config import Settings


def test_settings_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply when no env vars are set."""
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    for var in [
        "HOMELAB_MCP_HOSTS", "HOMELAB_MCP_PORT", "HOMELAB_MCP_SSH_CONFIG",
        "HOMELAB_MCP_POLL_ENABLED", "HOMELAB_MCP_POLL_INTERVAL",
        "HOMELAB_MCP_KEYCLOAK_URL", "HOMELAB_MCP_KEYCLOAK_REALM",
        "HOMELAB_MCP_KEYCLOAK_AUDIENCE",
    ]:
        monkeypatch.delenv(var, raising=False)

    s = Settings()
    assert s.hosts == ["unraid"]
    assert s.port == 18790
    assert s.state_dir == tmp_path
    assert s.poll_enabled is True
    assert s.poll_interval == 21600
    assert s.ntfy_url == "https://ntfy.sh/"
    assert s.ntfy_topic == ""
    assert s.llm_endpoint == "http://localhost:11434/v1/chat/completions"
    assert s.llm_model == ""
    assert s.auto_apply_policy == "safe-and-caution"
    assert s.local_host_alias == "unraid"
    assert s.dockge_stacks_root == "/mnt/Data/appdata/dockge/stacks"


def test_settings_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars override defaults."""
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_MCP_HOSTS", '["unraid","truenas","qnap"]')
    monkeypatch.setenv("HOMELAB_MCP_PORT", "9999")
    monkeypatch.setenv("HOMELAB_MCP_NTFY_TOPIC", "homelab-test")
    monkeypatch.setenv("HOMELAB_MCP_LLM_MODEL", "llama3.1:8b")
    monkeypatch.setenv("HOMELAB_MCP_LOCAL_HOST_ALIAS", "truenas")
    monkeypatch.setenv("HOMELAB_MCP_AUTO_APPLY_POLICY", "safe-only")

    s = Settings()
    assert s.hosts == ["unraid", "truenas", "qnap"]
    assert s.port == 9999
    assert s.ntfy_topic == "homelab-test"
    assert s.llm_model == "llama3.1:8b"
    assert s.local_host_alias == "truenas"
    assert s.auto_apply_policy == "safe-only"


def test_settings_hosts_must_be_nonempty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty hosts list is rejected."""
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_MCP_HOSTS", "[]")
    with pytest.raises(ValueError, match="at least one host"):
        Settings()


def test_settings_port_must_be_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port outside 1-65535 is rejected."""
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_MCP_PORT", "99999")
    with pytest.raises(ValueError, match="port"):
        Settings()


def test_settings_creates_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state_dir is created on instantiation."""
    import tempfile
    p = Path(tempfile.mkdtemp(prefix="homelab-mcp-test-")) / "data"
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(p))
    monkeypatch.delenv("HOMELAB_MCP_HOSTS", raising=False)
    s = Settings()
    assert p.is_dir()
    assert s.state_dir == p


def test_settings_invalid_policy_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown auto_apply_policy is rejected at config time."""
    monkeypatch.setenv("HOMELAB_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_MCP_AUTO_APPLY_POLICY", "everything-and-the-kitchen-sink")
    with pytest.raises(ValueError, match="auto_apply_policy"):
        Settings()
