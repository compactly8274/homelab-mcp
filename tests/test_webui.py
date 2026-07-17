"""Tests for the WebUI HTTP layer (v0.9.0).

Covers:
- /ui/ serves index.html
- /ui/static/style.css + app.js are served
- /api/dashboard returns a JSON body (delegates to the tool)
- /api/apply POST calls apply_update_tool with the right params
- /api/container_action POST calls container_action_tool
- /api/preflight GET calls preflight_check_tool
- WebUI disabled via HOMELAB_MCP_WEBUI_ENABLED=false -> 404 on /ui
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _enable_webui(monkeypatch):
    """Make sure the WebUI is enabled in tests (the daemon may set
    the env var to false in production to opt out)."""
    monkeypatch.setenv("HOMELAB_MCP_WEBUI_ENABLED", "true")


def _build_app():
    """Build a Starlette TestClient against a minimal app that
    uses our WebUIMiddleware. We pass through to a no-op base."""
    from starlette.applications import Starlette

    from homelab_mcp.webui import WebUIMiddleware

    async def base(scope, receive, send):
        msg = {"type": "http.response.start", "status": 200, "headers": []}
        await send(msg)
        await send({"type": "http.response.body", "body": b"passthrough"})

    app = Starlette(debug=False)
    app.add_middleware(WebUIMiddleware, get_state=None)
    # Replace the default router with our minimal base by adding it
    # to the app's middleware stack. Actually, Starlette routes
    # requests through middleware, then through the router, which
    # is empty. So our middleware will see all requests, and
    # the wrapped app is the empty router.
    return app


# ---------- static ----------


def test_webui_serves_index() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/ui/")
    assert r.status_code == 200
    assert "<title>homelab-mcp</title>" in r.text


def test_webui_serves_index_at_root() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/ui")
    assert r.status_code == 200
    assert "<title>homelab-mcp</title>" in r.text


def test_webui_serves_static_css() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/ui/static/style.css")
    assert r.status_code == 200
    assert "homelab-mcp webui" in r.text


def test_webui_serves_static_js() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/ui/static/app.js")
    assert r.status_code == 200
    assert "loadDashboard" in r.text


def test_webui_static_path_traversal_blocked() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/ui/static/../secret")
    # Either 400 (our explicit check) or 404 (cleaned by starlette
    # before reaching us). Both are acceptable. 200 with /secret
    # contents would be the bug.
    assert r.status_code in (400, 404)


# ---------- api ----------


def test_api_dashboard_returns_json() -> None:
    from starlette.testclient import TestClient
    fake_dashboard = {
        "summary": {"total_hosts": 2, "reachable": 2, "unhealthy": 0,
                    "total_containers": 10, "running": 8, "stopped": 2,
                    "total_stacks": 5},
        "hosts": [],
        "top_problems": [],
        "generated_at": "2026-07-16T00:00:00Z",
    }
    app = _build_app()
    with patch("homelab_mcp.tools.dashboard.health_dashboard_tool",
               AsyncMock(return_value=fake_dashboard)):
        with TestClient(app) as c:
            r = c.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total_containers"] == 10


def test_api_preflight_get() -> None:
    from starlette.testclient import TestClient
    fake_verdict = {"safe": True, "blockers": [], "warnings": [], "info": []}
    app = _build_app()
    with patch("homelab_mcp.tools.preflight.preflight_check_tool",
               AsyncMock(return_value=fake_verdict)) as m:
        with TestClient(app) as c:
            r = c.get("/api/preflight?host=truenas&stack=plex&action=apply_update")
    assert r.status_code == 200
    assert m.called
    # Tool was called with the right args
    kwargs = m.call_args.kwargs
    assert kwargs["host"] == "truenas"
    assert kwargs["stack"] == "plex"
    assert kwargs["action"] == "apply_update"


def test_api_apply_post_calls_tool() -> None:
    from starlette.testclient import TestClient
    fake_result = {"action": "applied", "verdict": {"risk": "SAFE"}}
    app = _build_app()
    with patch("homelab_mcp.tools.apply_update.apply_update_tool",
               AsyncMock(return_value=fake_result)) as m:
        with TestClient(app) as c:
            r = c.post("/api/apply",
                       json={"host": "truenas", "stack": "plex", "dry_run": True})
    assert r.status_code == 200
    assert m.called
    kwargs = m.call_args.kwargs
    assert kwargs["host"] == "truenas"
    assert kwargs["stack"] == "plex"
    assert kwargs["dry_run"] is True
    # require_approval defaults to True
    assert kwargs["require_approval"] is True


def test_api_apply_post_missing_fields() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.post("/api/apply", json={"host": "truenas"})  # no stack
    assert r.status_code == 200  # handler returns {"error": ...}, not 400
    assert "missing required fields" in r.json()["error"]


def test_api_container_action_post() -> None:
    from starlette.testclient import TestClient
    fake_result = {"action": "applied", "kind": "container",
                   "container": "plex", "exit_code": 0, "duration_ms": 1234}
    app = _build_app()
    with patch("homelab_mcp.tools.container_action.container_action_tool",
               AsyncMock(return_value=fake_result)) as m:
        with TestClient(app) as c:
            r = c.post("/api/container_action",
                       json={"host": "truenas", "target": "plex", "action": "restart"})
    assert r.status_code == 200
    assert m.called
    kwargs = m.call_args.kwargs
    assert kwargs["target"] == "plex"
    assert kwargs["action"] == "restart"


def test_api_unknown_path_returns_404() -> None:
    from starlette.testclient import TestClient
    app = _build_app()
    with TestClient(app) as c:
        r = c.get("/api/this_does_not_exist")
    assert r.status_code == 404


# ---------- opt-out ----------


def test_webui_disabled_returns_404_on_ui(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_WEBUI_ENABLED", "false")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from homelab_mcp.webui import WebUIMiddleware

    async def base(scope, receive, send):
        msg = {"type": "http.response.start", "status": 200, "headers": []}
        await send(msg)
        await send({"type": "http.response.body", "body": b"passthrough"})

    app = Starlette(debug=False)
    app.add_middleware(WebUIMiddleware, get_state=None)
    with TestClient(app) as c:
        r = c.get("/ui/")
    # When disabled, the middleware passes through to the empty
    # router which returns 404.
    assert r.status_code == 404
