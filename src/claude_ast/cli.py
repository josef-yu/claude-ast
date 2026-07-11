"""Command-line entry point — the engine's test harness before the MCP server.

    claude-ast index <path>   build/update the index for a project
    claude-ast status         show index freshness

During the engine-first build (P0-P2) this CLI is how we drive and evaluate the
engine on real repos (e.g. Django) without any MCP transport. The MCP server
(P3) wraps the same, proven engine.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-ast", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="build or update the index for a project")
    p_index.add_argument("path", help="project root to index")

    sub.add_parser("status", help="show index freshness")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    # P1 wires these subcommands to the engine.
    print(
        f"claude-ast: '{args.command}' is not implemented yet (P0 scaffold).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
