"""MCP tool wrappers: stacks, events, health, updates.

Tools are registered with the FastMCP singleton at import time via
``@mcp.tool()``. Keep imports lazy inside the tool bodies to avoid
importing host clients (which may try to talk to docker) at server
startup.
"""
