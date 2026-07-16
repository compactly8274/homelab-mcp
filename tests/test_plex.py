"""Tests for the Plex integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homelab_mcp.tools.plex import (
    plex_active_sessions,
    plex_library_sections,
    plex_recently_added,
    plex_search,
    plex_server_stats,
    plex_status,
)


def _mock_response(text: str = "", json_data=None, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


_PLEX_HOME_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<MediaContainer size="25" friendlyName="PHILSERV" version="1.43.3" '
    'machineIdentifier="abc123" platform="Linux" '
    'myPlexUsername="philjnewman@hotmail.com"/>'
)


def test_plex_status_parses_root_attributes() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text=_PLEX_HOME_XML)

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(plex_status())
    assert r["friendlyName"] == "PHILSERV"
    assert r["version"] == "1.43.3"
    assert r["machineIdentifier"] == "abc123"
    assert r["myPlexUsername"] == "philjnewman@hotmail.com"


_SECTIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="2">
  <Directory key="1" type="movie" title="Movies" agent="x" scanner="y">
    <Location id="1" path="/movies"/>
  </Directory>
  <Directory key="2" type="show" title="TV Shows" agent="x" scanner="y">
    <Location id="2" path="/shows"/>
  </Directory>
</MediaContainer>"""


def test_plex_library_sections_extracts_locations() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text=_SECTIONS_XML)

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        sections = asyncio.run(plex_library_sections())
    assert len(sections) == 2
    assert sections[0]["title"] == "Movies"
    assert sections[0]["Locations"][0]["path"] == "/movies"
    assert sections[1]["type"] == "show"


_SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="3">
  <Video ratingKey="1" title="A New Hope" type="movie" year="1977"/>
  <Video ratingKey="2" title="The Empire Strikes Back" type="movie" year="1980"/>
  <Video ratingKey="3" title="Return of the Jedi" type="movie" year="1983"/>
</MediaContainer>"""


def test_plex_search_returns_list() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text=_SEARCH_XML)

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(plex_search("star wars", limit=2))
    assert len(r) == 2
    assert r[0]["title"] == "A New Hope"
    assert r[0]["type"] == "video"
    assert r[0]["ratingKey"] == "1"


def test_plex_search_rejects_empty_query() -> None:
    import asyncio
    r = asyncio.run(plex_search(""))
    assert "error" in r[0]


_RECENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="2">
  <Video ratingKey="10" title="Movie A" addedAt="1000"/>
  <Video ratingKey="11" title="Movie B" addedAt="2000"/>
</MediaContainer>"""


def test_plex_recently_added_respects_limit() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text=_RECENT_XML)

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(plex_recently_added(1, limit=1))
    assert len(r) == 1


_SESSIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
  <Session id="sess-1">
    <User title="philjnewman@hotmail.com"/>
    <Player title="Apple TV" product="Plex for Apple TV" platform="tvOS" state="playing" bandwidth="5000"/>
    <Video ratingKey="1" title="Some Movie" type="movie" viewOffset="30000" duration="7200000"/>
  </Session>
</MediaContainer>"""


def test_plex_active_sessions_extracts_user_and_player() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text=_SESSIONS_XML)

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        sessions = asyncio.run(plex_active_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s["user"] == "philjnewman@hotmail.com"
    assert s["player"] == "Apple TV"
    assert s["player_state"] == "playing"
    assert s["bandwidth_kbps"] == "5000"
    assert s["title"] == "Some Movie"
    assert s["Session"] == "sess-1"


def test_plex_server_stats_aggregates() -> None:
    """plex_server_stats calls sections + active_sessions in sequence."""
    import asyncio
    calls = {"sections": 0, "sessions": 0}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            if "/library/sections" in url and "/all" in url:
                # count endpoint
                return _mock_response(
                    text='<MediaContainer totalSize="42"/>'
                )
            if "/library/sections" in url:
                calls["sections"] += 1
                return _mock_response(text=_SECTIONS_XML)
            if "/status/sessions" in url:
                calls["sessions"] += 1
                return _mock_response(
                    text='<MediaContainer size="0"/>'
                )
            return _mock_response(text="")

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        r = asyncio.run(plex_server_stats())
    assert "library_counts" in r
    assert len(r["library_counts"]) == 2
    assert r["active_sessions"] == 0
    assert r["library_counts"][0]["section"] == "Movies"
    assert r["library_counts"][0]["count"] == 42


def test_plex_status_handles_invalid_xml() -> None:
    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            return _mock_response(text="not xml <<<>>>")

    with patch("homelab_mcp.tools.plex.httpx.AsyncClient", _C):
        import asyncio
        r = asyncio.run(plex_status())
    assert "error" in r
