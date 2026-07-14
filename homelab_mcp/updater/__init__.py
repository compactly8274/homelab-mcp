"""Updater package: registry client, scanner, apply pipeline, rollback,
release-notes fetcher, LLM risk classifier, notifier, auto-apply
orchestrator, cron entry point.

Public surface:

- :mod:`homelab_mcp.updater.registry`  — Docker Registry v2 client
- :mod:`homelab_mcp.updater.scanner`   — image-drift scanner
- :mod:`homelab_mcp.updater.scheduler` — visibility scan cron
- :mod:`homelab_mcp.updater.snapshot`  — pre-update snapshot
- :mod:`homelab_mcp.updater.apply`     — pull + up + probe
- :mod:`homelab_mcp.updater.rollback`  — pull by digest + tag + up
- :mod:`homelab_mcp.updater.pipeline`  — apply + auto-rollback orchestrator
- :mod:`homelab_mcp.updater.release_notes` — GitHub Releases + CHANGELOG
- :mod:`homelab_mcp.updater.risk`      — LLM risk classifier
- :mod:`homelab_mcp.updater.notifier`  — ntfy (and friends) notification
- :mod:`homelab_mcp.updater.auto_apply` — SAFE+CAUTION apply, BREAKING notify
"""
