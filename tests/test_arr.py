"""Tests for the *arr (Sonarr/Radarr/Lidarr/Readarr) tools.

Covers routing, API-version selection (v3 vs v1), the unified ``arr_*``
shape, and graceful failure when a service is unconfigured.

We patch ``_arr_request`` (the only network surface) so tests stay
offline. End-to-end validation against the live stack is opt-in via
the ``live_*`` markers — see ``tests/live/README.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homelab_mcp.tools import arr


def _ok(data):
    return {"_ok": True, "_url": "http://x/api/v3/system/status", "data": data}


def _fail(error: str = "boom"):
    return {"_ok": False, "_url": "http://x/api/v3/system/status", "error": error}


# ----------------------------- routing ---------------------------------


def test_api_version_map_sonarr_radarr_use_v3() -> None:
    assert arr._API_VERSIONS["sonarr"] == "v3"
    assert arr._API_VERSIONS["radarr"] == "v3"


def test_api_version_map_lidarr_readarr_use_v1() -> None:
    assert arr._API_VERSIONS["lidarr"] == "v1"
    assert arr._API_VERSIONS["readarr"] == "v1"


def test_all_four_services_have_defaults() -> None:
    for svc in ("sonarr", "radarr", "lidarr", "readarr"):
        assert svc in arr._DEFAULTS
        assert arr._DEFAULTS[svc].startswith("http")


def test_url_and_key_unknown_service_raises() -> None:
    with pytest.raises(ValueError, match="unknown service"):
        arr._url_and_key("bazarr")


# ----------------------------- tool shapes -----------------------------


async def test_arr_status_unwraps_data_block() -> None:
    fake_status = {"version": "4.0.19.2979", "appName": "Sonarr", "instanceName": "x", "isProduction": True}
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok(fake_status))) as m:
        result = await arr.arr_status("sonarr")
    assert result["service"] == "sonarr"
    assert result["appName"] == "Sonarr"
    assert result["version"] == "4.0.19.2979"
    args, _ = m.call_args
    assert args == ("sonarr", "/system/status")


async def test_arr_status_returns_error_dict_on_failure() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_fail("503"))):
        result = await arr.arr_status("sonarr")
    assert "error" in result
    assert "instance" in result


async def test_arr_queue_passes_limit_as_pagesize() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok({"records": []}))) as m:
        result = await arr.arr_queue("radarr", limit=10)
    assert result == []
    args, kwargs = m.call_args
    assert args[0] == "radarr"
    assert "/queue" in args[1]
    assert kwargs["params"]["pageSize"] == "10"


async def test_arr_queue_clamps_limit_to_max() -> None:
    """Limits above 200 should be clamped to 200 (per docstring)."""
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok({"records": []}))) as m:
        await arr.arr_queue("sonarr", limit=500)
    _args, kwargs = m.call_args
    assert int(kwargs["params"]["pageSize"]) == 200


async def test_arr_history_omits_event_type_when_empty() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok({"records": []}))) as m:
        await arr.arr_history("sonarr")
    _args, kwargs = m.call_args
    assert "eventType" not in kwargs["params"]


async def test_arr_history_passes_event_type_when_set() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok({"records": []}))) as m:
        await arr.arr_history("sonarr", event_type="downloadFolderImported")
    _args, kwargs = m.call_args
    assert kwargs["params"]["eventType"] == "downloadFolderImported"


async def test_arr_calendar_passes_start_end_window() -> None:
    """Calendar must pass a start/end window covering `days` ahead."""
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([]))) as m:
        await arr.arr_calendar("lidarr", days=14, limit=50)
    args, kwargs = m.call_args
    assert "/calendar" in args[1]
    # The tool computes start/end from `days`; pageSize is NOT a param here
    assert "start" in kwargs["params"]
    assert "end" in kwargs["params"]
    # And the limit is enforced by slicing in the tool, not as a query param


async def test_arr_calendar_radarr_falls_back_to_movie() -> None:
    """Radarr has no /calendar; the tool should hit /movie with a release window."""
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([]))) as m:
        await arr.arr_calendar("radarr", days=7)
    args, _kwargs = m.call_args
    assert args[1] == "/movie"


async def test_arr_search_series_uses_lookup_path() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([{"title": "Foo Bar", "year": 2020, "tvdbId": 12345, "imdbId": "tt123", "overview": "x"}]))) as m:
        result = await arr.arr_search_series("sonarr", "Foo Bar", limit=5)
    assert result == [{"title": "Foo Bar", "year": 2020, "tvdbId": 12345, "imdbId": "tt123", "overview": "x"}]
    args, _kwargs = m.call_args
    assert args[1].startswith("/series/lookup")
    # term is passed as a query param, not URL-encoded in the path
    assert m.call_args.kwargs["params"]["term"] == "Foo Bar"


async def test_arr_search_series_rejects_non_sonarr() -> None:
    """Only Sonarr exposes /series/lookup."""
    result = await arr.arr_search_series("radarr", "foo")
    assert isinstance(result, list)
    assert "error" in result[0]


async def test_arr_search_series_rejects_empty_term() -> None:
    result = await arr.arr_search_series("sonarr", "   ")
    assert isinstance(result, list)
    assert "error" in result[0]


async def test_arr_search_series_truncates_long_overview() -> None:
    long = "x" * 500
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([{"title": "t", "overview": long}]))):
        result = await arr.arr_search_series("sonarr", "t", limit=1)
    assert result[0]["overview"].endswith("...")
    assert len(result[0]["overview"]) <= 200


async def test_arr_disk_space_normalizes_field_names() -> None:
    """Tool should normalize freeSpace/totalSpace → free/total."""
    fake = [{"path": "/movies", "freeSpace": 123_456_789, "totalSpace": 999_999_999, "label": ""}]
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok(fake))) as m:
        result = await arr.arr_disk_space("radarr")
    assert result == [{"path": "/movies", "free": 123_456_789, "total": 999_999_999, "label": ""}]
    args, _ = m.call_args
    assert args[1] == "/diskspace"


async def test_arr_wanted_returns_list_even_when_empty() -> None:
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([]))):
        result = await arr.arr_wanted("readarr", limit=25)
    assert isinstance(result, list)
    assert result == []


# -------------------------- service validation -------------------------


async def test_unknown_service_returns_error_dict_for_status() -> None:
    result = await arr.arr_status("bazarr")
    assert "error" in result
    assert "unknown service" in result["error"]


async def test_unknown_service_returns_error_list_for_queue() -> None:
    """List-returning tools should still produce a list (with the error)."""
    result = await arr.arr_queue("bazarr")
    assert isinstance(result, list)
    assert len(result) == 1
    assert "error" in result[0]


# -------------------------- multi-service search ------------------------


async def test_arr_search_series_now_supports_all_services() -> None:
    """Extended in v0.2.1: lookup works for sonarr/radarr/lidarr/readarr."""
    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([{"title": "X", "artistName": "X", "authorTitle": "A — B"}]))) as m:
        result = await arr.arr_search_series("radarr", "matrix")
    assert isinstance(result, list)
    assert "title" in result[0]
    args, _ = m.call_args
    assert args[1] == "/movie/lookup"

    with patch.object(arr, "_arr_request", new=AsyncMock(return_value=_ok([]))):
        result = await arr.arr_search_series("lidarr", "radiohead")
    assert isinstance(result, list)


async def test_arr_search_series_unknown_service_still_errors() -> None:
    result = await arr.arr_search_series("bazarr", "x")
    assert isinstance(result, list)
    assert "error" in result[0]


# -------------------------- bulk / all-services -------------------------


async def test_arr_status_all_returns_four_services() -> None:
    """arr_status_all fans out to all 4 services in parallel."""
    fake_status = {
        "sonarr": {"version": "4.0.19.2979", "appName": "Sonarr", "instanceName": "x", "isProduction": True},
        "radarr": {"version": "6.3.0.10514", "appName": "Radarr", "instanceName": "y", "isProduction": True},
        "lidarr": {"version": "3.1.0.4875", "appName": "Lidarr", "instanceName": "z", "isProduction": True},
        "readarr": {"version": "0.4.19.2811", "appName": "Readarr", "instanceName": "w", "isProduction": True},
    }

    async def fake_status_call(service: str) -> dict:
        return {"service": service, "version": fake_status[service]["version"]}

    with patch.object(arr, "arr_status", new=AsyncMock(side_effect=fake_status_call)) as m:
        result = await arr.arr_status_all()
    assert m.call_count == 4
    assert set(result["services"].keys()) == {"sonarr", "radarr", "lidarr", "readarr"}
    assert result["healthy_count"] == 4
    assert result["total"] == 4


async def test_arr_status_all_surfaces_exceptions() -> None:
    """If one service raises, it should appear as an error in the result, not crash."""
    async def fake_status_call(service: str) -> dict:
        if service == "radarr":
            raise RuntimeError("boom")
        return {"service": service, "version": "1.0.0"}

    with patch.object(arr, "arr_status", new=AsyncMock(side_effect=fake_status_call)):
        result = await arr.arr_status_all()
    assert "error" in result["services"]["radarr"]
    assert result["healthy_count"] == 3


async def test_arr_queue_all_returns_queues_and_totals() -> None:
    fake_queues = {
        "sonarr": [{"title": "S1"}, {"title": "S2"}],
        "radarr": [],
        "lidarr": [{"title": "L1"}],
        "readarr": [{"error": "down"}],
    }

    async def fake_queue_call(service: str, limit: int = 25) -> list:
        return fake_queues[service]

    with patch.object(arr, "arr_queue", new=AsyncMock(side_effect=fake_queue_call)):
        result = await arr.arr_queue_all(limit=10)
    assert result["totals"]["sonarr"] == 2
    assert result["totals"]["radarr"] == 0
    assert result["totals"]["lidarr"] == 1
    # readarr returned an error list — total should be 0
    assert result["totals"]["readarr"] == 0


async def test_arr_wanted_all_returns_wanted_and_totals() -> None:
    fake_wanted = {
        "sonarr": [{"title": "Missed Ep 1"}],
        "radarr": [],
        "lidarr": [],
        "readarr": [],
    }

    async def fake_wanted_call(service: str, limit: int = 25) -> list:
        return fake_wanted[service]

    with patch.object(arr, "arr_wanted", new=AsyncMock(side_effect=fake_wanted_call)):
        result = await arr.arr_wanted_all()
    assert result["totals"]["sonarr"] == 1
    assert result["totals"]["radarr"] == 0
    assert "wanted" in result


async def test_arr_search_all_fans_out_across_services() -> None:
    fake_results = {
        "sonarr": [{"title": "B"}],
        "radarr": [],
        "lidarr": [],
        "readarr": [],
    }

    async def fake_search_call(service: str, term: str, limit: int = 10) -> list:
        return fake_results[service]

    with patch.object(arr, "arr_search_series", new=AsyncMock(side_effect=fake_search_call)):
        result = await arr.arr_search_all("x", limit=5)
    assert result["term"] == "x"
    assert result["totals"]["sonarr"] == 1
    assert result["totals"]["radarr"] == 0


async def test_arr_search_all_rejects_empty_term() -> None:
    result = await arr.arr_search_all("   ")
    assert "error" in result
    assert "term" in result["error"]


async def test_arr_status_all_healthy_count_excludes_errors() -> None:
    """A service returning an error dict should NOT count as healthy."""

    async def fake_status_call(service: str) -> dict:
        if service == "lidarr":
            return {"service": "lidarr", "error": "401 unauthorized"}
        return {"service": service, "version": "1.0"}

    with patch.object(arr, "arr_status", new=AsyncMock(side_effect=fake_status_call)):
        result = await arr.arr_status_all()
    assert result["healthy_count"] == 3
    assert "error" in result["services"]["lidarr"]


# ---------------------- lookup-path routing -----------------------------


def test_lookup_paths_cover_all_services() -> None:
    """Every service in _VALID_SERVICES must have a lookup path."""
    for svc in arr._VALID_SERVICES:
        assert svc in arr._LOOKUP_PATHS
        assert arr._LOOKUP_PATHS[svc].endswith("/lookup")


def test_shape_lookup_per_service() -> None:
    """The per-service field shaper should only surface the documented fields."""
    sonarr = arr._shape_lookup("sonarr", {"title": "T", "year": 2020, "tvdbId": 1, "imdbId": "tt1", "overview": "x"})
    assert set(sonarr.keys()) == {"title", "year", "tvdbId", "imdbId", "overview"}

    radarr = arr._shape_lookup("radarr", {"title": "T", "year": 2020, "tmdbId": 1, "imdbId": "tt1", "runtime": 120, "overview": "x"})
    assert set(radarr.keys()) == {"title", "year", "tmdbId", "imdbId", "runtime", "overview"}

    lidarr = arr._shape_lookup("lidarr", {"artistName": "Radiohead", "foreignArtistId": "abc", "overview": "x"})
    assert set(lidarr.keys()) == {"title", "foreignArtistId", "overview"}
    assert lidarr["title"] == "Radiohead"

    readarr = arr._shape_lookup("readarr", {"title": "Book", "authorTitle": "A — Book", "foreignBookId": "b1", "releaseDate": "2020-01-01"})
    assert set(readarr.keys()) == {"title", "authorTitle", "foreignBookId", "releaseDate"}
