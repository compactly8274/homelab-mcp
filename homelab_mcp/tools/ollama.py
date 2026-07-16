"""Ollama model-housekeeping tools.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Endpoint is configured via ``HOMELAB_MCP_OLLAMA_URL``
(default ``http://192.168.1.104:11434``). No API key required for
Ollama.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from homelab_mcp.config import Settings
from homelab_mcp.server import mcp

log = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://192.168.1.104:11434"
_TIMEOUT = 30.0

_cached_settings: Settings | None = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


def _base_url() -> str:
    s = _get_settings()
    return (s.ollama_url or _DEFAULT_OLLAMA_URL).rstrip("/")


@mcp.tool()
async def ollama_list_models() -> dict[str, Any]:
    """List all models available on the Ollama instance (downloaded + ready).

    Returns a dict with ``models`` (list of {name, size_bytes, size_gb,
    modified_at, digest, details}) and ``instance`` (the URL queried).
    """
    url = f"{_base_url()}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"ollama list request failed: {e}", "url": url}
    out = []
    for m in data.get("models", []):
        size = m.get("size", 0) or 0
        out.append(
            {
                "name": m.get("name", ""),
                "size_bytes": size,
                "size_gb": round(size / (1024**3), 2) if size else 0,
                "modified_at": m.get("modified_at", ""),
                "digest": m.get("digest", ""),
                "family": m.get("details", {}).get("family", ""),
                "parameter_size": m.get("details", {}).get("parameter_size", ""),
                "quantization_level": m.get("details", {}).get("quantization_level", ""),
            }
        )
    out.sort(key=lambda m: m["name"])
    return {"instance": _base_url(), "models": out, "count": len(out)}


@mcp.tool()
async def ollama_list_running() -> dict[str, Any]:
    """List models currently loaded into VRAM on the Ollama instance.

    Returns a dict with ``models`` (list of {name, size_bytes, size_gb,
    size_vram_bytes, expires_at, digest}) and ``instance`` (the URL).
    """
    url = f"{_base_url()}/api/ps"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"ollama ps request failed: {e}", "url": url}
    out = []
    for m in data.get("models", []):
        size = m.get("size", 0) or 0
        vram = m.get("size_vram", 0) or 0
        out.append(
            {
                "name": m.get("name", ""),
                "size_bytes": size,
                "size_gb": round(size / (1024**3), 2) if size else 0,
                "size_vram_bytes": vram,
                "size_vram_gb": round(vram / (1024**3), 2) if vram else 0,
                "expires_at": m.get("expires_at", ""),
                "digest": m.get("digest", ""),
            }
        )
    return {"instance": _base_url(), "models": out, "count": len(out)}


@mcp.tool()
async def ollama_unload_model(name: str) -> dict[str, Any]:
    """Unload a specific model from VRAM (keeps it on disk, just frees GPU).

    Args:
        name: model name to unload (e.g. ``llama3.2:3b`` or the digest
            prefix). Must exactly match the ``name`` field returned by
            ``ollama_list_running``.

    Returns:
        dict with ``unloaded`` (bool), ``name`` (echoed), and ``error`` if any.
        Note: Ollama unloads models based on TTL expiry by default; this
        forces an immediate unload of the named model.
    """
    if not name or not name.strip():
        return {"error": "name must be non-empty"}
    # The "right" Ollama API for unload is keep_alive=0 on a generate/chat
    # call. We do a tiny generate with keep_alive=0 to evict the model.
    url = f"{_base_url()}/api/generate"
    payload = {
        "model": name.strip(),
        "prompt": "",
        "keep_alive": 0,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {
            "unloaded": False,
            "name": name,
            "error": f"ollama unload request failed: {e}",
            "url": url,
        }
    return {"unloaded": True, "name": name, "instance": _base_url()}


@mcp.tool()
async def ollama_unload_all() -> dict[str, Any]:
    """Unload every currently-running model from VRAM.

    Convenience tool equivalent to calling ``ollama_unload_model`` for
    every model in ``ollama_list_running``. Useful for the OWUI "VRAM
    Unload" workflow: free all GPU memory between heavy tasks.

    Returns:
        dict with ``unloaded_count`` (number of models successfully
        unloaded), ``failed`` (list of {name, error}), and ``instance``.
    """
    running = await ollama_list_running()
    if "error" in running:
        return running
    models = running.get("models", [])
    if not models:
        return {
            "unloaded_count": 0,
            "failed": [],
            "total_running": 0,
            "instance": _base_url(),
            "note": "no models were loaded",
        }
    failed: list[dict[str, Any]] = []
    unloaded = 0
    for m in models:
        result = await ollama_unload_model(m["name"])
        if result.get("unloaded"):
            unloaded += 1
        else:
            failed.append({"name": m["name"], "error": result.get("error", "unknown")})
    return {
        "unloaded_count": unloaded,
        "failed": failed,
        "instance": _base_url(),
        "total_running": len(models),
    }


@mcp.tool()
async def ollama_show_model(name: str) -> dict[str, Any]:
    """Get detailed info about a specific model (parameters, template, license).

    Args:
        name: model name (e.g. ``llama3.2:3b``).

    Returns:
        dict with model ``modelfile``, ``parameters``, ``template``,
        ``details`` (family, parameter_size, quantization_level),
        ``license``, and ``instance``.
    """
    if not name or not name.strip():
        return {"error": "name must be non-empty"}
    url = f"{_base_url()}/api/show"
    payload = {"name": name.strip()}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"ollama show request failed: {e}", "url": url}
    return {
        "instance": _base_url(),
        "name": name,
        "modelfile": data.get("modelfile", ""),
        "parameters": data.get("parameters", ""),
        "template": data.get("template", ""),
        "details": data.get("details", {}),
        "model_info": data.get("model_info", {}),
        "license": data.get("license", ""),
    }


@mcp.tool()
async def ollama_version() -> dict[str, Any]:
    """Return the Ollama server version and instance URL.

    Returns:
        dict with ``version`` (e.g. "0.32.0") and ``instance``.
    """
    url = f"{_base_url()}/api/version"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"ollama version request failed: {e}", "url": url}
    return {"instance": _base_url(), "version": data.get("version", "unknown")}
