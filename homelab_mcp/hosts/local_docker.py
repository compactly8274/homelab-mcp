"""``LocalDocker``: talks to a local docker daemon via the docker SDK.

Default socket is ``unix:///var/run/docker.sock``; a custom URL is also
supported for testing or non-default setups.

Both ``list_containers`` and ``list_stacks`` return a flat dict shape
(``NAME``, ``IMAGE``, ``STATE``, ``STATUS``, ``ID``, ``PROJECT``,
``SERVICE``, ``WORKDIR``, ``CONFIGFILES``) that matches what
:class:`~homelab_mcp.hosts.remote_ssh.RemoteSSH` produces, so downstream
tools can treat both backends identically.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, NotFound

from homelab_mcp.hosts.base import CommandResult


def _safe_json(obj: Any) -> Any:
    """Recursively convert an object to a JSON-safe form.

    Docker attrs include datetime, Decimal, and other non-JSON types.
    """
    if isinstance(obj, dict):
        return {k: _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_json(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # datetime, Path, etc.
    return str(obj)


log = logging.getLogger(__name__)


def _container_to_flat_dict(c: Any) -> dict[str, Any]:
    """Flatten a docker SDK Container to the canonical shape."""
    attrs = c.attrs
    name = (attrs.get("Name") or "").lstrip("/")
    state = (attrs.get("State") or {}).get("Status", "")
    labels = attrs.get("Config", {}).get("Labels") or {}
    if not isinstance(labels, dict):
        # some test fakes pass a list-of-pairs or string
        labels = dict(labels) if labels else {}
    image = attrs.get("Config", {}).get("Image", "")
    return {
        "NAME": name,
        "IMAGE": image,
        "STATE": state,
        "STATUS": state,
        "ID": attrs.get("Id", ""),
        "PROJECT": labels.get("com.docker.compose.project", ""),
        "SERVICE": labels.get("com.docker.compose.service", ""),
        "WORKDIR": labels.get("com.docker.compose.project.working_dir", ""),
        "CONFIGFILES": labels.get("com.docker.compose.project.config_files", ""),
    }


class LocalDocker:
    """LocalHost that satisfies the HostClient protocol via the docker SDK."""

    def __init__(
        self,
        name: str,
        socket_url: str = "unix:///var/run/docker.sock",
        verify_connection: bool = False,
    ) -> None:
        if not name:
            raise ValueError("LocalDocker requires a non-empty name")
        self._name = name
        self._socket_url = socket_url
        self._client: docker.DockerClient | None = None
        if verify_connection:
            self._ensure_client()

    @property
    def name(self) -> str:
        return self._name

    def _ensure_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self._socket_url)
        return self._client

    # -- containers ---------------------------------------------------------

    async def list_containers(self, all: bool = True) -> list[dict[str, Any]]:
        cs = self._ensure_client().containers.list(all=all)
        return [_container_to_flat_dict(c) for c in cs]

    async def inspect_container(self, name: str) -> dict[str, Any]:
        try:
            c = self._ensure_client().containers.get(name)
        except NotFound as e:
            raise KeyError(f"container {name!r} not found on host {self._name}") from e
        return _safe_json(c.attrs)

    async def container_logs(self, name: str, tail: int = 200) -> str:
        try:
            c = self._ensure_client().containers.get(name)
        except NotFound as e:
            raise KeyError(f"container {name!r} not found on host {self._name}") from e
        return c.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace")

    async def events(self, since_seconds: int = 300) -> list[dict[str, Any]]:
        """Return docker events from the last N seconds.

        Uses the docker SDK ``APIClient.events(since=...)`` instead
        of shelling out to ``docker events``. The previous
        subprocess approach required the docker CLI on PATH, which
        is not present in the homelab-mcp container — the SDK
        works because the docker socket IS mounted (read-only).

        Output shape: a list of dicts with at least
        ``{status, id, from, Type, Action, Actor}`` per event,
        matching what ``docker events --format {{json .}}`` would
        emit. We decode and return parsed JSON objects.

        Fix 2026-07-18: previously called ``api.events(since=...)``
        without an ``until=`` filter. The docker SDK ``events()``
        is a **streaming generator** — without ``until=`` it stays
        open and yields events as they happen, blocking the caller
        for the full stream timeout (default 10 min) on a quiet
        host. With both ``since=`` and ``until=`` the daemon
        closes the stream after replaying the historical window,
        and the generator returns promptly. Dashboard requests
        that previously took 15s now return in <1s.
        """
        try:
            from docker.errors import APIError
        except ImportError:
            return []
        # APIClient is a different class from DockerClient; both
        # share the same socket.
        try:
            client = self._ensure_client()
            # The high-level client doesn't expose events(); use
            # the low-level APIClient on the same socket.
            api = client.api
        except Exception as e:
            log.warning("LocalDocker.events: cannot get client: %s", e)
            return []
        out: list[dict[str, Any]] = []
        # Pin the start time so the window is stable for the
        # duration of the stream (avoids growing the window
        # mid-iteration if the call is slow).
        import time as _time
        now = int(_time.time())
        try:
            gen = api.events(
                since=now - int(since_seconds),
                until=now,
                decode=True,
            )
            for ev in gen:
                if isinstance(ev, dict):
                    out.append(ev)
                if len(out) >= 50:  # cap so a noisy host doesn't flood
                    break
        except APIError as e:
            log.warning("LocalDocker.events: APIClient.events failed: %s", e)
        except Exception as e:
            log.warning("LocalDocker.events: unexpected error: %s", e)
        return out

    async def list_stacks(self) -> list[dict[str, Any]]:
        """Group running containers by compose project.

        A stack is a compose project (or a single un-managed container).
        Each stack carries the project's working dir so callers can
        ``docker compose pull/up -d`` from the right place.
        """
        cs = await self.list_containers(all=True)
        by_project: dict[str, dict[str, Any]] = {}
        singles: list[dict[str, Any]] = []
        for c in cs:
            project = c.get("PROJECT", "")
            if project:
                stack = by_project.setdefault(project, {
                    "name": project,
                    "host": self._name,
                    "managed_by": "compose",
                    "services": [],
                    "workdir": c.get("WORKDIR", ""),
                })
                service = c.get("SERVICE")
                if service and service not in stack["services"]:
                    stack["services"].append(service)
            else:
                singles.append({
                    "name": c.get("NAME", ""),
                    "host": self._name,
                    "managed_by": "single",
                    "image": c.get("IMAGE", ""),
                    "state": c.get("STATE", ""),
                })
        stacks = list(by_project.values()) + singles
        stacks.sort(key=lambda s: s["name"])
        return stacks

    async def compose_pull(self, stack_dir: str) -> CommandResult:
        return await _run_compose(stack_dir, ["pull"])

    async def compose_up(self, stack_dir: str) -> CommandResult:
        return await _run_compose(stack_dir, ["up", "-d"])

    async def container_action(self, name: str, action: str) -> CommandResult:
        if action not in ("start", "stop", "restart", "kill", "pause", "unpause"):
            return CommandResult(2, "", f"unsupported action: {action}", 0)
        try:
            c = self._ensure_client().containers.get(name)
        except NotFound as e:
            return CommandResult(2, "", f"container not found: {e}", 0)
        t0 = time.monotonic()
        try:
            getattr(c, action)()
        except APIError as e:
            return CommandResult(1, "", str(e), int((time.monotonic() - t0) * 1000))
        return CommandResult(0, "", "", int((time.monotonic() - t0) * 1000))

    async def exec_in_container(
        self,
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        workdir: str | None = None,
        user: str | None = None,
    ) -> CommandResult:
        try:
            c = self._ensure_client().containers.get(name)
        except NotFound as e:
            return CommandResult(2, "", f"container not found: {e}", 0)
        t0 = time.monotonic()
        try:
            # exec_run returns (exit_code, output) where output is a generator
            # for demux=True or bytes for demux=False.
            exit_code, output = c.exec_run(
                command,
                environment=env or {},
                workdir=workdir,
                user=user,
                demux=False,
                tty=False,
            )
            stdout = (output or b"").decode("utf-8", errors="replace")
            stderr = ""
        except APIError as e:
            return CommandResult(1, "", str(e), int((time.monotonic() - t0) * 1000))
        return CommandResult(
            exit_code or 0, stdout, stderr,
            int((time.monotonic() - t0) * 1000),
        )

    async def run_command(self, command: str, timeout: float = 30.0) -> CommandResult:
        t0 = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                functools.partial(
                    subprocess.run,
                    shlex.split(command),
                    capture_output=True, text=True, timeout=timeout,
                )
            )
        except subprocess.TimeoutExpired as e:
            return CommandResult(124, e.stdout or "", f"timeout after {timeout}s",
                                 int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return CommandResult(1, "", f"{type(e).__name__}: {e}",
                                 int((time.monotonic() - t0) * 1000))
        return CommandResult(
            proc.returncode, proc.stdout, proc.stderr,
            int((time.monotonic() - t0) * 1000),
        )



    async def read_file(self, path: str) -> str:
        """Read a file from the local host filesystem."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception as e:
            return f"ERROR: {e}"

    async def write_file(
        self,
        path: str,
        content: str,
        mode: str = "w",
    ) -> CommandResult:
        """Write content to a file on the local host filesystem."""
        t0 = time.monotonic()
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if "b" in mode:
                p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
            else:
                p.write_text(content, encoding="utf-8")
            return CommandResult(0, "", "", int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return CommandResult(1, "", str(e), int((time.monotonic() - t0) * 1000))

    async def copy_to_container(
        self,
        name: str,
        host_path: str,
        container_path: str,
    ) -> CommandResult:
        """Copy a file from the local host into a container."""
        return await self.run_command(
            f"docker cp {shlex.quote(host_path)} {shlex.quote(f'{name}:{container_path}')}",
            timeout=60.0,
        )

async def _run_compose(stack_dir: str, args: list[str], timeout: int = 300) -> CommandResult:
    """Run a ``docker compose ...`` command in the given directory."""
    if not Path(stack_dir).is_dir():
        return CommandResult(2, "", f"stack dir does not exist: {stack_dir}", 0)
    cmd = ["docker", "compose", *args]
    t0 = time.monotonic()
    try:
        proc = await asyncio.to_thread(
            functools.partial(
                subprocess.run, cmd, cwd=stack_dir,
                capture_output=True, text=True, timeout=timeout,
            )
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timeout after {timeout}s", int((time.monotonic() - t0) * 1000))
    return CommandResult(
        proc.returncode, proc.stdout, proc.stderr,
        int((time.monotonic() - t0) * 1000),
    )
