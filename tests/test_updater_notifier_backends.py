"""Tests for the Discord and Pushover notifiers."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from homelab_mcp.updater.discord import (
    COLOR_BREAKING,
    COLOR_CAUTION,
    COLOR_SAFE,
    DiscordNotifier,
)
from homelab_mcp.updater.pushover import (
    PUSHOVER_PRIORITY_MAP,
    PushoverNotifier,
)

# ============================================================================
# Discord
# ============================================================================


# -- color selection --


def test_discord_color_for_tags() -> None:
    """Tag-driven color selection matches the risk semantics."""
    assert DiscordNotifier._color_for_tags(["breaking"]) == COLOR_BREAKING
    assert DiscordNotifier._color_for_tags(["caution"]) == COLOR_CAUTION
    assert DiscordNotifier._color_for_tags(["warning"]) == COLOR_CAUTION
    assert DiscordNotifier._color_for_tags(["safe", "success"]) == COLOR_SAFE
    # Unknown / no tags → info color (positive int)
    assert DiscordNotifier._color_for_tags([]) > 0
    assert DiscordNotifier._color_for_tags(None) > 0


# -- enabled state --


def test_discord_notifier_disabled_without_url() -> None:
    """A notifier with no webhook URL is a no-op."""
    n = DiscordNotifier(webhook_url="")
    assert n.enabled is False
    import asyncio
    asyncio.run(n.notify("hello", title="t"))  # no-op, no exception


def test_discord_notifier_enabled_with_valid_url() -> None:
    """A notifier with a real webhook URL is enabled."""
    n = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    assert n.enabled is True


def test_discord_notifier_rejects_garbage_url() -> None:
    """A webhook URL that doesn't look like Discord is treated as not configured."""
    n = DiscordNotifier(webhook_url="https://example.com/abc")
    assert n.enabled is False


# -- body building (pure function) --


def test_discord_build_body_basic() -> None:
    """_build_body produces a well-formed embed with title, color, description."""
    body = DiscordNotifier._build_body(
        "test body", title="t", tags=["breaking"], click="https://example.com",
        color=COLOR_BREAKING, username="test-bot",
    )
    assert body["username"] == "test-bot"
    assert len(body["embeds"]) == 1
    embed = body["embeds"][0]
    assert embed["title"] == "t"
    assert embed["description"] == "test body"
    assert embed["url"] == "https://example.com"
    assert embed["color"] == COLOR_BREAKING
    assert "breaking" in embed["footer"]["text"]


def test_discord_build_body_truncates_long_text() -> None:
    """Discord has a 4096-char embed description limit; we truncate cleanly."""
    long_text = "x" * 5000
    body = DiscordNotifier._build_body(long_text)
    assert len(body["embeds"][0]["description"]) == 4096


def test_discord_build_body_truncates_long_title() -> None:
    """Title is capped at 256 chars (Discord's limit)."""
    body = DiscordNotifier._build_body("x", title="y" * 1000)
    assert len(body["embeds"][0]["title"]) == 256


def test_discord_build_body_avatar_url_optional() -> None:
    """avatar_url is omitted when not set (avoids empty string in payload)."""
    body = DiscordNotifier._build_body("x", username="bot")
    assert "avatar_url" not in body
    body2 = DiscordNotifier._build_body("x", username="bot", avatar_url="https://x/y.png")
    assert body2["avatar_url"] == "https://x/y.png"


def test_discord_build_body_no_title_omits_field() -> None:
    """If no title is given, the embed doesn't have a 'title' key."""
    body = DiscordNotifier._build_body("x")
    assert "title" not in body["embeds"][0]


# -- network layer (the _post method) --


async def test_discord_post_sends_to_webhook_url() -> None:
    """_post targets the configured webhook URL with the body as JSON."""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    n = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    body = {"username": "x", "embeds": [{"description": "y"}]}
    await n._post(body, client=client)
    call = client.post.call_args
    assert call.args[0] == "https://discord.com/api/webhooks/123/abc"
    assert call.kwargs["json"] == body


async def test_discord_post_noop_when_disabled() -> None:
    """_post is a no-op when the notifier is disabled (no client, no raise)."""
    n = DiscordNotifier(webhook_url="")
    # Pass None to confirm we never touch the client
    await n._post({"x": 1}, client=None)  # no exception


async def test_discord_post_swallows_httperror() -> None:
    """An HTTPError during the post is logged and swallowed (not raised)."""
    import httpx
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.HTTPError("network down"))
    client.aclose = AsyncMock()
    n = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    # Should not raise
    await n._post({"x": 1}, client=client)


async def test_discord_post_warns_on_4xx() -> None:
    """A 4xx response is logged at WARNING level but not raised."""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 429
    resp.text = "rate limited"
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    n = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    await n._post({"x": 1}, client=client)  # no exception


# -- public notify() integration --


async def test_discord_notify_end_to_end() -> None:
    """notify() builds the body from Notifier-protocol args and posts it."""
    client = MagicMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    n = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc", username="bot")
    # Patch the internal client to our mock
    n._post = AsyncMock()
    await n.notify("test", title="t", tags=["breaking"], click="https://x.com")
    n._post.assert_awaited_once()
    # Args: (body, client=...) — kwargs is empty
    body = n._post.call_args.args[0]
    assert body["username"] == "bot"
    assert body["embeds"][0]["color"] == COLOR_BREAKING


# ============================================================================
# Pushover
# ============================================================================


# -- priority mapping --


def test_pushover_priority_map_in_range() -> None:
    """All mapped values are within Pushover's allowed range (-2..2)."""
    for label, value in PUSHOVER_PRIORITY_MAP.items():
        assert -2 <= value <= 2, f"priority {label!r} out of range: {value}"


def test_pushover_map_priority_unknown_defaults_to_normal() -> None:
    """Unknown priority labels default to 0 (normal)."""
    assert PushoverNotifier._map_priority("foo") == 0
    assert PushoverNotifier._map_priority(None) == 0
    assert PushoverNotifier._map_priority("") == 0
    assert PushoverNotifier._map_priority("DEFAULT") == 0  # case-insensitive


def test_pushover_map_priority_known_values() -> None:
    """Known labels map to documented Pushover priority numbers."""
    assert PushoverNotifier._map_priority("low") == -1
    assert PushoverNotifier._map_priority("min") == -2
    assert PushoverNotifier._map_priority("default") == 0
    assert PushoverNotifier._map_priority("high") == 1
    assert PushoverNotifier._map_priority("urgent") == 2


# -- enabled state --


def test_pushover_notifier_disabled_without_creds() -> None:
    """A notifier with no token or user_key is a no-op."""
    assert PushoverNotifier(app_token="", user_key="abc").enabled is False
    assert PushoverNotifier(app_token="abc", user_key="").enabled is False
    assert PushoverNotifier(app_token="", user_key="").enabled is False


def test_pushover_notifier_enabled_with_both() -> None:
    """A notifier with both token and user_key is enabled."""
    assert PushoverNotifier(app_token="abc", user_key="xyz").enabled is True


# -- body building (pure function) --


def test_pushover_build_body_basic() -> None:
    """_build_body produces a well-formed form body."""
    body = PushoverNotifier._build_body(
        "test body", title="t", click="https://x.com",
        device="phone1", sound="bike",
        app_token="tok", user_key="usr", priority=1,
    )
    assert body["token"] == "tok"
    assert body["user"] == "usr"
    assert body["message"] == "test body"
    assert body["title"] == "t"
    assert body["device"] == "phone1"
    assert body["sound"] == "bike"
    assert body["url"] == "https://x.com"
    assert body["priority"] == 1


def test_pushover_build_body_emergency_adds_retry_expire() -> None:
    """Priority 2 (emergency) requires retry/expire fields per Pushover's spec."""
    body = PushoverNotifier._build_body("x", app_token="t", user_key="u", priority=2)
    assert body["priority"] == 2
    assert body["retry"] == 60
    assert body["expire"] == 3600


def test_pushover_build_body_no_emergency_omits_retry_expire() -> None:
    """Non-emergency priorities don't have retry/expire fields."""
    for prio in (0, 1, -1, -2):
        body = PushoverNotifier._build_body("x", app_token="t", user_key="u", priority=prio)
        assert "retry" not in body
        assert "expire" not in body


def test_pushover_build_body_truncates_message() -> None:
    """Message is capped at 1024 chars (Pushover's max)."""
    body = PushoverNotifier._build_body("x" * 2000, app_token="t", user_key="u")
    assert len(body["message"]) == 1024


def test_pushover_build_body_truncates_title() -> None:
    """Title is capped at 250 chars."""
    body = PushoverNotifier._build_body("x", title="y" * 1000, app_token="t", user_key="u")
    assert len(body["title"]) == 250


def test_pushover_build_body_no_title_omits_field() -> None:
    """If no title, the body doesn't have a 'title' key."""
    body = PushoverNotifier._build_body("x", app_token="t", user_key="u")
    assert "title" not in body


def test_pushover_build_body_no_device_omits_field() -> None:
    """If no device, the body doesn't have a 'device' key."""
    body = PushoverNotifier._build_body("x", app_token="t", user_key="u")
    assert "device" not in body


# -- network layer --


async def test_pushover_post_targets_api_url() -> None:
    """_post hits the Pushover messages API with form data."""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    n = PushoverNotifier(app_token="t", user_key="u")
    await n._post({"token": "t", "user": "u", "message": "hi"}, client=client)
    call = client.post.call_args
    assert call.args[0] == "https://api.pushover.net/1/messages.json"
    assert call.kwargs["data"]["message"] == "hi"


async def test_pushover_post_noop_when_disabled() -> None:
    """_post is a no-op when the notifier is disabled."""
    n = PushoverNotifier(app_token="", user_key="u")
    await n._post({"x": 1}, client=None)


async def test_pushover_post_swallows_httperror() -> None:
    """HTTPError during the post is logged and swallowed."""
    import httpx
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.HTTPError("down"))
    client.aclose = AsyncMock()
    n = PushoverNotifier(app_token="t", user_key="u")
    await n._post({"x": 1}, client=client)


async def test_pushover_post_warns_on_4xx(caplog: pytest.LogCaptureFixture) -> None:
    """A 4xx response is logged as a warning."""
    caplog.set_level(logging.WARNING)
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 400
    resp.text = '{"errors":["invalid token"]}'
    client.post = AsyncMock(return_value=resp)
    client.aclose = AsyncMock()
    n = PushoverNotifier(app_token="t", user_key="u")
    await n._post({"token": "t", "user": "u", "message": "x"}, client=client)
    assert any("invalid token" in r.message for r in caplog.records)


# -- public notify() integration --


async def test_pushover_notify_end_to_end() -> None:
    """notify() builds the body from Notifier-protocol args and posts it."""
    n = PushoverNotifier(app_token="t", user_key="u", sound="bike")
    n._post = AsyncMock()
    await n.notify("test", title="t", priority="high", click="https://x.com")
    n._post.assert_awaited_once()
    body = n._post.call_args.args[0]
    assert body["priority"] == 1  # high -> 1
    assert body["sound"] == "bike"
    assert body["url"] == "https://x.com"


# ============================================================================
# MultiNotifier dispatch (covers all 3 backends together)
# ============================================================================


async def test_multi_notifier_dispatches_to_all_three_backends() -> None:
    """A MultiNotifier with ntfy + Discord + Pushover calls all three."""
    a = MagicMock(); a.notify = AsyncMock()
    b = MagicMock(); b.notify = AsyncMock()
    c = MagicMock(); c.notify = AsyncMock()
    from homelab_mcp.updater.notifier import MultiNotifier
    multi = MultiNotifier([a, b, c])
    await multi.notify("hello", title="t", tags=["breaking"])
    a.notify.assert_awaited_once()
    b.notify.assert_awaited_once()
    c.notify.assert_awaited_once()
    for n in (a, b, c):
        kw = n.notify.call_args.kwargs
        assert kw["tags"] == ["breaking"]
        assert kw["title"] == "t"


async def test_multi_notifier_isolates_per_notifier_errors() -> None:
    """A failing notifier does not prevent the others from being called."""
    a = MagicMock(); a.notify = AsyncMock(side_effect=RuntimeError("ntfy down"))
    b = MagicMock(); b.notify = AsyncMock()
    c = MagicMock(); c.notify = AsyncMock(side_effect=RuntimeError("pushover down"))
    from homelab_mcp.updater.notifier import MultiNotifier
    multi = MultiNotifier([a, b, c])
    await multi.notify("hello")  # should not raise
    b.notify.assert_awaited_once()
