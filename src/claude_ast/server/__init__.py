"""MCP server — FastMCP wrapping the proven engine (stdio, one process per project).

``build_server(index)`` registers the read-only navigation tools; ``__main__`` builds the
index and runs the stdio loop (``claude-ast-mcp [root]``). The live watcher that keeps the
index fresh across edits is the next P3 increment. The tool surface mirrors the
CLI-validated queries and grows by usefulness eval, not up-front spec.
"""

from .app import build_server

__all__ = ["build_server"]
