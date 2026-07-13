"""``claude-ast-mcp [root]`` / ``python -m claude_ast.server [root]`` — the stdio MCP server.

Holds the project's index in an ``IndexSession`` and serves the read-only navigation tools over
stdio, one long-lived process per project. A background watcher thread patches the session on
``.py`` edits, so the served view stays fresh without a restart.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from ..index import IndexSession
from ..log import configure
from ..watch import run_watcher
from .app import build_server


def main(argv: list[str] | None = None) -> int:
    configure()  # diagnostics -> stderr; stdout carries the MCP protocol
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    if not root.exists():
        print(f"claude-ast-mcp: path not found: {root}", file=sys.stderr)
        return 2
    session = IndexSession(root)
    threading.Thread(target=run_watcher, args=(session,), daemon=True).start()
    build_server(session).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
