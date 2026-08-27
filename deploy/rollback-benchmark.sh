#!/usr/bin/env bash
# rollback-benchmark.sh
# Reverts the live homelab-mcp stack to the pre-benchmark baseline.
# Run by hand inside the production window (00:00-05:00 PT) only.
set -euo pipefail

LIVE_DIR="/mnt/Data/appdata/dockge/stacks/homelab-mcp"
SRC_DIR="/mnt/Data/appdata/homelab-mcp/src"
BACKUP_BASE="$LIVE_DIR/compose.yaml.benchmark-phase1-direct-edit-20260827.bak"
PRE_MESH_BASE="$LIVE_DIR/compose.yaml.pre-mesh.bak"
TS=$(date +%Y%m%d-%H%M%S)

echo "[rollback-benchmark] starting at $TS"

# 1. Snapshot whatever is currently live
cp "$LIVE_DIR/compose.yaml" "$LIVE_DIR/compose.yaml.rollback-$TS.bak"

# 2. Restore the pre-benchmark compose baseline (production hours left at .env/default 06:00-24:00)
if [ -f "$PRE_MESH_BASE" ]; then
    echo "[rollback-benchmark] restoring $PRE_MESH_BASE -> compose.yaml"
    cp "$PRE_MESH_BASE" "$LIVE_DIR/compose.yaml"
else
    echo "[rollback-benchmark] WARNING: no pre-mesh baseline found; leaving current compose snapshot at $LIVE_DIR/compose.yaml.rollback-$TS.bak"
fi

# 3. Leave source code alone, but make sure we are not mounting a feature-branch overlay
git -C "$SRC_DIR" checkout main 2>/dev/null || git -C "$SRC_DIR" checkout master 2>/dev/null || echo "[rollback-benchmark] could not checkout main/master; verify branch manually"

# 4. Recreate container with baseline image/config
cd "$LIVE_DIR"
docker compose up -d --force-recreate homelab-mcp

# 5. Wait for healthcheck
echo "[rollback-benchmark] waiting for healthcheck..."
for i in {1..30}; do
    if docker inspect --format='{{.State.Health.Status}}' homelab-mcp 2>/dev/null | grep -q healthy; then
        echo "[rollback-benchmark] container healthy"
        break
    fi
    sleep 2
done

echo "[rollback-benchmark] done. Current compose: $LIVE_DIR/compose.yaml"
echo "[rollback-benchmark] snapshot before rollback: $LIVE_DIR/compose.yaml.rollback-$TS.bak"
