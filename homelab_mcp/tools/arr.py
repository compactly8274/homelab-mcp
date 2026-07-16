"""*arr-stack (Sonarr / Radarr / Lidarr / Readarr) tools.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Endpoints:
- ``HOMELAB_MCP_SONARR_URL``   / ``HOMELAB_MCP_SONARR_API_KEY``  (v3 API)
- ``HOMELAB_MCP_RADARR_URL``   / ``HOMELAB_MCP_RADARR_API_KEY``  (v3 API)
- ``HOMELAB_MCP_LIDARR_URL``   / ``HOMELAB_MCP_LIDARR_API_KEY``  (v1 API)
- ``HOMELAB_MCP_READARR_URL``  / ``HOMELAB_MCP_READARR_API_KEY`` (v1 API)

Tools are read-only (no command triggers, no download actions). The
unified ``arr_*`` family exposes the same tool shape for each service
so the agent can call ``arr_queue("sonarr")`` etc. without remembering
which API the service uses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)

_TIMEOUT = 30.0

# API version per service. v3 = Sonarr/Radarr, v1 = Lidarr/Readarr.
_API_VERSIONS: dict[str, str] = {
    "sonarr": "v3",
    "radarr": "v3",
    "lidarr": "v1",
    "readarr": "v1",
}

_DEFAULTS: dict[str, str] = {
    "sonarr": "http://192.168.1.104:8989",
    "radarr": "http://192.168.1.104:7878",
    "lidarr": "http://192.168.1.104:8686",
    "readarr": "http://192.168.1.104:8787",
}

_VALID_SERVICES = set(_API_VERSIONS)


_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


def _url_and_key(service: str) -> tuple[str, str]:
    """Return (base_url, api_key) for the given *arr service."""
    if service not in _VALID_SERVICES:
        raise ValueError(f"unknown service {service!r}; expected one of {sorted(_VALID_SERVICES)}")
    s = _get_settings()
    base = getattr(s, f"{service}_url", "") or _DEFAULTS[service]
    key = getattr(s, f"{service}_api_key", "")
    return base.rstrip("/"), key


async def _arr_request(
    service: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue an async GET to an *arr API endpoint.

    Returns ``{"_ok": True, "data": <json>}`` on success or
    ``{"_ok": False, "error": str, "_url": str}`` on failure.
    """
    base, key = _url_and_key(service)
    ver = _API_VERSIONS[service]
    url = f"{base}/api/{ver}{path}"
    headers: dict[str, str] = {"Accept": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=headers, params=params or {})
            r.raise_for_status()
            return {"_ok": True, "_url": url, "data": r.json()}
    except httpx.HTTPError as e:
        return {"_ok": False, "_url": url, "error": f"{service} {path} request failed: {e}"}
    except Exception as e:
        return {"_ok": False, "_url": url, "error": f"{service} {path} parse failed: {e}"}


# --- System / status -------------------------------------------------------


@mcp.tool()
async def arr_status(service: str) -> dict[str, Any]:
    """Return the *arr service status (version, instance name, etc.).

    Args:
        service: one of ``sonarr``, ``radarr``, ``lidarr``, ``readarr``.
    """
    if service not in _VALID_SERVICES:
        return {"error": f"unknown service {service!r}; expected one of {sorted(_VALID_SERVICES)}"}
    r = await _arr_request(service, "/system/status")
    if not r.get("_ok"):
        return {"error": r.get("error", "unknown"), "instance": _url_and_key(service)[0]}
    return {
        "service": service,
        "instance": _url_and_key(service)[0],
        "appName": r["data"].get("appName"),
        "version": r["data"].get("version"),
        "instanceName": r["data"].get("instanceName"),
        "isProduction": r["data"].get("isProduction"),
    }


# --- Queue / activity ------------------------------------------------------


@mcp.tool()
async def arr_queue(service: str, limit: int = 25) -> list[dict[str, Any]]:
    """List the current download/import queue (what's actively being processed).

    Args:
        service: ``sonarr`` | ``radarr`` | ``lidarr`` | ``readarr``.
        limit: max items to return (default 25).

    Returns:
        list of dicts with ``title``, ``status``, ``trackedDownloadState``,
        ``protocol`` (usenet/torrent), ``size``, ``sizeleft``, ``timeleft``,
        ``downloadClient`` (name), and ``indexer``.
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    limit = max(1, min(200, int(limit)))
    r = await _arr_request(service, "/queue", params={"pageSize": str(limit)})
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"].get("records") if isinstance(r["data"], dict) else r["data"]
    items = items or []
    out: list[dict[str, Any]] = []
    for it in items[:limit]:
        out.append(
            {
                "title": it.get("title", ""),
                "status": it.get("status", ""),
                "trackedDownloadState": it.get("trackedDownloadState", ""),
                "protocol": it.get("protocol", ""),
                "size": it.get("size"),
                "sizeleft": it.get("sizeleft"),
                "timeleft": it.get("timeleft"),
                "downloadClient": it.get("downloadClient", ""),
                "indexer": it.get("indexer", ""),
                "errorMessage": it.get("errorMessage"),
            }
        )
    return out


# --- History ---------------------------------------------------------------


@mcp.tool()
async def arr_history(service: str, limit: int = 25, event_type: str = "") -> list[dict[str, Any]]:
    """List recent history events (downloads, imports, upgrades, deletions).

    Args:
        service: ``sonarr`` | ``radarr`` | ``lidarr`` | ``readarr``.
        limit: max items to return (default 25).
        event_type: filter to event type, e.g. ``downloadFolderImported``,
            ``downloadFailed``, ``episodeFileDeleted``. Empty = all.

    Returns:
        list of dicts with ``eventType``, ``date``, ``sourceTitle``,
        ``quality`` (string repr), ``downloadClient``, ``language``.
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {"pageSize": str(limit), "sortKey": "date", "sortDir": "desc"}
    if event_type:
        params["eventType"] = event_type
    r = await _arr_request(service, "/history", params=params)
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"].get("records") if isinstance(r["data"], dict) else r["data"]
    items = items or []
    out: list[dict[str, Any]] = []
    for h in items[:limit]:
        q = h.get("quality") or {}
        qstr = q.get("quality", {}).get("name", "") if isinstance(q, dict) else ""
        out.append(
            {
                "eventType": h.get("eventType", ""),
                "date": h.get("date", ""),
                "sourceTitle": h.get("sourceTitle", ""),
                "quality": qstr,
                "downloadClient": h.get("downloadClient", ""),
                "language": (h.get("language") or {}).get("name", ""),
            }
        )
    return out


# --- Wanted / missing -----------------------------------------------------


@mcp.tool()
async def arr_wanted(service: str, limit: int = 25) -> list[dict[str, Any]]:
    """List wanted (missing/cutoff-unmet) items for the service.

    Sonarr/Radarr: returns missing episodes/movies.
    Lidarr: returns missing albums.
    Readarr: returns missing books.

    Args:
        service: ``sonarr`` | ``radarr`` | ``lidarr`` | ``readarr``.
        limit: max items to return (default 25).

    Returns:
        list of dicts with ``title``, ``year`` (where applicable), and
        service-specific identifiers (id, tvdbId/tmdbId/musicbrainzId).
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    limit = max(1, min(200, int(limit)))
    r = await _arr_request(service, "/wanted/missing", params={"pageSize": str(limit)})
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"].get("records") if isinstance(r["data"], dict) else r["data"]
    items = items or []
    out: list[dict[str, Any]] = []
    for w in items[:limit]:
        if service in ("sonarr", "radarr"):
            out.append(
                {
                    "title": w.get("title", ""),
                    "year": w.get("year"),
                    "id": w.get("id"),
                    "tvdbId": w.get("tvdbId"),
                    "tmdbId": w.get("tmdbId"),
                }
            )
        elif service == "lidarr":
            out.append(
                {
                    "title": w.get("title", ""),
                    "id": w.get("id"),
                    "foreignArtistId": w.get("foreignArtistId"),
                    "albumCount": len(w.get("albums", [])),
                }
            )
        else:  # readarr
            out.append(
                {
                    "title": w.get("title", ""),
                    "id": w.get("id"),
                    "authorTitle": w.get("authorTitle", ""),
                    "bookCount": len(w.get("books", [])),
                }
            )
    return out


# --- Calendar / upcoming --------------------------------------------------


@mcp.tool()
async def arr_calendar(service: str, days: int = 7, limit: int = 25) -> list[dict[str, Any]]:
    """List upcoming releases for the service.

    Args:
        service: ``sonarr`` | ``radarr`` | ``lidarr`` | ``readarr``.
            Note: Radarr has no /calendar; for Radarr this falls back to
            the missing list.
        days: how many days into the future to include (default 7).
        limit: max items to return (default 25).

    Returns:
        list of dicts with ``title``, ``airDateUtc`` (or release date),
        ``series`` (for Sonarr), ``runtime``.
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    limit = max(1, min(200, int(limit)))
    start = datetime.now(UTC).replace(microsecond=0)
    end = start + timedelta(days=max(1, int(days)))
    params = {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
    }
    if service == "radarr":
        # Radarr has no /calendar; return upcoming movies by physical release
        path = "/movie"
        r = await _arr_request(service, path, params=params)
    else:
        path = "/calendar"
        r = await _arr_request(service, path, params=params)
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"] if isinstance(r["data"], list) else (r["data"].get("records", []) if isinstance(r["data"], dict) else [])
    out: list[dict[str, Any]] = []
    for w in items[:limit]:
        if service == "sonarr":
            out.append(
                {
                    "title": w.get("title", ""),
                    "series": (w.get("series") or {}).get("title", ""),
                    "airDateUtc": w.get("airDateUtc", ""),
                    "seasonNumber": w.get("seasonNumber"),
                    "episodeNumber": w.get("episodeNumber"),
                    "runtime": w.get("runtime"),
                }
            )
        elif service == "lidarr":
            out.append(
                {
                    "title": w.get("title", ""),
                    "airDateUtc": w.get("releaseDate", ""),
                    "artist": (w.get("artist") or {}).get("artistName", ""),
                }
            )
        elif service == "readarr":
            out.append(
                {
                    "title": w.get("title", ""),
                    "airDateUtc": w.get("releaseDate", ""),
                    "authorTitle": w.get("authorTitle", ""),
                }
            )
        else:  # radarr
            out.append(
                {
                    "title": w.get("title", ""),
                    "year": w.get("year"),
                    "airDateUtc": w.get("physicalRelease", "") or w.get("digitalRelease", ""),
                    "runtime": w.get("runtime"),
                }
            )
    return out


# --- Search (lookup) -------------------------------------------------------


@mcp.tool()
async def arr_search_series(service: str, term: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search Sonarr for a TV series (returns matches from Sonarr's indexers).

    Useful for "is X available?" lookups. Use ``arr_calendar`` or
    ``arr_wanted`` for "what should I download?" workflows.

    Args:
        service: must be ``sonarr`` (only Sonarr exposes /series/lookup).
        term: search term (e.g. "Breaking Bad").
        limit: max results to return (default 10).

    Returns:
        list of dicts with ``title``, ``year``, ``tvdbId``, ``tvRageId``,
        ``imdbId``, ``overview`` (truncated).
    """
    if service != "sonarr":
        return [{"error": "arr_search_series is currently only supported for sonarr"}]
    if not term or not term.strip():
        return [{"error": "term must be non-empty"}]
    limit = max(1, min(50, int(limit)))
    r = await _arr_request(service, "/series/lookup", params={"term": term.strip()})
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"] if isinstance(r["data"], list) else []
    out: list[dict[str, Any]] = []
    for s in items[:limit]:
        overview = s.get("overview", "") or ""
        if len(overview) > 200:
            overview = overview[:197] + "..."
        out.append(
            {
                "title": s.get("title", ""),
                "year": s.get("year"),
                "tvdbId": s.get("tvdbId"),
                "imdbId": s.get("imdbId"),
                "overview": overview,
            }
        )
    return out


# --- Disk space ----------------------------------------------------------


@mcp.tool()
async def arr_disk_space(service: str) -> list[dict[str, Any]]:
    """List disk space on the server the *arr service runs on.

    Returns a list of dicts with ``path``, ``free`` (bytes), ``total`` (bytes),
    ``label``. Useful for the "is the disk full?" triage.
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    r = await _arr_request(service, "/diskspace")
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"] if isinstance(r["data"], list) else []
    out: list[dict[str, Any]] = []
    for d in items:
        out.append(
            {
                "path": d.get("path", ""),
                "free": d.get("freeSpace", 0),
                "total": d.get("totalSpace", 0),
                "label": d.get("label", ""),
            }
        )
    return out
