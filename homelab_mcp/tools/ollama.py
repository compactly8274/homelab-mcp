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


@mcp.tool()
async def ollama_show_running() -> dict[str, Any]:
    """Return every loaded model with its disk + VRAM footprint in one call.

    Convenience for "what's actually in GPU right now?" triage. Combines
    ``/api/tags`` (downloaded) and ``/api/ps`` (loaded into VRAM) and
    joins on model name.

    Returns:
        dict with ``instance``, ``models`` (list of {name, size_gb,
        size_vram_gb, expires_at, digest, family, parameter_size,
        quantization_level, loaded}), ``loaded_count``, ``total_count``,
        and ``vram_total_gb`` (sum of size_vram_gb across loaded models).
    """
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            tags_r, ps_r = await client.get(f"{base}/api/tags"), await client.get(f"{base}/api/ps")
            tags_r.raise_for_status()
            ps_r.raise_for_status()
            tags = tags_r.json()
            ps = ps_r.json()
    except httpx.HTTPError as e:
        return {"error": f"ollama show-running request failed: {e}", "instance": base}
    by_name = {}
    for m in tags.get("models", []):
        size = m.get("size", 0) or 0
        by_name[m.get("name", "")] = {
            "name": m.get("name", ""),
            "size_bytes": size,
            "size_gb": round(size / (1024**3), 2) if size else 0,
            "digest": m.get("digest", ""),
            "family": m.get("details", {}).get("family", ""),
            "parameter_size": m.get("details", {}).get("parameter_size", ""),
            "quantization_level": m.get("details", {}).get("quantization_level", ""),
            "size_vram_bytes": 0,
            "size_vram_gb": 0.0,
            "expires_at": "",
            "loaded": False,
        }
    vram_total = 0.0
    loaded_count = 0
    for m in ps.get("models", []):
        name = m.get("name", "")
        vram = m.get("size_vram", 0) or 0
        vram_gb = round(vram / (1024**3), 2) if vram else 0.0
        if name in by_name:
            by_name[name]["size_vram_bytes"] = vram
            by_name[name]["size_vram_gb"] = vram_gb
            by_name[name]["expires_at"] = m.get("expires_at", "")
            by_name[name]["loaded"] = True
        else:
            # A loaded model not in /api/tags is unusual but possible (evicted
            # state); surface it anyway.
            size = m.get("size", 0) or 0
            by_name[name] = {
                "name": name,
                "size_bytes": size,
                "size_gb": round(size / (1024**3), 2) if size else 0,
                "digest": m.get("digest", ""),
                "family": "",
                "parameter_size": "",
                "quantization_level": "",
                "size_vram_bytes": vram,
                "size_vram_gb": vram_gb,
                "expires_at": m.get("expires_at", ""),
                "loaded": True,
            }
        vram_total += vram_gb
        loaded_count += 1
    models = sorted(by_name.values(), key=lambda m: m["name"])
    return {
        "instance": base,
        "models": models,
        "loaded_count": loaded_count,
        "total_count": len(models),
        "vram_total_gb": round(vram_total, 2),
    }


@mcp.tool()
async def ollama_pull_model(name: str, insecure: bool = False) -> dict[str, Any]:
    """Pull a model from the Ollama library (multi-GB download).

    **Gated by config.** This tool refuses to run unless
    ``HOMELAB_MCP_OLLAMA_ALLOW_PULL=true`` is set on the daemon. Default
    is off so an LLM cannot accidentally yank arbitrary models.

    Args:
        name: model name to pull, e.g. ``llama3.2:3b`` or ``qwen2.5:14b``.
        insecure: if True, allow pulling over HTTP (not HTTPS). Default False.

    Returns:
        dict with ``pulled`` (bool), ``name``, ``instance``, and
        ``error`` if any. Pulls are synchronous and may take several
        minutes for large models; the HTTP timeout here is 30 minutes.
    """
    s = _get_settings()
    if not s.ollama_allow_pull:
        return {
            "pulled": False,
            "name": name,
            "error": (
                "ollama_pull_model is disabled. Set "
                "HOMELAB_MCP_OLLAMA_ALLOW_PULL=true and restart the daemon "
                "to enable it. (This is a write tool — keep it off unless "
                "you've audited the deployment.)"
            ),
        }
    if not name or not name.strip():
        return {"pulled": False, "error": "name must be non-empty"}
    # Ollama's /api/pull streams JSON lines; we don't stream, we read the
    # final response. For a 30-GB model this can take 10+ minutes, so use
    # a 30-minute client timeout.
    url = f"{_base_url()}/api/pull"
    payload: dict[str, Any] = {"model": name.strip(), "stream": False}
    if insecure:
        payload["insecure"] = True
    try:
        async with httpx.AsyncClient(timeout=1800.0) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {
            "pulled": False,
            "name": name,
            "error": f"ollama pull request failed: {e}",
            "url": url,
        }
    return {"pulled": True, "name": name, "instance": _base_url()}


@mcp.tool()
async def ollama_delete_model(name: str) -> dict[str, Any]:
    """Delete a downloaded model from the Ollama disk cache.

    **Gated by config.** This tool refuses to run unless
    ``HOMELAB_MCP_OLLAMA_ALLOW_DELETE=true`` is set on the daemon.
    Default is off. The model is force-unloaded from VRAM first if
    loaded.

    Args:
        name: model name to delete, e.g. ``llama3.2:3b``.

    Returns:
        dict with ``deleted`` (bool), ``name``, ``instance``, and
        ``error`` if any.
    """
    s = _get_settings()
    if not s.ollama_allow_delete:
        return {
            "deleted": False,
            "name": name,
            "error": (
                "ollama_delete_model is disabled. Set "
                "HOMELAB_MCP_OLLAMA_ALLOW_DELETE=true and restart the daemon "
                "to enable it. (This is a destructive tool — keep it off "
                "unless you've audited the deployment.)"
            ),
        }
    if not name or not name.strip():
        return {"deleted": False, "error": "name must be non-empty"}
    # If the model is currently loaded, unload first so the delete succeeds.
    ps_url = f"{_base_url()}/api/ps"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            ps_r = await client.get(ps_url)
            ps_r.raise_for_status()
            loaded = any(m.get("name") == name.strip() for m in ps_r.json().get("models", []))
    except httpx.HTTPError:
        loaded = False
    if loaded:
        await ollama_unload_model(name)
    # DELETE /api/delete with {"model": "..."} body (Ollama quirk: it's a POST
    # with a "name" body, not a real HTTP DELETE; the docs are inconsistent).
    url = f"{_base_url()}/api/delete"
    payload = {"model": name.strip()}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request("DELETE", url, json=payload)
            r.raise_for_status()
    except httpx.HTTPError as e:
        return {
            "deleted": False,
            "name": name,
            "error": f"ollama delete request failed: {e}",
            "url": url,
        }
    return {"deleted": True, "name": name, "instance": _base_url()}
