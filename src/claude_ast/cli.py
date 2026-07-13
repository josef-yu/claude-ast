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

from .index import Index, store_path
from .log import configure as configure_logging
from .model import Confidence, Span
from .query import render_repo_map


def _add_min_confidence(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--min-confidence",
        choices=["high", "medium", "low"],
        default="medium",
        help="lowest confidence to include — high=definite, medium=+typed, "
        "low=+name-match heuristics (default: medium)",
    )


def _add_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s", "--source", action="store_true",
        help="show the source line at each resolved site (a resolved 'grep' — no false positives)",
    )
    parser.add_argument(
        "--context", type=int, default=0, metavar="N",
        help="lines of surrounding context to show with --source (default: 0)",
    )


def _read_source(span: Span, context: int) -> list[tuple[int, str]]:
    """The source line(s) at ``span`` (1-based, ± ``context``), or [] if unreadable."""
    try:
        lines = Path(span.file).read_text(errors="replace").splitlines()
    except OSError:
        return []
    lo = max(span.line - context, 1)
    hi = min((span.end_line or span.line) + context, len(lines))
    return [(n, lines[n - 1]) for n in range(lo, hi + 1)]


def main(argv: list[str] | None = None) -> int:
    configure_logging()  # diagnostics -> stderr, keeping stdout a clean data channel
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
    _add_min_confidence(p_callers)
    _add_source(p_callers)

    p_deps = sub.add_parser("deps", help="what a symbol uses")
    p_deps.add_argument("symbol", help="qualified id, e.g. pkg.mod.func")
    p_deps.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    _add_min_confidence(p_deps)
    _add_source(p_deps)

    p_importers = sub.add_parser("importers", help="modules that import a module")
    p_importers.add_argument("module", help="module id, e.g. pkg.mod")
    p_importers.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    _add_source(p_importers)

    p_map = sub.add_parser("repo-map", help="ranked skeleton of the codebase")
    p_map.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    p_map.add_argument("--focus", default=None, help="bias the map toward a symbol/module id")
    p_map.add_argument("--budget", type=int, default=2000, help="token budget (default: 2000)")

    p_status = sub.add_parser("status", help="show index freshness")
    p_status.add_argument("path", nargs="?", default=".", help="project root (default: .)")

    args = parser.parse_args(argv)

    if args.command == "index":
        return _cmd_index(Path(args.path))
    if args.command == "def":
        return _cmd_def(args.name, Path(args.path))
    if args.command == "outline":
        return _cmd_outline(args.module, Path(args.path))
    if args.command == "callers":
        return _cmd_relations(
            args.symbol, Path(args.path), "callers", args.min_confidence, args.source, args.context
        )
    if args.command == "deps":
        return _cmd_relations(
            args.symbol, Path(args.path), "deps", args.min_confidence, args.source, args.context
        )
    if args.command == "importers":
        return _cmd_relations(
            args.module, Path(args.path), "importers", "medium", args.source, args.context
        )
    if args.command == "repo-map":
        return _cmd_repo_map(Path(args.path), args.focus, args.budget)
    if args.command == "status":
        return _cmd_status(Path(args.path))

    parser.print_help()
    return 0


def _cmd_index(root: Path) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    # Build through the Index so the run actually persists the snapshot it warms
    # — the CLI's whole "build/update the index" contract.
    index = Index.build(root)
    symbols = list(index.graph.symbols())
    kinds = Counter(sym.kind.value for sym in symbols)
    files = len({sym.span.file for sym in symbols})

    print(f"indexed {files} files · {len(symbols)} symbols")
    for kind, count in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>6}  {kind}")
    external = sum(1 for _ in index.graph.externals())
    if external:
        print(f"  {external:>6}  external targets (library/stdlib)")

    m = index.metrics
    if m.total_refs:
        definite = m.by_confidence.get("high", 0)
        possible = sum(n for c, n in m.by_confidence.items() if c != "high")
        print(
            f"resolution: {m.coverage:.0%} of {m.total_refs} refs bound"
            f" · {definite} definite / {possible} possible"
        )
        by_source = sorted(m.by_source.items(), key=lambda kv: (-kv[1], kv[0]))
        print("  by source: " + ", ".join(f"{src} {n}" for src, n in by_source))

    if index.skipped:
        print(f"  skipped {len(index.skipped)} file(s) — unreadable or syntax error")
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


def _cmd_relations(
    symbol: str, root: Path, which: str, min_confidence: str,
    source: bool = False, context: int = 0,
) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    index = Index.build(root)
    conf = Confidence(min_confidence)
    if which == "callers":
        refs = index.find_callers(symbol, conf)
    elif which == "importers":
        refs = index.find_importers(symbol)
    else:
        refs = index.find_dependencies(symbol, conf)
    if not refs:
        verb = {"callers": "callers of", "importers": "importers of"}.get(which, "dependencies for")
        print(f"no {verb} {symbol!r}", file=sys.stderr)
        return 1
    for r in refs:
        loc = f"{r.at.file}:{r.at.line}  " if r.at else ""
        ext = "  [external]" if r.external else ""
        print(f"{loc}[{r.tier}] {r.kind:<9} {r.id}{ext}")
        if source and r.at is not None:
            for line_no, text in _read_source(r.at, context):
                print(f"    {line_no:>6}  {text}")
    return 0


def _cmd_repo_map(root: Path, focus: str | None, budget: int) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    entries = Index.build(root).repo_map(budget=budget, focus=focus)
    if not entries:
        print("claude-ast: empty index", file=sys.stderr)
        return 1
    print(render_repo_map(entries))
    return 0


def _cmd_status(root: Path) -> int:
    if not root.exists():
        print(f"claude-ast: path not found: {root}", file=sys.stderr)
        return 2

    snapshot = store_path(root)
    warm = snapshot.exists()
    index = Index.build(root)
    print(f"root:     {root}")
    print(f"symbols:  {len(index.graph)}")
    print(f"snapshot: {snapshot} ({'warm — reused' if warm else 'created (cold start)'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
