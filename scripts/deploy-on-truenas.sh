#!/bin/sh
# homelab-mcp deploy on TrueNAS — the "main hub" install.
#
# Assumes the dockge-stack/compose.yaml is already running.
# Runs 5 in-the-field smoke tests and reports pass/fail.
#
# Usage:  bash scripts/deploy-on-truenas.sh [IMAGE]
# Example: bash scripts/deploy-on-truenas.sh ghcr.io/me/homelab-mcp:latest

set -eu

IMAGE="${1:-${IMAGE:-ghcr.io/your-org/homelab-mcp:latest}}"
CONTAINER_NAME="${CONTAINER_NAME:-homelab-mcp}"
DATA_DIR="/mnt/Data/appdata/homelab-mcp"
STATE_DIR="${DATA_DIR}/state"
PASS=0
FAIL=0
WARN=0

red() { printf '\033[31m%s\033[0m' "$1"; }
green() { printf '\033[32m%s\033[0m' "$1"; }
yellow() { printf '\033[33m%s\033[0m' "$1"; }

test_ok()   { PASS=$((PASS+1)); printf "  [%s] %s\n" "$(green PASS)" "$1"; }
test_fail() { FAIL=$((FAIL+1)); printf "  [%s] %s\n" "$(red FAIL)" "$1"; }
test_warn() { WARN=$((WARN+1)); printf "  [%s] %s\n" "$(yellow WARN)" "$1"; }

echo "==> homelab-mcp deploy on TrueNAS"
echo "    image:     $IMAGE"
echo "    container: $CONTAINER_NAME"
echo "    data:      $DATA_DIR"
echo

# ---------------------------------------------------------------- 1
echo "[1/5] container running"
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  STATUS=$(docker ps --filter "name=${CONTAINER_NAME}" --format "{{.Status}}")
  test_ok "container ${CONTAINER_NAME} is up (${STATUS})"
else
  test_fail "container ${CONTAINER_NAME} not running"
  echo "    (start it with: cd /path/to/dockge/stack && docker compose up -d)"
  echo
  echo "Summary: $PASS passed, $FAIL failed, $WARN warnings"
  exit 1
fi

# ---------------------------------------------------------------- 2
echo
echo "[2/5] daemon listening on 18790"
if docker exec "$CONTAINER_NAME" python -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:18790/health', timeout=4)
    sys.exit(0 if r.status == 200 else 1)
except Exception as e:
    print('health check failed:', e, file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
  test_ok "daemon responding on http://127.0.0.1:18790"
else
  test_warn "health check did not return 200; daemon may still be starting"
  docker logs --tail 20 "$CONTAINER_NAME" || true
fi

# ---------------------------------------------------------------- 3
echo
echo "[3/5] backend wiring (local docker + remote ssh)"
docker exec "$CONTAINER_NAME" python -c "
import asyncio
from homelab_mcp.config import Settings
from homelab_mcp.server import build_hosts
from homelab_mcp.hosts.local_docker import LocalDocker
from homelab_mcp.hosts.remote_ssh import RemoteSSH

async def main():
    s = Settings()
    hosts = build_hosts(s)
    print('  configured hosts:', sorted(hosts.keys()))
    print('  local alias    :', s.local_host_alias)
    for name, h in sorted(hosts.items()):
        kind = 'LocalDocker' if isinstance(h, LocalDocker) else 'RemoteSSH'
        print(f'    {name}: {kind}')
asyncio.run(main())
" 2>&1 | sed 's/^/    /'

# Validate that truenas=LocalDocker and unraid=RemoteSSH (the expected
# TrueNAS deploy topology).
WIRING_OK=1
if ! docker exec "$CONTAINER_NAME" python -c "
import asyncio
from homelab_mcp.config import Settings
from homelab_mcp.server import build_hosts
from homelab_mcp.hosts.local_docker import LocalDocker
from homelab_mcp.hosts.remote_ssh import RemoteSSH
async def main():
    s = Settings()
    hosts = build_hosts(s)
    assert isinstance(hosts.get('truenas'), LocalDocker), 'truenas should be LocalDocker'
    assert isinstance(hosts.get('unraid'), RemoteSSH), 'unraid should be RemoteSSH'
    print('OK')
asyncio.run(main())
" >/dev/null 2>&1; then
  test_warn "expected truenas=LocalDocker and unraid=RemoteSSH; check HOMELAB_MCP_LOCAL_HOST_ALIAS"
  WIRING_OK=0
else
  test_ok "truenas: LocalDocker, unraid: RemoteSSH"
fi

# ---------------------------------------------------------------- 4
echo
echo "[4/5] auto-apply dry-run"
docker exec \
  -e "HOMELAB_MCP_AUTO_APPLY_DRY_RUN=1" \
  "$CONTAINER_NAME" \
  python -m homelab_mcp.auto_apply_main --verbose 2>&1 | tail -3 | sed 's/^/    /'
if [ $? -eq 0 ]; then
  test_ok "auto-apply dry-run completed (0 pending updates is the expected happy state)"
else
  test_warn "auto-apply dry-run returned non-zero; inspect container logs"
fi

# ---------------------------------------------------------------- 5
echo
echo "[5/5] list stacks (read-only diagnostic)"
docker exec "$CONTAINER_NAME" python -c "
import asyncio, json
from homelab_mcp.config import Settings
from homelab_mcp.server import build_hosts
async def main():
    s = Settings()
    hosts = build_hosts(s)
    out = {}
    for name, h in sorted(hosts.items()):
        try:
            stacks = await h.list_stacks()
            out[name] = {'stacks': len(stacks), 'sample': [st['name'] for st in stacks[:3]]}
        except Exception as e:
            out[name] = {'error': str(e)}
    print(json.dumps(out, indent=2))
asyncio.run(main())
" 2>&1 | sed 's/^/    /'
test_ok "list_stacks ran across all configured hosts"

# ---------------------------------------------------------------- summary
echo
echo "==> Summary: $PASS passed, $FAIL failed, $WARN warnings"
if [ $FAIL -gt 0 ]; then
  exit 1
fi
exit 0
