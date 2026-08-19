"""SearXNG-backed web search tools, including deep multi-step search.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Endpoint is configured via ``HOMELAB_MCP_SEARXNG_URL``
(default ``http://192.168.1.7:8080``). The endpoint must accept
``format=json`` and return SearXNG's JSON shape; some instances disable
JSON output and will return HTML even with ``format=json`` -- in that
case the tool will fail with a parse error and the operator needs to
add ``json`` to ``search.formats`` in settings.yml.

Deep search additionally uses an Ollama-compatible endpoint to
decompose the user question into parallel sub-queries and to curate a
synthesized answer from the gathered evidence. By default the tool calls
a local Ollama instance (``HOMELAB_MCP_OLLAMA_URL``), which can act as a
conduit to Ollama Cloud. If ``HOMELAB_MCP_OLLAMA_CLOUD_URL`` and
``HOMELAB_MCP_OLLAMA_CLOUD_API_KEY`` are set, models whose names end in
``:cloud`` (or are otherwise cloud-only) are routed to Ollama Cloud
directly, skipping the local hop.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

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
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_SEARXNG_TIMEOUT = 30.0
_OLLAMA_TIMEOUT = 60.0
_PAGE_TIMEOUT = 8.0
_MAX_BODY_BYTES = 400 * 1024  # 400 KiB cap per page fetch
_EVIDENCE_BUDGET_CHARS = 80_000  # ~20k tokens ceiling for curator context

# Strip most HTML tags, keep a little whitespace between them.
_TAG_RE = re.compile(r"<[^>]+>")
# Collapse whitespace.
_WS_RE = re.compile(r"\s+")

# Query-classification hints for routing searches to the most useful engines.
# The model/LLM can override via explicit category/engines parameters.
_ACADEMIC_KEYWORDS = frozenset(
    "arxiv paper preprint journal article doi pubmed medline"
    " citation review literature meta-analysis"
    " physics cosmology astrophysics astronomy quantum relativity"
    " string theory brane dark matter black hole higgs neutrino"
    " thermodynamics condensed matter plasma spectroscopy"
    " chemistry biochemistry molecular cell biology genome protein"
    " mathematics theorem proof algebra geometry topology analysis"
    " thesis dissertation"
    " chromodynamics gluon confinement quark hadron qcd qed"
    " standard model particle physics electroweak asymptotic freedom"
    " lattice qcd meson baryon fermion boson gauge theory"
    .split()
)

_GENERAL_TECH_KEYWORDS = frozenset(
    "github repository api documentation tutorial howto guide"
    " install configure deploy docker kubernetes linux error"
    " troubleshooting bug issue changelog release notes"
    " computer science algorithm machine learning neural network llm"
    " dataset benchmark conference proceedings"
    .split()
)

# Fast, reliable general engines. Only include engines actually enabled on SearXNG.
_GENERAL_ENGINES = "bing,duckduckgo,wikipedia,wikidata"

# Science engines that are usually fast and accurate on academic queries.
# crossref is excluded because it is disabled in SearXNG settings.
_SCIENCE_ENGINES = "arxiv,google scholar,semantic scholar,crossref,pubmed,pdbe,openairedatasets,openairepublications"

# Local/IT/tech engines. Only include engines actually enabled on SearXNG.
_IT_ENGINES = "github,stackoverflow,askubuntu,superuser,arch linux wiki,docker hub,pypi,gentoo,mankier"

# News engines.
_NEWS_ENGINES = "bing news,google news,duckduckgo news"

_FILES_ENGINES = "bt4g,solidtorrents,piratebay,kickass"

_DEFAULT_ENGINES_BY_CATEGORY: dict[str, str] = {
    "science": _SCIENCE_ENGINES,
    "it": _IT_ENGINES,
    "news": _NEWS_ENGINES,
    "files": _FILES_ENGINES,
    "general": _GENERAL_ENGINES,
}


def _searxng_base_url() -> str:
    s = _get_settings()
    return (s.searxng_url or _DEFAULT_SEARXNG_URL).rstrip("/")


def _ollama_base_url() -> str:
    s = _get_settings()
    return (s.ollama_url or _DEFAULT_OLLAMA_URL).rstrip("/")


def _ollama_cloud_url() -> str | None:
    s = _get_settings()
    return s.ollama_cloud_url.rstrip("/") if s.ollama_cloud_url else None


def _ollama_cloud_api_key() -> str | None:
    s = _get_settings()
    return s.ollama_cloud_api_key or None


# Cache of enabled engine names populated lazily from /config.
_ENABLED_ENGINES_TTL_SECONDS = 300.0  # 5 minutes
_ENABLED_ENGINES_CACHE_ERROR_TTL_SECONDS = 10.0
_enabled_engines_cache: dict[str, str] | None = None
_enabled_engines_cache_ts: float = 0.0
_searxng_config_cache: dict[str, Any] | None = None
_searxng_config_cache_ts: float = 0.0


async def _get_searxng_config(client: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch SearXNG /config with a 5-minute TTL.

    On error we cache an empty dict for only 10 seconds so a transient
    SearXNG hiccup does not leave callers with stale empty data.
    This is the single source of truth used by both ``searxng_engines``
    and ``_get_enabled_engines``.
    """
    global _searxng_config_cache, _searxng_config_cache_ts
    now = time.time()
    if (
        _searxng_config_cache is not None
        and (now - _searxng_config_cache_ts) < _ENABLED_ENGINES_TTL_SECONDS
    ):
        return _searxng_config_cache

    try:
        r = await client.get(
            f"{_searxng_base_url()}/config",
            headers={"Accept": "application/json"},
            timeout=_SEARXNG_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("Failed to fetch SearXNG /config")
        data = {}

    _searxng_config_cache = data
    # Short retry window on errors.
    _searxng_config_cache_ts = now if data else now - (
        _ENABLED_ENGINES_TTL_SECONDS - _ENABLED_ENGINES_CACHE_ERROR_TTL_SECONDS
    )
    return data


async def _get_enabled_engines(client: httpx.AsyncClient, *, force_refresh: bool = False) -> dict[str, str]:
    """Return a mapping of lowercased engine name/shortcut -> canonical name.

    Derives the mapping from the shared /config cache so only one request
    is issued per TTL window. On failure we return an empty mapping, which
    causes engine filtering to strip all requested engines and fall back
    to category defaults.
    """
    global _enabled_engines_cache, _enabled_engines_cache_ts
    now = time.time()
    if (
        not force_refresh
        and _enabled_engines_cache is not None
        and _searxng_config_cache_ts == _enabled_engines_cache_ts
    ):
        return _enabled_engines_cache

    config = await _get_searxng_config(client)
    mapping: dict[str, str] = {}
    engines_list = config.get("engines", [])
    if isinstance(engines_list, dict):
        # Some SearXNG versions expose engines as a dict keyed by name.
        engines_list = list(engines_list.values())
    for e in engines_list:
        if not isinstance(e, dict) or not e.get("enabled"):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        mapping[name.lower()] = name
        shortcut = str(e.get("shortcut", "")).strip()
        if shortcut:
            mapping[shortcut.lower()] = name

    _enabled_engines_cache = mapping
    _enabled_engines_cache_ts = _searxng_config_cache_ts
    return _enabled_engines_cache


def _filter_engines(enabled: dict[str, str], engines: str) -> str:
    """Drop disabled/unknown engine names and resolve shortcuts.

    ``enabled`` is a mapping of lowercased engine name/shortcut to the
    canonical engine name. We do not do substring matching: an unknown
    engine is silently dropped.
    """
    if not engines or not engines.strip():
        return ""
    requested = [e.strip() for e in engines.split(",") if e.strip()]
    canonical: list[str] = []
    seen: set[str] = set()
    for e in requested:
        key = e.lower()
        if key in enabled and key not in seen:
            canonical.append(enabled[key])
            seen.add(key)
    return ",".join(canonical) if canonical else ""


def _is_cloud_model(model: str) -> bool:
    """Return True if the model name should route to Ollama Cloud."""
    return model.endswith(":cloud")


def _normalize_results(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Flatten SearXNG JSON into a list of result dicts."""
    out: list[dict[str, Any]] = []
    for r in payload.get("results", []):
        if not isinstance(r, dict):
            continue
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
        if len(out) >= limit:
            break
    return out


async def _search_searxng(
    client: httpx.AsyncClient,
    query: str,
    *,
    category: str = "general",
    engines: str = "",
    language: str = "en",
    limit: int = 10,
) -> dict[str, Any]:
    """Call the SearXNG ``/search`` JSON endpoint."""
    enabled = await _get_enabled_engines(client)
    filtered_engines = _filter_engines(enabled, engines)
    params: dict[str, str] = {
        "q": query.strip(),
        "format": "json",
        "language": language,
        "count": str(max(1, min(50, int(limit)))),
    }
    # Only send category when engines are also specified, or when the caller
    # explicitly set one. Sending both lets SearXNG validate; sending category
    # alone makes it use category defaults, which is what we want for auto-routing.
    if category and category.strip():
        params["category"] = category.strip()
    if filtered_engines:
        params["engines"] = filtered_engines
    url = f"{_searxng_base_url()}/search"
    r = await client.post(url, data=params, headers={"Accept": "application/json"})
    r.raise_for_status()
    payload = r.json()
    normalized = _normalize_results(payload, limit)
    return {
        "query": payload.get("query", query),
        "category": category,
        "number_of_results": len(normalized),
        "results": normalized,
        "suggestions": payload.get("suggestions", []),
        "unresponsive_engines": [
            (e[0] if isinstance(e, (list, tuple)) else str(e))
            for e in payload.get("unresponsive_engines", [])
            if e
        ],
    }


async def _call_ollama(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    json_mode: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call an Ollama-compatible endpoint and return the parsed response.

    If the model name ends in ``:cloud`` and ``HOMELAB_MCP_OLLAMA_CLOUD_URL``
    plus ``HOMELAB_MCP_OLLAMA_CLOUD_API_KEY`` are configured, the request is
    sent to Ollama Cloud directly using its native ``/api/generate`` shape.
    Otherwise the request is forwarded to the local Ollama instance (which
    itself can be a conduit to Ollama Cloud).
    """
    use_cloud = _is_cloud_model(model)
    cloud_url = _ollama_cloud_url()
    cloud_key = _ollama_cloud_api_key()

    if use_cloud and cloud_url and cloud_key:
        url = f"{cloud_url}/api/generate"
        headers = {"Authorization": f"Bearer {cloud_key}"}
    else:
        url = f"{_ollama_base_url()}/api/generate"
        headers = {}

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"
    r = await client.post(
        url,
        json=payload,
        headers=headers,
        timeout=timeout or _OLLAMA_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    response_text = data.get("response", "")
    if json_mode and response_text:
        try:
            data["parsed_response"] = json.loads(response_text)
        except json.JSONDecodeError:
            data["parsed_response"] = None
    return data


def _strip_html(raw: str) -> str:
    """Best-effort HTML-to-text for page snippets."""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:\s*(?:alpha|beta|rc|release|version))?\b", re.IGNORECASE)


def _classify_query(query: str) -> tuple[str, str, bool]:
    """Classify a query and return (category, engines, fetch_full_pages_default).

    Heuristic routing used only when the caller did not explicitly supply
    ``category`` and ``engines``. The goal is to push academic questions toward
    science engines and code/error/package questions toward IT engines, while
    keeping broad information lookups (release notes, new features, news) on
    general engines where coverage is wider.
    """
    lowered = query.lower()
    # Drop common noise words so "the dark matter review" still matches.
    tokens = set(re.findall(r"[a-z][a-z0-9-]*", lowered))

    academic_hits = len(tokens & _ACADEMIC_KEYWORDS)
    tech_hits = len(tokens & _GENERAL_TECH_KEYWORDS)
    has_version = bool(_VERSION_RE.search(query))

    # Explicit category hints from the query itself.
    if "arxiv" in lowered or "doi" in lowered or "pubmed" in lowered:
        return "science", _SCIENCE_ENGINES, False

    if academic_hits >= 2:
        return "science", _SCIENCE_ENGINES, False

    if any(k in lowered for k in ("latest news", "breaking news", "today")):
        return "news", _NEWS_ENGINES, False

    # Strong IT signals: code hosting, container platforms, error patterns,
    # package names, or "how to" with a technical verb/object.
    strong_it_signals = {"github", "stackoverflow", "docker", "kubernetes", "npm", "pypi", "error", "bug", "issue"}
    if strong_it_signals & tokens or any(k in lowered for k in ("how to install", "how to configure", "how to deploy", "troubleshooting")):
        return "it", _IT_ENGINES, False

    # Software release/version/feature lookups are still tech content unless they
    # explicitly ask for "news". Route them to IT engines when a strong tech
    # keyword is present; otherwise fall through to general.
    if has_version and any(k in lowered for k in ("release", "new features", "changelog", "release notes", "what's new")):
        if tech_hits >= 1:
            return "it", _IT_ENGINES, False
        return "general", _GENERAL_ENGINES, False

    # File-sharing / torrent lookups.
    if any(k in lowered for k in ("torrent", "magnet", "download", "iso", "apk", "crack")):
        return "files", _FILES_ENGINES, False

    if tech_hits >= 2:
        return "it", _IT_ENGINES, False

    return "general", _GENERAL_ENGINES, False


def _maybe_override_category_engines(
    query: str,
    *,
    category: str,
    engines: str,
) -> tuple[str, str]:
    """Apply caller defaults; only classify query if both category and engines are empty."""
    suggested_category, suggested_engines, _ = _classify_query(query)
    final_category = category if category and category.strip() else suggested_category
    if engines and engines.strip():
        final_engines = engines
    else:
        # If caller gave a category, honor its canonical engines. Otherwise fall
        # back to the query-classified default engines.
        final_engines = _DEFAULT_ENGINES_BY_CATEGORY.get(final_category, suggested_engines)
    return final_category, final_engines


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_chars: int = 6000,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Fetch a URL and return stripped text plus metadata.

    Failure is non-fatal: the returned dict contains ``ok`` so callers
    can decide whether to include the fetched text.
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith(("http", "https")):
        return {"ok": False, "url": url, "error": "non-http scheme"}
    try:
        r = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "identity",
            },
            timeout=timeout or _PAGE_TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "url": url, "error": f"fetch failed: {e}"}

    # Limit bytes before decoding to avoid memory spikes on huge files.
    body = r.content[:_MAX_BODY_BYTES]
    charset = "utf-8"
    if r.encoding:
        charset = r.encoding
    else:
        # Crude charset extraction.
        m = re.search(rb'charset=["\']?([A-Za-z0-9._-]+)', body, re.IGNORECASE)
        if m:
            charset = m.group(1).decode("ascii", errors="ignore")
    try:
        text = body.decode(charset, errors="ignore")
    except Exception:
        text = body.decode("utf-8", errors="ignore")
    text = _strip_html(text)
    title = ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body.decode(charset, errors="ignore"), re.IGNORECASE | re.DOTALL)
    if title_m:
        title = _WS_RE.sub(" ", title_m.group(1)).strip()
    return {
        "ok": True,
        "url": str(r.url),
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


async def _maybe_enrich_results(
    results: list[dict[str, Any]],
    *,
    fetch_full_pages: bool,
    page_timeout: float,
) -> list[dict[str, Any]]:
    """Optionally fetch and attach full page text to each result."""
    if not fetch_full_pages or not results:
        return results
    async with httpx.AsyncClient() as page_client:
        fetches = [
            _fetch_page(page_client, r.get("url", ""), timeout=page_timeout)
            for r in results
        ]
        pages = await asyncio.gather(*fetches, return_exceptions=True)
    for r, page in zip(results, pages):
        if isinstance(page, Exception):
            r["page"] = {"ok": False, "error": str(page)}
        else:
            r["page"] = page
    return results


def _dedup_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate results by URL, keeping the first occurrence."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in results:
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(r)
    return out


@mcp.tool()
async def searxng_search(
    query: str,
    category: str = "",
    engines: str = "",
    language: str = "en",
    limit: int = 10,
    fetch_full_pages: bool = False,
) -> dict[str, Any]:
    """Web search via the local SearXNG instance.

    Args:
        query: search query string.
        category: SearXNG category (general, images, news, videos, files, it, science, map).
            If omitted, the tool will pick a category based on the query.
        engines: comma-separated engine names to restrict to (e.g. "google,bing,duckduckgo").
            Empty string means "use SearXNG's default engines for the category".
            If omitted, the tool will choose engines tuned for the query.
        language: two-letter language code (default "en").
        limit: max number of results to return (1-50, default 10).
        fetch_full_pages: if true, fetch each result's page text and include it in
            the ``page`` field of each result. Defaults to false for speed.

    Returns:
        dict with ``query``, ``category``, ``number_of_results``,
        ``results`` (list of {url, title, content, engine, engines, ...}),
        ``suggestions`` (SearXNG's "did you mean" list), ``unresponsive_engines``.
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    limit = max(1, min(50, int(limit)))
    s = _get_settings()
    page_timeout = s.search_page_timeout or _PAGE_TIMEOUT
    final_category, final_engines = _maybe_override_category_engines(
        query, category=category, engines=engines
    )
    try:
        async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as client:
            answer = await _search_searxng(
                client,
                query,
                category=final_category,
                engines=final_engines,
                language=language,
                limit=limit,
            )
    except httpx.HTTPError as e:
        log.exception("searxng_search failed")
        return {"error": f"searxng request failed: {e}"}
    except Exception as e:
        log.exception("searxng_search parse failed")
        return {"error": f"searxng returned non-JSON or invalid JSON: {e}"}
    answer["results"] = await _maybe_enrich_results(
        answer["results"],
        fetch_full_pages=fetch_full_pages,
        page_timeout=page_timeout,
    )
    return answer


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
    url = f"{_searxng_base_url()}/autocompleter"
    params = {"q": query.strip(), "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as client:
            r = await client.get(url, params=params, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {
            "error": f"searxng suggestions request failed: {e}",
            "note": "SearXNG may have autocomplete disabled in settings.yml (autocomplete: ''). This is non-fatal -- web_search and engines tools still work.",
            "url": url,
        }
    suggestions: list[Any] = []
    if isinstance(data, dict):
        suggestions = data.get("suggestions", [])
    elif isinstance(data, list):
        # SearXNG /autocompleter returns either [query, [sug1, sug2, ...]]
        # or just a flat list of suggestions.
        if len(data) == 2 and isinstance(data[1], list):
            suggestions = data[1]
        else:
            suggestions = data
    if not isinstance(suggestions, list):
        suggestions = []
    return {"query": query, "suggestions": [
        str(s).strip() for s in suggestions
        if isinstance(s, (str, int, float)) and str(s).strip()
    ]}


@mcp.tool()
async def searxng_engines() -> dict[str, Any]:
    """List the engines currently enabled on the local SearXNG instance.

    Returns a dict with ``engines`` (list of {name, category, language, enabled, ...})
    and ``categories`` (list of category names SearXNG supports).
    Useful for discovering what the ``engines`` and ``category`` parameters accept.
    """
    try:
        async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as client:
            data = await _get_searxng_config(client)
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
        cats = meta.get("categories", [])
        category = (
            str(cats[0])
            if isinstance(cats, list) and cats
            else (str(cats) if cats else "")
        )
        languages = meta.get("languages", [])
        language = (
            str(languages[0])
            if isinstance(languages, list) and languages
            else (str(languages) if languages else "")
        )
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


@mcp.tool()
async def quick_search(
    query: str,
    category: str = "",
    engines: str = "",
    language: str = "en",
    limit: int = 10,
    fetch_full_pages: bool | None = None,
) -> dict[str, Any]:
    """Run a single SearXNG search and return the top results.

    This is the lighter sibling of ``deep_search``: no query decomposition,
    no LLM synthesis. Use it when you just need current search results.

    Args:
        query: search query string.
        category: SearXNG category (default "general"). If omitted, the tool will
            pick a category based on the query.
        engines: comma-separated engine names (e.g. "google,bing,duckduckgo").
            If omitted, the tool will choose engines tuned for the query.
        language: two-letter language code (default "en").
        limit: max results to return (1-50, default 10).
        fetch_full_pages: override ``HOMELAB_MCP_SEARCH_FETCH_FULL_PAGES``.
            If true, each result includes a ``page`` field with stripped full text.

    Returns:
        Same shape as ``searxng_search``.
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    s = _get_settings()
    fetch_pages = s.search_fetch_full_pages if fetch_full_pages is None else fetch_full_pages
    final_category, final_engines = _maybe_override_category_engines(
        query, category=category, engines=engines
    )
    return await searxng_search(
        query=query,
        category=final_category,
        engines=final_engines,
        language=language,
        limit=limit,
        fetch_full_pages=fetch_pages,
    )


@mcp.tool()
async def deep_search(
    query: str,
    category: str = "",
    engines: str = "",
    language: str = "en",
    max_subqueries: int | None = None,
    results_per_subquery: int | None = None,
    limit: int = 0,
    fetch_full_pages: bool | None = None,
) -> dict[str, Any]:
    """Multi-step web search: decompose the question, search in parallel,
    optionally fetch pages, then synthesize a curated answer via Ollama.

    Args:
        query: the original question or topic.
        category: SearXNG category (default "general"). If omitted, the tool will
            pick a category and engines tuned to the query.
        engines: comma-separated engine names.
        language: two-letter language code (default "en").
        max_subqueries: override ``HOMELAB_MCP_SEARCH_MAX_SUBQUERIES``.
        results_per_subquery: override ``HOMELAB_MCP_SEARCH_RESULTS_PER_SUBQUERY``.
        limit: cap the final number of results returned and passed to the curator.
            ``0`` (default) means no cap; positive values are clamped to 1-50.
        fetch_full_pages: override ``HOMELAB_MCP_SEARCH_FETCH_FULL_PAGES``.

    Returns:
        dict with:

        - ``query``: the original query.
        - ``subqueries``: list of sub-queries that were generated.
        - ``results``: deduplicated list of source results. Each result has
          the usual SearXNG fields plus an optional ``page`` field when full
          page fetching is enabled.
        - ``synthesis``: the LLM-curated answer (plain string).
    """
    if not query or not query.strip():
        return {"error": "query must be non-empty"}

    s = _get_settings()
    max_sub = max(1, min(10, max_subqueries or s.search_max_subqueries))
    per_query = max(1, min(20, results_per_subquery or s.search_results_per_subquery))
    fetch_pages = s.search_fetch_full_pages if fetch_full_pages is None else fetch_full_pages
    page_timeout = s.search_page_timeout or _PAGE_TIMEOUT

    final_category, final_engines = _maybe_override_category_engines(
        query, category=category, engines=engines
    )
    routing_info = {"category": final_category, "engines": final_engines}

    decomposer_model = s.search_decomposer_model or "qwen3.5:cloud"
    curator_model = s.search_curator_model or "command-r-plus:cloud"

    decompose_prompt = (
        "You are a query decomposer. Break the user's question into focused web search "
        "queries that, together, would cover all important aspects of the question. "
        "Return ONLY a JSON object with a single key \"subqueries\" mapping to a list of "
        "query strings. Do not include markdown, commentary, or explanation. "
        f"Limit to {max_sub} subqueries."
    )

    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as ollama_client:
            decomp_resp = await _call_ollama(
                ollama_client,
                decomposer_model,
                query.strip(),
                system=decompose_prompt,
                json_mode=True,
                timeout=_OLLAMA_TIMEOUT,
            )
    except Exception as e:
        log.exception("deep_search decomposer failed")
        return {
            "error": f"decomposer failed: {e}",
            "query": query,
            "subqueries": [],
            "result_count": 0,
            "results": [],
            "routing": routing_info,
            "synthesis": "[decomposer failed; could not generate subqueries]",
        }

    parsed = decomp_resp.get("parsed_response") or {}
    subqueries_raw = parsed.get("subqueries", []) if isinstance(parsed, dict) else []
    subqueries: list[str] = []
    for sq in subqueries_raw[:max_sub]:
        text = sq.strip() if isinstance(sq, str) else str(sq).strip()
        if text:
            subqueries.append(text)
    if not subqueries:
        # Fallback: search the original question directly.
        subqueries = [query.strip()]

    # Search all subqueries in parallel.
    try:
        async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as search_client:
            searches = [
                _search_searxng(
                    search_client,
                    sq,
                    category=final_category,
                    engines=final_engines,
                    language=language,
                    limit=per_query,
                )
                for sq in subqueries
            ]
            search_results = await asyncio.gather(*searches, return_exceptions=True)
            for sq, sr in zip(subqueries, search_results):
                if isinstance(sr, Exception):
                    log.warning("Deep search subquery failed: %s: %s", sq, sr)
    except Exception as e:
        return {"error": f"searxng search failed: {e}", "subqueries": subqueries, "routing": routing_info}

    all_results: list[dict[str, Any]] = []
    for sr in search_results:
        if isinstance(sr, Exception):
            continue
        all_results.extend(sr.get("results", []))
    deduped = _dedup_results(all_results)

    # Apply final result cap if the caller requested one.
    result_cap = max(0, min(50, int(limit)))
    if result_cap and len(deduped) > result_cap:
        deduped = deduped[:result_cap]

    if fetch_pages:
        deduped = await _maybe_enrich_results(
            deduped,
            fetch_full_pages=True,
            page_timeout=page_timeout,
        )
    # Attach routing metadata to the final response so callers can inspect it.
    if deduped and isinstance(deduped, list):
        for r in deduped:
            if isinstance(r, dict):
                r.setdefault("routing_info", routing_info)

    # Build evidence block for the curator.
    evidence_lines: list[str] = []
    for idx, r in enumerate(deduped, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("content", "")
        if r.get("page", {}).get("ok"):
            page_text = r["page"].get("text", "")
            evidence_lines.append(
                f"[{idx}] {title}\nURL: {url}\n{snippet}\n---\n{page_text[:1200]}"
            )
        else:
            evidence_lines.append(f"[{idx}] {title}\nURL: {url}\n{snippet}")

    evidence = "\n\n".join(evidence_lines)
    if len(evidence) > _EVIDENCE_BUDGET_CHARS:
        evidence = evidence[:_EVIDENCE_BUDGET_CHARS] + "\n\n[Additional evidence truncated due to context window.]"
    curator_prompt = (
        "You are a research assistant synthesizing web-search evidence. "
        "Use ONLY the evidence below to answer the question. "
        "TRIANGULATE: do not fixate on a single source; synthesize findings across multiple "
        "sources and call out where they agree or disagree. "
        "WEIGHT sources by authority and recency: prefer recent review articles and widely "
        "cited work over old, obscure, or single-study claims. "
        "CITE sources with [index] markers. "
        "If the evidence is insufficient, conflicting, or only covers one narrow view, say so. "
        "Be concise but thorough.\n\n"
        f"QUESTION: {query.strip()}\n\n"
        f"EVIDENCE:\n{evidence}\n\n"
        "ANSWER:"
    )

    try:
        async with httpx.AsyncClient(timeout=120.0) as ollama_client:
            curator_resp = await _call_ollama(
                ollama_client,
                curator_model,
                curator_prompt,
                temperature=0.3,
                timeout=120.0,
            )
        synthesis_text = curator_resp.get("response", "")
        synthesis = synthesis_text
    except Exception as e:
        synthesis = f"[curator failed: {e}]"

    return {
        "query": query,
        "subqueries": subqueries,
        "result_count": len(deduped),
        "results": deduped,
        "routing": routing_info,
        "synthesis": synthesis,
    }