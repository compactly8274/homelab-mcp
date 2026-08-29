#!/usr/bin/env bash
set -euo pipefail
# Production window: 00:00-05:00 America/Los_Angeles
hour_pt=$(TZ=America/Los_Angeles date +%H)
if [ "$hour_pt" -lt 0 ] || [ "$hour_pt" -ge 5 ]; then
    echo "Outside production window (00:00-05:00 PT, now PT hour=$hour_pt). Aborting." >&2
    exit 1
fi
cd /mnt/Data/appdata/dockge/stacks/homelab-mcp
mkdir -p /mnt/Data/appdata/homelab-mcp/logs
cp compose.yaml "compose.yaml.pre-recreate-$(date -u +%Y%m%d-%H%M%S).bak"
docker compose up -d --force-recreate homelab-mcp
sleep 20
docker ps -a | grep homelab-mcp
docker logs --tail 30 homelab-mcp
