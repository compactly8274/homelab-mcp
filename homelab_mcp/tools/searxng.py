"""SearXNG-backed web search tools.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Endpoint is configured via ``HOMELAB_MCP_SEARXNG_URL``
(default ``http://192.168.1.7:8080``). The endpoint must accept
``format=json`` and return SearXNG's JSON shape; some instances disable
JSON output and will return HTML even with ``format=json`` -- in that
case the tool will fail with a parse error and the operator needs to
add ``json`` to ``search.formats`` in settings.yml.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings

log = logging.getLogger(__name__)


_DEFAULT_SEARXNG_URL = "http://192.168.1.7:8080"
_TIMEOUT = 30.0


def _base_url() -> str:
    s = _get_settings()
    return (s.searxng_url or _DEFAULT_SEARXNG_URL).rstrip("/")


def _normalize_results(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Flatten SearXNG JSON into a list of result dicts."""
    out: list[dict[str, Any]] = []
    for r in payload.get("results", [])[:limit]:
        out.append(
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "engine": r.get("engine", ""),
                "engines": r.get("engines", []),
                "category": r.get("category", ""),
                "publishedDate": r.get("publishedDate"),
                "score": r.get("score"),
            }
        )
    return out


@mcp.tool()
async def searxng_search(
    query: str,
    category: str = "general",
    engines: str = "",
    language: str = "en",
    limit: int = 10,
) -> dict[str, Any]:
    """Web search via the local SearXNG instance.

    Args:
        query: search query string.
        category: SearXNG category (general, images, news, videos, files, it, science, map).
        engines: comma-separated engine names to restrict to (e.g. "google,bing,duckduckgo").
            Empty string means "use SearXNG's default engines for the category".
        language: two-letter language code (default "en").
        limit: max number of results to return (1-50, default 10).

    Returns:
        dict with ``query``, ``category``, ``number_of_results``,
        ``results`` (list of {url, title, content, engine, engines, ...}),
        ``suggestions`` (SearXNG's "did you mean" list), ``unresponsive_engines``.
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    limit = max(1, min(50, int(limit)))
    params: dict[str, str] = {
        "q": query.strip(),
        "format": "json",
        "category": category,
        "language": language,
    }
    if engines.strip():
        params["engines"] = engines.strip()
    url = f"{_base_url()}/search"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, data=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as e:
        log.exception("searxng search failed")
        return {"error": f"searxng request failed: {e}", "url": url}
    except Exception as e:
        log.exception("searxng search parse failed")
        return {"error": f"searxng returned non-JSON or invalid JSON: {e}", "url": url}
    return {
        "query": payload.get("query", query),
        "category": category,
        "number_of_results": payload.get("number_of_results", 0),
        "results": _normalize_results(payload, limit),
        "suggestions": payload.get("suggestions", []),
        "unresponsive_engines": [
            e[0] for e in payload.get("unresponsive_engines", []) if isinstance(e, (list, tuple))
        ],
    }


@mcp.tool()
async def searxng_suggestions(query: str) -> dict[str, Any]:
    """Fetch SearXNG's query-completion suggestions for a partial query.

    Returns a dict with ``query`` and ``suggestions`` (list of strings).
    If the SearXNG instance has autocomplete disabled (``autocomplete``
    empty in ``/config``), the response will contain ``error`` with
    a clear explanation; callers should treat that as "no suggestions
    available" rather than a failure.
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    url = f"{_base_url()}/suggestions"
    params = {"q": query.strip(), "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {
            "error": f"searxng suggestions request failed: {e}",
            "note": "SearXNG may have autocomplete disabled in settings.yml (autocomplete: ''). This is non-fatal -- web_search and engines tools still work.",
            "url": url,
        }
    suggestions = data.get("suggestions", []) if isinstance(data, dict) else data
    if not isinstance(suggestions, list):
        suggestions = []
    return {"query": query, "suggestions": [str(s) for s in suggestions]}


@mcp.tool()
async def searxng_engines() -> dict[str, Any]:
    """List the engines currently enabled on the local SearXNG instance.

    Returns a dict with ``engines`` (list of {name, category, language, enabled, ...})
    and ``categories`` (list of category names SearXNG supports).
    Useful for discovering what the ``engines`` and ``category`` parameters accept.
    """
    url = f"{_base_url()}/config"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"searxng config request failed: {e}"}
    engines_raw = data.get("engines", [])
    engines_out: list[dict[str, Any]] = []
    for meta in engines_raw:
        if not isinstance(meta, dict):
            continue
        name = meta.get("name", "")
        if not name:
            continue
        # categories in /config is a list; in /engine-name info it's a single str.
        cats = meta.get("categories", [])
        category = str(cats[0]) if isinstance(cats, list) and cats else (str(cats) if cats else "")
        languages = meta.get("languages", [])
        language = str(languages[0]) if isinstance(languages, list) and languages else (str(languages) if languages else "")
        engines_out.append(
            {
                "name": name,
                "category": category,
                "language": language,
                "enabled": bool(meta.get("enabled", False)),
                "shortcut": meta.get("shortcut", ""),
                "timeout": meta.get("timeout"),
            }
        )
    engines_out.sort(key=lambda e: (e["category"], e["name"]))
    categories_raw = data.get("categories", [])
    if isinstance(categories_raw, list):
        categories = sorted(str(c) for c in categories_raw)
    elif isinstance(categories_raw, dict):
        categories = sorted(categories_raw.keys())
    else:
        categories = []
    return {
        "engines": engines_out,
        "categories": categories,
        "instance_name": data.get("instance_name", ""),
    }
