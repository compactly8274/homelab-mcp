# homelab-mcp

MCP server: homelab diagnostics + auto-update pipeline.

- **Read-only diagnostics** (list_stacks, stack_status, recent_events, get_logs, check_nfs_shares, check_dns, check_vpn_health)
- **Visibility drift scanner** (trigger_scan, list_pending_updates, pending_update_dismiss)
- **Apply pipeline** (apply_update_tool, rollback_tool, update_pipeline_tool with `dry_run` pre-flight and automatic rollback on apply failure)
- **Auto-apply orchestrator** (`python -m homelab_mcp.auto_apply_main`) — fetches release notes, classifies via an LLM (SAFE/CAUTION/BREAKING), auto-applies SAFE + CAUTION, and notifies on BREAKING.

See `scripts/deploy-on-truenas.sh` and `scripts/install-on-unraid.sh` for the install paths.

Quick start:

```bash
uv sync --extra dev
uv run pytest
uv run python -m homelab_mcp               # start the MCP server
uv run python -m homelab_mcp.auto_apply_main --dry-run   # one cycle, no applies
```
