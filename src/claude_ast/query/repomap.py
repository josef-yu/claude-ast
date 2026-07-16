"""repo_map — a compressed, ranked skeleton of the codebase.

Deterministic and LLM-free: signatures come from the parse, the "summary" is the
author's docstring first line, importance is the confidence-weighted PageRank,
and the whole thing is filled to a token budget. It is rank-and-render over the
same normalized model — orientation is just the graph, ranked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from ..model import Graph, Symbol, SymbolId, SymbolKind
from .rank import pagerank

# The structural skeleton — modules become headers, variables are omitted as noise.
_STRUCTURAL = (SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD)

# The no-focus ranks and the (-rank, id)-sorted structural candidates are a pure function of the
# graph, but cost a 40-iteration PageRank plus a full-population sort each call. The server serves
# repo_map with the default (no) focus repeatedly against one graph, so memoize both, keyed weakly
# on graph identity: a watcher patch swaps in a fresh graph and the stale entry is collected. Only
# the focus=None case is cached (a focus makes the ranks and the sort focus-specific).
_NoFocus = tuple[dict[SymbolId, float], list[Symbol]]
_NO_FOCUS_CACHE: WeakKeyDictionary[Graph, _NoFocus] = WeakKeyDictionary()


def _ranked_candidates(graph: Graph, focus: str | None) -> _NoFocus:
    """The ranks and rank-sorted structural candidates, memoized for the no-focus hot path.

    Gated on ``if focus`` (truthiness), matching ``_teleport``: a falsy focus (``None`` *or* an
    empty string) yields the uniform no-focus teleport, so both share the one memoized entry
    rather than re-running the full PageRank + sort on every call."""
    if focus:
        return _rank_and_sort(graph, focus)
    cached = _NO_FOCUS_CACHE.get(graph)
    if cached is None:
        cached = _rank_and_sort(graph, None)
        _NO_FOCUS_CACHE[graph] = cached
    return cached


def _rank_and_sort(graph: Graph, focus: str | None) -> _NoFocus:
    ranks = pagerank(graph, focus)
    # Rank desc, then id asc as an explicit tie-break: the huge population of equal-rank
    # (no-inbound) symbols must order on stable id, not insertion order.
    candidates = [s for s in graph.symbols() if s.kind in _STRUCTURAL]
    candidates.sort(key=lambda s: (-ranks.get(s.id, 0.0), s.id))
    return ranks, candidates


@dataclass(slots=True)
class RepoMapEntry:
    id: SymbolId
    name: str
    kind: str
    signature: str | None
    doc: str | None
    module: SymbolId
    depth: int
    line: int
    rank: float


def repo_map(graph: Graph, budget: int = 2000, focus: str | None = None) -> list[RepoMapEntry]:
    """The top structural symbols by rank, filled to ``budget`` tokens."""
    # ranks + candidates are budget-independent, so they're shared across budgets and cached
    # for the focus=None hot path; only the fill loop below depends on ``budget``.
    ranks, candidates = _ranked_candidates(graph, focus)

    entries: list[RepoMapEntry] = []
    spent = 0
    for sym in candidates:
        cost = _tokens(sym)
        if entries and spent + cost > budget:
            break
        spent += cost
        module, depth = _module_and_depth(graph, sym)
        entries.append(
            RepoMapEntry(
                id=sym.id,
                name=sym.name,
                kind=sym.kind.value,
                signature=sym.signature,
                doc=sym.doc,
                module=module,
                depth=depth,
                line=sym.span.line,
                rank=ranks.get(sym.id, 0.0),
            )
        )
    return entries


def render_repo_map(entries: list[RepoMapEntry]) -> str:
    """Render entries as an indented skeleton, most-important module first."""
    by_module: dict[SymbolId, list[RepoMapEntry]] = defaultdict(list)
    for entry in entries:
        by_module[entry.module].append(entry)
    modules = sorted(by_module, key=lambda m: (-max(x.rank for x in by_module[m]), m))

    lines: list[str] = []
    for module in modules:
        lines.append(module)
        for entry in sorted(by_module[module], key=lambda x: (x.line, x.id)):
            indent = "  " * max(entry.depth, 1)
            label = entry.signature or f"{entry.kind} {entry.name}"
            doc = f"    # {entry.doc}" if entry.doc else ""
            lines.append(f"{indent}{label}{doc}")
    return "\n".join(lines)


def _module_and_depth(graph: Graph, sym: Symbol) -> tuple[SymbolId, int]:
    """Walk parents to the owning module, counting hops — the neutral replacement for parsing
    dotted ids. Stops at the nearest ``MODULE`` ancestor (the symbol's *own* module), NOT the
    topmost ancestor: with the module-tree adjacency a submodule's topmost ancestor is its
    package, but a member belongs to its submodule. The hop count is the nesting depth within
    that module (indentation for the skeleton).
    """
    cur = sym
    depth = 0
    while cur.kind is not SymbolKind.MODULE and cur.parent is not None:
        parent = graph.symbol(cur.parent)
        if parent is None:
            break
        cur = parent
        depth += 1
    return cur.id, depth


def _tokens(sym: Symbol) -> int:
    text = f"{sym.signature or sym.name} {sym.doc or ''}"
    return max(1, len(text) // 4)
