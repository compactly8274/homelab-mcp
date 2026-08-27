# benchmark-framework deployment notes

## Scope
Phase 1 of a generic diagnostics/execution toolset for homelab-mcp:
- `exec_in_container_tool`: run read-only/allowlisted commands inside containers
- Tight command allowlist (default-deny) instead of blocklist
- Preflight gate integration for container execution
- Production hours corrected to `00:00-05:00 America/Los_Angeles`
- Unit tests covering allowlist, preflight triggers, and execution logic

## What changed
| File | Change |
|------|--------|
| `homelab_mcp/hosts/base.py` | Add `exec_in_container()` to `HostClient` protocol |
| `homelab_mcp/hosts/local_docker.py` | Implement `exec_in_container()` via Docker SDK |
| `homelab_mcp/hosts/remote_ssh.py` | Implement `exec_in_container()` via `docker exec` over SSH |
| `homelab_mcp/tools/exec_in_container.py` | New tool; default-deny allowlist, preflight-aware |
| `homelab_mcp/tools/preflight.py` | Add `exec_in_container` action and validation |
| `homelab_mcp/server.py` | Import/exec registration for `exec_in_container_tool` |
| `tests/test_exec_in_container.py` | Unit tests (17 passing) |
| `deploy/compose.yaml.benchmark` | Reference compose: corrected hours + branch source mount |
| `deploy/rollback-benchmark.sh` | One-command rollback to pre-Phase-1 baseline |

## Test results
```
tests/test_exec_in_container.py ................. 17 passed in 0.XXs
```

## How to deploy (at 00:00-05:00 PT only)
1. Confirm production window:
   ```bash
   date +%Z  # should be PDT/PST
   # verify hour is between 00:00 and 05:00 America/Los_Angeles
   ```
2. From truenas:
   ```bash
   cd /mnt/Data/appdata/homelab-mcp/src
   git checkout feature/benchmark-framework
   git pull
   ```
3. Run tests inside the target image:
   ```bash
   docker run --rm -v /mnt/Data/appdata/homelab-mcp/src:/src:rw -w /src \
     -e PYTHONPATH=/src --user root --entrypoint /install/bin/python3 \
     homelab-mcp:hermes-watchdog-mesh -m pytest tests/test_exec_in_container.py -v
   ```
4. If tests pass, copy reference compose to live stack and recreate:
   ```bash
   cp /mnt/Data/appdata/homelab-mcp/src/deploy/compose.yaml.benchmark \
      /mnt/Data/appdata/dockge/stacks/homelab-mcp/compose.yaml
   cd /mnt/Data/appdata/dockge/stacks/homelab-mcp
   docker compose up -d --force-recreate homelab-mcp
   ```
5. Verify health and sanity-smoke:
   ```bash
   for i in {1..30}; do docker inspect --format='{{.State.Health.Status}}' homelab-mcp | grep -q healthy && break; sleep 2; done
   # optional smoke test via MCP client
   ```

## Rollback
```bash
/mnt/Data/appdata/homelab-mcp/src/deploy/rollback-benchmark.sh
```
This restores the live compose to the pre-Phase-1 baseline (`compose.yaml.pre-mesh.bak`),
checks out `main`, and recreates the container. A timestamped snapshot of the current
compose is kept before any change.

## Pre-merge cleanup
- Remove the temporary branch-source bind mount from `deploy/compose.yaml.benchmark`
- Rebuild/push `homelab-mcp:hermes-watchdog-mesh` from this branch so the next deploy
  does not need source overlay
- Update `.env` `IMAGE` tag if the rebuilt image has a new tag

## House-rules compliance
- No live source edits: all changes are on `feature/benchmark-framework`
- No deploy without explicit user go-ahead
- Deployment restricted to the configured 00:00-05:00 PT window
- Rollback script is pre-staged and tested as a dry-run shape (it copies files; verify
  in a non-production clone if possible before the first live use)
