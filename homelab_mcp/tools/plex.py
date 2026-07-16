"""Plex Media Server read-only tools.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Endpoints:
- ``HOMELAB_MCP_PLEX_URL``   base URL (default ``http://192.168.1.104:32400``)
- ``HOMELAB_MCP_PLEX_TOKEN`` X-Plex-Token (required for any authenticated
                             endpoint). Get from Plex Web UI -> Account ->
                             "Claim Token" or from
                             ``Preferences.xml`` as ``PlexOnlineToken``.

All endpoints are read-only. The library/search/recent tools return
parsed dicts (not raw XML).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)

_DEFAULT_PLEX_URL = "http://192.168.1.104:32400"
_TIMEOUT = 30.0

_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


def _base_url() -> str:
    s = _get_settings()
    return (s.plex_url or _DEFAULT_PLEX_URL).rstrip("/")


def _headers() -> dict[str, str]:
    """Common Plex request headers. Includes X-Plex-Token if configured.

    Note: Plex servers with secure defaults can 401 on requests that
    include non-essential X-Plex-* headers (Product/Version/Identifier)
    or an Accept header that doesn't match the response type. The
    minimal token-only request has been confirmed to work against this
    server.
    """
    s = _get_settings()
    h: dict[str, str] = {}
    if s.plex_token:
        h["X-Plex-Token"] = s.plex_token
    return h


def _flatten_dir(d: ET.Element) -> dict[str, Any]:
    """Convert a Plex XML <Directory> or <Video>/<Track> element to a dict."""
    out: dict[str, Any] = {k: v for k, v in d.attrib.items()}
    # recurse one level for <Media> containers
    for child in d:
        tag = child.tag
        if tag not in out:
            out[tag] = []
        out[tag].append({k: v for k, v in child.attrib.items()})
    return out


@mcp.tool()
async def plex_status() -> dict[str, Any]:
    """Return the Plex server status (version, machineIdentifier, friendlyName).

    Returns dict with ``friendlyName``, ``version``, ``machineIdentifier``,
    ``platform``, ``myPlexUsername`` (if signed in), and ``instance``.
    """
    url = _base_url() + "/"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": f"plex status request failed: {e}", "url": url}
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return {"error": f"plex returned non-XML: {e}"}
    return {
        "instance": _base_url(),
        "friendlyName": root.attrib.get("friendlyName", ""),
        "version": root.attrib.get("version", ""),
        "machineIdentifier": root.attrib.get("machineIdentifier", ""),
        "platform": root.attrib.get("platform", ""),
        "platformVersion": root.attrib.get("platformVersion", ""),
        "myPlexUsername": root.attrib.get("myPlexUsername", ""),
    }


@mcp.tool()
async def plex_library_sections() -> list[dict[str, Any]]:
    """List all library sections (Movies, TV Shows, Music, etc.).

    Returns a list of dicts with ``key``, ``title``, ``type``
    (movie/show/artist/photo), ``agent``, ``scanner``, ``language``,
    and ``Locations`` (list of {id, path}).
    """
    url = _base_url() + "/library/sections"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
    except httpx.HTTPError as e:
        return [{"error": f"plex sections request failed: {e}", "url": url}]
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return [{"error": f"plex returned non-XML: {e}"}]
    out: list[dict[str, Any]] = []
    for d in root.findall("Directory"):
        section = {k: v for k, v in d.attrib.items()}
        locations: list[dict[str, Any]] = []
        for loc in d.findall("Location"):
            locations.append({k: v for k, v in loc.attrib.items()})
        section["Locations"] = locations
        out.append(section)
    return out


@mcp.tool()
async def plex_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search across all library types (movies, shows, episodes, artists, albums).

    Args:
        query: search term.
        limit: max results to return (1-100, default 25).

    Returns:
        list of dicts, each with ``title``, ``type`` (movie/show/episode/track/artist/album),
        ``year``, ``ratingKey``, ``summary`` (truncated), and ``thumb``.
    """
    if not query or not query.strip():
        return [{"error": "query must be non-empty"}]
    limit = max(1, min(100, int(limit)))
    url = _base_url() + "/search"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                url,
                params={"query": query.strip(), "limit": str(limit)},
                headers=_headers(),
            )
            r.raise_for_status()
    except httpx.HTTPError as e:
        return [{"error": f"plex search request failed: {e}", "url": url}]
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return [{"error": f"plex returned non-XML: {e}"}]
    out: list[dict[str, Any]] = []
    for child in root:
        if child.tag in ("Directory", "Video", "Track", "Photo", "Playlist"):
            entry = {k: v for k, v in child.attrib.items()}
            entry["type"] = child.tag.lower()
            # Truncate long summaries
            if "summary" in entry and len(entry["summary"]) > 300:
                entry["summary"] = entry["summary"][:297] + "..."
            out.append(entry)
    return out[:limit]


@mcp.tool()
async def plex_recently_added(section_key: int, limit: int = 25) -> list[dict[str, Any]]:
    """List the most recently added items in a library section.

    Args:
        section_key: the ``key`` of a library section (from
            ``plex_library_sections``). Movies = 1, Kids Shows = 11, etc.
        limit: max items to return (default 25).

    Returns:
        list of dicts (each with ``title``, ``type``, ``year``,
        ``addedAt``, ``ratingKey``, ``thumb``).
    """
    limit = max(1, min(100, int(limit)))
    url = f"{_base_url()}/library/sections/{int(section_key)}/recentlyAdded"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                url,
                params={"X-Plex-Container-Size": str(limit)},
                headers=_headers(),
            )
            r.raise_for_status()
    except httpx.HTTPError as e:
        return [{"error": f"plex recently-added request failed: {e}", "url": url}]
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return [{"error": f"plex returned non-XML: {e}"}]
    out: list[dict[str, Any]] = []
    for child in root:
        if child.tag in ("Directory", "Video", "Track"):
            entry = {k: v for k, v in child.attrib.items()}
            entry["type"] = child.tag.lower()
            out.append(entry)
    return out[:limit]


@mcp.tool()
async def plex_active_sessions() -> list[dict[str, Any]]:
    """List currently active streaming sessions.

    Returns a list of dicts with ``title`` (the media), ``year``,
    ``user`` (username), ``player`` (device + product), ``state``
    (playing/paused), ``viewOffset`` (ms), ``duration`` (ms),
    ``bandwidth`` (kbps), and ``Session`` (raw id).
    """
    url = _base_url() + "/status/sessions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
    except httpx.HTTPError as e:
        return [{"error": f"plex sessions request failed: {e}", "url": url}]
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return [{"error": f"plex returned non-XML: {e}"}]
    out: list[dict[str, Any]] = []
    for sess in root.findall("Session"):
        session_id = sess.attrib.get("id", "")
        for video in sess.findall("Video") + sess.findall("Track"):
            entry = {k: v for k, v in video.attrib.items()}
            entry["type"] = video.tag.lower()
            # User and Player are nested
            user = sess.find("User")
            if user is not None:
                entry["user"] = user.attrib.get("title", user.attrib.get("name", ""))
            player = sess.find("Player")
            if player is not None:
                entry["player"] = player.attrib.get("title", "")
                entry["player_product"] = player.attrib.get("product", "")
                entry["player_platform"] = player.attrib.get("platform", "")
                entry["player_state"] = player.attrib.get("state", "")
                entry["bandwidth_kbps"] = player.attrib.get("bandwidth")
            entry["Session"] = session_id
            out.append(entry)
        # If session has no Video/Track children (rare; e.g. empty session), still report it
        if not (sess.findall("Video") or sess.findall("Track")):
            entry = {"Session": session_id, "type": "empty"}
            user = sess.find("User")
            if user is not None:
                entry["user"] = user.attrib.get("title", user.attrib.get("name", ""))
            out.append(entry)
    return out


@mcp.tool()
async def plex_server_stats() -> dict[str, Any]:
    """Return aggregate server stats: library sizes + active session count.

    Returns dict with ``library_counts`` (list of {section, key, type, count}),
    ``active_sessions`` (int), and ``instance``.
    """
    sections = await plex_library_sections()
    if isinstance(sections, list) and sections and "error" in sections[0]:
        return {"error": sections[0]["error"]}
    library_counts: list[dict[str, Any]] = []
    for sec in sections:
        key = sec.get("key")
        if not key:
            continue
        url = f"{_base_url()}/library/sections/{key}/all"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(url, params={"X-Plex-Container-Size": "0"}, headers=_headers())
                r.raise_for_status()
                root = ET.fromstring(r.text)
                count = int(root.attrib.get("totalSize", 0))
        except (httpx.HTTPError, ET.ParseError, ValueError):
            count = -1
        library_counts.append(
            {
                "section": sec.get("title", ""),
                "key": key,
                "type": sec.get("type", ""),
                "count": count,
            }
        )
    sessions = await plex_active_sessions()
    active = 0
    if isinstance(sessions, list) and not (sessions and "error" in sessions[0]):
        active = len(sessions)
    return {
        "instance": _base_url(),
        "library_counts": library_counts,
        "active_sessions": active,
    }


@mcp.tool()
async def plex_get_metadata(rating_key: int | str) -> dict[str, Any]:
    """Fetch full metadata for a single item (movie/show/episode/track) by ratingKey.

    Companion to ``plex_search`` and ``plex_recently_added`` — those return
    a shallow row with only basic attributes; this tool returns the full
    detail (summary, genres, runtime, view count, rating, Media info,
    available parts, etc.) for one item.

    Args:
        rating_key: the ``ratingKey`` of the item (an int as a string or
            number; whatever the search results returned).

    Returns:
        dict with ``ratingKey``, ``title``, ``type``, ``year``,
        ``summary`` (truncated to 1000 chars), ``genres`` (list),
        ``duration`` (Plex's raw millisecond value as a string),
        ``viewCount``, ``rating``, ``studio``,
        ``addedAt``, ``updatedAt``, ``Media`` (list of {container,
        videoCodec, audioCodec, bitrate, etc.}), and ``instance``.
        On error, returns ``{"error": ...}``.
    """
    try:
        rating_key_int = int(rating_key)
    except (TypeError, ValueError):
        return {"error": f"rating_key must be an int or numeric string, got {rating_key!r}"}
    if rating_key_int <= 0:
        return {"error": f"rating_key must be positive, got {rating_key_int}"}
    url = f"{_base_url()}/library/metadata/{rating_key_int}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": f"plex metadata request failed: {e}", "url": url, "rating_key": rating_key_int}
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        return {"error": f"plex returned non-XML: {e}", "rating_key": rating_key_int}
    # The container is <MediaContainer><Directory|Video|Track/></MediaContainer>
    item = None
    for child in root:
        if child.tag in ("Directory", "Video", "Track", "Photo"):
            item = child
            break
    if item is None:
        return {"error": f"no metadata found for ratingKey {rating_key_int}", "rating_key": rating_key_int}
    out: dict[str, Any] = {k: v for k, v in item.attrib.items()}
    out["type"] = item.tag.lower()
    # Truncate long summaries (some Plex summaries are 5-10 KB)
    if "summary" in out and len(out["summary"]) > 1000:
        out["summary"] = out["summary"][:997] + "..."
    # Genre is split on "|" or stored in a list of <Genre/> children
    genre_list: list[str] = []
    for g in item.findall("Genre"):
        tag = g.attrib.get("tag", "")
        if tag:
            genre_list.append(tag)
    if genre_list:
        out["genres"] = genre_list
    elif out.get("genre"):
        out["genres"] = [g.strip() for g in out["genre"].split("|") if g.strip()]
    # Flatten Media[] containers (file/bitrate/codec info)
    media_list: list[dict[str, Any]] = []
    for media in item.findall("Media"):
        m: dict[str, Any] = {k: v for k, v in media.attrib.items()}
        parts: list[dict[str, Any]] = []
        for part in media.findall("Part"):
            parts.append({k: v for k, v in part.attrib.items()})
        if parts:
            m["Part"] = parts
        media_list.append(m)
    if media_list:
        out["Media"] = media_list
    out["instance"] = _base_url()
    return out
