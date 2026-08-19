"""HTTP middleware that adds /health and /status endpoints to the SSE app.

FastMCP 0.4.1's ``run_sse_async`` builds an internal Starlette app
with just /sse and /messages routes. Anything else returns 404. We
replace that Starlette app with a pure-ASGI dispatcher that handles
GET /sse and POST /messages directly through SseServerTransport,
intercepts GET /health and GET /status for JSON responses, and leaves
WebUI/static handling to the WebUIMiddleware outer layer.

2026-08-17: Reimplemented as pure ASGI because BaseHTTPMiddleware
buffers streaming responses and corrupts SSE/MCP message posts.

2026-08-17-2: Removed Starlette Route wrapping around the SSE endpoints
entirely. Starlette Route always expects a Response return value, so it
was sending a second http.response.start and killing the SSE stream.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


class HealthAndStatusMiddleware:
    """Add /health and /status to the FastMCP SSE app (pure ASGI)."""

    def __init__(self, app: Any, *, get_state: Any = None) -> None:
        self.app = app
        self._get_state = get_state
        self._started_at = time.monotonic()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if path == "/health" and method == "GET":
            await JSONResponse({
                "status": "ok",
                "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            })(scope, receive, send)
            return
        if path == "/status" and method == "GET":
            await JSONResponse(await self._build_status())(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _build_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "daemon": "homelab-mcp",
        }
        if self._get_state is not None:
            try:
                state = self._get_state()
                out["state"] = await state.summary()
            except Exception as e:
                log.warning("/status: state DB summary failed: %s", e)
                out["state"] = {"error": str(e)}
        return out


def build_sse_app(mcp_instance: Any, *, get_state: Any = None) -> Any:
    """Build a pure-ASGI app that handles SSE/MCP plus /health + /status."""
    from mcp.server.sse import SseServerTransport  # type: ignore[import-not-found]

    sse = SseServerTransport("/messages")

    async def mcp_app(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # Should not happen, but be safe
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        if path == "/sse" and method == "GET":
            async with sse.connect_sse(scope, receive, send) as (read_stream, write_stream):
                await mcp_instance._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_instance._mcp_server.create_initialization_options(),
                )
            return

        if path == "/messages" and method == "POST":
            await sse.handle_post_message(scope, receive, send)
            return

        # Not an MCP path; return 404 so middleware layers can handle if needed.
        await JSONResponse({"detail": "not found"}, status_code=404)(scope, receive, send)

    # Wrap: HealthAndStatus inner, WebUI outer. WebUIMiddleware is pure ASGI.
    app = HealthAndStatusMiddleware(mcp_app, get_state=get_state)
    from homelab_mcp.webui import WebUIMiddleware
    app = WebUIMiddleware(app, get_state=get_state)
    return app


def run_sse_with_health(mcp_instance: Any, *, host: str, port: int, get_state: Any = None) -> None:
    """Drop-in replacement for ``mcp.run(transport="sse")`` that adds /health + /status."""
    import uvicorn

    app = build_sse_app(mcp_instance, get_state=get_state)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=mcp_instance.settings.log_level.lower(),
    )
    uvicorn.Server(config).run()
