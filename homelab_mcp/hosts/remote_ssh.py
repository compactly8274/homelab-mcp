"""``RemoteSSH``: docker-over-SSH backend.

Talks to a remote host's docker daemon by shelling out via the user's
SSH config (an alias like ``unraid`` or ``truenas``). Used for any host
that isn't the one the daemon is running on.

The container inspect output is normalized to the same shape that
:class:`~homelab_mcp.hosts.local_docker.LocalDocker` returns so the
downstream tool surface doesn't care which backend produced the data.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Any

import asyncssh

from homelab_mcp.hosts.base import CommandResult

_SSH_TIMEOUT_S = 15.0
_SSH_QUIET = True  # suppress banner / motd in stdout


class RemoteSSH:
    """A host backend that runs docker commands over SSH.

    The SSH connection is established lazily on the first command and
    reused for the lifetime of the instance. We don't pool connections
    across instances — each tool call gets its own ``RemoteSSH``.
    """

    def __init__(
        self,
        name: str,
        ssh_alias: str,
        ssh_config_path: str | Path,
        verify_config: bool = True,
    ) -> None:
        if not name:
            raise ValueError("RemoteSSH requires a non-empty name")
        if not ssh_alias:
            raise ValueError("RemoteSSH requires a non-empty ssh_alias")
        if not ssh_config_path:
            raise ValueError("RemoteSSH requires a non-empty ssh_config_path")
        self._name = name
        self._alias = ssh_alias
        self._config_path = Path(ssh_config_path).expanduser()
        if verify_config and not self._config_path.is_file():
            raise FileNotFoundError(f"ssh config not found: {self._config_path}")
        self._conn: asyncssh.SSHClientConnection | None = None

    @property
    def name(self) -> str:
        return self._name

    async def _connect(self) -> asyncssh.SSHClientConnection:
        if self._conn is None or self._conn.is_closed:
            # asyncssh >=2.18 moved config_path off connect(); pass it via config=
            self._conn = await asyncssh.connect(
                self._alias,
                config=[str(self._config_path)],
                known_hosts=None,  # use ~/.ssh/known_hosts by default
            )
        return self._conn

    async def _run(self, command: str, timeout: float = _SSH_TIMEOUT_S) -> CommandResult:
        """Run a shell command on the remote host. Returns CommandResult."""
        conn = await self._connect()
        t0 = time.monotonic()
        try:
            completed = await asyncio.wait_for(
                conn.run(command, check=False, stderr=asyncssh.PIPE),
                timeout=timeout,
            )
        except TimeoutError:
            return CommandResult(124, "", f"timeout after {timeout}s",
                                 int((time.monotonic() - t0) * 1000))
        except (OSError, asyncssh.Error) as e:
            return CommandResult(1, "", f"{type(e).__name__}: {e}",
                                 int((time.monotonic() - t0) * 1000))
        return CommandResult(
            completed.returncode or 0,
            completed.stdout or "",
            completed.stderr or "",
            int((time.monotonic() - t0) * 1000),
        )

    async def aclose(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            self._conn.close()
            await self._conn.wait_closed()
        self._conn = None

    # -- containers ---------------------------------------------------------

    async def list_containers(self, all: bool = True) -> list[dict[str, Any]]:
        """List containers via a flat ``docker ps`` format.

        Uses ``{{.Label "key"}}`` to fetch specific labels without the
        comma-joined-string problem. We pull the four labels that matter
        for stack detection plus the four basic identity fields.

        Quoting history (Fix 2026-07-18):
        - v0.9.0: double-quoted Go template keys inside a single-quoted
          shell string. On unraid the inner double-quotes got expanded
          by the shell as env-var lookups (unraid auto-sets NAME, IMAGE,
          etc. on every container), corrupting the format string.
        - v0.9.3: switched to single-quoted Go template keys. Worked on
          modern docker, but unraid's older Go template engine doesn't
          accept single-quoted string literals, so every docker ps call
          on unraid errored out and returned 0 containers.
        - v0.9.4 (this version): write the format string to a temp file
          on the remote host, then use ``docker ps --format "$(cat
          /tmp/fmt-XXX)"``. This avoids BOTH shell expansion and the
          bash quote-nesting problem; the format is read directly by
          the docker CLI without going through any shell interpreter.
        The format file is removed in a finally block.
        """
        tmpl = (
            "NAME={{.Names}}"
            "\tIMAGE={{.Image}}"
            "\tSTATE={{.State}}"
            "\tSTATUS={{.Status}}"
            "\tID={{.ID}}"
            "\tPROJECT={{.Label \"com.docker.compose.project\"}}"
            "\tSERVICE={{.Label \"com.docker.compose.service\"}}"
            "\tWORKDIR={{.Label \"com.docker.compose.project.working_dir\"}}"
            "\tCONFIGFILES={{.Label \"com.docker.compose.project.config_files\"}}"
        )
        # Use a heredoc to land the format string verbatim on the remote,
        # then read it back with $(cat ...). The whole tmpl never goes
        # through bash's quote parser, so the {{.Label "key"}} double
        # quotes survive intact. mktemp picks a unique name so
        # concurrent calls don't collide.
        all_flag = "--all" if all else ""
        cmd = (
            "TMPL=$(mktemp); "
            "cat > \"$TMPL\" <<'__HOMELAB_MCP_FMT__'\n"
            f"{tmpl}\n"
            "__HOMELAB_MCP_FMT__\n"
            f"docker ps {all_flag} --format \"$(cat $TMPL)\"; "
            "RC=$?; rm -f \"$TMPL\"; exit $RC"
        )
        r = await self._run(cmd, timeout=30.0)
        if not r.ok:
            return []
        out: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 9:
                continue
            # The {{.Label "..."}} template emits the field name followed
            # by '=' even when the label is absent, so an un-managed
            # container comes through as e.g. 'PROJECT='. Strip the
            # 'KEY=' prefixes to leave just the value (empty string
            # when the label is missing) — keeps the contract with
            # LocalDocker.list_containers, where missing labels
            # default to ''.
            out.append({
                "NAME": parts[0].removeprefix("NAME="),
                "IMAGE": parts[1].removeprefix("IMAGE="),
                "STATE": parts[2].removeprefix("STATE="),
                "STATUS": parts[3].removeprefix("STATUS="),
                "ID": parts[4].removeprefix("ID="),
                "PROJECT": parts[5].removeprefix("PROJECT="),
                "SERVICE": parts[6].removeprefix("SERVICE="),
                "WORKDIR": parts[7].removeprefix("WORKDIR="),
                "CONFIGFILES": parts[8].removeprefix("CONFIGFILES="),
            })
        return out

    async def inspect_container(self, name: str) -> dict[str, Any]:
        r = await self._run(f"docker inspect {shlex.quote(name)}", timeout=15.0)
        if not r.ok:
            raise KeyError(f"container {name!r} not found on host {self._name}: {r.stderr.strip()[:200]}")
        try:
            data = json.loads(r.stdout)
        except Exception as e:
            raise KeyError(f"docker inspect returned non-JSON: {e}") from e
        if isinstance(data, list) and data:
            return data[0]
        raise KeyError(f"container {name!r} not found on host {self._name}")

    async def container_logs(self, name: str, tail: int = 200) -> str:
        r = await self._run(
            f"docker logs --tail {int(tail)} --timestamps=false {shlex.quote(name)}",
            timeout=30.0,
        )
        return r.stdout if r.ok else r.stderr

    async def events(self, since_seconds: int = 300) -> list[dict[str, Any]]:
        # Pin now() so the window is stable for the duration of the call.
        # Without --until, `docker events` blocks waiting for new events
        # and the SSH command hits the _run timeout (15s) on quiet hosts.
        # Fix 2026-07-18: matches the same fix applied to LocalDocker.events().
        import time as _time
        now = int(_time.time())
        until_ts = now
        since_ts = now - int(since_seconds)
        r = await self._run(
            f"docker events --since {since_ts} --until {until_ts} "
            f"--format '{{{{json .}}}}'",
            timeout=15.0,
        )
        out: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    async def list_stacks(self) -> list[dict[str, Any]]:
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
        if not stack_dir or not stack_dir.strip():
            return CommandResult(2, "", "stack_dir is empty", 0)
        # Check the path exists remotely before invoking compose.
        check = await self._run(f"test -d {shlex.quote(stack_dir)}", timeout=5.0)
        if not check.ok:
            return CommandResult(2, "", f"stack dir does not exist: {stack_dir}", 0)
        return await self._run(
            f"cd {shlex.quote(stack_dir)} && docker compose pull", timeout=300.0
        )

    async def compose_up(self, stack_dir: str) -> CommandResult:
        if not stack_dir or not stack_dir.strip():
            return CommandResult(2, "", "stack_dir is empty", 0)
        check = await self._run(f"test -d {shlex.quote(stack_dir)}", timeout=5.0)
        if not check.ok:
            return CommandResult(2, "", f"stack dir does not exist: {stack_dir}", 0)
        return await self._run(
            f"cd {shlex.quote(stack_dir)} && docker compose up -d", timeout=300.0
        )

    async def container_action(self, name: str, action: str) -> CommandResult:
        if action not in ("start", "stop", "restart", "kill", "pause", "unpause"):
            return CommandResult(2, "", f"unsupported action: {action}", 0)
        return await self._run(f"docker {action} {shlex.quote(name)}", timeout=30.0)

    async def run_command(self, command: str, timeout: float = 30.0) -> CommandResult:
        return await self._run(command, timeout=timeout)
