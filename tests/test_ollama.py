"""Tests for the Ollama integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from httpx import HTTPStatusError, Request, Response

from homelab_mcp.tools.ollama import (
    ollama_delete_model,
    ollama_list_models,
    ollama_list_running,
    ollama_pull_model,
    ollama_show_model,
    ollama_show_running,
    ollama_unload_all,
    ollama_unload_model,
    ollama_version,
)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        req = Request("GET", "http://x")
        r.raise_for_status.side_effect = HTTPStatusError(
            "err", request=req, response=Response(status_code, request=req)
        )
    return r


def test_ollama_list_models_normalizes_size() -> None:
    fake = {
        "models": [
            {
                "name": "llama3.2:3b",
                "size": 3_213_233_408,
                "modified_at": "2025-01-01",
                "digest": "abc",
                "details": {"family": "llama", "parameter_size": "3B", "quantization_level": "Q4_0"},
            }
        ]
    }

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _mock_response(fake)

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_list_models())
    assert r["count"] == 1
    assert r["models"][0]["name"] == "llama3.2:3b"
    assert r["models"][0]["size_gb"] == 2.99  # 3213233408 / 2^30
    assert r["models"][0]["family"] == "llama"


def test_ollama_list_running_handles_empty() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _mock_response({"models": []})

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_list_running())
    assert r["count"] == 0
    assert r["models"] == []


def test_ollama_unload_model_rejects_empty_name() -> None:
    import asyncio
    r = asyncio.run(ollama_unload_model(""))
    assert "error" in r
    assert r.get("unloaded") is None or r.get("unloaded") is False


def test_ollama_unload_model_sends_keep_alive_zero() -> None:
    captured: dict = {}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _mock_response({"response": "ok"})

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_unload_model("llama3.2:3b"))
    assert r["unloaded"] is True
    assert captured["json"]["model"] == "llama3.2:3b"
    assert captured["json"]["keep_alive"] == 0


def test_ollama_unload_all_with_no_running_models() -> None:
    """If no models are loaded, unload_all is a no-op (not a failure)."""

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _mock_response({"models": []})

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_unload_all())
    assert r["unloaded_count"] == 0
    assert r["failed"] == []
    assert r["total_running"] == 0


def test_ollama_show_model_rejects_empty_name() -> None:
    import asyncio
    r = asyncio.run(ollama_show_model(""))
    assert "error" in r


def test_ollama_version_returns_version_string() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _mock_response({"version": "0.32.0"})

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_version())
    assert r["version"] == "0.32.0"


# ----------------------------- show_running -----------------------------


def test_ollama_show_running_joins_tags_and_ps() -> None:
    """Tags + ps should be merged; loaded models get vram + expires_at."""
    tags_payload = {
        "models": [
            {
                "name": "llama3.2:3b",
                "size": 2_000_000_000,
                "digest": "abc",
                "details": {"family": "llama", "parameter_size": "3B", "quantization_level": "Q4_0"},
            },
            {
                "name": "qwen2.5:14b",
                "size": 9_000_000_000,
                "digest": "def",
                "details": {"family": "qwen", "parameter_size": "14B", "quantization_level": "Q4_K_M"},
            },
        ]
    }
    ps_payload = {
        "models": [
            {"name": "llama3.2:3b", "size": 2_000_000_000, "size_vram": 1_500_000_000, "expires_at": "2099-01-01"}
        ]
    }

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            if url.endswith("/api/tags"):
                return _mock_response(tags_payload)
            if url.endswith("/api/ps"):
                return _mock_response(ps_payload)
            raise AssertionError(f"unexpected url: {url}")

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_show_running())
    assert r["total_count"] == 2
    assert r["loaded_count"] == 1
    assert r["vram_total_gb"] == round(1_500_000_000 / (1024**3), 2)
    by_name = {m["name"]: m for m in r["models"]}
    assert by_name["llama3.2:3b"]["loaded"] is True
    assert by_name["llama3.2:3b"]["size_vram_gb"] == round(1_500_000_000 / (1024**3), 2)
    assert by_name["llama3.2:3b"]["family"] == "llama"
    assert by_name["qwen2.5:14b"]["loaded"] is False
    assert by_name["qwen2.5:14b"]["size_vram_gb"] == 0.0


def test_ollama_show_running_handles_no_loaded_models() -> None:
    """If /api/ps is empty, every model should still appear with loaded=False."""

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            if url.endswith("/api/tags"):
                return _mock_response({"models": [{"name": "x", "size": 1000}]})
            if url.endswith("/api/ps"):
                return _mock_response({"models": []})
            raise AssertionError(url)

    with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(ollama_show_running())
    assert r["total_count"] == 1
    assert r["loaded_count"] == 0
    assert r["vram_total_gb"] == 0.0
    assert r["models"][0]["loaded"] is False


# ----------------------------- pull_model -------------------------------


def test_ollama_pull_model_refuses_when_disabled() -> None:
    """Default config: pull is blocked."""
    # `clear=True` so we know the env var is truly unset
    with patch.dict("os.environ", {}, clear=True):
        from homelab_mcp.tools import ollama as ollama_mod
        ollama_mod._cached_settings = None
        import asyncio
        r = asyncio.run(ollama_pull_model("llama3.2:3b"))
    assert r["pulled"] is False
    assert "HOMELAB_MCP_OLLAMA_ALLOW_PULL" in r["error"]


def test_ollama_pull_model_succeeds_when_enabled() -> None:
    """With the env var set, the tool should POST to /api/pull."""
    captured: dict = {}

    class _C:
        def __init__(self, *a, **k):
            self._timeout = k.get("timeout")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _mock_response({"status": "success"})

    with patch.dict("os.environ", {"HOMELAB_MCP_OLLAMA_ALLOW_PULL": "true"}, clear=True):
        from homelab_mcp.tools import ollama as ollama_mod
        ollama_mod._cached_settings = None
        with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
            import asyncio
            r = asyncio.run(ollama_pull_model("llama3.2:3b", insecure=True))
    assert r["pulled"] is True
    assert captured["url"].endswith("/api/pull")
    assert captured["json"]["model"] == "llama3.2:3b"
    assert captured["json"]["insecure"] is True
    assert captured["json"]["stream"] is False


def test_ollama_pull_model_rejects_empty_name() -> None:
    with patch.dict("os.environ", {"HOMELAB_MCP_OLLAMA_ALLOW_PULL": "true"}, clear=True):
        from homelab_mcp.tools import ollama as ollama_mod
        ollama_mod._cached_settings = None
        import asyncio
        r = asyncio.run(ollama_pull_model(""))
    assert r["pulled"] is False
    assert "non-empty" in r["error"]


# ----------------------------- delete_model -----------------------------


def test_ollama_delete_model_refuses_when_disabled() -> None:
    import asyncio
    r = asyncio.run(ollama_delete_model("llama3.2:3b"))
    assert r["deleted"] is False
    assert "HOMELAB_MCP_OLLAMA_ALLOW_DELETE" in r["error"]


def test_ollama_delete_model_unloads_first_when_loaded() -> None:
    """If the target is in /api/ps, the tool should unload before deleting."""
    ps_payload = {"models": [{"name": "llama3.2:3b"}]}
    captured: dict = {}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            captured.setdefault("gets", []).append(url)
            if url.endswith("/api/ps"):
                return _mock_response(ps_payload)
            return _mock_response({"models": []})
        async def post(self, url, json=None):
            captured.setdefault("posts", []).append((url, json))
            return _mock_response({"response": ""})
        async def request(self, method, url, json=None):
            captured.setdefault("requests", []).append((method, url, json))
            return _mock_response({})

    with patch.dict("os.environ", {"HOMELAB_MCP_OLLAMA_ALLOW_DELETE": "true"}, clear=True):
        from homelab_mcp.tools import ollama as ollama_mod
        ollama_mod._cached_settings = None
        with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
            import asyncio
            r = asyncio.run(ollama_delete_model("llama3.2:3b"))
    assert r["deleted"] is True
    # unload: POST /api/generate with keep_alive=0
    unload_posts = [p for p in captured.get("posts", []) if "/api/generate" in p[0]]
    assert len(unload_posts) == 1
    assert unload_posts[0][1]["model"] == "llama3.2:3b"
    assert unload_posts[0][1]["keep_alive"] == 0
    # delete: DELETE /api/delete
    assert any(req[1].endswith("/api/delete") and req[2]["model"] == "llama3.2:3b" for req in captured["requests"])


def test_ollama_delete_model_skips_unload_when_not_loaded() -> None:
    """If the model is not in /api/ps, just delete; no unload POST."""
    captured: dict = {}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            if url.endswith("/api/ps"):
                return _mock_response({"models": []})
            return _mock_response({})
        async def post(self, url, json=None):
            captured.setdefault("posts", []).append((url, json))
            return _mock_response({})
        async def request(self, method, url, json=None):
            captured.setdefault("requests", []).append((method, url, json))
            return _mock_response({})

    with patch.dict("os.environ", {"HOMELAB_MCP_OLLAMA_ALLOW_DELETE": "true"}, clear=True):
        from homelab_mcp.tools import ollama as ollama_mod
        ollama_mod._cached_settings = None
        with patch("homelab_mcp.tools.ollama.httpx.AsyncClient", _C):
            import asyncio
            r = asyncio.run(ollama_delete_model("gemma2:9b"))
    assert r["deleted"] is True
    # No unload should have fired
    unload_posts = [p for p in captured.get("posts", []) if "/api/generate" in p[0]]
    assert unload_posts == []
