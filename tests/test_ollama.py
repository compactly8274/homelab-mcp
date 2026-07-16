"""Tests for the Ollama integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from httpx import HTTPStatusError, Request, Response

from homelab_mcp.tools.ollama import (
    ollama_list_models,
    ollama_list_running,
    ollama_show_model,
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
