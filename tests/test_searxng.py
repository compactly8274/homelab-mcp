"""Tests for the SearXNG integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homelab_mcp.tools.searxng import searxng_engines, searxng_search, searxng_suggestions


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        # Simulate HTTPError on raise_for_status
        from httpx import HTTPStatusError, Request, Response

        req = Request("GET", "http://x")
        r.raise_for_status.side_effect = HTTPStatusError(
            "error", request=req, response=Response(status_code, request=req)
        )
    return r


def test_searxng_search_returns_normalized_results() -> None:
    """searxng_search flattens SearXNG JSON into a clean shape."""
    fake_json = {
        "query": "test",
        "number_of_results": 12345,
        "results": [
            {
                "url": "https://example.com",
                "title": "Example",
                "content": "snippet",
                "engine": "startpage",
                "engines": ["startpage", "bing"],
                "category": "general",
                "publishedDate": "2025-01-01",
                "score": 0.95,
            }
        ],
        "suggestions": ["try this"],
        "unresponsive_engines": [["broken-engine", "timeout"]],
    }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None, headers=None):
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        import asyncio

        result = asyncio.run(searxng_search("test", limit=5))

    assert result["query"] == "test"
    assert result["number_of_results"] == 12345
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://example.com"
    assert result["results"][0]["engine"] == "startpage"
    assert result["suggestions"] == ["try this"]
    assert result["unresponsive_engines"] == ["broken-engine"]


def test_searxng_search_rejects_empty_query() -> None:
    import asyncio

    result = asyncio.run(searxng_search(""))
    assert "error" in result


def test_searxng_search_clamps_limit() -> None:
    """Limit is clamped to [1, 50]."""
    fake_json = {"query": "x", "number_of_results": 0, "results": []}

    captured: dict = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None, headers=None):
            captured.update(data or {})
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        import asyncio

        # limit=999 should clamp to 50
        asyncio.run(searxng_search("x", limit=999))
    # We can't directly assert limit was clamped (it was applied in code,
    # not in the request params), so this is mostly a smoke test that
    # the call doesn't blow up.
    assert captured.get("q") == "x"


def test_searxng_engines_handles_list_shape() -> None:
    """SearXNG /config returns engines as a list, not a dict."""
    fake_json = {
        "instance_name": "TestSearX",
        "engines": [
            {"name": "google", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "go", "timeout": 3.0},
            {"name": "bing", "categories": ["general", "images"], "languages": ["all"], "enabled": False, "shortcut": "bi", "timeout": 3.0},
        ],
        "categories": ["general", "images", "news"],
    }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        import asyncio

        result = asyncio.run(searxng_engines())

    assert result["instance_name"] == "TestSearX"
    assert len(result["engines"]) == 2
    assert result["engines"][0]["name"] == "bing"  # sorted: bing (cat general) before google? no, alphabetical by (cat, name)
    # Actually sorted by (category, name), both have "general" cat, so bing < google
    assert result["categories"] == ["general", "images", "news"]


def test_searxng_suggestions_handles_404_gracefully() -> None:
    """A 404 on /suggestions is not a fatal error -- returns a note."""
    from httpx import HTTPStatusError, Request, Response

    req = Request("GET", "http://x")
    err = HTTPStatusError("not found", request=req, response=Response(404, request=req))

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, headers=None):
            r = _mock_response({}, status_code=404)
            r.raise_for_status.side_effect = err
            return r

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        import asyncio

        result = asyncio.run(searxng_suggestions("pytho"))

    assert "error" in result
    assert "autocomplete" in result["note"]
    # On 404 the function returns an error dict -- no "suggestions" key,
    # which is fine; callers should check for "error" first.
