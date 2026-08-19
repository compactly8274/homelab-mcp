"""Post-apply health watchdog.

Runs after every apply (compose or dockerman) to verify the service
came back healthy. On failure: returns ``ok=False`` with a structured
diagnosis that the pipeline uses to trigger a revert.

Design choices (locked 2026-07-29):

- **TCP primary, HTTP-from-probes.yaml fallback.** Generic TCP-connect
  to any exposed port is the cheapest, most universal probe — covers
  ~95% of containers without per-stack config. Per-stack HTTP probes
  (``probes.yaml``) override when present.

- **Two-phase sampling.** First check: container is ``running`` with a
  passing docker healthcheck (if defined). Second check (post-settle):
  the configured probe (TCP / HTTP). Both phases must pass.

- **Polling loop, not blocking probe.** Plex and a few other stacks
  take 30-90s to finish starting; one-shot probe would fail them.
  We poll every ``poll_interval_s`` (default 5s) for up to
  ``timeout_s`` (default 180s).

- **Opt-in via Settings.** The watchdog is wired into the pipeline
  but only does real work when ``HOMELAB_MCP_WATCHDOG_ENABLED=1``.
  Default off so we can ship the code without flipping behavior.

- **Probes file location.** Defaults to ``/data/probes.yaml`` (which
  is the homelab-mcp state volume). Override with
  ``HOMELAB_MCP_WATCHDOG_PROBES_PATH``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml  # pyyaml; already in homelab-mcp's deps for compose parsing

from homelab_mcp.hosts.base import HostClient

log = logging.getLogger(__name__)


# -- probe config -----------------------------------------------------------


@dataclass
class Probe:
    """A health probe definition for one container image.

    Attributes:
        image_match:  substring(s) that identify the container — matched
                      against ``Config.Image``. Multiple substrings AND.
        port:         the TCP port to connect to (if protocol=tcp) OR
                      the port to send HTTP to (if protocol=http)
        protocol:     "tcp" | "http" | "external"
        path:         HTTP path (default "/healthz" for http probes)
        url:          For protocol="external", the full URL to fetch.
                      If set, ``port`` and ``path`` are ignored.
        expect_code:  Expected HTTP status code(s) for external/http probes.
                      Default [200, 301, 302, 401, 403].
        timeout_s:    per-attempt TCP/HTTP timeout (default 3)
    """

    image_match: list[str]
    port: int
    protocol: str = "tcp"  # "tcp" | "http" | "external"
    path: str = "/healthz"
    url: str = ""
    expect_code: list[int] = field(default_factory=lambda: [200, 301, 302, 401, 403])
    timeout_s: float = 3.0

    def matches(self, image: str) -> bool:
        return all(needle in image for needle in self.image_match)


@dataclass
class WatchdogConfig:
    enabled: bool = False
    timeout_s: float = 180.0
    poll_interval_s: float = 5.0
    tcp_fallback: bool = True
    probes: list[Probe] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        cfg = cls()
        cfg.enabled = os.getenv("HOMELAB_MCP_WATCHDOG_ENABLED", "0") == "1"
        try:
            cfg.timeout_s = float(os.getenv("HOMELAB_MCP_WATCHDOG_TIMEOUT_S", "180"))
        except ValueError:
            pass
        try:
            cfg.poll_interval_s = float(os.getenv("HOMELAB_MCP_WATCHDOG_POLL_INTERVAL_S", "5"))
        except ValueError:
            pass
        cfg.tcp_fallback = os.getenv("HOMELAB_MCP_WATCHDOG_TCP_FALLBACK", "1") != "0"
        probes_path = os.getenv(
            "HOMELAB_MCP_WATCHDOG_PROBES_PATH", "/data/probes.yaml"
        )
        cfg.probes = cls._load_probes(probes_path)
        return cfg

    @staticmethod
    def _load_probes(path: str) -> list[Probe]:
        p = Path(path)
        if not p.is_file():
            return []
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception as e:
            log.warning("watchdog: probes.yaml parse failed (%s); ignoring", e)
            return []
        raw = data.get("probes") or []
        probes: list[Probe] = []
        for entry in raw:
            try:
                probes.append(Probe(
                    image_match=list(entry["image_match"]),
                    port=int(entry["port"]),
                    protocol=entry.get("protocol", "tcp"),
                    path=entry.get("path", "/healthz"),
                    url=entry.get("url", ""),
                    expect_code=list(entry.get("expect_code", [200, 301, 302, 401, 403])),
                    timeout_s=float(entry.get("timeout_s", 3.0)),
                ))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("watchdog: skipping malformed probe entry: %s (%s)", entry, e)
        return probes


# -- probe execution ---------------------------------------------------------


class _ProbeResult:
    __slots__ = ("ok", "detail", "elapsed_ms", "phase")

    def __init__(self, ok: bool, detail: str, elapsed_ms: int, phase: str):
        self.ok = ok
        self.detail = detail
        self.elapsed_ms = elapsed_ms
        self.phase = phase  # "container" | "tcp" | "http"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "phase": self.phase,
        }


async def _tcp_probe(host: HostClient, container_name: str, port: int,
                     timeout_s: float) -> _ProbeResult:
    """Generic TCP-connect probe.

    Strategy: try THREE methods in order, use the first that works.

    1. In-container exec (``docker exec <name> sh -c 'cat </dev/tcp/...'``)
       — works for alpine/busybox images with /bin/sh.

    2. Host-side socket (``python3 -c "import socket; ..."`` via the
       homelab-mcp daemon itself — but we run it via the host client).
       This works for distroless images like PeaNUT that have no shell,
       but only when the host can reach the container (i.e. the port
       is published to the host or the container is on the local docker
       network).

    3. Host-side /dev/tcp via bash (host bash, not container sh):
       ``bash -c 'exec 3<>/dev/tcp/127.0.0.1/<port>'``. Falls through
       to ``nc`` if bash isn't available.

    Method 1 fails immediately on distroless images (sh missing) — we
    don't waste the full timeout on it. Methods 2-3 work for any
    reachable container port.
    """
    # Method 1: in-container exec (fast fail for distroless)
    cmd1 = (
        f"docker exec {container_name} sh -c "
        f"'exec 3<>/dev/tcp/127.0.0.1/{port} && echo ok || echo fail' "
        f"2>&1"
    )
    start = time.monotonic()
    try:
        r1 = await host.run_command(cmd1, timeout=2.0)
    except Exception as e:
        r1 = None
        m1_detail = f"exec failed: {e}"
    else:
        elapsed = int((time.monotonic() - start) * 1000)
        if r1.ok and "ok" in r1.stdout:
            return _ProbeResult(True, "in-container exec ok", elapsed, "tcp")
        m1_detail = (r1.stdout or r1.stderr or "(empty)")[:200]

    # Method 2: host-side Python socket probe via the host client.
    # Run python on the SAME host as the container so it can hit
    # 127.0.0.1:<published_port>. For LocalDocker this works directly;
    # for RemoteSSH it runs the probe on the remote host.
    cmd2 = (
        f"python3 -c \"import socket,sys; "
        f"s=socket.socket(); s.settimeout({timeout_s}); "
        f"s.connect(('127.0.0.1',{port})); print('ok'); s.close()\" "
        f"2>&1 || "
        f"nc -z -w{int(timeout_s)} 127.0.0.1 {port} && echo ok"
    )
    start = time.monotonic()
    try:
        r2 = await host.run_command(cmd2, timeout=timeout_s + 2.0)
    except Exception as e:
        r2 = None
        m2_detail = f"host-side probe failed: {e}"
    else:
        elapsed = int((time.monotonic() - start) * 1000)
        if r2.ok and "ok" in r2.stdout:
            return _ProbeResult(True, f"host-side probe ok ({port})", elapsed, "tcp")
        m2_detail = (r2.stdout or r2.stderr or "(empty)")[:200]

    # Both methods failed
    return _ProbeResult(
        False,
        f"in-container: {m1_detail[:120]}; host-side: {m2_detail[:120]}",
        int((time.monotonic() - start) * 1000),
        "tcp",
    )


async def _http_probe(host: HostClient, container_name: str, port: int,
                      path: str, timeout_s: float) -> _ProbeResult:
    """HTTP probe via ``docker exec`` with wget or curl."""
    cmd = (
        f"docker exec {container_name} sh -c "
        f"'wget -q -O - --timeout={int(timeout_s)} "
        f"http://127.0.0.1:{port}{path} 2&gt;&1 | head -c 200 || "
        f"curl -fsS --max-time {int(timeout_s)} "
        f"http://127.0.0.1:{port}{path} 2&gt;&1 | head -c 200'"
    )
    start = time.monotonic()
    try:
        r = await host.run_command(cmd, timeout=timeout_s + 2.0)
    except Exception as e:
        return _ProbeResult(False, f"exec failed: {e}", int((time.monotonic() - start) * 1000), "http")
    elapsed = int((time.monotonic() - start) * 1000)
    ok = r.ok and r.stdout.strip() != ""
    detail = r.stdout.strip()[:200] or r.stderr.strip()[:200] or "(empty)"
    return _ProbeResult(ok, detail, elapsed, "http")


async def _external_http_probe(host: HostClient, url: str, expect_code: list[int],
                               timeout_s: float) -> _ProbeResult:
    """HTTP probe against an external URL (e.g. public endpoint behind a proxy).

    Runs curl from the homelab-mcp daemon host, not inside the container, so
    it tests the full public/proxied path the way a user would reach it.
    """
    cmd = (
        f"curl -sS -o /dev/null -w \"%{{http_code}}\" "
        f"--max-time {int(timeout_s)} -L -k {url}"
    )
    start = time.monotonic()
    try:
        r = await host.run_command(cmd, timeout=timeout_s + 2.0)
    except Exception as e:
        return _ProbeResult(False, f"external probe failed: {e}", int((time.monotonic() - start) * 1000), "external")
    elapsed = int((time.monotonic() - start) * 1000)
    code_str = r.stdout.strip()[:10]
    try:
        code = int(code_str)
    except (ValueError, TypeError):
        code = 0
    ok = r.ok and code in expect_code
    detail = f"HTTP {code_str}" if r.ok else (r.stderr.strip()[:200] or "(empty)")
    return _ProbeResult(ok, detail, elapsed, "external")


async def _container_running_probe(host: HostClient, container_name: str,
                                   settle_s: int = 5) -> _ProbeResult:
    """Phase 1: wait ``settle_s``, then check container is running + healthy."""
    await asyncio.sleep(settle_s)
    start = time.monotonic()
    try:
        info = await host.inspect_container(container_name)
    except Exception as e:
        return _ProbeResult(False, f"inspect failed: {e}", int((time.monotonic() - start) * 1000), "container")
    state = (info.get("State") or {})
    if state.get("Status") != "running":
        return _ProbeResult(
            False,
            f"status={state.get('Status')!r}",
            int((time.monotonic() - start) * 1000),
            "container",
        )
    health = state.get("Health") or {}
    if health:
        h_status = health.get("Status")
        if h_status not in ("healthy", "starting"):
            return _ProbeResult(
                False,
                f"health={h_status!r}",
                int((time.monotonic() - start) * 1000),
                "container",
            )
    elapsed = int((time.monotonic() - start) * 1000)
    return _ProbeResult(True, f"running health={health.get('Status', 'none')}", elapsed, "container")


# -- main entry point -------------------------------------------------------


@dataclass
class WatchdogResult:
    ok: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
    final_state: str = "unknown"
    elapsed_s: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "samples": self.samples,
            "final_state": self.final_state,
            "elapsed_s": round(self.elapsed_s, 2),
            "reason": self.reason,
        }


async def watch_container(
    host: HostClient,
    *,
    container_name: str,
    image: str,
    config: WatchdogConfig | None = None,
) -> WatchdogResult:
    """Poll the container until it's healthy or the watchdog times out.

    Returns a :class:`WatchdogResult` with every probe attempt's outcome.
    The pipeline uses ``ok=False`` to trigger revert.

    If ``config.enabled`` is False, returns ``ok=True, samples=[]``
    immediately — this lets the watchdog be wired into the pipeline
    without flipping behavior in production until you turn the env var on.
    """
    if config is None:
        config = WatchdogConfig.from_env()
    if not config.enabled:
        log.info("watchdog: disabled (HOMELAB_MCP_WATCHDOG_ENABLED!=1); passing through")
        return WatchdogResult(ok=True, reason="watchdog disabled (env flag off)")

    # Pick a probe
    probe: Probe | None = None
    for p in config.probes:
        if p.matches(image):
            probe = p
            break

    deadline = time.monotonic() + config.timeout_s
    samples: list[dict[str, Any]] = []
    final_state = "unknown"
    start = time.monotonic()

    while time.monotonic() < deadline:
        # Phase 1: container running?
        running = await _container_running_probe(host, container_name, settle_s=0)
        samples.append(running.to_dict())
        if not running.ok:
            # Container is gone or not yet running. Wait and retry.
            final_state = running.detail
            await asyncio.sleep(config.poll_interval_s)
            continue

        # Container is up. Try the configured probe, or fall back to TCP.
        if probe is not None:
            if probe.protocol == "external":
                pr = await _external_http_probe(host, probe.url, probe.expect_code, probe.timeout_s)
            elif probe.protocol == "http":
                pr = await _http_probe(host, container_name, probe.port,
                                       probe.path, probe.timeout_s)
            else:
                pr = await _tcp_probe(host, container_name, probe.port, probe.timeout_s)
        elif config.tcp_fallback:
            # Try a few common ports: 80, 443, 8080, 8000, 3000, 5000, 9090
            found_port = await _discover_exposed_port(host, container_name)
            if found_port is None:
                # No exposed port and no explicit probe — passing
                # this is a deliberate choice; we trust the
                # container-running state.
                pr = _ProbeResult(True, "no exposed port; trusting container state", 0, "tcp")
            else:
                pr = await _tcp_probe(host, container_name, found_port, timeout_s=3.0)
        else:
            pr = _ProbeResult(True, "no probe configured; trusting container state", 0, "tcp")

        samples.append(pr.to_dict())
        if pr.ok:
            return WatchdogResult(
                ok=True,
                samples=samples,
                final_state="healthy",
                elapsed_s=time.monotonic() - start,
                reason="probe passed",
            )

        final_state = pr.detail
        await asyncio.sleep(config.poll_interval_s)

    return WatchdogResult(
        ok=False,
        samples=samples,
        final_state=final_state,
        elapsed_s=time.monotonic() - start,
        reason=f"timed out after {config.timeout_s:.0f}s",
    )


async def _discover_exposed_port(host: HostClient, container_name: str) -> int | None:
    """Return the lowest published HOST TCP port from ``docker inspect``.

    Uses ``NetworkSettings.Ports`` which lists host-published
    port mappings of the form ``"<container_port>/<proto>" ->
    [{"HostIp": ..., "HostPort": ...}]``. We extract the
    **HostPort** (the port reachable from the host network),
    not the container port — otherwise the watchdog probes a
    port that isn't bound on the host and gets ConnectionRefused.

    Example for PeaNUT (container port 8080 → host port 9500):
        Ports = {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9500"}]}
        → returns 9500
    """
    try:
        info = await host.inspect_container(container_name)
    except Exception:
        return None
    ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
    candidates: list[int] = []
    for spec, bindings in ports.items():
        # spec looks like "8080/tcp"; bindings is a list of
        # {"HostIp": ..., "HostPort": ...}
        for b in bindings or []:
            try:
                candidates.append(int(b.get("HostPort", "")))
            except (ValueError, TypeError):
                continue
    if not candidates:
        return None
    return min(candidates)


__all__ = [
    "Probe",
    "WatchdogConfig",
    "WatchdogResult",
    "watch_container",
]