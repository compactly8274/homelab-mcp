#!/usr/bin/env bash
# Rollback for benchmark framework Phase 6 deploy.
# Restores the compose file saved by deploy-benchmark-phase6.sh and recreates the container.
#
# Usage (from truenas):
#   bash /mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark-phase6.sh

set -euo pipefail

STACK_DIR="/mnt/Data/appdata/dockge/stacks/homelab-mcp"
ENV_FILE="$STACK_DIR/.rollback-phase6.env"

if [[ -f "$ENV_FILE" ]]; then
  BACKUP=$(grep "^BACKUP_PATH=" "$ENV_FILE" | cut -d= -f2-)
else
  # Find most recent phase6 backup
  BACKUP=$(ls -t "$STACK_DIR"/compose.yaml.pre-phase6-*.bak 2>/dev/null | head -n1 || true)
fi

if [[ -z "$BACKUP" || ! -f "$BACKUP" ]]; then
  echo "ERROR: no rollback backup found. Check $STACK_DIR/compose.yaml.pre-phase6-*.bak"
  exit 1
fi

cp -v "$BACKUP" "$STACK_DIR/compose.yaml"
cd "$STACK_DIR"
docker compose up -d --force-recreate homelab-mcp

# Wait for healthy
for i in {1..30}; do
  status=$(docker inspect -f '{{.State.Health.Status}}' homelab-mcp 2>/dev/null || true)
  if [[ "$status" == "healthy" ]]; then
    echo "Rollback complete. homelab-mcp is healthy."
    exit 0
  fi
  echo "  attempt $i/30: status=$status"
  sleep 2
done

echo "ERROR: rollback container did not become healthy."
exit 1
