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
from collections import Counter
from pathlib import Path

from .index import Index
from .ingest import ingest_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-ast", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="build or update the index for a project")
    p_index.add_argument("path", help="project root to index")

    p_def = sub.add_parser("def", help="find where a name is defined")
    p_def.add_argument("name", help="a bare name (User) or qualified id (pkg.mod.User)")
    p_def.add_argument("path", nargs="?", default=".", help="project root (default: .)")

    p_outline = sub.add_parser("outline", help="list a module's symbols")
    p_outline.add_argument("module", help="module id, e.g. pkg.mod")
    p_outline.add_argument("path", nargs="?", default=".", help="project root (default: .)")

    p_callers = sub.add_parser("callers", help="who calls a symbol")
    p_callers.add_argument("symbol", help="qualified id, e.g. pkg.mod.func")
    p_callers.add_argument("path", nargs="?", default=".", help="project root (default: .)")

    p_deps = sub.add_parser("deps", help="what a symbol uses")
    p_deps.add_argument("symbol", help="qualified id, e.g. pkg.mod.func")
    p_deps.add_argument("path", nargs="?", default=".", help="project root (default: .)")

    sub.add_parser("status", help="show index freshness")

    args = parser.parse_args(argv)

    if args.command == "index":
        return _cmd_index(Path(args.path))
    if args.command == "def":
        return _cmd_def(args.name, Path(args.path))
    if args.command == "outline":
        return _cmd_outline(args.module, Path(args.path))
    if args.command == "callers":
        return _cmd_relations(args.symbol, Path(args.path), "callers")
    if args.command == "deps":
        return _cmd_relations(args.symbol, Path(args.path), "deps")
    if args.command == "status":
        print("claude-ast: 'status' is not implemented yet (P1).", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


def _cmd_index(root: Path) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    result = ingest_project(root)
    kinds = Counter(sym.kind.value for fi in result.files for sym in fi.symbols)
    total = sum(kinds.values())

    print(f"indexed {len(result.files)} files · {total} symbols")
    for kind, count in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>6}  {kind}")
    if result.skipped:
        print(f"  skipped {len(result.skipped)} file(s) — unreadable or syntax error")
    return 0


def _cmd_def(name: str, root: Path) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    defs = Index.build(root).find_definition(name)
    if not defs:
        print(f"no definition found for {name!r}", file=sys.stderr)
        return 1
    for d in defs:
        sig = f"  {d.signature}" if d.signature else ""
        print(f"{d.span.file}:{d.span.line}  {d.kind:<8} {d.id}{sig}")
    return 0


def _cmd_outline(module: str, root: Path) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    entries = Index.build(root).outline(module)
    if not entries:
        print(f"no module {module!r} in the index", file=sys.stderr)
        return 1
    for e in entries:
        indent = "  " * e.depth
        label = e.signature or f"{e.kind} {e.name}"
        doc = f"    # {e.doc}" if e.doc else ""
        print(f"{indent}{label}{doc}")
    return 0


def _cmd_relations(symbol: str, root: Path, which: str) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    index = Index.build(root)
    refs = index.find_callers(symbol) if which == "callers" else index.find_dependencies(symbol)
    if not refs:
        verb = "callers of" if which == "callers" else "dependencies for"
        print(f"no {verb} {symbol!r}", file=sys.stderr)
        return 1
    for r in refs:
        loc = f"{r.at.file}:{r.at.line}  " if r.at else ""
        print(f"{loc}[{r.tier}] {r.kind:<9} {r.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
