# Deploy benchmark framework (Phases 1 + 2 + 3)

## What this branch changes
* Phase 3: `container_metrics_tool` for point-in-time Docker stats on any host.
- Adds `exec_in_container_tool` with a strict command allowlist and real preflight gate.
- Adds `http_probe_tool` for curl-based health/endpoint checks inside the container network.
- Adds `db_snapshot_tool` / `db_restore_tool` using Python's stdlib `sqlite3` inside containers.
- Fixes the preflight `valid` action set to include `start` and `kill`.
- Corrects `HOMELAB_MCP_PRODUCTION_HOURS` default to `["00:00-05:00"]` PT.
- Adds unit tests + deploy/rollback notes.

## Pre-deploy checklist
1. Verify you are on `main` (or the relevant release tag):
   ```bash
   cd /mnt/Data/appdata/homelab-mcp/src
   git fetch origin
   git status
   git log --oneline -5
   ```
2. Save the current live compose baseline:
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

## Snapshot / restore path convention
The container mounts `/mnt/Data/appdata/homelab-mcp` as `/data` **read-write**, while the full
`/mnt/Data/appdata` tree is mounted read-only. When using `db_snapshot_tool` or `db_restore_tool`,
always use a path under `/data/...` from the container's perspective, e.g.:
- snapshot_path: `/data/backups/prowlarr.db.snapshot.sql`
- db_path: `/config/prowlarr.db`

This writes to `/mnt/Data/appdata/homelab-mcp/backups/...` on the host. Use absolute paths
inside typical container data dirs (`/config/`, `/data/`, `/app/`, etc.) for `db_path`.

## Smoke test after deploy
Use the WebUI/Claude to call:
- `exec_in_container_tool(host="truenas", container="prowlarr", command=["ls", "/app"])` → allowed.
- `exec_in_container_tool(..., command=["rm", "-rf", "/"])` → blocked by allowlist.
- `http_probe_tool(url="http://prowlarr:9696", host="truenas")` → returns HTTP status.
- `db_snapshot_tool(host="truenas", container="prowlarr", db_path="/config/prowlarr.db",
  snapshot_path="/data/backups/prowlarr-test.sql")` → creates snapshot.
- `db_restore_tool(..., db_path="/config/prowlarr-restored.db",
  snapshot_path="/data/backups/prowlarr-test.sql")` → restores.
- `container_metrics_tool(host="truenas", container="prowlarr")` → returns CPU/mem/net/block-IO stats.

## Rollback
If anything looks wrong, run:
```bash
bash /mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark.sh
```
This restores the pre-Phase-1 compose baseline and recreates the container.

## Do not ship until
- This deployment has been exercised once successfully, or
- The file-level benchmark mounts have been removed and the code instead lands in the shipped image.
