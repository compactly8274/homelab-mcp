"""Load patched module overlays when running inside the Docker container."""

import sys
from pathlib import Path

import pytest

PATCHES = Path("/mnt/Data/appdata/homelab-mcp/patches")
if PATCHES.exists():
    # Ensure any patched source modules shadow the installed package copy.
    for p in sorted(PATCHES.rglob("*.py")):
        rel = p.relative_to(PATCHES)
        pkg_parts = rel.with_suffix("").parts
        mod_name = ".".join(pkg_parts)
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        for i in range(1, len(pkg_parts)):
            parent = ".".join(pkg_parts[:i])
            if parent in sys.modules:
                del sys.modules[parent]


@pytest.fixture(autouse=True)
def _reset_searxng_client():
    """Isolate SearXNG global client/caches between tests."""
    from homelab_mcp.tools import searxng as mod

    mod._searxng_client = None
    mod._searxng_config_cache = None
    mod._searxng_config_cache_ts = 0.0
    mod._enabled_engines_cache = None
    mod._enabled_engines_cache_ts = 0.0
    yield
    mod._searxng_client = None
    mod._searxng_config_cache = None
    mod._searxng_config_cache_ts = 0.0
    mod._enabled_engines_cache = None
    mod._enabled_engines_cache_ts = 0.0
