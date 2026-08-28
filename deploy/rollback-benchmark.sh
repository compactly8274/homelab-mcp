# Quick rollback for the benchmark-framework Phase 1 deployment.
# Run this if the live daemon misbehaves after deploying the benchmark file mounts.
# It restores the pre-Phase-1 compose baseline and recreates the container.
#
# Usage (from truenas):
#   cd /mnt/Data/appdata/dockge/stacks/homelab-mcp
#   bash /mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark.sh
#
# No source-code branches are touched by this script; the running container is
# driven by the compose file alone.

set -euo pipefail

STACK_DIR="/mnt/Data/appdata/dockge/stacks/homelab-mcp"
BASELINE="/mnt/Data/appdata/dockge/stacks/homelab-mcp/compose.yaml.pre-mesh.bak"

if [[ ! -f "$BASELINE" ]]; then
  echo "ERROR: baseline not found: $BASELINE"
  echo "You must save the pre-Phase-1 compose.yaml before using this rollback."
  exit 1
fi

cp -v "$BASELINE" "$STACK_DIR/compose.yaml"
cd "$STACK_DIR"
docker compose up -d --force-recreate homelab-mcp

echo "Rollback complete. Live compose restored from $BASELINE."
echo "Daemon should be healthy in ~20s. Verify with: docker ps -a | grep homelab-mcp"
