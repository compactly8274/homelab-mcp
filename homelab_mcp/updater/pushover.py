"""Pushover notifier.

Pushover (pushover.net) is a simple HTTP POST to their API:

  POST https://api.pushover.net/1/messages.json
  body: token=<app_token>&user=<user_key>&message=<text>&title=<title>&priority=<n>

Required env vars:
  HOMELAB_MCP_PUSHOVER_APP_TOKEN
  HOMELAB_MCP_PUSHOVER_USER_KEY

Optional:
  HOMELAB_MCP_PUSHOVER_DEVICE  (target a specific device name; if unset, all user's devices)
  HOMELAB_MCP_PUSHOVER_SOUND  (alert sound; default 'pushover')

Priority values per Pushover docs:
  -2 = lowest, -1 = low, 0 = normal, 1 = high, 2 = emergency (requires retry/expire)

We map our `priority` (which is one of the Notifier protocol's values)
to Pushover's numeric priorities:
  high/urgent → 1
  default    → 0
  low        → -1
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Pushover numeric priority mapping
PUSHOVER_PRIORITY_MAP: dict[str, int] = {
    "low":     -1,
    "min":     -2,
    "default": 0,
    "high":    1,
    "urgent":  2,
}


class PushoverNotifier:
    """Post notifications to Pushover."""

    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(
        self,
        app_token: str,
        user_key: str,
        *,
        device: str | None = None,
        sound: str = "pushover",
        timeout: float = 10.0,
    ) -> None:
        self.app_token = app_token.strip()
        self.user_key = user_key.strip()
        self.device = device.strip() if device else None
        self.sound = sound
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.app_token) and bool(self.user_key)

    @staticmethod
    def _map_priority(value: str | None) -> int:
        if not value:
            return 0
        return PUSHOVER_PRIORITY_MAP.get(value.strip().lower(), 0)

    @staticmethod
    def _build_body(
        text: str,
        *,
        title: str = "",
        click: str | None = None,
        device: str | None = None,
        sound: str = "pushover",
        app_token: str = "",
        user_key: str = "",
        priority: int = 0,
    ) -> dict[str, Any]:
        """Build the form body from Notifier-protocol args. Pure function."""
        body: dict[str, Any] = {
            "token": app_token,
            "user": user_key,
            "message": text[:1024],  # Pushover's max message length
        }
        if title:
            body["title"] = title[:250]
        if device:
            body["device"] = device
        if sound:
            body["sound"] = sound
        if click:
            body["url"] = click
        body["priority"] = priority
        # Pushover's emergency priority (2) requires retry/expire, otherwise
        # the API rejects it.
        if priority == 2:
            body["retry"] = 60
            body["expire"] = 3600
        return body

    async def _post(self, body: dict[str, Any], *, client: Any = None) -> None:
        """Internal: send a pre-built form body.

        ``client`` is a kwarg for testing — pass in a mock httpx
        client to avoid the real network. In production, ``client``
        is None and we create one per call.
        """
        if not self.enabled:
            return
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            own_client = True
        else:
            own_client = False
        try:
            r = await client.post(self.API_URL, data=body)
            if r.status_code >= 400:
                log.warning("pushover returned %d: %s", r.status_code, r.text[:300])
        except (httpx.HTTPError, OSError) as e:
            log.warning("pushover post failed: %s", e)
        finally:
            if own_client:
                await client.aclose()

    async def notify(
        self,
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None:
        """Send a notification. Matches the Notifier protocol from notifier.py."""
        body = self._build_body(
            text,
            title=title, click=click,
            device=self.device or None, sound=self.sound,
            app_token=self.app_token, user_key=self.user_key,
            priority=self._map_priority(priority),
        )
        await self._post(body)
