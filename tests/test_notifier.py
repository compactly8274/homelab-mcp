"""Tests for the notifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homelab_mcp.updater.notifier import (
    ConsoleNotifier,
    MultiNotifier,
    NtfyNotifier,
)

# -- NtfyNotifier ---------------------------------------------------------


async def test_ntfy_notifier_uses_topic_and_priority() -> None:
    """POSTs to ntfy_url/topic with the priority header set."""
    client = MagicMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    notifier = NtfyNotifier(
        base_url="https://ntfy.sh",
        topic="homelab",
        priority="high",
    )
    await notifier._post(client, "hello", title="t", tags=["x"])
    call = client.post.call_args
    url = call.args[0]
    headers = call.kwargs.get("headers") or {}
    assert url == "https://ntfy.sh/homelab"
    assert headers.get("Title") == "t"
    assert headers.get("Priority") == "high"
    assert "x" in headers.get("Tags", "")


async def test_ntfy_notifier_skips_when_no_topic() -> None:
    """A notifier without a topic is a no-op (it would 404)."""
    notifier = NtfyNotifier(base_url="https://ntfy.sh", topic="", priority="default")
    # No client needed; it should short-circuit.
    await notifier.notify("hello")
    # Nothing to assert beyond "did not raise"


# -- MultiNotifier --------------------------------------------------------


async def test_multi_notifier_fans_out() -> None:
    """All notifiers are called; one failing does not stop the others."""
    a = MagicMock(); a.notify = AsyncMock(side_effect=RuntimeError("a broken"))
    b = MagicMock(); b.notify = AsyncMock()
    multi = MultiNotifier([a, b])
    await multi.notify("hello")
    a.notify.assert_awaited_once()
    b.notify.assert_awaited_once()


# -- ConsoleNotifier ------------------------------------------------------


async def test_console_notifier_writes_to_stream(capsys: pytest.CaptureFixture) -> None:
    notifier = ConsoleNotifier()
    await notifier.notify("test message", title="t")
    out = capsys.readouterr()
    assert "test message" in out.out or "test message" in out.err
