"""WebUI HTTP routes.

The homelab-mcp daemon exposes its MCP tools via SSE. The same
daemon process also serves a small WebUI at /ui/ that wraps the
most useful tools in a browser-friendly form.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_WEBUI_DIR = Path(__file__).parent / "webui"


class WebUIMiddleware:
    """Add /ui/ static + /api/* JSON routes to the SSE app (pure ASGI).

    2026-08-17: Reimplemented as pure ASGI middleware because
    BaseHTTPMiddleware buffers streaming responses and corrupts the
    SSE/MCP message endpoint.
    """

    def __init__(self, app: Any, *, get_state: Any = None) -> None:
        self.app = app
        self._get_state = get_state
        self._enabled = os.environ.get("HOMELAB_MCP_WEBUI_ENABLED", "true").lower() in (
            "1", "true", "yes", "on",
        )
        if not self._enabled:
            log.info("WebUI disabled (HOMELAB_MCP_WEBUI_ENABLED=false)")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        if path.startswith("/ui/static/"):
            resp = self._serve_static(path.removeprefix("/ui/static/"))
            await resp(scope=scope, receive=receive, send=send)
            return

        if path in ("/ui", "/ui/") and method == "GET":
            resp = self._serve_index()
            await resp(scope=scope, receive=receive, send=send)
            return

        if path.startswith("/api/"):
            request = Request(scope, receive=receive)
            resp = await self._handle_api(request, path, method)
            await resp(scope=scope, receive=receive, send=send)
            return

        await self.app(scope, receive, send)

    _DASHBOARD_CACHE_TTL_S: ClassVar[float] = 5.0
    _dashboard_cache: ClassVar[dict[str, Any]] = {}
    _dashboard_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def _serve_static(self, rel: str) -> Response:
        if ".." in rel or rel.startswith("/"):
            return JSONResponse({"error": "bad path"}, status_code=400)
        f = _WEBUI_DIR / "static" / rel
        if not f.is_file():
            return JSONResponse({"error": "not found", "path": rel}, status_code=404)
        resp = FileResponse(f)
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp

    def _serve_index(self) -> Response:
        f = _WEBUI_DIR / "index.html"
        if not f.is_file():
            return JSONResponse(
                {"error": "index.html not found; webui/ dir is missing from the image"},
                status_code=500,
            )
        return FileResponse(f, media_type="text/html")

    async def _handle_api(self, request: Request, path: str, method: str) -> Response:
        log.info("webui api: %s %s", method, path)
        try:
            handler = _API_HANDLERS.get(path)
            if handler is None:
                return JSONResponse({"error": "unknown api", "path": path}, status_code=404)
            if method not in handler["methods"]:
                return JSONResponse(
                    {"error": f"{path} only accepts {handler['methods']}, got {method}"},
                    status_code=405,
                )
            result = await handler["fn"](request)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
                body, status = result
            else:
                body, status = result, 200
            log.info("webui api %s %s -> %d (%d bytes)", method, path, status, len(json.dumps(body, default=str)))
            return JSONResponse(body, status_code=status)
        except ValueError as e:
            log.warning("webui api handler %s %s bad request: %s", method, path, e)
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            log.exception("webui api handler %s %s failed", method, path)
            return JSONResponse({"error": str(e)}, status_code=500)


async def _api_dashboard(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.dashboard import health_dashboard_tool
    only_host = request.query_params.get("host") or None
    cache_key = only_host or "__all__"
    now = time.monotonic()
    cached = WebUIMiddleware._dashboard_cache.get(cache_key)
    if cached and (now - cached["ts"]) < WebUIMiddleware._DASHBOARD_CACHE_TTL_S:
        return cached["data"]
    async with WebUIMiddleware._dashboard_lock:
        cached = WebUIMiddleware._dashboard_cache.get(cache_key)
        if cached and (now - cached["ts"]) < WebUIMiddleware._DASHBOARD_CACHE_TTL_S:
            return cached["data"]
        data = await health_dashboard_tool(only_host=only_host, top_problems=10)
        WebUIMiddleware._dashboard_cache[cache_key] = {"data": data, "ts": time.monotonic()}
        return data


async def _api_pendings(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.updates import list_pending_updates_tool
    host = request.query_params.get("host")
    if not host:
        return {"error": "host query param is required"}, 400
    rows = await list_pending_updates_tool(host=host)
    return {"host": host, "count": len(rows), "rows": rows}


async def _api_history(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.get_update_history import get_update_history_tool
    host = request.query_params.get("host")
    stack = request.query_params.get("stack")
    if not host or not stack:
        return {"error": "host and stack query params are required"}, 400
    limit = int(request.query_params.get("limit", "20"))
    rows = await get_update_history_tool(host=host, stack=stack, limit=limit)
    return {"host": host, "stack": stack, "rows": rows}


async def _api_notifier(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.notifier_status import notifier_status_tool
    return await notifier_status_tool(test_notify=False)


async def _api_preflight(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.preflight import preflight_check_tool
    host = request.query_params.get("host")
    stack = request.query_params.get("stack")
    action = request.query_params.get("action", "apply_update")
    if not host or not stack:
        return {"error": "host and stack query params are required"}, 400
    return await preflight_check_tool(host=host, stack=stack, action=action)


async def _api_apply(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.apply_update import apply_update_tool
    body = await _read_json_body(request)
    required = ("host", "stack")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return {"error": f"missing required fields: {missing}"}, 400
    return await apply_update_tool(
        host=body["host"],
        stack=body["stack"],
        force=bool(body.get("force", False)),
        dry_run=bool(body.get("dry_run", False)),
        require_approval=bool(body.get("require_approval", True)),
    )


async def _api_container_action(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.container_action import container_action_tool
    body = await _read_json_body(request)
    required = ("host", "target", "action")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return {"error": f"missing required fields: {missing}"}, 400
    return await container_action_tool(
        host=body["host"],
        target=body["target"],
        action=body["action"],
        require_approval=bool(body.get("require_approval", True)),
    )


async def _api_heal(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.auto_heal import auto_heal_container_tool, auto_heal_scan_tool
    body = await _read_json_body(request)
    if not body.get("host"):
        return {"error": "host is required"}, 400
    settle = int(body.get("settle_seconds", 10))
    if body.get("name"):
        return await auto_heal_container_tool(
            host=body["host"], name=body["name"], settle_seconds=settle,
        )
    return await auto_heal_scan_tool(
        host=body["host"], settle_seconds=settle,
    )


async def _api_dismiss(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.updates import pending_update_dismiss_tool
    body = await _read_json_body(request)
    required = ("host", "stack", "latest_digest")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return {"error": f"missing required fields: {missing}"}, 400
    return await pending_update_dismiss_tool(
        host=body["host"],
        stack=body["stack"],
        latest_digest=body["latest_digest"],
    )


async def _api_stacks(request: Request) -> dict[str, Any]:
    from homelab_mcp import server
    out: dict[str, Any] = {"hosts": []}
    for name, host_client in server._host_clients.items():
        try:
            stacks = await host_client.list_stacks()
            out["hosts"].append({
                "name": name,
                "stacks": [s["name"] for s in stacks if s.get("name")],
            })
        except Exception as e:
            out["hosts"].append({"name": name, "stacks": [], "error": str(e)})
    return out


_API_HANDLERS: dict[str, dict[str, Any]] = {
    "/api/dashboard":        {"fn": _api_dashboard,        "methods": ("GET",)},
    "/api/pendings":         {"fn": _api_pendings,         "methods": ("GET",)},
    "/api/history":          {"fn": _api_history,          "methods": ("GET",)},
    "/api/notifier":         {"fn": _api_notifier,         "methods": ("GET",)},
    "/api/preflight":        {"fn": _api_preflight,        "methods": ("GET",)},
    "/api/apply":            {"fn": _api_apply,            "methods": ("POST",)},
    "/api/container_action": {"fn": _api_container_action, "methods": ("POST",)},
    "/api/heal":             {"fn": _api_heal,             "methods": ("POST",)},
    "/api/dismiss":          {"fn": _api_dismiss,          "methods": ("POST",)},
    "/api/stacks":           {"fn": _api_stacks,           "methods": ("GET",)},
}


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"body is not valid JSON: {e}") from e


def build_webui_app(base_app: Any, *, get_state: Any = None) -> Any:
    """Wrap ``base_app`` with the WebUI middleware. Pass-through if disabled."""
    return WebUIMiddleware(base_app, get_state=get_state)
