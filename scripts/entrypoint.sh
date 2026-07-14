#!/bin/sh
# homelab-mcp entrypoint. Dispatches on the first arg.
#
#   daemon      — long-running MCP server (default). Reads HOMELAB_MCP_* env.
#   auto-apply  — one cycle of the auto-apply pipeline (no daemon).
#   shell       — drop into a Python REPL with the package imported.
#   pytest      — run the test suite.
#   ruff        — lint check.
#   --help, -h  — print this help.
#
# Anything else is passed through to the Python interpreter.

set -eu

case "${1:-daemon}" in
  daemon)
    exec python -m homelab_mcp
    ;;
  auto-apply)
    shift
    exec python -m homelab_mcp.auto_apply_main "$@"
    ;;
  shell)
    exec python -i -c "import homelab_mcp; print('homelab_mcp loaded; try mcp, get_state, build_hosts')"
    ;;
  pytest)
    shift
    exec python -m pytest "$@"
    ;;
  ruff)
    shift
    exec python -m ruff check "$@"
    ;;
  --help|-h|help)
    cat <<EOF
homelab-mcp container entrypoint

  daemon       long-running MCP server (default)
  auto-apply   one cycle of the auto-apply pipeline (cron-style)
  shell        drop into a Python REPL with homelab_mcp imported
  pytest       run the test suite
  ruff         lint check (pyproject-configured)
  --help, -h   this help

Environment:
  HOMELAB_MCP_HOSTS             JSON list of host aliases (required)
  HOMELAB_MCP_PORT              port for the MCP SSE transport (default 18790)
  HOMELAB_MCP_STATE_DIR         sqlite/state directory (default /data)
  HOMELAB_MCP_SSH_CONFIG        ssh config path (default ~/.ssh/config)
  HOMELAB_MCP_NTFY_URL          ntfy base URL (default https://ntfy.sh/)
  HOMELAB_MCP_NTFY_TOPIC        ntfy topic for BREAKING alerts (required for alerts)
  HOMELAB_MCP_LLM_ENDPOINT      OpenAI-compatible chat-completions URL
  HOMELAB_MCP_LLM_API_KEY       bearer token (optional; Ollama doesn't need it)
  HOMELAB_MCP_LLM_MODEL         model name (required for classifier)
  HOMELAB_MCP_LLM_TIMEOUT       seconds (default 30)
  HOMELAB_MCP_AUTO_APPLY_POLICY safe-and-caution (default) or safe-only
  HOMELAB_MCP_LOCAL_HOST_ALIAS  which alias is the local docker socket
                                (default 'unraid'; set to 'truenas' for
                                TrueNAS deploys)
  HOMELAB_MCP_DOCKGE_STACKS_ROOT Dockge stack root
                                 (default /mnt/Data/appdata/dockge/stacks)
EOF
    ;;
  *)
    exec python "$@"
    ;;
esac
