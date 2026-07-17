"""FastMCP server — read-only navigation tools over a project's Index, spoken on stdio.

Wraps the proven engine: the same queries the CLI validated, exposed as MCP tools for
Claude Code. ``build_server`` takes a pre-built ``Index`` (so it is transport-free and
testable); the entry point (``__main__``) builds the index once per project and runs the
stdio loop. Diagnostics go to stderr via the logging seam — stdout is the protocol channel.

Tool bodies delegate to module-level shapers (``_definition`` etc.) that turn the engine's
dataclasses into JSON-friendly dicts, so the surface is unit-testable without a transport.
The tool set mirrors the CLI-validated queries; it grows by usefulness eval, not up front.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from ..index import Index, IndexSession
from ..model import Confidence
from ..query import ReassignMode, Reference, Suppressed, render_repo_map

_Conf = Literal["high", "medium", "low"]
_Reassign = Literal["split", "off", "union"]


def _definition(index: Index, name: str) -> list[dict]:
    return [
        {
            "id": d.id,
            "kind": d.kind,
            "file": d.span.file,
            "line": d.span.line,
            "signature": d.signature,
        }
        for d in index.find_definition(name)
    ]


def _outline(index: Index, module: str, focus: str | None = None) -> list[dict]:
    return [
        {
            "id": e.id,
            "name": e.name,
            "kind": e.kind,
            "depth": e.depth,
            "signature": e.signature,
            "doc": e.doc,
            "focus": e.id == focus,
        }
        for e in index.outline(module, focus)
    ]


def _ref(r: Reference) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "tier": r.tier,  # definite | possible
        "location": f"{r.at.file}:{r.at.line}" if r.at else None,
        "external": r.external,
    }


def _relations(index: Index, symbol: str, refs: list[Reference], sup: Suppressed) -> dict:
    """Shape a relationship result so an *unknown* id reads differently from a true *empty* answer.
    ``found`` says whether ``symbol`` is a known id; when false, ``suggestions`` gives near-misses,
    so Claude retries a mistyped id instead of trusting a bogus 'no results'. ``results`` are the
    tiered references — empty both for an unknown id and for a real symbol that is simply unused,
    which ``found`` disambiguates. ``suppressed`` counts edges the ``min_confidence`` /
    ``reassignments`` dials hid, so a trimmed result is never read as complete."""
    lookup = index.lookup_symbol(symbol)
    return {
        "symbol": symbol,
        "found": lookup.known,
        "results": [_ref(r) for r in refs],
        "suppressed": {"confidence": sup.confidence, "reassignment": sup.reassignment},
        "suggestions": lookup.suggestions,
    }


def build_server(session: IndexSession) -> FastMCP:
    """Register the read-only navigation tools over ``session`` and return the FastMCP app.

    Tools read ``session.current`` at call time, so a watcher patch is picked up on the next
    query — the served view is always fresh without rebuilding the app.
    """
    mcp = FastMCP("claude-ast")

    @mcp.tool()
    def find_definition(name: str) -> list[dict]:
        """Where a name is defined. `name` is a bare name (`User`) or a qualified id
        (`pkg.mod.User`); a bare name returns every symbol with that short name."""
        return _definition(session.current, name)

    @mcp.tool()
    def outline(module: str, focus: str | None = None) -> list[dict]:
        """A module's symbols, each with a nesting `depth` and signature. Child submodules are
        collapsed leaves (a table-of-contents); pass `focus` (a symbol id under the module) to
        expand the submodule containing it and reveal the neighbourhood around it (its entry is
        flagged `focus: true`). A `focus` that isn't a symbol under `module` is ignored, yielding
        the plain shallow outline."""
        return _outline(session.current, module, focus)

    @mcp.tool()
    def find_callers(
        symbol: str, min_confidence: _Conf = "medium", reassignments: _Reassign = "split"
    ) -> dict:
        """Symbols that call `symbol`. Returns `{symbol, found, results, suppressed, suggestions}`:
        each result carries a `tier` (definite | possible); widen `min_confidence` (high, medium,
        low) for recall. `reassignments` controls edges from a reassigned variable — `split` (the
        type live at each use), `off` (drop them), `union` (every type it takes). `suppressed` has
        what the dials hid. `found` is false for an unknown id (see `suggestions`)."""
        idx, conf, mode = session.current, Confidence(min_confidence), ReassignMode(reassignments)
        refs = idx.find_callers(symbol, conf, mode)
        return _relations(idx, symbol, refs, idx.suppression(symbol, "callers", conf, mode))

    @mcp.tool()
    def find_dependencies(
        symbol: str, min_confidence: _Conf = "medium", reassignments: _Reassign = "split"
    ) -> dict:
        """What `symbol` uses — calls, inheritance, and library targets (flagged `external`).
        Returns `{symbol, found, results, suppressed, suggestions}`; widen `min_confidence` for
        recall. `reassignments` controls edges from a reassigned variable — `split` (type live at
        each use), `off` (drop them), `union` (every type it takes). `suppressed` counts what the
        dials hid. `found` is false for an unknown id (with `suggestions` near-misses)."""
        idx, conf, mode = session.current, Confidence(min_confidence), ReassignMode(reassignments)
        refs = idx.find_dependencies(symbol, conf, mode)
        return _relations(idx, symbol, refs, idx.suppression(symbol, "dependencies", conf, mode))

    @mcp.tool()
    def repo_map(focus: str | None = None, budget: int = 2000) -> str:
        """A ranked, token-budgeted skeleton of the codebase, optionally biased toward a
        `focus` symbol/module id. `budget` caps the approximate token size."""
        return render_repo_map(session.current.repo_map(budget=budget, focus=focus))

    return mcp
