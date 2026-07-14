"""Notification fan-out.

A small protocol with a single ``notify`` method, plus three shipped
implementations:

- :class:`NtfyNotifier` — POSTs to an ntfy topic. Headers carry the
  title, tags, and priority. Default notifier for the auto-apply
  pipeline.
- :class:`ConsoleNotifier` — writes to stdout/stderr. Used for tests
  and as a last-resort default.
- :class:`MultiNotifier` — fans out to several notifiers, swallowing
  per-notifier errors so one broken channel doesn't kill the rest.

Other channels (Discord webhook, Pushover, Email) plug in behind the
``Notifier`` protocol.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Protocol, runtime_checkable


log = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """Send a single notification.

    Implementations must not raise. Errors should be logged at WARNING
    level and swallowed.
    """

    async def notify(
        self,
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None: ...


class NtfyNotifier:
    """Post to an ntfy topic.

    The topic is appended to the base URL (so the full URL is
    ``base_url/<topic>``). ntfy uses headers for everything: ``Title``,
    ``Priority``, ``Tags``, ``Click``.
    """

    def __init__(
        self,
        base_url: str,
        topic: str,
        priority: str = "default",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.topic = topic.strip().strip("/")
        self.priority = priority
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    async def _post(self, client, text: str, **kw: Any) -> None:
        if not self.enabled:
            return  # nothing to post to
        url = f"{self.base_url}/{self.topic}"
        headers = {
            "Title": kw.get("title", "")[:200],
            "Priority": (kw.get("priority") or self.priority)[:16],
            "Tags": ",".join(kw.get("tags") or [])[:256],
        }
        click = kw.get("click")
        if click:
            headers["Click"] = click
        try:
            await client.post(url, content=text.encode("utf-8"), headers=headers, timeout=self.timeout)
        except Exception as e:
            log.warning("ntfy post to %s failed: %s", url, e)

    async def notify(
        self,
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await self._post(
                client, text,
                title=title, tags=tags or [], priority=priority, click=click,
            )


class MultiNotifier:
    """Fan out a notification to several notifiers.

    Per-notifier errors are caught and logged. A failing Discord
    webhook must not prevent a ntfy alert from going out.
    """

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = list(notifiers)

    async def notify(
        self,
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None:
        for n in self.notifiers:
            try:
                await n.notify(
                    text, title=title, tags=tags, priority=priority, click=click,
                )
            except Exception as e:
                log.warning("notifier %r failed: %s", n, e)


class ConsoleNotifier:
    """Write notifications to a stream. Used for tests and as a fallback."""

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    async def notify(
        self,
        text: str,
        *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None:
        prefix = f"[{title}] " if title else ""
        line = f"{prefix}{text}\n"
        try:
            self.stream.write(line)
            self.stream.flush()
        except Exception as e:
            log.warning("console notifier write failed: %s", e)
