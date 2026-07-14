"""Configuration for the homelab-mcp server.

Loaded from environment variables. All settings are optional except
those that are explicitly validated to be non-empty (e.g. ``hosts``).
A ``.env`` file in the current working directory or ``$HOME`` is
loaded via :mod:`python_dotenv`.

Env-var reference (all optional unless noted):

- ``HOMELAB_MCP_HOSTS``              JSON list (default ``["unraid"]``)
- ``HOMELAB_MCP_PORT``               int 1-65535 (default 18790)
- ``HOMELAB_MCP_STATE_DIR``          path (default ``$XDG_STATE_HOME/homelab-mcp``)
- ``HOMELAB_MCP_SSH_CONFIG``         path (default ``~/.ssh/config``)
- ``HOMELAB_MCP_POLL_ENABLED``       bool (default true)
- ``HOMELAB_MCP_POLL_INTERVAL``      int seconds (default 21600 = 6h)
- ``HOMELAB_MCP_NTFY_URL``           ntfy base URL (default ``https://ntfy.sh/``)
- ``HOMELAB_MCP_NTFY_TOPIC``         ntfy topic (default empty; required for alerts)
- ``HOMELAB_MCP_NTFY_PRIORITY``      default ntfy priority (default ``default``)
- ``HOMELAB_MCP_LLM_ENDPOINT``       OpenAI-compatible chat-completions URL
- ``HOMELAB_MCP_LLM_API_KEY``        Bearer token (default empty; sent if set)
- ``HOMELAB_MCP_LLM_MODEL``          model name (default empty)
- ``HOMELAB_MCP_LLM_TIMEOUT``        seconds (default 30)
- ``HOMELAB_MCP_AUTO_APPLY_POLICY``  ``safe-and-caution`` (default) or ``safe-only``
- ``HOMELAB_MCP_LOCAL_HOST_ALIAS``   which alias is the local docker socket
                                      (default ``unraid``; set to ``truenas``
                                      when deploying the daemon to TrueNAS)
- ``HOMELAB_MCP_DOCKGE_STACKS_ROOT`` Dockge stack root (default
                                      ``/mnt/Data/appdata/dockge/stacks``)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


def _reload_dotenv() -> None:
    """Load .env from cwd and $HOME. Idempotent."""
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    load_dotenv(dotenv_path=Path(os.getenv("HOME", "~")) / ".env", override=False)


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{name} must be a JSON list, got {raw!r}")
    return [str(x) for x in parsed]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


def _default_state_dir() -> Path:
    xdg = os.getenv("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "homelab-mcp"
    return Path("~/.local/share/homelab-mcp").expanduser()


def _default_ssh_config() -> Path:
    return Path(os.getenv("HOME", "~")) / ".ssh" / "config"


class Settings(BaseModel):
    """MCP server settings, populated from environment variables."""

    hosts: list[str] = Field(default_factory=list)
    port: int = 0  # sentinel; replaced in model_validator
    state_dir: Path = Field(default_factory=Path)
    ssh_config: Path = Field(default_factory=Path)
    poll_enabled: bool = True
    poll_interval: int = 21600

    # ntfy notifier
    ntfy_url: str = "https://ntfy.sh/"
    ntfy_topic: str = ""
    ntfy_priority: str = "default"

    # LLM risk classifier
    llm_endpoint: str = "http://localhost:11434/v1/chat/completions"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 30.0

    # Auto-apply policy
    auto_apply_policy: str = "safe-and-caution"
    local_host_alias: str = "unraid"
    dockge_stacks_root: str = "/mnt/Data/appdata/dockge/stacks"

    @field_validator("hosts")
    @classmethod
    def _hosts_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one host required (HOMELAB_MCP_HOSTS)")
        cleaned = [h.strip() for h in v if h and h.strip()]
        if not cleaned:
            raise ValueError("at least one host required (HOMELAB_MCP_HOSTS)")
        return cleaned

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"port must be 1-65535, got {v}")
        return v

    @field_validator("auto_apply_policy")
    @classmethod
    def _policy_valid(cls, v: str) -> str:
        if v not in ("safe-and-caution", "safe-only"):
            raise ValueError(
                f"auto_apply_policy must be 'safe-and-caution' or 'safe-only', got {v!r}"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _from_env(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        _reload_dotenv()
        if "hosts" not in data:
            data["hosts"] = _env_list("HOMELAB_MCP_HOSTS", ["unraid"])
        if "port" not in data:
            data["port"] = _env_int("HOMELAB_MCP_PORT", 18790)
        if "state_dir" not in data:
            data["state_dir"] = Path(os.getenv("HOMELAB_MCP_STATE_DIR") or str(_default_state_dir()))
        if "ssh_config" not in data:
            data["ssh_config"] = Path(
                os.getenv("HOMELAB_MCP_SSH_CONFIG") or str(_default_ssh_config())
            )
        if "poll_enabled" not in data:
            data["poll_enabled"] = _env_bool("HOMELAB_MCP_POLL_ENABLED", True)
        if "poll_interval" not in data:
            data["poll_interval"] = _env_int("HOMELAB_MCP_POLL_INTERVAL", 21600)
        if "ntfy_url" not in data:
            data["ntfy_url"] = os.getenv("HOMELAB_MCP_NTFY_URL", "https://ntfy.sh/")
        if "ntfy_topic" not in data:
            data["ntfy_topic"] = os.getenv("HOMELAB_MCP_NTFY_TOPIC", "")
        if "ntfy_priority" not in data:
            data["ntfy_priority"] = os.getenv("HOMELAB_MCP_NTFY_PRIORITY", "default")
        if "llm_endpoint" not in data:
            data["llm_endpoint"] = os.getenv(
                "HOMELAB_MCP_LLM_ENDPOINT",
                "http://localhost:11434/v1/chat/completions",
            )
        if "llm_api_key" not in data:
            data["llm_api_key"] = os.getenv("HOMELAB_MCP_LLM_API_KEY", "")
        if "llm_model" not in data:
            data["llm_model"] = os.getenv("HOMELAB_MCP_LLM_MODEL", "")
        if "llm_timeout" not in data:
            data["llm_timeout"] = float(os.getenv("HOMELAB_MCP_LLM_TIMEOUT", "30"))
        if "auto_apply_policy" not in data:
            data["auto_apply_policy"] = os.getenv(
                "HOMELAB_MCP_AUTO_APPLY_POLICY", "safe-and-caution"
            )
        if "local_host_alias" not in data:
            data["local_host_alias"] = os.getenv(
                "HOMELAB_MCP_LOCAL_HOST_ALIAS", "unraid"
            )
        if "dockge_stacks_root" not in data:
            data["dockge_stacks_root"] = os.getenv(
                "HOMELAB_MCP_DOCKGE_STACKS_ROOT", "/mnt/Data/appdata/dockge/stacks"
            )
        return data

    @model_validator(mode="after")
    def _create_state_dir(self) -> "Settings":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self
