#!/bin/sh
# homelab-mcp install on Unraid (the legacy primary NAS).
#
# Pulls the GHCR image, starts the daemon, registers a 6h cron.
# Idempotent: re-running re-pulls the image and re-creates the container.

set -eu

REPO_OWNER="${REPO_OWNER:-your-org}"
IMAGE="${IMAGE:-ghcr.io/${REPO_OWNER}/homelab-mcp:latest}"
CONTAINER_NAME="homelab-mcp"
DATA_DIR="/mnt/user/appdata/homelab-mcp"
STATE_DIR="${DATA_DIR}/state"
CRON_FILE="/boot/config/plugins/dynamix/homelab-mcp.cron"
DOCKER_SOCK="/var/run/docker.sock"

echo "[install-on-unraid] image: $IMAGE"
echo "[install-on-unraid] data:  $DATA_DIR"

# 1. State directory
mkdir -p "$STATE_DIR"

# 2. Pull image
docker pull "$IMAGE"

# 3. Stop + remove existing container
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "[install-on-unraid] removing existing $CONTAINER_NAME container"
  docker stop "$CONTAINER_NAME" || true
  docker rm -f "$CONTAINER_NAME" || true
fi

# 4. Start the daemon
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network host \
  -v "${DOCKER_SOCK}:${DOCKER_SOCK}:ro" \
  -v "${DATA_DIR}:/data" \
  -v "/root/.ssh/config:/root/.ssh/config:ro" \
  -e "HOMELAB_MCP_HOSTS=${HOMELAB_MCP_HOSTS:-[\"unraid\"]}" \
  -e "HOMELAB_MCP_PORT=${HOMELAB_MCP_PORT:-18790}" \
  -e "HOMELAB_MCP_STATE_DIR=/data" \
  -e "HOMELAB_MCP_POLL_ENABLED=${HOMELAB_MCP_POLL_ENABLED:-true}" \
  -e "HOMELAB_MCP_POLL_INTERVAL=${HOMELAB_MCP_POLL_INTERVAL:-21600}" \
  -e "HOMELAB_MCP_NTFY_URL=${HOMELAB_MCP_NTFY_URL:-https://ntfy.sh/}" \
  -e "HOMELAB_MCP_NTFY_TOPIC=${HOMELAB_MCP_NTFY_TOPIC:-}" \
  -e "HOMELAB_MCP_NTFY_PRIORITY=${HOMELAB_MCP_NTFY_PRIORITY:-default}" \
  -e "HOMELAB_MCP_LLM_ENDPOINT=${HOMELAB_MCP_LLM_ENDPOINT:-http://localhost:11434/v1/chat/completions}" \
  -e "HOMELAB_MCP_LLM_API_KEY=${HOMELAB_MCP_LLM_API_KEY:-}" \
  -e "HOMELAB_MCP_LLM_MODEL=${HOMELAB_MCP_LLM_MODEL:-}" \
  -e "HOMELAB_MCP_LLM_TIMEOUT=${HOMELAB_MCP_LLM_TIMEOUT:-30}" \
  -e "HOMELAB_MCP_AUTO_APPLY_POLICY=${HOMELAB_MCP_AUTO_APPLY_POLICY:-safe-and-caution}" \
  -e "HOMELAB_MCP_LOCAL_HOST_ALIAS=${HOMELAB_MCP_LOCAL_HOST_ALIAS:-unraid}" \
  -e "HOMELAB_MCP_DOCKGE_STACKS_ROOT=${HOMELAB_MCP_DOCKGE_STACKS_ROOT:-/mnt/Data/appdata/dockge/stacks}" \
  "$IMAGE" daemon

# 5. Cron: every 6h, run the auto-apply cycle
CRON_LINE="0 */6 * * * docker exec ${CONTAINER_NAME} auto-apply --per-row-timeout 120 >/var/log/homelab-mcp-apply.log 2>&1"
echo "$CRON_LINE" > "$CRON_FILE"
echo "[install-on-unraid] cron registered: $CRON_FILE"

# 6. Wait for readiness
echo "[install-on-unraid] waiting for ${CONTAINER_NAME} to become ready..."
for i in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" python -c "import urllib.request, sys; r = urllib.request.urlopen('http://127.0.0.1:18790/health', timeout=4); sys.exit(0 if r.status == 200 else 1)" 2>/dev/null; then
    echo "[install-on-unraid] ready (after ${i}s)"
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
  fi
  sleep 1
done

echo "[install-on-unraid] WARNING: container did not become ready in 30s"
docker logs --tail 20 "$CONTAINER_NAME" || true
exit 1
