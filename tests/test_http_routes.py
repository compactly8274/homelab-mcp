"""Tests for the /health and /status HTTP endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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
