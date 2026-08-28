"""Common types and the HostClient protocol.

Both :class:`~homelab_mcp.hosts.local_docker.LocalDocker` and
:class:`~homelab_mcp.hosts.remote_ssh.RemoteSSH` implement the same
``HostClient`` surface so that tools can call them interchangeably.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class CommandResult:
    """Result of a docker / docker-compose shell command."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class HostClient(Protocol):
    """Protocol every host backend implements.

    The methods are intentionally coroutines even when the local backend
    could return synchronously — this keeps the tool surface uniform.
    """

    @property
    def name(self) -> str: ...

    async def list_containers(self, all: bool = True) -> list[dict[str, Any]]: ...

    async def inspect_container(self, name: str) -> dict[str, Any]: ...

    async def container_logs(self, name: str, tail: int = 200) -> str: ...

    async def list_stacks(self) -> list[dict[str, Any]]: ...

    async def compose_pull(self, stack_dir: str) -> CommandResult: ...

    async def compose_up(self, stack_dir: str) -> CommandResult: ...

    async def container_action(self, name: str, action: str) -> CommandResult: ...

    async def events(self, since_seconds: int = 300) -> list[dict[str, Any]]: ...

    async def run_command(
        self, command: str, timeout: float = 30.0
    ) -> CommandResult: ...

    async def exec_in_container(
        self,
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        workdir: str | None = None,
        user: str | None = None,
    ) -> CommandResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(
        self,
        path: str,
        content: str,
        mode: str = "w",
    ) -> CommandResult: ...

    async def copy_to_container(
        self,
        name: str,
        host_path: str,
        container_path: str,
    ) -> CommandResult: ...
