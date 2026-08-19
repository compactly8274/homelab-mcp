"""Tests for the deep SearXNG integration (curation)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homelab_mcp.tools.searxng import quick_search


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response

        req = Request("GET", "http://x")
        r.raise_for_status.side_effect = HTTPStatusError(
            "error", request=req, response=Response(status_code, request=req)
        )
    return r


async def test_quick_search_returns_results() -> None:
    """quick_search surfaces normalized search results."""
    fake_json = {
        "query": "python asyncio",
        "number_of_results": 42,
        "results": [
            {
                "url": "https://example.com/asyncio",
                "title": "Async IO",
                "content": "async guide",
                "engine": "duckduckgo",
                "engines": ["duckduckgo"],
                "category": "general",
                "score": 0.9,
            }
        ],
        "suggestions": [],
        "unresponsive_engines": [],
    }

    captured: dict = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, params=None, **kwargs):
            captured["url"] = url
            return _mock_response({"instance_name": "TestSearX", "engines": []})

        async def post(self, url, data=None, headers=None, **kwargs):
            captured.update(data or {})
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", lambda *a, **kw: _Client()):
        from homelab_mcp.tools import searxng as _searxng_mod

        _searxng_mod._searxng_config_cache = None
        _searxng_mod._searxng_config_cache_ts = 0.0
        _searxng_mod._enabled_engines_cache = None
        _searxng_mod._enabled_engines_cache_ts = 0.0
        result = await quick_search("python asyncio")

    assert result["query"] == "python asyncio"
    assert result["number_of_results"] == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://example.com/asyncio"
    assert "results" in result


async def test_quick_search_rejects_empty_query() -> None:
    result = await quick_search("")
    assert "error" in result
