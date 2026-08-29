"""Tavily-backed web search tool with SearXNG fallback.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. The Tavily API key is read from
``HOMELAB_MCP_TAVILY_API_KEY``. When the key is unset or the Tavily
call fails, requests transparently fall back to the local SearXNG
implementation in ``homelab_mcp.tools.searxng``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_TAVILY_TIMEOUT = 20.0

_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


def _tavily_api_key() -> str:
    return _get_settings().tavily_api_key.strip()


def _normalize_tavily_result(r: dict[str, Any]) -> dict[str, Any]:
    """Convert a Tavily result row into the SearXNG-normalized shape."""
    return {
        "url": r.get("url", ""),
        "title": r.get("title", ""),
        "content": r.get("content", ""),
        "engine": "tavily",
        "engines": ["tavily"],
        "category": "general",
        "publishedDate": None,
        "score": r.get("score"),
    }


def _normalize_tavily_response(
    payload: dict[str, Any], query: str, limit: int
) -> dict[str, Any]:
    """Return a SearXNG-shaped dict from a Tavily JSON response."""
    results: list[dict[str, Any]] = []
    for r in payload.get("results", []):
        if not isinstance(r, dict):
            continue
        results.append(_normalize_tavily_result(r))
        if len(results) >= limit:
            break
    return {
        "query": payload.get("query", query),
        "category": "general",
        "number_of_results": len(results),
        "results": results,
        "suggestions": [],
        "unresponsive_engines": [],
        "source": "tavily",
    }


async def _search_tavily(
    query: str,
    *,
    limit: int = 10,
    search_depth: str = "basic",
    include_answer: bool = False,
    include_raw_content: bool = False,
) -> dict[str, Any] | None:
    """Call Tavily and return normalized results, or None if unavailable/failed."""
    key = _tavily_api_key()
    if not key:
        return None
    body: dict[str, Any] = {
        "api_key": key,
        "query": query.strip(),
        "search_depth": search_depth,
        "max_results": max(1, min(50, int(limit))),
        "include_answer": include_answer,
        "include_images": False,
        "include_raw_content": include_raw_content,
    }
    try:
        async with httpx.AsyncClient(timeout=_TAVILY_TIMEOUT) as client:
            r = await client.post(
                _TAVILY_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPStatusError as e:
        log.warning("Tavily HTTP error: %s - %s", e.response.status_code, e.response.text[:200])
        return None
    except Exception as e:
        log.warning("Tavily request failed: %s", e)
        return None
    return _normalize_tavily_response(payload, query, limit)


async def _search_primary(
    query: str,
    *,
    category: str = "general",
    engines: str = "",
    language: str = "en",
    limit: int = 10,
    search_depth: str = "basic",
    include_answer: bool = False,
) -> dict[str, Any]:
    """Try Tavily first, then fall back to SearXNG.

    Preserves the existing SearXNG fallback behaviour including category
    and engine overrides. This is the internal helper used by the primary
    ``quick_search`` and ``deep_search`` tools.
    """
    from homelab_mcp.tools.searxng import _SEARXNG_TIMEOUT, _search_searxng

    tavily_result = await _search_tavily(
        query,
        limit=limit,
        search_depth=search_depth,
        include_answer=include_answer,
    )
    if tavily_result is not None and tavily_result.get("results"):
        log.info(
            "Tavily primary search returned %d results for %r",
            tavily_result["number_of_results"],
            query,
        )
        return tavily_result

    log.info("Tavily unavailable or empty; falling back to SearXNG for %r", query)
    try:
        async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as client:
            return await _search_searxng(
                client,
                query,
                category=category,
                engines=engines,
                language=language,
                limit=limit,
            )
    except Exception:
        log.exception("SearXNG fallback failed")
        if tavily_result is not None:
            return tavily_result
        raise


@mcp.tool()
async def tavily_search(
    query: str,
    limit: int = 10,
    search_depth: str = "basic",
    include_answer: bool = False,
) -> dict[str, Any]:
    """Search the web via Tavily.

    Requires ``HOMELAB_MCP_TAVILY_API_KEY`` to be set. Returns the same
    result shape as ``searxng_search`` so callers can switch providers
    without re-parsing.

    Args:
        query: search query string.
        limit: max results to return (1-50, default 10).
        search_depth: ``basic`` (fast, cheaper) or ``advanced`` (deeper, more tokens).
        include_answer: if true, ask Tavily to also return a short generated answer.

    Returns:
        dict with ``query``, ``category``, ``number_of_results``,
        ``results`` (list of {url, title, content, engine, ...}),
        ``suggestions``, ``unresponsive_engines``, and ``answer`` if requested.
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    key = _tavily_api_key()
    if not key:
        return {
            "error": "HOMELAB_MCP_TAVILY_API_KEY is not configured",
            "hint": "Add the key to the homelab-mcp .env and restart the container.",
        }
    result = await _search_tavily(
        query.strip(),
        limit=max(1, min(50, int(limit))),
        search_depth=search_depth if search_depth in ("basic", "advanced") else "basic",
        include_answer=include_answer,
    )
    if result is None:
        return {"error": "Tavily request failed"}
    return result
