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
        # Simulate HTTPStatusError on raise_for_status
        from httpx import HTTPStatusError, Request, Response

        req = Request("GET", "http://x")
        r.raise_for_status.side_effect = HTTPStatusError(
            "error", request=req, response=Response(status_code, request=req)
        )
    return r


async def test_searxng_search_returns_normalized_results() -> None:
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

        async def get(self, url, headers=None, params=None, **kwargs):
            return _mock_response({
                "instance_name": "TestSearX",
                "engines": [
                    {"name": "yandex", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "ynd"},
                    {"name": "wikipedia", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "wp"},
                    {"name": "bing", "categories": ["general", "images"], "languages": ["all"], "enabled": False, "shortcut": "bi"},
                ],
            })

        async def post(self, url, data=None, headers=None, **kwargs):
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        from homelab_mcp.tools import searxng as _searxng_mod

        _searxng_mod._searxng_config_cache = None
        _searxng_mod._searxng_config_cache_ts = 0.0
        _searxng_mod._enabled_engines_cache = None
        _searxng_mod._enabled_engines_cache_ts = 0.0
        result = await searxng_search("test", limit=5)

    assert result["query"] == "test"
    # live code normalizes to len(results), not the raw upstream count
    assert result["number_of_results"] == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://example.com"
    assert result["results"][0]["engine"] == "startpage"
    assert result["suggestions"] == ["try this"]
    assert result["unresponsive_engines"] == ["broken-engine"]


async def test_searxng_search_rejects_empty_query() -> None:
    result = await searxng_search("")
    assert "error" in result


async def test_searxng_search_clamps_limit() -> None:
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

        async def get(self, url, headers=None, params=None, **kwargs):
            captured["url"] = url
            if url.endswith("/config"):
                return _mock_response({
                    "instance_name": "TestSearX",
                    "engines": [
                        {"name": "yandex", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "ynd"},
                        {"name": "wikipedia", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "wp"},
                        {"name": "bing", "categories": ["general"], "languages": ["all"], "enabled": False, "shortcut": "bi"},
                    ],
                })
            return _mock_response(fake_json)

        async def post(self, url, data=None, headers=None, **kwargs):
            captured.update(data or {})
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        from homelab_mcp.tools import searxng as _searxng_mod

        _searxng_mod._searxng_config_cache = None
        _searxng_mod._searxng_config_cache_ts = 0.0
        _searxng_mod._enabled_engines_cache = None
        _searxng_mod._enabled_engines_cache_ts = 0.0
        await searxng_search("x", limit=999)
    assert captured.get("q") == "x"


async def test_searxng_engines_handles_list_shape() -> None:
    """SearXNG /config returns engines as a list, not a dict."""
    fake_json = {
        "instance_name": "TestSearX",
        "engines": [
            {"name": "wikipedia", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "wp", "timeout": 3.0},
            {"name": "yandex", "categories": ["general"], "languages": ["en"], "enabled": True, "shortcut": "ynd", "timeout": 3.0},
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

        async def get(self, url, headers=None, **kwargs):
            return _mock_response(fake_json)

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        from homelab_mcp.tools import searxng as _searxng_mod

        _searxng_mod._searxng_config_cache = None
        _searxng_mod._searxng_config_cache_ts = 0.0
        _searxng_mod._enabled_engines_cache = None
        _searxng_mod._enabled_engines_cache_ts = 0.0
        result = await searxng_engines()

    assert result["instance_name"] == "TestSearX"
    assert len(result["engines"]) == 2
    assert result["engines"][0]["name"] == "wikipedia"
    assert result["categories"] == ["general", "images", "news"]


async def test_searxng_suggestions_handles_404_gracefully() -> None:
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

        async def get(self, url, params=None, headers=None, **kwargs):
            r = _mock_response({}, status_code=404)
            r.raise_for_status.side_effect = err
            return r

    with patch("homelab_mcp.tools.searxng.httpx.AsyncClient", _Client):
        result = await searxng_suggestions("pytho")

    assert "error" in result
    assert "autocomplete" in result["note"]


# ---------------------------------------------------------------------------
# Engine policy tests (Bing quarantine, recency/default/evergreen sets)
# ---------------------------------------------------------------------------

from homelab_mcp.tools import searxng as _searxng_mod


def _split_engines(csv: str) -> list[str]:
    return [e.strip() for e in csv.split(",") if e.strip()]


def test_bing_quarantine_flag_defaults_to_false() -> None:
    """Bing is disabled by default."""
    assert _searxng_mod.BING_ENABLED is False


def test_engine_sets_exclude_bing_when_disabled() -> None:
    """With BING_ENABLED False, no active engine set contains the general bing engine."""
    sets = [
        _searxng_mod._GENERAL_ENGINES,
        _searxng_mod._EVERGREEN_ENGINES,
        _searxng_mod._RECENCY_ENGINES,
    ]
    for engine_csv in sets:
        assert "bing" not in _split_engines(engine_csv), f"bing found in {engine_csv}"


def test_default_engine_set_exact_when_bing_disabled() -> None:
    """Default fan-out is exactly fynd + yandex + wikipedia."""
    assert set(_split_engines(_searxng_mod._GENERAL_ENGINES)) == {"fynd", "yandex", "wikipedia"}


def test_recency_engine_set_exact() -> None:
    """Recency queries use only yandex + wikipedia."""
    assert _searxng_mod._RECENCY_ENGINES == "yandex,wikipedia"
    category, engines, _ = _searxng_mod._classify_query("who won the 2024 world series")
    assert engines == "yandex,wikipedia"
    assert category == "general"


def test_evergreen_set_formula_restores_bing_when_enabled() -> None:
    """The evergreen set formula restores Bing when BING_ENABLED is True."""
    expected = "bing,yandex,wikipedia" if _searxng_mod.BING_ENABLED else "yandex,wikipedia"
    assert _searxng_mod._EVERGREEN_ENGINES == expected


def test_general_set_formula_matches_bing_toggle() -> None:
    """The general set formula matches the Bing quarantine toggle."""
    expected = "yandex,wikipedia" if _searxng_mod.BING_ENABLED else "fynd,yandex,wikipedia"
    assert _searxng_mod._GENERAL_ENGINES == expected


# ---------------------------------------------------------------------------
# Silent-empty failure detector tests
# ---------------------------------------------------------------------------


def test_silent_empty_failure_single_engine_zero_results() -> None:
    """A single responsive engine returning zero results is a silent empty failure."""
    payload = {"number_of_results": 0, "unresponsive_engines": []}
    assert _searxng_mod._is_silent_empty_failure(payload, "yandex") is True


def test_silent_empty_failure_not_triggered_for_multiple_engines() -> None:
    """Zero results with multiple engines may be a genuine empty query; do not flag."""
    payload = {"number_of_results": 0, "unresponsive_engines": []}
    assert _searxng_mod._is_silent_empty_failure(payload, "yandex,wikipedia") is False


def test_silent_empty_failure_not_triggered_when_engine_marked_unresponsive() -> None:
    """An engine reported as unresponsive is a normal failure, not silent empty."""
    payload = {"number_of_results": 0, "unresponsive_engines": ["yandex"]}
    assert _searxng_mod._is_silent_empty_failure(payload, "yandex") is False

