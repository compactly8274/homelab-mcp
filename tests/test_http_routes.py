"""Tests for the /health and /status HTTP endpoints, plus the SSE
handler's return-value contract.

The SSE contract is the one we've been chasing: the FastMCP library's
docstring explicitly states that ``handle_sse`` must return a Response
after ``connect_sse`` ends, otherwise starlette/uvicorn raises
"TypeError: 'NoneType' object is not callable" when the client
disconnects mid-stream. The C-side bridge flap we've been chasing
since v0.4.0 was caused by this exact bug.

See homelab_mcp/http_routes.py for the fix.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homelab_mcp.http_routes import (
    HealthAndStatusMiddleware,
    build_sse_app,
    run_sse_with_health,
)


def _make_request(path: str = "/health", method: str = "GET") -> MagicMock:
    req = MagicMock()
    req.url.path = path
    req.method = method
    return req


async def test_health_returns_200_and_payload() -> None:
    """GET /health returns 200 with status and uptime."""
    async def call_next(_request: MagicMock) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mw = HealthAndStatusMiddleware(app=MagicMock())
    resp = await mw.dispatch(_make_request("/health", "GET"), call_next)
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert "uptime_seconds" in body
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0


async def test_status_returns_200_and_state_summary() -> None:
    """GET /status returns 200 with state.summary() included."""
    async def call_next(_request: MagicMock) -> MagicMock:
        return MagicMock()

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
    resp = await mw.dispatch(_make_request("/status", "GET"), call_next)
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["daemon"] == "homelab-mcp"
    assert body["state"]["stacks"] == 7
    assert body["state"]["pending_updates"] == 2


async def test_status_handles_state_failure_gracefully() -> None:
    """If state.summary() throws, /status still returns 200 with an error field."""
    async def call_next(_request: MagicMock) -> MagicMock:
        return MagicMock()

    fake_state = MagicMock()
    fake_state.summary = AsyncMock(side_effect=RuntimeError("db locked"))
    mw = HealthAndStatusMiddleware(app=MagicMock(), get_state=lambda: fake_state)
    resp = await mw.dispatch(_make_request("/status", "GET"), call_next)
    body = json.loads(resp.body)
    assert body["state"] == {"error": "db locked"}


async def test_status_without_get_state_still_returns_200() -> None:
    """No get_state configured → /status still 200, just no state field."""
    async def call_next(_request: MagicMock) -> MagicMock:
        return MagicMock()

    mw = HealthAndStatusMiddleware(app=MagicMock())
    resp = await mw.dispatch(_make_request("/status", "GET"), call_next)
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert "state" not in body


async def test_non_health_routes_pass_through() -> None:
    """Anything not /health or /status falls through to call_next()."""
    seen: dict = {}

    async def call_next(request: MagicMock) -> MagicMock:
        seen["path"] = request.url.path
        return MagicMock()

    mw = HealthAndStatusMiddleware(app=MagicMock())
    await mw.dispatch(_make_request("/sse", "GET"), call_next)
    assert seen["path"] == "/sse"


async def test_health_only_responds_to_get() -> None:
    """POST /health is not intercepted; it falls through to the SSE router."""
    seen: dict = {}

    async def call_next(request: MagicMock) -> MagicMock:
        seen["method"] = request.method
        return MagicMock()

    mw = HealthAndStatusMiddleware(app=MagicMock())
    await mw.dispatch(_make_request("/health", "POST"), call_next)
    assert seen["method"] == "POST"


def test_build_sse_app_returns_starlette_app() -> None:
    """build_sse_app returns an object with add_middleware + routes."""
    mcp = MagicMock()
    mcp.settings.debug = False
    mcp.settings.log_level = "INFO"
    mcp._mcp_server.create_initialization_options = MagicMock(return_value={})
    app = build_sse_app(mcp)
    assert hasattr(app, "add_middleware")
    assert hasattr(app, "router")
    paths = [r.path for r in app.router.routes]
    assert "/sse" in paths
    assert "/messages" in paths


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
# The MCP library's own docstring (in mcp/server/sse.py) says:
#   "The handle_sse function must return a Response to avoid a
#    'TypeError: NoneType object is not callable' error when client
#    disconnects. The example above returns an empty Response() after
#    the SSE connection ends to fix this."
#
# This is the root cause of the long-standing hermes-agent MCP bridge
# flap: when hermes-agent disconnects (e.g. on keepalive timeout), the
# SSE handler returns None, starlette/uvicorn throws TypeError, the
# SSE POST gets a ReadError, and the bridge enters a 60s+ reconnect
# loop. The fix in homelab_mcp/http_routes.py adds `return Response()`
# after the connect_sse context manager. These tests pin that fix.


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
    """After connect_sse ends (client disconnect simulated), the handler
    must return a non-None Response. Without it, uvicorn throws
    'TypeError: NoneType object is not callable' and the SSE bridge
    enters a 60s+ reconnect loop."""
    import mcp.server.sse as sse_mod

    @asynccontextmanager
    async def _noop_connect_sse(*a, **kw):
        yield (AsyncMock(), AsyncMock())

    mcp = _build_minimal_mcp_instance()
    with patch.object(sse_mod, "SseServerTransport") as mock_sse_cls:
        mock_sse_instance = MagicMock()
        mock_sse_instance.connect_sse = _noop_connect_sse
        mock_sse_cls.return_value = mock_sse_instance
        app = build_sse_app(mcp, get_state=None)
        for route in app.router.routes:
            if getattr(route, "path", None) == "/sse":
                handler = route.endpoint
                break

        fake_request = MagicMock()
        fake_request.scope = {"type": "http"}
        fake_request.receive = AsyncMock()
        fake_request._send = AsyncMock()
        result = await handler(fake_request)

    assert result is not None, (
        "handle_sse returned None — this causes uvicorn 'NoneType is not "
        "callable' errors and the hermes-agent SSE bridge flap"
    )
    assert result.status_code == 200
    assert result.body == b""


def test_handle_sse_code_returns_response_in_source() -> None:
    """Static check: the source of build_sse_app must contain
    'return Response()' at the end of handle_sse, as documented by
    the FastMCP library to avoid the NoneType error."""
    import inspect

    from homelab_mcp.http_routes import build_sse_app

    source = inspect.getsource(build_sse_app)
    assert "return Response()" in source, (
        "build_sse_app does not include 'return Response()' after the "
        "connect_sse context manager. This is the documented requirement "
        "to avoid uvicorn 'NoneType is not callable' errors when the "
        "client disconnects."
    )
