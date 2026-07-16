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

# Lookup endpoint per service (used by arr_search_series + arr_search_all).
# Sonarr→series, Radarr→movie, Lidarr→artist, Readarr→book.
_LOOKUP_PATHS: dict[str, str] = {
    "sonarr": "/series/lookup",
    "radarr": "/movie/lookup",
    "lidarr": "/artist/lookup",
    "readarr": "/book/lookup",
}

# Per-service field shaper for lookup results (we only surface the fields
# that are useful for "is this the one I want?" lookups; full payloads can
# be 50+ keys).
def _shape_lookup(service: str, item: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, normalized dict for one lookup result."""
    if service == "sonarr":
        overview = item.get("overview", "") or ""
        if len(overview) > 200:
            overview = overview[:197] + "..."
        return {
            "title": item.get("title", ""),
            "year": item.get("year"),
            "tvdbId": item.get("tvdbId"),
            "imdbId": item.get("imdbId"),
            "overview": overview,
        }
    if service == "radarr":
        overview = item.get("overview", "") or ""
        if len(overview) > 200:
            overview = overview[:197] + "..."
        return {
            "title": item.get("title", ""),
            "year": item.get("year"),
            "tmdbId": item.get("tmdbId"),
            "imdbId": item.get("imdbId"),
            "runtime": item.get("runtime"),
            "overview": overview,
        }
    if service == "lidarr":
        return {
            "title": item.get("artistName", ""),
            "foreignArtistId": item.get("foreignArtistId"),
            "overview": (item.get("overview", "") or "")[:200],
        }
    # readarr
    return {
        "title": item.get("title", ""),
        "authorTitle": item.get("authorTitle", ""),
        "foreignBookId": item.get("foreignBookId"),
        "releaseDate": item.get("releaseDate", ""),
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
    """Search an *arr service's catalog for a series/movie/artist/book.

    Useful for "is X available?" lookups. Use ``arr_calendar`` or
    ``arr_wanted`` for "what should I download?" workflows.

    Args:
        service: ``sonarr`` | ``radarr`` | ``lidarr`` | ``readarr``.
            Sonarr→series, Radarr→movie, Lidarr→artist, Readarr→book.
        term: search term (e.g. "Breaking Bad", "The Matrix", "Radiohead").
        limit: max results to return (default 10, max 50).

    Returns:
        list of compact dicts, one per result. Fields vary by service:
        sonarr→{title, year, tvdbId, imdbId, overview},
        radarr→{title, year, tmdbId, imdbId, runtime, overview},
        lidarr→{title (artist), foreignArtistId, overview},
        readarr→{title, authorTitle, foreignBookId, releaseDate}.
    """
    if service not in _VALID_SERVICES:
        return [{"error": f"unknown service {service!r}"}]
    if not term or not term.strip():
        return [{"error": "term must be non-empty"}]
    limit = max(1, min(50, int(limit)))
    path = _LOOKUP_PATHS[service]
    r = await _arr_request(service, path, params={"term": term.strip()})
    if not r.get("_ok"):
        return [{"error": r.get("error", "unknown")}]
    items = r["data"] if isinstance(r["data"], list) else []
    return [_shape_lookup(service, s) for s in items[:limit]]


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


# --- Bulk / all-services --------------------------------------------------


@mcp.tool()
async def arr_status_all() -> dict[str, Any]:
    """Return status for all four *arr services in one call.

    Convenience tool for "is the whole stack healthy?" checks. Calls
    ``arr_status`` for each of sonarr/radarr/lidarr/readarr in parallel
    via asyncio.gather and returns a dict keyed by service.

    Returns:
        dict with ``services`` (ordered {sonarr, radarr, lidarr, readarr},
        each the same shape as ``arr_status``), and ``healthy_count``
        (int — services with version != null and no error key).
    """
    import asyncio

    services = sorted(_VALID_SERVICES)
    results = await asyncio.gather(
        *[arr_status(s) for s in services], return_exceptions=True
    )
    out: dict[str, Any] = {}
    healthy = 0
    for svc, res in zip(services, results, strict=True):
        if isinstance(res, Exception):
            out[svc] = {"error": f"unexpected exception: {res}"}
            continue
        out[svc] = res
        if "error" not in res and res.get("version"):
            healthy += 1
    return {"services": out, "healthy_count": healthy, "total": len(_VALID_SERVICES)}


@mcp.tool()
async def arr_queue_all(limit: int = 25) -> dict[str, Any]:
    """Return the active download queue for all four *arr services.

    Args:
        limit: per-service max items to return (default 25, max 200).

    Returns:
        dict with ``queues`` (ordered {sonarr, radarr, lidarr, readarr},
        each a list of queue items in the same shape as ``arr_queue``),
        and ``totals`` ({service: count}) for quick triage.
    """
    import asyncio

    limit = max(1, min(200, int(limit)))
    services = sorted(_VALID_SERVICES)
    results = await asyncio.gather(
        *[arr_queue(s, limit=limit) for s in services], return_exceptions=True
    )
    out: dict[str, Any] = {}
    totals: dict[str, int] = {}
    for svc, res in zip(services, results, strict=True):
        if isinstance(res, Exception):
            out[svc] = [{"error": f"unexpected exception: {res}"}]
            totals[svc] = 0
            continue
        out[svc] = res
        # `arr_queue` returns a list (possibly [{error: ...}] on failure)
        totals[svc] = len(res) if isinstance(res, list) and not (res and "error" in res[0]) else 0
    return {"queues": out, "totals": totals}


@mcp.tool()
async def arr_wanted_all(limit: int = 25) -> dict[str, Any]:
    """Return wanted/missing items for all four *arr services.

    Args:
        limit: per-service max items to return (default 25, max 200).

    Returns:
        dict with ``wanted`` (ordered {sonarr, radarr, lidarr, readarr},
        each a list in the same shape as ``arr_wanted``),
        and ``totals`` ({service: count}).
    """
    import asyncio

    limit = max(1, min(200, int(limit)))
    services = sorted(_VALID_SERVICES)
    results = await asyncio.gather(
        *[arr_wanted(s, limit=limit) for s in services], return_exceptions=True
    )
    out: dict[str, Any] = {}
    totals: dict[str, int] = {}
    for svc, res in zip(services, results, strict=True):
        if isinstance(res, Exception):
            out[svc] = [{"error": f"unexpected exception: {res}"}]
            totals[svc] = 0
            continue
        out[svc] = res
        totals[svc] = len(res) if isinstance(res, list) and not (res and "error" in res[0]) else 0
    return {"wanted": out, "totals": totals}


@mcp.tool()
async def arr_search_all(term: str, limit: int = 10) -> dict[str, Any]:
    """Search all four *arr services for a series/movie/artist/book.

    Fan-out version of ``arr_search_series``. Useful for "where can I get X?"
    cross-catalog lookups.

    Args:
        term: search term (e.g. "Breaking Bad", "The Matrix", "Radiohead").
        limit: per-service max results to return (default 10, max 50).

    Returns:
        dict with ``results`` (ordered {sonarr, radarr, lidarr, readarr},
        each a list in the same shape as ``arr_search_series``),
        and ``totals`` ({service: count}).
    """
    import asyncio

    if not term or not term.strip():
        return {"error": "term must be non-empty"}
    limit = max(1, min(50, int(limit)))
    services = sorted(_VALID_SERVICES)
    results = await asyncio.gather(
        *[arr_search_series(s, term, limit=limit) for s in services],
        return_exceptions=True,
    )
    out: dict[str, Any] = {}
    totals: dict[str, int] = {}
    for svc, res in zip(services, results, strict=True):
        if isinstance(res, Exception):
            out[svc] = [{"error": f"unexpected exception: {res}"}]
            totals[svc] = 0
            continue
        out[svc] = res
        totals[svc] = len(res) if isinstance(res, list) and not (res and "error" in res[0]) else 0
    return {"term": term.strip(), "results": out, "totals": totals}
