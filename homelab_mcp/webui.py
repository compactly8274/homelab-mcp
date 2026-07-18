"""WebUI HTTP routes.

The homelab-mcp daemon exposes its MCP tools via SSE. The same
daemon process also serves a small WebUI at /ui/ that wraps the
most useful tools in a browser-friendly form.

The WebUI is opt-in via the HOMELAB_MCP_WEBUI_ENABLED env var
(default: "true" — the WebUI is part of the v0.9.0 feature set;
set to "false" to disable). When disabled, the middleware does
nothing.

Bind: 127.0.0.1 only. The user is expected to expose /ui/
through their existing reverse proxy (NPM, Pangolin, Traefik)
if remote access is needed; we deliberately do NOT bind to
0.0.0.0 because the WebUI has no auth and is intended to be
LAN-only.

Pages:
  GET  /ui/                       -> single-page UI (index.html)
  GET  /ui/static/*               -> js, css (served from webui/static/)

JSON API (no auth — bind to 127.0.0.1 only):
  GET  /api/dashboard             -> health_dashboard_tool() result
  GET  /api/pendings?host=X       -> list_pending_updates_tool(host)
  GET  /api/history?host=X&stack=Y -> get_update_history_tool
  GET  /api/notifier              -> notifier_status_tool()
  GET  /api/preflight?host=X&stack=Y&action=Z -> preflight_check_tool
  POST /api/apply                 -> {host, stack, force, dry_run, require_approval}
                                     calls apply_update_tool
  POST /api/container_action      -> {host, target, action, require_approval}
                                     calls container_action_tool
  POST /api/dismiss               -> {host, stack, latest_digest}
                                     calls pending_update_dismiss_tool
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# Resolve webui/ dir once at import. In a wheel install, this lives
# inside the package. In a dev checkout, it's at the repo root.
_WEBUI_DIR = Path(__file__).parent / "webui"


class WebUIMiddleware(BaseHTTPMiddleware):
    """Add /ui/ static + /api/* JSON routes to the SSE app.

    Disabled when HOMELAB_MCP_WEBUI_ENABLED=false. The middleware
    is still installed (cheap), it just no-ops on every request.
    """

    def __init__(self, app: Any, *, get_state: Any = None) -> None:
        super().__init__(app)
        self._get_state = get_state
        self._enabled = os.environ.get("HOMELAB_MCP_WEBUI_ENABLED", "true").lower() in (
            "1", "true", "yes", "on",
        )
        if not self._enabled:
            log.info("WebUI disabled (HOMELAB_MCP_WEBUI_ENABLED=false)")

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._enabled:
            return await call_next(request)
        path = request.url.path
        method = request.method

        # Static files (CSS, JS, favicon)
        if path.startswith("/ui/static/"):
            return self._serve_static(path.removeprefix("/ui/static/"))

        # The single-page UI
        if path in ("/ui", "/ui/") and method == "GET":
            return self._serve_index()

        # JSON API
        if path.startswith("/api/"):
            return await self._handle_api(request, path, method)

        # Everything else (including /sse, /messages, /health, /status)
        # passes through to the wrapped app.
        return await call_next(request)

    # ----- static -----

    def _serve_static(self, rel: str) -> Response:
        # Reject path-traversal attempts
        if ".." in rel or rel.startswith("/"):
            return JSONResponse({"error": "bad path"}, status_code=400)
        f = _WEBUI_DIR / "static" / rel
        if not f.is_file():
            return JSONResponse({"error": "not found", "path": rel}, status_code=404)
        return FileResponse(f)

    def _serve_index(self) -> Response:
        f = _WEBUI_DIR / "index.html"
        if not f.is_file():
            return JSONResponse(
                {"error": "index.html not found; webui/ dir is missing from the image"},
                status_code=500,
            )
        return FileResponse(f, media_type="text/html")

    # ----- api -----

    async def _handle_api(self, request: Request, path: str, method: str) -> Response:
        log.info("webui api: %s %s", method, path)
        # We dispatch by path. All endpoints are thin wrappers over
        # the same tool functions the LLM calls. The tool returns
        # a dict; we just JSON-encode it. Errors return 500 with
        # the exception message.
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
            log.info("webui api %s %s -> %d bytes", method, path, len(json.dumps(result, default=str)))
            return JSONResponse(result)
        except Exception as e:
            log.exception("webui api handler %s %s failed", method, path)
            return JSONResponse({"error": str(e)}, status_code=500)


# ----- api handlers -----
# Each handler is a small async function that takes the request
# and calls the relevant MCP tool. We import tools lazily to keep
# startup fast and to avoid a chicken-and-egg between the webui
# module and the tools module (which imports server, which sets
# up mcp, which lists all tools).


async def _api_dashboard(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.dashboard import health_dashboard_tool
    only_host = request.query_params.get("host") or None
    return await health_dashboard_tool(only_host=only_host, top_problems=10)


async def _api_pendings(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.updates import list_pending_updates_tool
    host = request.query_params.get("host")
    if not host:
        return {"error": "host query param is required"}
    rows = await list_pending_updates_tool(host=host)
    return {"host": host, "count": len(rows), "rows": rows}


async def _api_history(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.get_update_history import get_update_history_tool
    host = request.query_params.get("host")
    stack = request.query_params.get("stack")
    if not host or not stack:
        return {"error": "host and stack query params are required"}
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
        return {"error": "host and stack query params are required"}
    return await preflight_check_tool(host=host, stack=stack, action=action)


async def _api_apply(request: Request) -> dict[str, Any]:
    from homelab_mcp.tools.apply_update import apply_update_tool
    body = await _read_json_body(request)
    required = ("host", "stack")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return {"error": f"missing required fields: {missing}"}
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
        return {"error": f"missing required fields: {missing}"}
    return await container_action_tool(
        host=body["host"],
        target=body["target"],
        action=body["action"],
        require_approval=bool(body.get("require_approval", True)),
    )


async def _api_dismiss(request: Request) -> dict[str, Any]:
    # pending_update_dismiss_tool lives in tools/updates.py, not tools/dismiss_all_pending.py
    # (dismiss_all_pending.py exports the bulk dismiss_all_pending_tool only).
    # Fix 2026-07-18: was importing from the wrong module → 500 on /api/dismiss.
    from homelab_mcp.tools.updates import pending_update_dismiss_tool
    body = await _read_json_body(request)
    required = ("host", "stack", "latest_digest")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return {"error": f"missing required fields: {missing}"}
    return await pending_update_dismiss_tool(
        host=body["host"],
        stack=body["stack"],
        latest_digest=body["latest_digest"],
    )


async def _api_stacks(request: Request) -> dict[str, Any]:
    """Return the configured hosts + their stacks, for the dropdowns."""
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
    "/api/dismiss":          {"fn": _api_dismiss,          "methods": ("POST",)},
    "/api/stacks":           {"fn": _api_stacks,           "methods": ("GET",)},
}


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read the request body as JSON, with a useful error on bad input."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"body is not valid JSON: {e}") from e


# ----- build_sse_app hook -----
# We don't replace build_sse_app; instead we add WebUIMiddleware
# alongside HealthAndStatusMiddleware. This is done in build_sse_app
# below, exported for use by __main__.
def build_webui_app(base_app: Any, *, get_state: Any = None) -> Any:
    """Wrap ``base_app`` with the WebUI middleware. Pass-through if disabled."""
    base_app.add_middleware(WebUIMiddleware, get_state=get_state)
    return base_app
