"""Discord webhook notifier.

Discord webhooks are dead-simple: POST a JSON body to a URL, and
Discord renders it. We use the "embed" object for rich text. The
webhook URL looks like:

  https://discord.com/api/webhooks/<id>/<token>

To create one: in a Discord server, channel settings → Integrations
→ Webhooks → New Webhook. Copy the URL.

For BREAKING / CAUTION alerts we use color-coded embeds:
  - BREAKING: red (15158332 = 0xE74C3C)
  - CAUTION:  yellow (15105570 = 0xE67E22)
  - INFO/SAFE: green (3066993 = 0x2ECC71)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Color codes (decimal RGB)
COLOR_BREAKING = 15158332   # red
COLOR_CAUTION  = 15105570   # orange
COLOR_SAFE     = 3066993    # green
COLOR_INFO     = 3447003    # blue


class DiscordNotifier:
    """Post notifications to a Discord webhook.

    The webhook URL is the only required configuration. Mentions,
    username override, and avatar URL are optional.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "homelab-mcp",
        avatar_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.username = username
        self.avatar_url = avatar_url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url) and "discord.com/api/webhooks/" in self.webhook_url

    @staticmethod
    def _color_for_tags(tags: list[str] | None) -> int:
        if not tags:
            return COLOR_INFO
        tagset = {t.lower() for t in tags}
        if "breaking" in tagset:
            return COLOR_BREAKING
        if "warning" in tagset or "caution" in tagset:
            return COLOR_CAUTION
        if "safe" in tagset or "success" in tagset:
            return COLOR_SAFE
        return COLOR_INFO

    @staticmethod
    def _build_body(
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        click: str | None = None,
        color: int = COLOR_INFO,
        username: str = "homelab-mcp",
        avatar_url: str | None = None,
    ) -> dict[str, Any]:
        """Build the webhook body from Notifier-protocol args.

        Pure function — no network. Easy to test.
        """
        embed: dict[str, Any] = {
            "description": text[:4096],  # Discord's max embed description
            "color": color,
        }
        if title:
            embed["title"] = title[:256]
        if click:
            embed["url"] = click
        if tags:
            embed["footer"] = {"text": " ".join(f"#{t}" for t in tags[:5])[:200]}

        body: dict[str, Any] = {
            "username": username[:80],
            "embeds": [embed],
        }
        if avatar_url:
            body["avatar_url"] = avatar_url
        return body

    async def _post(self, body: dict[str, Any], *, client: Any = None) -> None:
        """Internal: send a pre-built webhook body.

        ``client`` is a kwarg for testing — pass in a mock httpx
        client to avoid the real network. In production, ``client``
        is None and we create one per call.
        """
        if not self.enabled:
            return  # no-op when no webhook URL is configured
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            own_client = True
        else:
            own_client = False
        try:
            r = await client.post(
                self.webhook_url,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                log.warning(
                    "discord webhook returned %d: %s",
                    r.status_code,
                    r.text[:300],
                )
        except (httpx.HTTPError, OSError) as e:
            log.warning("discord webhook post failed: %s", e)
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
            title=title, tags=tags, click=click,
            color=self._color_for_tags(tags),
            username=self.username,
            avatar_url=self.avatar_url,
        )
        await self._post(body)
