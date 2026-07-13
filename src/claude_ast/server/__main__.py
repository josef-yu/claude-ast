"""``claude-ast-mcp [root]`` / ``python -m claude_ast.server [root]`` — the stdio MCP server.

Builds the project's index once and serves the read-only navigation tools over stdio, one
long-lived process per project. Keeping the index fresh across edits is the watcher's job
(next increment); for now a restart re-indexes (warm, so it's cheap).
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..index import Index
from ..log import configure
from .app import build_server


def main(argv: list[str] | None = None) -> int:
    configure()  # diagnostics -> stderr; stdout carries the MCP protocol
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    if not root.exists():
        print(f"claude-ast-mcp: path not found: {root}", file=sys.stderr)
        return 2
    build_server(Index.build(root)).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
