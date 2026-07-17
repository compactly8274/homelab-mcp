# Changelog

All notable changes to homelab-mcp. This project uses 0-based
image tags (e.g. `0.4.0`, not `v0.4.0`) in GHCR — see the
[release workflow](.github/workflows/build.yml) for the build
process. Conventional Commits (feat/fix/chore) are used for
commit messages; this file is the human-readable summary.

## [0.8.0] — 2026-07-16

### Added
- **`container_action_tool(host, target, action, require_approval=True)`**:
  start / stop / restart / kill / pause / unpause a container
  or an entire stack from the LLM surface. Goes through the
  preflight gate (same as `apply_update_tool`): unknown hosts,
  unknown targets, and multi-container stacks on destructive
  actions are blocked by default. `target` accepts either a
  container NAME or a compose project name (= stack); container
  names take precedence. Closes the preflight-gap noted in the
  v0.7.0 writeup.
- **`notifier_status_tool(test_notify=False)`**: surfaces the
  notifier configuration — which backends (ntfy / pushover /
  discord) are wired up via env vars, which are missing.
  With `test_notify=True` it sends a self-test message to every
  configured backend so the wire can be verified after a
  container restart. Closes the "notifier is a silent no-op"
  failure mode (every backend's `notify()` returned `None`
  silently if the env vars were missing).

### Fixed
- **Local-host apply bug**: the homelab-mcp image had no
  `docker` CLI on PATH, so `LocalDocker.compose_pull` /
  `compose_up` (used by the apply pipeline on the local host)
  failed with `FileNotFoundError`. The docker socket was always
  mounted; we just didn't have the CLI to talk to it. Fixed by
  installing `docker-ce-cli` and `docker-compose-plugin` in the
  runtime image (single apt-get, ~30MB). The local-host apply
  path now works end-to-end: live-verified by `apply_update_tool`
  with `dry_run=True` on truenas/PlexAutoLanguages, which
  successfully fetched release notes, classified as CAUTION, and
  returned `would_apply=True`.

### Tests
- 10 new tests in `tests/test_v080.py` (5 container_action,
  5 notifier_status).
- 325 passed, 10 skipped (up from 315 in v0.7.0).

## [0.7.0] — 2026-07-16

### Added
- **`apply_update_tool` now has a `require_approval=True` gate.**
  When True (the default), the tool calls `preflight_check_tool`
  first and refuses to apply if there are any blockers. Returns
  `{action: "blocked", preflight: {full verdict}}` so the
  caller can decide whether to override with `require_approval=False`.
  `dry_run=True` always bypasses the gate (a read-only preview
  is safe). `apply_all_pending_tool` inherits the gate
  automatically. The autonomous cron path is unchanged — it
  calls `evaluate_and_act` directly, not through the MCP tool.

### Fixed
- **Dashboard `recent_events` was empty** for the local host.
  Root cause: `LocalDocker.events()` was shelling out to
  `docker events`, which fails inside the homelab-mcp container
  because the docker CLI is not on PATH (only the docker socket
  is mounted). Switched to `docker.APIClient.events(since=N)`
  which works through the socket. Now returns real events.
- **RemoteSSH `list_containers` emitted `PROJECT=foo`** when
  the `com.docker.compose.project` label was missing, due to
  the `{{.Label "..."}}` template in `docker ps --format`.
  Stripped the `KEY=` prefix on parse so un-managed containers
  come through as `''` (matching the LocalDocker contract).
  v0.5.0's dashboard was double-counting these as stacks named
  "PROJECT=bookshelf" etc. Now they're correctly classified
  as single, un-managed containers. Total stack count went
  from 66 (with noise) to 155 (clean) on this homelab.

### Tests
- 5 new tests (4 preflight-gate integration,
  1 RemoteSSH PROJECT-prefix stripping).
- 315 passed, 10 skipped (up from 310 in v0.6.0).

## [0.6.0] — 2026-07-16

### Added
- **`preflight_check_tool(host, stack, action)`**: pre-flight
  check before destructive ops. Returns
  `{safe, blockers, warnings, info, suggested_alternative}`
  for 5 action types: remove / stop / restart / apply_update
  / dismiss_pending. Detects the patterns that have caused
  real damage before:
  - container in restart loop (FGC chromium pattern, warn)
  - container started <60s ago (Apply storm, warn)
  - apply_update with no last-known-good image (warn)
  - dismiss_pending on a broken stack (block)
  - remove on a multi-container stack (block — would orphan)
  - unknown stack name on a known host (block)
  - unknown host (block)
  The Lidarr/qBittorrent 11-album MEAL on 2026-07-16 would
  have been caught by this tool. Conservative in v0.6.0: it
  returns a verdict but does NOT block actions. The LLM (or
  user) is expected to honor the verdict; a future patch will
  wire it into the destructive tool functions themselves.
- **`suggest_memories_tool(since_minutes, max_suggestions)`**:
  surface "store this?" candidates from recent memory
  activity. Heuristic: tags that appear 3+ times in recent
  notes are flagged as a recurring theme. Does NOT auto-store;
  the LLM presents candidates and the user confirms.

### Fixed (caught during live testing)
- `preflight_check_tool` returned `safe=True, 0 blockers` when
  given an unknown stack name on a known host. Now blocks with
  "no container or stack named X found on Y. Refusing to act
  on a non-existent target." This was a silent-green-light path
  that would have allowed the LLM to "remove" a non-existent
  container.

### Tests
- 11 new tests in `tests/test_preflight_suggest.py` covering
  all the safety cases plus 3 suggest-tool cases.
- 310 passed, 10 skipped (up from 299 in v0.5.0).

## [0.5.0] — 2026-07-16

### Fixed
- **C-side: hermes-agent MCP bridge flap (root cause)**: the
  homelab-mcp SSE handler was returning `None` from both
  `/sse` and `/messages` endpoints. Starlette's `Route` class
  expects every endpoint to return a `Response`; when given
  `None`, it throws `TypeError: 'NoneType' object is not callable`
  in `routing.py:62` on the next request. The MCP library's
  own docstring explicitly documents the fix (return `Response()`
  after `connect_sse` ends), but it was missing from
  `homelab_mcp/http_routes.py`.
  - **Symptom on the daemon side**: every SSE POST or
    disconnect produced a `TypeError: 'NoneType' object is
    not callable` exception in the uvicorn log, and a
    `starlette/middleware/errors.py:164` traceback.
  - **Symptom on the client side (hermes-agent)**: the SSE POST
    got an `httpcore.ReadError` ("Error in post_writer" in the
    log), the bridge entered a 60+ second reconnect loop, and
    every `mcp__homelab__*` tool call timed out with
    "MCP call timed out after 180.0s". This was making the
    v0.3.0 memory tools and all 55 homelab-mcp tools
    effectively unreachable from the WebUI LLM surface since
    v0.4.0 was deployed.
  - **Fix**: add `return Response()` to `handle_sse` after
    `connect_sse` exits, and wrap `handle_post_message` in
    `handle_messages` so it always returns a `Response` after
    the inner handler does its own 202 Accepted send.
  - **Tests**: 2 new tests in `tests/test_http_routes.py`
    (`test_handle_sse_returns_response_after_disconnect` and
    `test_handle_sse_code_returns_response_in_source`).
- The first fix in this series (handle_sse) wasn't sufficient
  on its own — the same NoneType error also fires on POST
  /messages. The second fix wraps handle_post_message.

## [0.4.0] — 2026-07-16

### Added
- **`dry_run` parameter** on `apply_update_tool` and
  `apply_all_pending_tool`. Returns the LLM risk classification
  and the plan (`would_apply`, `to_digest`, `stack_dir`,
  `notes_source`) WITHOUT snapshotting, pulling, or restarting
  anything. Safe to call on production. Combine with
  `force=False` to preview what the policy would do.
  - `apply_all_pending_tool` result now includes a `dry_run`
    count alongside `applied`, `notified_breaking`, etc.
- 4 new unit tests in `tests/test_auto_apply.py` covering
  `dry_run=True` for all four code paths (SAFE,
  CAUTION + safe-and-caution, CAUTION + safe-only, BREAKING).

### Fixed
- **CI publish job** in `.github/workflows/build.yml` now
  fires for both `vX.Y.Z` and `X.Y.Z` tag forms. Previously
  the publish step only ran for `v*` tags, but the repo
  convention (and the GHCR image tag) is `X.Y.Z` (no `v`).
  This means v0.3.0 was published manually — from 0.4.0
  forward, the normal `git tag X.Y.Z && git push --tags`
  workflow is sufficient.
- **Version extraction** in the publish step correctly
  handles both `refs/tags/vX.Y.Z` and `refs/tags/X.Y.Z`.
- **Existing test** `test_apply_all_pending_processes_each_row_in_isolation`
  updated to accept the new `dry_run` kwarg in its
  `fake_apply` mock.
- **Real production bug found and fixed**: the homelab-mcp
  compose.yaml was missing the v0.2.0 service-integration
  env vars (PLEX_TOKEN, SONARR/RADARR/LIDARR/READARR_API_KEY).
  All HTTP calls to those services were returning 401
  Unauthorized. `.env` had the keys, but the compose file
  didn't reference them, so the running container had no
  API keys. Fixed in `scripts/dockge-stack/compose.yaml`;
  5 env-var references added; compose restarted; verified
  all API calls now succeed.

### Known Limitations
- **`apply_update_tool` on the local host (`truenas`) fails
  with "stack dir does not exist"** because the homelab-mcp
  container does not have the `docker` CLI installed and
  the `LocalDocker.compose_pull` path requires it. The
  `dry_run` path works fine (it just inspects via the
  docker SDK socket). Apply works correctly for SSH
  hosts (`unraid`); the local-host apply path needs either
  a multi-stage build that bundles `docker` CLI, or the
  local host to be reconfigured as `RemoteSSH`. Not in
  scope for 0.4.0; tracked for a future patch release.
  Workaround for the local host: SSH in and run
  `cd /mnt/Data/appdata/dockge/stacks/<stack> && docker compose pull && docker compose up -d`
  by hand. `dry_run=True` still works as a preview.
- **MCP bridge flaps on daemon restart**: the hermes-agent
  SSE client takes 60+ seconds to recover after a homelab-mcp
  container recreate, and during that window all tool calls
  time out. Pre-existing, not specific to 0.4.0. Workaround:
  open a new WebUI chat session after any daemon restart.
  The 180s timeout on tool calls is intentional — it gives
  the bridge time to recover on its own without throwing
  errors at the LLM.
  - **This was actually caused by the v0.4.1 fix above**;
    the 0.4.1 release removes this limitation entirely.

## [0.3.0] — 2026-07-15

### Added
- **Long-term memory store** (7 new MCP tools): `memory_store`,
  `memory_recall`, `memory_search`, `memory_list`,
  `memory_recent`, `memory_forget`, `memory_stats`. SQLite
  FTS5-backed, three namespaces (`notes`, `prefs`, `facts`),
  importance weights, TTL support, and superseded-row
  preservation. Replaces ad-hoc MEMORY.md/USER.md dumping
  for facts that aren't worth always-injecting into the
  system prompt.
- 32 unit tests for the memory module (100% pass).

## [0.2.1] — 2026-07-15

### Added
- **9 new MCP tools**:
  - 4 *arr bulk tools: `arr_status_all`, `arr_queue_all`,
    `arr_wanted_all`, `arr_search_all` (one call returns
    data from all 4 services).
  - 3 Ollama tools: `ollama_show_running`, `ollama_pull_model`,
    `ollama_delete_model`. The pull and delete tools are
    gated behind env vars (`HOMELAB_MCP_OLLAMA_ALLOW_PULL`,
    `HOMELAB_MCP_OLLAMA_ALLOW_DELETE`; both default to false).
  - 1 Plex metadata tool: `plex_get_metadata` for full
    detail on a single rating key.
  - 1 *arr search expansion: `arr_search_series` now works
    for sonarr, radarr, lidarr, readarr (previously only
    sonarr).
- Tests for the new tools in `tests/test_arr.py`,
  `tests/test_ollama.py`, `tests/test_plex.py`.

## [0.2.0] — 2026-07-13

### Added
- Smart-update pipeline: `apply_update_tool` (single host/stack)
  and `apply_all_pending_tool` (bulk), with the full
  release-notes → LLM classify → policy gate → snapshot →
  apply → healthcheck → rollback flow. LLM classifies each
  update as SAFE / CAUTION / BREAKING; default policy is
  `safe-and-caution` (apply SAFE and CAUTION, notify on
  BREAKING). Switchable via `HOMELAB_MCP_AUTO_APPLY_POLICY`.
- `force=True` parameter on both apply tools overrides
  BREAKING classification.
- 6-hour poll cron that auto-scans both `truenas` and
  `unraid` for image drift and writes pending updates
  to the SQLite state DB.
- Notifier backends: ntfy, console, multi (configurable
  via env vars `HOMELAB_MCP_NTFY_URL`, `HOMELAB_MCP_NTFY_TOPIC`).
- 240+ unit tests covering the full update pipeline.

## [0.1.x] — 2026-07-08 to 2026-07-12

### Added
- Initial release: MCP server with `arr_status`, `arr_queue`,
  `arr_history`, `arr_wanted`, `arr_calendar`, `arr_search_series`,
  `arr_disk_space`, `plex_status`, `plex_library_sections`,
  `plex_search`, `plex_recently_added`, `plex_active_sessions`,
  `plex_server_stats`, `ollama_list_models`, `ollama_list_running`,
  `ollama_unload_model`, `ollama_unload_all`, `ollama_show_model`,
  `ollama_version`, `searxng_search`, `searxng_suggestions`,
  `searxng_engines`, `list_stacks_tool`, `stack_status_tool`,
  `get_logs_tool`, `recent_events_tool`, `check_dns_tool`,
  `check_nfs_shares_tool`, `check_vpn_health_tool`, and
  the read-side of the auto-update pipeline (list pending,
  trigger scan, get update history, dismiss).
- 5-host inventory: `truenas` (local), `unraid` (remote SSH),
  plus 3 additional hosts configured via
  `HOMELAB_MCP_HOSTS` JSON.
- Local-docker and remote-SSH host clients in
  `homelab_mcp/hosts/`.
