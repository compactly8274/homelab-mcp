"""Internal helpers for tools: state accessor with friendly errors."""

from __future__ import annotations

from homelab_mcp.server import get_state as _get_state
from homelab_mcp.state import State


def get_state() -> State:
    """Return the State singleton or raise a clear error."""
    try:
        return _get_state()
    except RuntimeError as e:
        raise RuntimeError(
            f"state not initialized; the server must be started via __main__: {e}"
        ) from e
