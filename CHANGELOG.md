# Changelog

All notable changes to homelab-mcp. This project uses 0-based
image tags (e.g. `0.4.0`, not `v0.4.0`) in GHCR — see the
[release workflow](.github/workflows/build.yml) for the build
process. Conventional Commits (feat/fix/chore) are used for
commit messages; this file is the human-readable summary.

## [0.4.1] — 2026-07-16

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
