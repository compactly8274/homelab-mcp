# homelab-mcp

MCP server for homelab diagnostics + auto-update pipeline. Runs on
TrueNAS (or anywhere) as the central control plane; talks to one or
more downstream Docker hosts (Unraid, QNAP, etc.) over SSH.

## What it does

- **Read-only diagnostics** (MCP tools, safe to call any time):
  - `list_stacks_tool` — every compose project + single container on a host
  - `stack_status_tool` — full state of one stack (containers, digests)
  - `recent_events_tool` — docker events from the last N seconds
  - `get_logs_tool` — last N log lines of a container (capped at 5000)
  - `check_nfs_shares_tool` — mounted NFS exports
  - `check_dns_tool` — name resolution against the host's resolver
  - `check_vpn_health_tool` — gluetun container health

- **Image-drift visibility** (MCP tools, populates `pending_updates`):
  - `trigger_scan_tool` — on-demand scan of one or all hosts
  - `list_pending_updates_tool` — rows from the latest scan / cron
  - `pending_update_dismiss_tool` — drop a row after deciding not to apply

- **Auto-update pipeline** (cron-style; one cycle at a time):
  - **Fetch release notes** — image → GitHub repo heuristic
    (ghcr.io / quay.io / lscr.io/linuxserver → GitHub Releases API,
    then CHANGELOG.md fallback). 8KB cap.
  - **Classify via LLM** — OpenAI-compatible chat-completions
    endpoint (works with Ollama, vLLM, MiniMax M3). Returns
    `SAFE` / `CAUTION` / `BREAKING` plus migration steps, compose
    changes, env changes. Any LLM error falls back to `CAUTION`
    (better to over-apply than under-classify).
  - **Apply policy**:
    - default `safe-and-caution` — auto-apply SAFE + CAUTION, notify on BREAKING
    - `safe-only` — auto-apply SAFE only, notify on CAUTION + BREAKING
  - **Apply** — `docker compose pull && docker compose up -d` from
    the resolved stack directory. Stack-dir resolution: 4-tier
    (label override > compose working_dir > Dockge path > CA
    compose.manager). Probes container health post-restart.
  - **Auto-rollback** on apply failure (pull old digest, up -d).
  - **Notify** — ntfy by default (configurable); BREAKING alerts
    go to the ntfy topic with high priority.

## Quick start

```bash
# 1. Sync deps (the project is configured to use .venv/)
uv sync --extra dev
source .venv/bin/activate

# 2. Configure
cp .env.example .env
# ... edit .env (set HOMELAB_MCP_HOSTS, ntfy topic, LLM endpoint, etc.)

# 3. Test
pytest

# 4. Run the MCP server (SSE on :18790)
python -m homelab_mcp

# 5. Run one cycle of the auto-apply pipeline (dry-run)
python -m homelab_mcp.auto_apply_main --dry-run
```

> If `uv sync` puts the venv in a `venv/` directory instead of
> `.venv/`, set `UV_PROJECT_ENVIRONMENT=.venv` once or use
> `uv venv .venv && uv sync --extra dev` to force the location.

## Install on Unraid

```bash
# On Unraid:
bash scripts/install-on-unraid.sh
# (pulls the GHCR image, starts the daemon, registers a 6h cron)
```

## Install on TrueNAS (the "main hub" config)

```bash
# On TrueNAS, in a Dockge stack directory:
git clone https://github.com/<owner>/homelab-mcp.git
cd homelab-mcp
cp scripts/dockge-stack/.env.example .env
# ... edit .env (set HOMELAB_MCP_HOSTS=["truenas","unraid"],
#     HOMELAB_MCP_LOCAL_HOST_ALIAS=truenas, etc.)
docker compose -f scripts/dockge-stack/compose.yaml up -d
bash scripts/deploy-on-truenas.sh
# (runs 5 in-the-field smoke tests)
```

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example).

| Env var | Default | Notes |
| --- | --- | --- |
| `HOMELAB_MCP_HOSTS` | `["unraid"]` | JSON list of host aliases |
| `HOMELAB_MCP_PORT` | `18790` | MCP SSE transport port |
| `HOMELAB_MCP_STATE_DIR` | `~/.local/share/homelab-mcp` | sqlite + last_scan.txt |
| `HOMELAB_MCP_SSH_CONFIG` | `~/.ssh/config` | for RemoteSSH |
| `HOMELAB_MCP_POLL_ENABLED` | `true` | visibility cron on/off |
| `HOMELAB_MCP_POLL_INTERVAL` | `21600` | seconds between scans (6h) |
| `HOMELAB_MCP_NTFY_URL` | `https://ntfy.sh/` | notifier base |
| `HOMELAB_MCP_NTFY_TOPIC` | `""` | required for alerts |
| `HOMELAB_MCP_NTFY_PRIORITY` | `default` | default priority |
| `HOMELAB_MCP_DISCORD_WEBHOOK_URL` | `""` | Discord webhook (empty = disabled) |
| `HOMELAB_MCP_DISCORD_USERNAME` | `homelab-mcp` | webhook display name |
| `HOMELAB_MCP_PUSHOVER_APP_TOKEN` | `""` | Pushover app token (empty = disabled) |
| `HOMELAB_MCP_PUSHOVER_USER_KEY` | `""` | Pushover user key |
| `HOMELAB_MCP_PUSHOVER_DEVICE` | `""` | target device by name (optional) |
| `HOMELAB_MCP_PUSHOVER_SOUND` | `pushover` | alert sound |
| `HOMELAB_MCP_LLM_ENDPOINT` | `http://localhost:11434/v1/chat/completions` | OpenAI-compatible |
| `HOMELAB_MCP_LLM_API_KEY` | `""` | Bearer token (optional) |
| `HOMELAB_MCP_LLM_MODEL` | `""` | model name (required) |
| `HOMELAB_MCP_LLM_TIMEOUT` | `30` | seconds |
| `HOMELAB_MCP_AUTO_APPLY_POLICY` | `safe-and-caution` | or `safe-only` |
| `HOMELAB_MCP_LOCAL_HOST_ALIAS` | `unraid` | set to `truenas` for TrueNAS deploys |
| `HOMELAB_MCP_DOCKGE_STACKS_ROOT` | `/mnt/Data/appdata/dockge/stacks` | Dockge stacks path |

## Architecture

```
                 +------------+
                 |  TrueNAS   |  <-- MCP daemon here (LocalDocker)
                 | (main hub) |
                 +-----+------+
                       |
                  SSH  |  (RemoteSSH for unraid + qnap)
                       |
        +--------------+--------------+
        |                             |
   +----+-----+                +------+----+
   |  Unraid  |                |   QNAP    |
   | (117 ct) |                | (60+ ct)  |
   +----------+                +-----------+
```

- **LocalDocker backend**: the daemon host's docker socket
  (`unix:///var/run/docker.sock`).
- **RemoteSSH backend**: SSH config alias → `ssh <alias> docker ps`.
  Each host alias in `HOMELAB_MCP_HOSTS` must have a corresponding
  `Host` block in `HOMELAB_MCP_SSH_CONFIG`.
- **Stack-dir resolution** (in order):
  1. `auto-update.stack-dir` label on the container (override)
  2. `com.docker.compose.project.working_dir` (the standard compose label)
  3. If `com.dockge.owner` or `auto-update.dockge=true` is set →
     `<HOMELAB_MCP_DOCKGE_STACKS_ROOT>/<project>`
  4. Otherwise → `<compose_manager_root>/<project>` (CA compose.manager
     style; `compose_manager_root` is auto-detected for the local host)

## Tests

```bash
uv run pytest -q
```

Currently 145 tests across:
- config (env-var loading, validation)
- state (SQLite CRUD, idempotent writes, busy_timeout)
- hosts (LocalDocker + RemoteSSH, structural conformance to HostClient)
- updater.registry (image-ref parsing, registry result kinds)
- updater.scanner (drift detection, transient errors, digests)
- updater.pipeline (snapshot, apply+probe, rollback, run_pipeline)
- updater.release_notes (image→GitHub, GH Releases API, CHANGELOG fallback, truncation)
- updater.risk (SAFE/CAUTION/BREAKING classification, error fallbacks)
- updater.notifier (ntfy POST, multi-fan-out, console)
- updater.notifier_backends (Discord + Pushover: body building, network errors, MultiNotifier dispatch, error isolation)
- updater.auto_apply (policy decisions, classifier-fail CAUTION fallback)
- auto_apply_main (cron arg parsing, per-row exception isolation, summary)
- server (FastMCP singleton, tool registration, build_hosts wiring)

### Live SSH tests (opt-in)

There is a separate test file at `tests/test_live_ssh.py` that
exercises the `RemoteSSH` backend against a real homelab host. These
are **skipped by default** (the sandbox has no LAN egress). To run
them on a host that has SSH access to your homelab:

```bash
HOMELAB_MCP_LIVE=1 \
HOMELAB_MCP_LIVE_HOST_NAME=unraid \
HOMELAB_MCP_LIVE_HOST_HOSTNAME=192.168.1.104 \
HOMELAB_MCP_LIVE_HOST_USER=root \
HOMELAB_MCP_SSH_CONFIG=/root/.ssh/config \
pytest tests/test_live_ssh.py -v
```

The tests cover 9 real-host behaviors:
- `list_containers` returns real data
- `list_stacks` shape is right
- `inspect_container` returns valid `docker inspect` JSON
- `run_command` exit code, stdout, stderr are captured correctly
- `run_command` timeout cancels the command
- The configured user can run `docker` (no permission errors)
- A `compose_pull` against a real directory returns successfully

Full configuration recipe + troubleshooting is in
[`tests/live/README.md`](tests/live/README.md).

## Notifiers

The auto-apply pipeline supports **three backends**, all sharing the
same `Notifier` protocol. Configure any combination — the orchestrator
dispatches to all of them.

### ntfy (default)

Set `HOMELAB_MCP_NTFY_TOPIC` and you get free-form notifications on
every BREAKING alert. Topic-based, no account required, supports
priorities and tags.

### Discord

Set `HOMELAB_MCP_DISCORD_WEBHOOK_URL` to get color-coded embeds in a
Discord channel. BREAKING alerts are red, CAUTION orange, SAFE green.
To create a webhook: Discord server → channel settings → Integrations
→ Webhooks → New Webhook → copy URL.

### Pushover

Set both `HOMELAB_MCP_PUSHOVER_APP_TOKEN` (create at
https://pushover.net/apps/build) and `HOMELAB_MCP_PUSHOVER_USER_KEY`
to get push notifications on your phone. Sounds and per-device
targeting are configurable.

### Adding a new notifier

Implement the `Notifier` protocol in `homelab_mcp/updater/`:

```python
class Notifier(Protocol):
    async def notify(
        self, text: str, *,
        title: str = "",
        tags: list[str] | None = None,
        priority: str | None = None,
        click: str | None = None,
    ) -> None: ...
```

Then add it to `_build_notifier` in `homelab_mcp/auto_apply_main.py`.
Tests should follow the pattern in
`tests/test_updater_notifier_backends.py`: pure `_build_body` +
network `_post` separated.

## Security

- The MCP server runs in SSE mode and is bound to `0.0.0.0:18790` by
  default. **Put a reverse proxy in front of it** (npmplus, Traefik,
  Caddy) with OIDC or HTTP basic auth — don't expose 18790 directly.
- ntfy topics should be unique and unguessable. Add a token in the
  URL (`HOMELAB_MCP_NTFY_URL=https://ntfy.sh/?token=...`) for
  private topics.
- LLM API keys never appear in logs or state. The state DB at
  `HOMELAB_MCP_STATE_DIR/state.db` is not encrypted — host-level
  ACLs apply.
- The `auto-update` machinery touches docker. Run as a dedicated
  user with docker-socket access; do not run as root if you can
  avoid it.

## License

MIT.
