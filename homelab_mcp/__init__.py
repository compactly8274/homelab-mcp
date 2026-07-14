"""homelab-mcp: MCP server for homelab diagnostics + auto-update pipeline.

Public surface:

- :mod:`homelab_mcp.config` — env-var driven Settings (pydantic).
- :mod:`homelab_mcp.state` — async SQLite state layer.
- :mod:`homelab_mcp.hosts` — HostClient protocol + LocalDocker + RemoteSSH.
- :mod:`homelab_mcp.updater` — registry client, drift scanner, apply/rollback
  pipeline, release-notes fetcher, LLM risk classifier, notifier,
  auto-apply orchestrator, and the cron entry point.
- :mod:`homelab_mcp.server` — FastMCP singleton + tool wiring.
- :mod:`homelab_mcp.tools` — MCP tool wrappers (stacks, events, health,
  apply/rollback, drift visibility).
"""
