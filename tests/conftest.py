"""Load patched module overlays when running inside the Docker container."""

import sys
from pathlib import Path

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
