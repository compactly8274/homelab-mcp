from __future__ import annotations

import time
from typing import Any

from homelab_mcp.hosts.base import CommandResult
from homelab_mcp.server import get_host

_MAX_TIMEOUT_S = 30.0


async def http_probe_tool(
    url: str,
    method: str = "GET",
    timeout: float = 10.0,
    host: str | None = None,
    allow_redirects: bool = True,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Probe an HTTP(S) endpoint and return timing + response metadata.

    Runs curl on the specified host (or the local daemon host if none
    is provided). This is a read-only, non-destructive probe; it never
    writes to the target service.
    """
    if not url:
        return {"ok": False, "error": "url is required"}

    method = (method or "GET").upper()
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        return {"ok": False, "error": f"unsupported HTTP method: {method}"}

    if "http://" not in url and "https://" not in url:
        url = f"http://{url}"

    timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT_S))

    header_args = []
    for h in _flatten_headers(headers or {}):
        header_args.extend(["-H", h])

    redirect_flag = ["-L"] if allow_redirects else []
    method_flag = ["-X", method] if method != "GET" else []
    output_flag = ["-I"] if method == "HEAD" else ["-o", "/dev/null"]

    fmt = (
        "%{http_code}\t%{time_total}\t%{size_download}\t"
        "%{content_type}\t%{url_effective}\t%{redirect_url}\n"
    )

    cmd_tokens = (
        ["curl", "-sS", *output_flag, "-m", f"{timeout:.1f}", "-w", fmt]
        + redirect_flag
        + method_flag
        + header_args
        + [url]
    )
    # Build a shell string with proper quoting. run_command splits via shlex.
    cmd = " ".join(_quote(t) for t in cmd_tokens)

    target_host = host or "local"
    try:
        h = get_host(target_host)
    except KeyError as e:
        return {"ok": False, "error": f"unknown host: {target_host} ({e})"}

    t0 = time.monotonic()
    r: CommandResult = await h.run_command(cmd, timeout=timeout + 5.0)
    duration_ms = int((time.monotonic() - t0) * 1000)

    parsed = _parse_curl_output(r.stdout)
    http_code = parsed.get("http_code")
    ok = r.ok and http_code is not None and 100 <= http_code < 600
    return {
        "ok": ok,
        "host": target_host,
        "url": url,
        "method": method,
        "curl_exit_code": r.exit_code,
        "http_code": http_code,
        "time_total_seconds": parsed.get("time_total_seconds"),
        "size_download_bytes": parsed.get("size_download_bytes"),
        "content_type": parsed.get("content_type"),
        "url_effective": parsed.get("url_effective"),
        "redirect_url": parsed.get("redirect_url"),
        "stderr": r.stderr.strip() or None,
        "duration_ms": duration_ms,
    }


def _quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def _flatten_headers(headers: dict[str, str] | list[str]) -> list[str]:
    if isinstance(headers, dict):
        return [f"{k}: {v}" for k, v in headers.items()]
    return list(headers)


def _parse_curl_output(stdout: str) -> dict[str, Any]:
    """Parse the curl -w output line."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {}
    parts = lines[-1].split("\t")
    if len(parts) < 5:
        return {}
    try:
        return {
            "http_code": int(parts[0]) if parts[0].isdigit() else None,
            "time_total_seconds": float(parts[1]) if parts[1] else None,
            "size_download_bytes": int(parts[2]) if parts[2] else None,
            "content_type": parts[3] or None,
            "url_effective": parts[4] or None,
            "redirect_url": parts[5] if len(parts) > 5 else None,
        }
    except Exception:
        return {}

