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
- ``HOMELAB_MCP_DISCORD_WEBHOOK_URL`` Discord webhook URL (default empty;
                                       enabled when set; see Discord
                                       channel settings → Integrations
                                       → Webhooks)
- ``HOMELAB_MCP_DISCORD_USERNAME``    Username for the webhook bot
                                       (default ``homelab-mcp``)
- ``HOMELAB_MCP_PUSHOVER_APP_TOKEN``  Pushover app token (default empty;
                                       enabled when set; create at
                                       https://pushover.net/apps/build)
- ``HOMELAB_MCP_PUSHOVER_USER_KEY``   Pushover user/group key (default empty)
- ``HOMELAB_MCP_PUSHOVER_DEVICE``     Pushover target device name (optional;
                                       if unset, all user's devices)
- ``HOMELAB_MCP_PUSHOVER_SOUND``     Pushover sound name (default ``pushover``)
- ``HOMELAB_MCP_SEARXNG_URL``        SearXNG base URL (default
                                      ``http://192.168.1.7:8080``; used by
                                      the searxng_* tools)
- ``HOMELAB_MCP_OLLAMA_URL``         Ollama base URL (default
                                     ``http://192.168.1.104:11434``; used by
                                     the ollama_* tools)
- ``HOMELAB_MCP_OLLAMA_ALLOW_PULL``   bool (default ``false``). When ``true``,
                                     enables the ``ollama_pull_model`` write
                                     tool. **LLMs can pull arbitrary models
                                     (multi-GB downloads)** — keep off unless
                                     you've audited the deployment.
- ``HOMELAB_MCP_OLLAMA_ALLOW_DELETE`` bool (default ``false``). When ``true``,
                                     enables the ``ollama_delete_model`` write
                                     tool. **LLMs can delete downloaded
                                     models** — keep off unless you've audited
                                     the deployment.
- ``HOMELAB_MCP_PLEX_URL``           Plex base URL (default
                                      ``http://192.168.1.104:32400``)
- ``HOMELAB_MCP_PLEX_TOKEN``         Plex API token (X-Plex-Token); see
                                      https://support.plex.tv/articles/204059436
- ``HOMELAB_MCP_IMMICH_URL``         Immich base URL (default
                                      ``http://192.168.1.104:2283``)
- ``HOMELAB_MCP_IMMICH_API_KEY``     Immich API key (Settings -> API Keys)
- ``HOMELAB_MCP_SONARR_URL``         Sonarr base URL (default
                                      ``http://192.168.1.104:8989``)
- ``HOMELAB_MCP_SONARR_API_KEY``     Sonarr API key (Settings -> General)
- ``HOMELAB_MCP_RADARR_URL``         Radarr base URL (default
                                      ``http://192.168.1.104:7878``)
- ``HOMELAB_MCP_RADARR_API_KEY``     Radarr API key
- ``HOMELAB_MCP_LIDARR_URL``         Lidarr base URL (default
                                      ``http://192.168.1.104:8686``)
- ``HOMELAB_MCP_LIDARR_API_KEY``     Lidarr API key
- ``HOMELAB_MCP_READARR_URL``        Readarr base URL (default
                                      ``http://192.168.1.104:8787``)
- ``HOMELAB_MCP_READARR_API_KEY``    Readarr API key
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

    # Discord notifier
    discord_webhook_url: str = ""
    discord_username: str = "homelab-mcp"

    # Pushover notifier
    pushover_app_token: str = ""
    pushover_user_key: str = ""
    pushover_device: str = ""
    pushover_sound: str = "pushover"

    # Service integrations
    searxng_url: str = "http://192.168.1.7:8080"
    ollama_url: str = "http://192.168.1.104:11434"
    ollama_allow_pull: bool = False
    ollama_allow_delete: bool = False
    plex_url: str = "http://192.168.1.104:32400"
    plex_token: str = ""
    immich_url: str = "http://192.168.1.104:2283"
    immich_api_key: str = ""
    sonarr_url: str = "http://192.168.1.104:8989"
    sonarr_api_key: str = ""
    radarr_url: str = "http://192.168.1.104:7878"
    radarr_api_key: str = ""
    lidarr_url: str = "http://192.168.1.104:8686"
    lidarr_api_key: str = ""
    readarr_url: str = "http://192.168.1.104:8787"
    readarr_api_key: str = ""

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
        if "discord_webhook_url" not in data:
            data["discord_webhook_url"] = os.getenv("HOMELAB_MCP_DISCORD_WEBHOOK_URL", "")
        if "discord_username" not in data:
            data["discord_username"] = os.getenv("HOMELAB_MCP_DISCORD_USERNAME", "homelab-mcp")
        if "pushover_app_token" not in data:
            data["pushover_app_token"] = os.getenv("HOMELAB_MCP_PUSHOVER_APP_TOKEN", "")
        if "pushover_user_key" not in data:
            data["pushover_user_key"] = os.getenv("HOMELAB_MCP_PUSHOVER_USER_KEY", "")
        if "pushover_device" not in data:
            data["pushover_device"] = os.getenv("HOMELAB_MCP_PUSHOVER_DEVICE", "")
        if "pushover_sound" not in data:
            data["pushover_sound"] = os.getenv("HOMELAB_MCP_PUSHOVER_SOUND", "pushover")
        # Service integrations
        if "searxng_url" not in data:
            data["searxng_url"] = os.getenv("HOMELAB_MCP_SEARXNG_URL", "http://192.168.1.7:8080")
        if "ollama_url" not in data:
            data["ollama_url"] = os.getenv("HOMELAB_MCP_OLLAMA_URL", "http://192.168.1.104:11434")
        if "ollama_allow_pull" not in data:
            data["ollama_allow_pull"] = _env_bool("HOMELAB_MCP_OLLAMA_ALLOW_PULL", False)
        if "ollama_allow_delete" not in data:
            data["ollama_allow_delete"] = _env_bool("HOMELAB_MCP_OLLAMA_ALLOW_DELETE", False)
        if "plex_url" not in data:
            data["plex_url"] = os.getenv("HOMELAB_MCP_PLEX_URL", "http://192.168.1.104:32400")
        if "plex_token" not in data:
            data["plex_token"] = os.getenv("HOMELAB_MCP_PLEX_TOKEN", "")
        if "immich_url" not in data:
            data["immich_url"] = os.getenv("HOMELAB_MCP_IMMICH_URL", "http://192.168.1.104:2283")
        if "immich_api_key" not in data:
            data["immich_api_key"] = os.getenv("HOMELAB_MCP_IMMICH_API_KEY", "")
        if "sonarr_url" not in data:
            data["sonarr_url"] = os.getenv("HOMELAB_MCP_SONARR_URL", "http://192.168.1.104:8989")
        if "sonarr_api_key" not in data:
            data["sonarr_api_key"] = os.getenv("HOMELAB_MCP_SONARR_API_KEY", "")
        if "radarr_url" not in data:
            data["radarr_url"] = os.getenv("HOMELAB_MCP_RADARR_URL", "http://192.168.1.104:7878")
        if "radarr_api_key" not in data:
            data["radarr_api_key"] = os.getenv("HOMELAB_MCP_RADARR_API_KEY", "")
        if "lidarr_url" not in data:
            data["lidarr_url"] = os.getenv("HOMELAB_MCP_LIDARR_URL", "http://192.168.1.104:8686")
        if "lidarr_api_key" not in data:
            data["lidarr_api_key"] = os.getenv("HOMELAB_MCP_LIDARR_API_KEY", "")
        if "readarr_url" not in data:
            data["readarr_url"] = os.getenv("HOMELAB_MCP_READARR_URL", "http://192.168.1.104:8787")
        if "readarr_api_key" not in data:
            data["readarr_api_key"] = os.getenv("HOMELAB_MCP_READARR_API_KEY", "")
        return data

    @model_validator(mode="after")
    def _create_state_dir(self) -> Settings:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self
