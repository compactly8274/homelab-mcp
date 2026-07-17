"""notifier_status_tool: surface the configured notifier backends.

The notifier stack is configured entirely via env vars:
- HOMELAB_MCP_NTFY_URL / NTFY_TOPIC / NTFY_PRIORITY
- HOMELAB_MCP_PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY / PUSHOVER_DEVICE
- HOMELAB_MCP_DISCORD_WEBHOOK_URL
- HOMELAB_MCP_WEBHOOK_URL (+ secret)

If a backend is configured but the URL/topic/etc is empty, the
notifier silently does nothing on every notify() call, and the
user has no signal that updates are happening (or not happening).

This tool answers two questions:
1. Which notifier backends are currently enabled? (a count + list)
2. If requested, actually POST a test message to each one so
   the user can verify the wire is up.

The diagnostic is read-only by default. test_notify=True
sends a single low-priority test message to every enabled
backend; it does NOT touch any container state.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)


def _configured_backends(settings: Settings) -> list[dict[str, Any]]:
    """Return one entry per backend that has the minimum config to work.

    A backend is 'configured' if its env vars were set in a way
    that wouldn't make notify() a silent no-op. We don't try to
    validate the URL is reachable — that's what test_notify is for.
    """
    out: list[dict[str, Any]] = []
    if settings.ntfy_topic:
        out.append({
            "backend": "ntfy",
            "url": settings.ntfy_url,
            "topic": settings.ntfy_topic,
            "priority": settings.ntfy_priority,
        })
    if settings.pushover_app_token and settings.pushover_user_key:
        out.append({
            "backend": "pushover",
            "device": settings.pushover_device or "(all devices)",
            "sound": settings.pushover_sound,
        })
    if settings.discord_webhook_url:
        out.append({"backend": "discord", "url_prefix": settings.discord_webhook_url[:60] + "..."})
    if settings.webhook_url:
        out.append({
            "backend": "webhook",
            "url_prefix": settings.webhook_url[:60] + "...",
            "has_secret": bool(settings.webhook_secret),
        })
    return out


def _missing_env_hints() -> list[str]:
    """Return a list of env var names that, if set, would enable
    the most common notifier backends. Used in the diagnostic
    output to help the user see what's NOT configured."""
    candidates = [
        ("HOMELAB_MCP_NTFY_TOPIC", "ntfy"),
        ("HOMELAB_MCP_PUSHOVER_APP_TOKEN", "Pushover"),
        ("HOMELAB_MCP_PUSHOVER_USER_KEY", "Pushover"),
        ("HOMELAB_MCP_DISCORD_WEBHOOK_URL", "Discord webhook"),
        ("HOMELAB_MCP_WEBHOOK_URL", "generic webhook"),
    ]
    return [var for var, _ in candidates if not os.environ.get(var)]


@mcp.tool()
async def notifier_status_tool(test_notify: bool = False) -> dict[str, Any]:
    """Return the notifier configuration status.

    Parameters
    ----------
    test_notify : bool, default False
        If True, also send a single test message to every configured
        backend. Use this to verify the wire is up after you set
        the env vars and restarted the container.

    Returns
    -------
    dict
        {
            "configured": [list of backends],
            "configured_count": int,
            "missing_env_hints": [list of unfilled env vars],
            "healthy": bool,  # True iff at least one backend is configured
            "test_results": [...]  # only present when test_notify=True
        }
    """
    settings = Settings()
    configured = _configured_backends(settings)
    out: dict[str, Any] = {
        "configured": configured,
        "configured_count": len(configured),
        "missing_env_hints": _missing_env_hints(),
        "healthy": bool(configured),
    }

    if test_notify and configured:
        # Build the same notifier stack the apply pipeline builds.
        # We re-import here to avoid loading the notifier module
        # until we actually need to send a test.
        from homelab_mcp.tools.apply_update import _build_notifier_from_settings
        notifier = _build_notifier_from_settings(settings)
        test_msg = (
            "homelab-mcp: notifier self-test. "
            "If you see this on your ntfy/Discord/Pushover, "
            "the wire is up."
        )
        try:
            await notifier.notify(
                test_msg,
                title="homelab-mcp notifier self-test",
                tags=["white_check_mark"],
                priority="low",
            )
            out["test_results"] = [
                {"backend": b["backend"], "ok": True}
                for b in configured
            ]
        except Exception as e:
            log.warning("notifier self-test failed: %s", e)
            out["test_results"] = [
                {"backend": b["backend"], "ok": False, "error": str(e)}
                for b in configured
            ]
    elif test_notify:
        out["test_results"] = []
        out["test_skipped_reason"] = (
            "test_notify=True but no backends are configured; "
            "the notifier is currently a silent no-op."
        )
    return out
