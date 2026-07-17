"""HTTP middleware that adds /health and /status endpoints to the SSE app.

FastMCP 0.4.1's ``run_sse_async`` builds an internal Starlette app
with just /sse and /messages routes. Anything else returns 404. We
wrap the Starlette app with a BaseHTTPMiddleware that intercepts
GET /health and GET /status *before* the router sees the request,
returning 200 with JSON bodies. Everything else passes through
untouched to the SSE/MCP routes.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    pass

# webui.WebUIMiddleware is imported lazily inside build_sse_app
# to avoid a circular import (webui imports tools which import
# server which sets up mcp which lists all tools).

log = logging.getLogger(__name__)


class HealthAndStatusMiddleware(BaseHTTPMiddleware):
    """Add /health and /status to the FastMCP SSE app.

    ``/health`` returns 200 with ``{"status": "ok", "uptime_seconds": N}``
    on every request, no dependencies. Suitable for k8s liveness probes
    or any external healthcheck.

    ``/status`` returns 200 with a richer payload describing the daemon:
    the configured hosts, the state DB stats, and a per-host
    reachability summary. Errors here are non-fatal: if the state DB
    is unreachable, we still return 200 with the error in the body.
    """

    def __init__(self, app: Any, *, get_state: Any = None) -> None:
        super().__init__(app)
        self._get_state = get_state
        self._started_at = time.monotonic()

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path == "/health" and request.method == "GET":
            return JSONResponse({
                "status": "ok",
                "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            })
        if path == "/status" and request.method == "GET":
            return JSONResponse(await self._build_status())
        return await call_next(request)

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
    """Build the same Starlette app FastMCP builds for SSE, plus our routes.

    Duplicates FastMCP's internal construction so we can layer the
    middleware in. When FastMCP exposes a public route hook, this
    can be simplified.
    """
    from mcp.server.sse import SseServerTransport  # type: ignore[import-not-found]
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    sse = SseServerTransport("/messages")

    async def handle_sse(request: Any) -> Any:
        # The MCP library's docstring explicitly warns: the SSE handler
        # MUST return a Response after connect_sse ends, otherwise
        # starlette/uvicorn throws "TypeError: 'NoneType' object is not
        # callable" when the client disconnects mid-stream. This is the
        # root cause of the long-standing hermes-agent bridge flap.
        async with sse.connect_sse(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        ) as streams:
            await mcp_instance._mcp_server.run(  # type: ignore[attr-defined]
                streams[0],
                streams[1],
                mcp_instance._mcp_server.create_initialization_options(),  # type: ignore[attr-defined]
            )
        return Response()

    async def handle_messages(request: Any) -> Any:
        # SseServerTransport.handle_post_message returns None (it does
        # its own response via sse.handle_post_message's internal
        # await response(scope, receive, send) for 202 Accepted). But
        # starlette's Route class expects every endpoint to return a
        # Response object — if the endpoint returns None, starlette
        # raises "TypeError: 'NoneType' object is not callable" on
        # `await response(scope, receive, send)` in routing.py line 62.
        # We wrap the call to ensure a Response is always returned.
        await sse.handle_post_message(request.scope, request.receive, request._send)
        # If handle_post_message sent a response already, this empty
        # Response is ignored; if it didn't (e.g. session not found
        # 404 already sent), this prevents the NoneType crash.
        from starlette.responses import Response as _Resp
        return _Resp()

    starlette_app = Starlette(
        debug=mcp_instance.settings.debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/messages", endpoint=handle_messages, methods=["POST"]),
        ],
    )
    starlette_app.add_middleware(HealthAndStatusMiddleware, get_state=get_state)
    # WebUI is layered AFTER HealthAndStatus so /health + /status
    # are still intercepted by the inner middleware. Starlette
    # runs middleware in reverse-add order, so the LAST added is
    # the OUTERMOST — meaning the WebUI sees the request first,
    # and only delegates to HealthAndStatusMiddleware / SSE when
    # the path doesn't match a WebUI route.
    from homelab_mcp.webui import WebUIMiddleware
    starlette_app.add_middleware(WebUIMiddleware, get_state=get_state)
    return starlette_app


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
