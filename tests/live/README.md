# Live SSH integration tests

These tests exercise the **real** `RemoteSSH` backend against a real
host, not mocks. They're **opt-in** and **skipped by default** — the
sandbox has no LAN egress to your homelab.

## When to run them

Run them on **TrueNAS** (the daemon host) after a fresh deploy, before
you trust the auto-apply pipeline. They prove:

- `RemoteSSH.list_containers` returns a real list
- `RemoteSSH.inspect_container` returns valid `docker inspect` JSON
- `RemoteSSH.run_command` returns the right exit code, stdout, stderr
- `RemoteSSH.run_command` timeouts work
- The `docker` binary is reachable on the remote host as the configured user
- A `compose_pull` on a real directory returns successfully

If all 9 tests pass, the auto-apply pipeline's SSH path is verified
end-to-end. If any fails, the auto-apply pipeline WILL fail in
production — fix the SSH config or user permissions first.

## How to run

On the host where `homelab-mcp` is deployed (TrueNAS, the host that
will run the cron — not the sandbox that built the image):

```bash
docker exec homelab-mcp shell

# Inside the container:
HOMELAB_MCP_LIVE=1 \
HOMELAB_MCP_LIVE_HOST_NAME=unraid \
HOMELAB_MCP_LIVE_HOST_HOSTNAME=192.168.1.104 \
HOMELAB_MCP_LIVE_HOST_USER=root \
HOMELAB_MCP_SSH_CONFIG=/root/.ssh/config \
pytest tests/test_live_ssh.py -v
```

The container's `shell` entrypoint drops you into a Python REPL with
`homelab_mcp` imported, so you can also run an ad-hoc check first:

```python
import asyncio
from homelab_mcp.hosts.remote_ssh import RemoteSSH
h = RemoteSSH(
    name="unraid", ssh_host_alias="unraid",
    hostname="192.168.1.104", user="root",
    key_path="/root/.ssh/id_ed25519",
    ssh_config_path="/root/.ssh/config",
)
cs = await h.list_containers()
print(len(cs), "running containers on unraid")
```

## Required env vars

| Variable | Required | Default | Notes |
|---|---|---|---|
| `HOMELAB_MCP_LIVE` | yes | _(unset)_ | Must be `1`, `true`, or `yes` to enable |
| `HOMELAB_MCP_LIVE_HOST_NAME` | yes | _(unset)_ | SSH alias (matches a `Host` block in `~/.ssh/config`) |
| `HOMELAB_MCP_LIVE_HOST_HOSTNAME` | yes | _(unset)_ | IP or hostname, used as fallback |
| `HOMELAB_MCP_LIVE_HOST_USER` | no | `root` | SSH user |
| `HOMELAB_MCP_LIVE_HOST_PORT` | no | `22` | SSH port |
| `HOMELAB_MCP_LIVE_HOST_KEY_PATH` | no | auto-detect from `~/.ssh/id_{ed25519,rsa}` | Path to the private key |
| `HOMELAB_MCP_SSH_CONFIG` | no | `/root/.ssh/config` | Used by `asyncssh` for known_hosts and Host aliases |

## What to do if a test fails

| Failure | Likely cause | Fix |
|---|---|---|
| `live_docker_via_ssh_works` fails with "permission denied" | SSH user can't run `docker` | Add user to `docker` group on the host, or use a user that can |
| `live_run_command_timeout` flakes | Network jitter on a busy host | Re-run; if it persists, the SSH connection to the host is slow/unstable |
| All tests skip | `HOMELAB_MCP_LIVE` not set, or `_HOST_NAME`/`_HOSTNAME` missing | Check the env vars per the table above |
| `inspect_container` returns a non-`State` dict | The host has an old docker version that returns a different shape | Upgrade docker on the remote host |

## Running as part of CI

These tests do **not** run in CI by default. The CI workflow (in
`.github/workflows/build.yml`) only sets `HOMELAB_MCP_LIVE=0` explicitly
in the `test` job, which keeps them skipped. If you want to add a
dedicated `test-live` job that runs against a real homelab host:

1. Add a `test-live` job in `.github/workflows/build.yml` with the
   same steps as `test` but with `HOMELAB_MCP_LIVE=1` and the
   `HOMELAB_MCP_LIVE_HOST_*` env vars populated from
   `secrets.HOMELAB_MCP_LIVE_*` GitHub Actions secrets.
2. Use a self-hosted runner on TrueNAS (so it can reach the homelab
   hosts), or use the sandbox-host-only smoke tests instead.

The `make test-live` shortcut (if a Makefile is added later) would
just be:

```makefile
test-live:
	HOMELAB_MCP_LIVE=1 \
	HOMELAB_MCP_LIVE_HOST_NAME=$${HOMELAB_MCP_LIVE_HOST_NAME} \
	HOMELAB_MCP_LIVE_HOST_HOSTNAME=$${HOMELAB_MCP_LIVE_HOST_HOSTNAME} \
	HOMELAB_MCP_LIVE_HOST_USER=$${HOMELAB_MCP_LIVE_HOST_USER} \
	pytest tests/test_live_ssh.py -v
```
