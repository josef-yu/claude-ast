"""MCP server — FastMCP wrapping the proven engine.

stdio transport, one long-lived process per project, ambient watcher, read-only
``status`` / ``list_projects``. Built LAST, once the engine is validated on real
repos via the CLI. The tool surface is sized empirically by usefulness eval, not
specced up front.  [P3]
"""
