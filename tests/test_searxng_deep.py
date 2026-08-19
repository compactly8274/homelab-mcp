"""Tests for deep_search and quick_search tools."""

from __future__ import annotations

import asyncio
import json as _json
from unittest.mock import MagicMock, patch

from homelab_mcp.tools.searxng import deep_search, quick_search


def _mock_response(json_data: dict, status_code: int = 200, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.text = text
    r.content = text.encode("utf-8")
    r.url = "https://example.com/page"
    r.encoding = "utf-8"
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response

        req = Request("GET", "http://x")
        r.raise_for_status.side_effect = HTTPStatusError(
            "error", request=req, response=Response(status_code, request=req)
        )
    return r


class FakeAsyncClient:
    """httpx.AsyncClient mock that routes by method/URL."""

    def __init__(self, *args, **kwargs):
        self._calls: list[tuple[str, str]] = []
        self._handlers: dict[tuple[str, str], object] = {}

    def route(self, method: str, prefix: str, response: object) -> FakeAsyncClient:
        self._handlers[(method, prefix)] = response
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url: str, **kwargs):
        self._calls.append(("POST", url))
        for (method, prefix), resp in self._handlers.items():
            if method == "POST" and url.startswith(prefix):
                return resp
        raise RuntimeError(f"unexpected POST {url}")

    async def get(self, url: str, **kwargs):
        self._calls.append(("GET", url))
        for (method, prefix), resp in self._handlers.items():
            if method == "GET" and url.startswith(prefix):
                return resp
        raise RuntimeError(f"unexpected GET {url}")


def _extract_synthesis_answer(synthesis: str) -> str:
    """Return the answer portion after the Model/Duration header."""
    # Synthesis format: "Model: <name>\nDuration: <ms>ms\n\n<answer>"
    parts = synthesis.split("\n\n", 1)
    return parts[-1] if parts else synthesis


def test_quick_search_rejects_empty_query() -> None:
    assert asyncio.run(quick_search("")) == {"error": "query must be non-empty"}


def test_quick_search_forwards_to_searxng() -> None:
    fake_json = {
        "query": "python asyncio",
        "number_of_results": 42,
        "results": [
            {
                "url": "https://docs.python.org/3/library/asyncio.html",
                "title": "asyncio — Asynchronous I/O",
                "content": "Python asyncio docs",
                "engine": "google",
                "engines": ["google"],
                "category": "general",
                "score": 1.0,
            }
        ],
        "suggestions": [],
        "unresponsive_engines": [],
    }
    client = FakeAsyncClient().route("POST", "http://192.168.1.7:8080/search", _mock_response(fake_json))

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", lambda *a, **kw: client):
        result = asyncio.run(quick_search("python asyncio"))

    assert result["query"] == "python asyncio"
    assert result["number_of_results"] == 42
    assert result["results"][0]["title"] == "asyncio — Asynchronous I/O"


def test_quick_search_with_full_pages() -> None:
    fake_json = {
        "query": "hello",
        "number_of_results": 1,
        "results": [
            {"url": "https://example.com", "title": "Example", "content": "snippet", "engine": "google"}
        ],
        "suggestions": [],
        "unresponsive_engines": [],
    }
    html = "<html><title>Example Domain</title><body><p>Hello world</p></body></html>"
    client = (
        FakeAsyncClient()
        .route("POST", "http://192.168.1.7:8080/search", _mock_response(fake_json))
        .route("GET", "https://example.com", _mock_response({}, text=html))
    )

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", lambda *a, **kw: client):
        result = asyncio.run(quick_search("hello", fetch_full_pages=True))

    page = result["results"][0].get("page", {})
    assert page.get("ok") is True
    assert "Hello world" in page.get("text", "")


def test_deep_search_rejects_empty_query() -> None:
    assert asyncio.run(deep_search("")) == {"error": "query must be non-empty"}


def test_deep_search_composes_synthesis() -> None:
    ollama_calls: list[int] = []

    async def fake_call_ollama(client, model, prompt, *, system=None, temperature=0.2, json_mode=False, timeout=None):
        ollama_calls.append(1)
        if len(ollama_calls) == 1:
            # decomposer
            return {"response": _json.dumps({"subqueries": ["python asyncio tutorial", "asyncio gather"]}), "parsed_response": {"subqueries": ["python asyncio tutorial", "asyncio gather"]}}
        # curator
        return {"response": "Use `asyncio.gather` to run coroutines concurrently [1].", "total_duration": 123000000}

    async def fake_search_searxng(client, query, **kwargs):
        if "asyncio tutorial" in query:
            return {
                "results": [
                    {
                        "url": "https://docs.python.org/3/library/asyncio-task.html",
                        "title": "Coroutines and Tasks",
                        "content": "How to use asyncio.gather",
                        "engine": "google",
                    }
                ]
            }
        return {
            "results": [
                {
                    "url": "https://realpython.com/async-python/",
                    "title": "Async IO in Python",
                    "content": "Concurrency tutorial",
                    "engine": "duckduckgo",
                }
            ]
        }

    with patch("homelab_mcp.tools.searxng._call_ollama", fake_call_ollama), patch("homelab_mcp.tools.searxng._search_searxng", fake_search_searxng):
        result = asyncio.run(deep_search("how to run coroutines concurrently in python"))

    assert "error" not in result
    assert result["subqueries"] == ["python asyncio tutorial", "asyncio gather"]
    assert result["result_count"] == 2
    synthesis = result["synthesis"]
    assert isinstance(synthesis, str)
    answer = _extract_synthesis_answer(synthesis)
    assert answer
    assert "asyncio.gather" in answer


def test_deep_search_dedups_identical_urls() -> None:
    ollama_calls: list[int] = []

    async def fake_call_ollama(client, model, prompt, *, system=None, temperature=0.2, json_mode=False, timeout=None):
        ollama_calls.append(1)
        if len(ollama_calls) == 1:
            return {"response": _json.dumps({"subqueries": ["q1", "q2"]}), "parsed_response": {"subqueries": ["q1", "q2"]}}
        return {"response": "ok", "total_duration": 0}

    shared = {"url": "https://shared.example", "title": "Shared Result", "content": "content", "engine": "google"}

    async def fake_search_searxng(client, query, **kwargs):
        return {"results": [shared]}

    with patch("homelab_mcp.tools.searxng._call_ollama", fake_call_ollama), patch("homelab_mcp.tools.searxng._search_searxng", fake_search_searxng):
        result = asyncio.run(deep_search("x"))

    assert result["result_count"] == 1


def test_deep_search_falls_back_when_decomposer_returns_no_subqueries() -> None:
    ollama_calls: list[int] = []

    async def fake_call_ollama(client, model, prompt, *, system=None, temperature=0.2, json_mode=False, timeout=None):
        ollama_calls.append(1)
        if len(ollama_calls) == 1:
            return {"response": _json.dumps({"subqueries": []}), "parsed_response": {"subqueries": []}}
        return {"response": "fallback synthesis", "total_duration": 0}

    async def fake_search_searxng(client, query, **kwargs):
        return {"results": [{"url": "https://example.com", "title": "T", "content": "c", "engine": "google"}]}

    with patch("homelab_mcp.tools.searxng._call_ollama", fake_call_ollama), patch("homelab_mcp.tools.searxng._search_searxng", fake_search_searxng):
        result = asyncio.run(deep_search("original question"))

    assert result["subqueries"] == ["original question"]
    answer = _extract_synthesis_answer(result["synthesis"])
    assert answer == "fallback synthesis"
