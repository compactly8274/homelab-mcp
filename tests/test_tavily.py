
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_mcp.tools.tavily import (
    _normalize_tavily_response,
    _normalize_tavily_result,
    _search_tavily,
    tavily_search,
)


def test_normalize_tavily_result_minimal():
    r = {"url": "https://example.com", "title": "Example", "content": "hello"}
    out = _normalize_tavily_result(r)
    assert out["url"] == "https://example.com"
    assert out["title"] == "Example"
    assert out["content"] == "hello"
    assert out["engine"] == "tavily"
    assert out["engines"] == ["tavily"]
    assert out["category"] == "general"


def test_normalize_tavily_response_trims_to_limit():
    payload = {
        "query": "q",
        "results": [{"url": f"https://x{i}.com", "title": str(i), "content": "c"} for i in range(10)],
    }
    out = _normalize_tavily_response(payload, "q", 3)
    assert out["number_of_results"] == 3
    assert len(out["results"]) == 3
    assert out["source"] == "tavily"


@pytest.mark.asyncio
async def test_search_tavily_returns_none_without_key():
    with patch("homelab_mcp.tools.tavily._tavily_api_key", return_value=""):
        out = await _search_tavily("hello")
        assert out is None


@pytest.mark.asyncio
async def test_search_tavily_happy_path():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "query": "hello",
        "results": [{"url": "https://example.com", "title": "Ex", "content": "body"}],
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

    with patch("homelab_mcp.tools.tavily._tavily_api_key", return_value="fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            out = await _search_tavily("hello", limit=1)

    assert out is not None
    assert out["number_of_results"] == 1
    assert out["results"][0]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_tavily_search_rejects_empty_query():
    out = await tavily_search("")
    assert out["error"] == "query must be non-empty"


@pytest.mark.asyncio
async def test_tavily_search_rejects_missing_key():
    with patch("homelab_mcp.tools.tavily._tavily_api_key", return_value=""):
        out = await tavily_search("hello")
        assert "HOMELAB_MCP_TAVILY_API_KEY" in out["error"]
