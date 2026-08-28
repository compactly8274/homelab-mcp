"""Tests for the /health and /status HTTP endpoints, plus the SSE
handler's return-value contract.

The live implementation is pure ASGI (no Starlette BaseHTTPMiddleware),
so these tests drive the middleware via ASGI scope/receive/send instead of
``.dispatch()``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import homelab_mcp.http_routes  # used by static source test
from homelab_mcp.http_routes import HealthAndStatusMiddleware, build_sse_app, run_sse_with_health


async def _asgi_call(mw: Any, path: str = "/health", method: str = "GET") -> dict[str, Any]:
    """Drive an ASGI app and return the JSON body."""
    sent: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    scope = {"type": "http", "path": path, "method": method, "headers": []}
    await mw(scope, receive, send)
    assert sent, f"no response sent for {path}"
    start = sent[0]
    body = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    return {
        "status": start.get("status"),
        "body": json.loads(body.decode()) if body else None,
    }


async def test_health_returns_200_and_payload() -> None:
    mw = HealthAndStatusMiddleware(app=MagicMock())
    resp = await _asgi_call(mw, "/health", "GET")
    assert resp["status"] == 200
    assert resp["body"]["status"] == "ok"
    assert "uptime_seconds" in resp["body"]
    assert isinstance(resp["body"]["uptime_seconds"], (int, float))
    assert resp["body"]["uptime_seconds"] >= 0


async def test_status_returns_200_and_state_summary() -> None:
    fake_state = MagicMock()
    fake_state.summary = AsyncMock(
        return_value={
            "stacks": 7,
            "pending_updates": 2,
            "last_scan_ts": "2026-07-15T12:00:00Z",
            "last_applied_update_ts": None,
        }
    )
    mw = HealthAndStatusMiddleware(app=MagicMock(), get_state=lambda: fake_state)
    resp = await _asgi_call(mw, "/status", "GET")
    assert resp["status"] == 200
    assert resp["body"]["status"] == "ok"
    assert resp["body"]["daemon"] == "homelab-mcp"
    assert resp["body"]["state"]["stacks"] == 7
    assert resp["body"]["state"]["pending_updates"] == 2


async def test_status_handles_state_failure_gracefully() -> None:
    fake_state = MagicMock()
    fake_state.summary = AsyncMock(side_effect=RuntimeError("db locked"))
    mw = HealthAndStatusMiddleware(app=MagicMock(), get_state=lambda: fake_state)
    resp = await _asgi_call(mw, "/status", "GET")
    assert resp["status"] == 200
    assert resp["body"]["state"] == {"error": "db locked"}


async def test_status_without_get_state_still_returns_200() -> None:
    mw = HealthAndStatusMiddleware(app=MagicMock())
    resp = await _asgi_call(mw, "/status", "GET")
    assert resp["status"] == 200
    assert resp["body"]["status"] == "ok"
    assert "state" not in resp["body"]


async def test_non_health_routes_pass_through() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen["path"] = scope.get("path")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    mw = HealthAndStatusMiddleware(app=app)
    resp = await _asgi_call(mw, "/sse", "GET")
    assert resp["status"] == 200
    assert seen["path"] == "/sse"


async def test_health_only_responds_to_get() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen["method"] = scope.get("method")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    mw = HealthAndStatusMiddleware(app=app)
    resp = await _asgi_call(mw, "/health", "POST")
    assert resp["status"] == 200
    assert seen["method"] == "POST"


def test_run_sse_with_health_callable() -> None:
    """run_sse_with_health is importable and callable (signature check)."""
    import inspect

    sig = inspect.signature(run_sse_with_health)
    params = list(sig.parameters.keys())
    assert "mcp_instance" in params
    assert "host" in params
    assert "port" in params
    assert "get_state" in params


# --- SSE handler return-Response contract ---------------------------------
# The pure-ASGI implementation handles /sse directly; we verify the handler
# runs to completion and emits http.response.start/body.


def _build_minimal_mcp_instance() -> Any:
    mcp = MagicMock()
    mcp.settings.debug = False
    mcp.settings.log_level = "INFO"
    inner = MagicMock()

    async def _run(*args, **kwargs):
        return None

    inner.run = _run
    inner.create_initialization_options = MagicMock(return_value={})
    mcp._mcp_server = inner
    return mcp


async def test_handle_sse_returns_response_after_disconnect() -> None:
    mcp = _build_minimal_mcp_instance()

    class _FakeTransport:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def connect_sse(scope, receive, send):
            class _CM:
                async def __aenter__(self):
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    return (AsyncMock(), AsyncMock())

                async def __aexit__(self, *a):
                    return False

            return _CM()

        @staticmethod
        async def handle_post_message(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b""})

    with patch("mcp.server.sse.SseServerTransport", _FakeTransport):
        app = build_sse_app(mcp, get_state=None)
        sent: list[dict[str, Any]] = []

        async def send(event):
            sent.append(event)

        async def receive():
            return {"type": "http.disconnect"}

        scope = {"type": "http", "path": "/sse", "method": "GET", "headers": []}
        await app(scope, receive, send)

    assert any(e["type"] == "http.response.start" and e.get("status") == 200 for e in sent)
    body = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    assert body == b""


def test_handle_sse_code_returns_response_in_source() -> None:
    """Static check: http_routes source emits ASGI responses via JSONResponse."""
    import inspect

    source = inspect.getsource(homelab_mcp.http_routes)
    assert "JSONResponse" in source
    assert "http.response" in source
