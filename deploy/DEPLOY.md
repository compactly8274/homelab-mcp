# Deploy benchmark framework Phase 1 (`exec_in_container` tool)

## What this branch changes
- Adds `exec_in_container_tool` with a strict command allowlist and real preflight gate.
- Fixes the previously-broken preflight action for `exec_in_container`.
- Corrects `HOMELAB_MCP_PRODUCTION_HOURS` default to `["00:00-05:00"]` PT.
- Adds unit tests + deploy/rollback notes.

## Pre-deploy checklist
1. Verify you are on the **clean** branch:
   ```bash
   cd /mnt/Data/appdata/homelab-mcp/src
   git fetch origin
   git status   # should show feature/benchmark-framework-clean
   git log --oneline -3
   ```
2. Save the current live compose baseline (if not already saved):
   ```bash
   cp /mnt/Data/appdata/dockge/stacks/homelab-mcp/compose.yaml \
      /mnt/Data/appdata/dockge/stacks/homelab-mcp/compose.yaml.pre-benchmark.bak
   ```
3. Review the diff of the reference compose:
   ```bash
   diff /mnt/Data/appdata/dockge/stacks/homelab-mcp/compose.yaml \
        /mnt/Data/appdata/homelab-mcp/src/deploy/compose.yaml.benchmark
   ```
4. Make sure rollback script is in place:
   `/mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark.sh`

## Deploy (within 00:00-05:00 PT only)
```bash
cd /mnt/Data/appdata/dockge/stacks/homelab-mcp
cp /mnt/Data/appdata/homelab-mcp/src/deploy/compose.yaml.benchmark ./compose.yaml
# Preserve your real env vars (token, API keys, host list if overridden):
# .env or Dockge env settings are not replaced by this file.
docker compose up -d --force-recreate homelab-mcp
sleep 20
docker ps -a | grep homelab-mcp
docker logs --tail 30 homelab-mcp
```

## Smoke test after deploy
Use the WebUI/Claude to call `exec_in_container_tool`:
- `host=truenas`, `container=prowlarr`, `command=["ls", "/app"]` → should succeed.
- `command=["rm", "-rf", "/"]` → should be blocked by the allowlist.
- Try `require_approval=true` on a stopped container → preflight should block with `preflight` present.

## Rollback
If anything looks wrong, run:
```bash
bash /mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark.sh
```
This restores the pre-Phase-1 compose baseline and recreates the container.

## Do not merge this PR until
- This deployment note has been executed once successfully, or
- The file-level benchmark mounts have been removed and the code instead lands in the shipped image.
