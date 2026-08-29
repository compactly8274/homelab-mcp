#!/usr/bin/env bash
# Deploy benchmark framework Phases 4-6 to the live homelab-mcp daemon.
# This script is safe to run only during the 00:00-05:00 America/Los_Angeles window.
# It adds file-level mounts for benchmark_load, benchmark_restart, and benchmark_diff,
# recreates the container, and verifies health. On failure it restores the backup.
#
# Usage (from truenas, during the window):
#   bash /mnt/Data/appdata/homelab-mcp/src/deploy/deploy-benchmark-phase6.sh

set -euo pipefail

STACK_DIR="/mnt/Data/appdata/dockge/stacks/homelab-mcp"
COMPOSE="$STACK_DIR/compose.yaml"
BENCHMARK_COMPOSE="/mnt/Data/appdata/homelab-mcp/src/deploy/compose.yaml.benchmark"
BACKUP_DIR="$STACK_DIR"
ROLLBACK_SCRIPT="/mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark-phase6.sh"

# --- Production-hours guard ---
export TZ=America/Los_Angeles
hour_pt=$(date +%H)
if (( hour_pt < 0 || hour_pt >= 5 )); then
  echo "ERROR: current PT hour is $hour_pt. Deploy allowed only 00:00-05:00 PT."
  exit 1
fi

# --- Backup current compose ---
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_DIR/compose.yaml.pre-phase6-$TS.bak"
cp -v "$COMPOSE" "$BACKUP"
echo "BACKUP_PATH=$BACKUP" > "$STACK_DIR/.rollback-phase6.env"

# --- Ensure the three new file mounts are present (idempotent) ---
add_mount() {
  local src="$1"
  local dst="$2"
  local line="      - $src:$dst:ro"
  if ! grep -qF "$line" "$COMPOSE"; then
    # Insert before the smart_apply.py patch line to keep benchmark mounts grouped
    if grep -qF "# v0.9.14-hermes-2: smart_apply.py" "$COMPOSE"; then
      sed -i "/# v0.9.14-hermes-2: smart_apply.py/i \\$line" "$COMPOSE"
    else
      echo "$line" >> "$COMPOSE"
    fi
  fi
}

add_mount \
  "/mnt/Data/appdata/homelab-mcp/src/homelab_mcp/tools/benchmark_load.py" \
  "/install/lib/python3.12/site-packages/homelab_mcp/tools/benchmark_load.py"
add_mount \
  "/mnt/Data/appdata/homelab-mcp/src/homelab_mcp/tools/benchmark_restart.py" \
  "/install/lib/python3.12/site-packages/homelab_mcp/tools/benchmark_restart.py"
add_mount \
  "/mnt/Data/appdata/homelab-mcp/src/homelab_mcp/tools/benchmark_diff.py" \
  "/install/lib/python3.12/site-packages/homelab_mcp/tools/benchmark_diff.py"

# --- Validate compose file syntax ---
cd "$STACK_DIR"
docker compose config -q

# --- Recreate container ---
docker compose up -d --force-recreate homelab-mcp

# --- Wait for health ---
echo "Waiting for healthcheck..."
for i in {1..30}; do
  status=$(docker inspect -f '{{.State.Health.Status}}' homelab-mcp 2>/dev/null || true)
  if [[ "$status" == "healthy" ]]; then
    echo "homelab-mcp is healthy."
    break
  fi
  echo "  attempt $i/30: status=$status"
  sleep 2
done

# Final verification via HTTP health endpoint
if ! curl -fsS --max-time 5 http://127.0.0.1:18790/health >/dev/null 2>&1; then
  echo "ERROR: health endpoint did not return 200."
  echo "Rolling back to $BACKUP..."
  bash "$ROLLBACK_SCRIPT"
  exit 1
fi

# --- Smoke test new tools ---
echo "Running smoke tests for new tools..."
docker exec homelab-mcp python -m pytest tests/test_benchmark_load.py tests/test_benchmark_restart.py tests/test_benchmark_diff.py -q

echo "Deploy complete. Backup: $BACKUP"
