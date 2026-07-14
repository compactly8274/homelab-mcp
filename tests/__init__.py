"""Test suite for homelab-mcp.

Tests are organized by module:

- ``test_config.py``         — Settings env-var loading + validation
- ``test_state.py``          — async SQLite state layer
- ``test_hosts.py``          — LocalDocker + RemoteSSH protocol
- ``test_updater_registry.py``  — Docker Registry v2 client
- ``test_updater_scanner.py``   — image-drift scanner
- ``test_updater_apply.py``     — apply pipeline (snapshot + pull + up + probe)
- ``test_updater_rollback.py``  — rollback (pull by digest, tag, up)
- ``test_updater_pipeline.py``  — apply + auto-rollback orchestrator
- ``test_release_notes.py``     — GitHub Releases + CHANGELOG fetcher
- ``test_risk.py``              — LLM risk classifier
- ``test_notifier.py``          — ntfy notifier
- ``test_auto_apply.py``        — auto-apply orchestrator (SAFE+CAUTION apply, BREAKING notify)
- ``test_auto_apply_main.py``   — cron entry point
- ``test_server.py``            — FastMCP server skeleton + tool registration

Live tests (skipped unless ``HOMELAB_MCP_LIVE=1``):

- ``test_hosts_live.py``        — talks to a real docker daemon over SSH
"""
