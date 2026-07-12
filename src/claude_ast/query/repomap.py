"""repo_map — a compressed, ranked skeleton of the codebase.

Deterministic and LLM-free: signatures come from the parse, the "summary" is the
author's docstring first line, importance is the confidence-weighted PageRank,
and the whole thing is filled to a token budget. It is rank-and-render over the
same normalized model — orientation is just the graph, ranked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..model import Graph, Symbol, SymbolId, SymbolKind
from .rank import pagerank

# The structural skeleton — modules become headers, variables are omitted as noise.
_STRUCTURAL = (SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD)


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
    ranks = pagerank(graph, focus)
    candidates = [s for s in graph.symbols() if s.kind in _STRUCTURAL]
    candidates.sort(key=lambda s: ranks.get(s.id, 0.0), reverse=True)

    entries: list[RepoMapEntry] = []
    spent = 0
    for sym in candidates:
        cost = _tokens(sym)
        if entries and spent + cost > budget:
            break
        spent += cost
        module = _module_of(graph, sym)
        entries.append(
            RepoMapEntry(
                id=sym.id,
                name=sym.name,
                kind=sym.kind.value,
                signature=sym.signature,
                doc=sym.doc,
                module=module,
                depth=max(sym.id.count(".") - module.count("."), 0),
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
    modules = sorted(by_module, key=lambda m: max(x.rank for x in by_module[m]), reverse=True)

    lines: list[str] = []
    for module in modules:
        lines.append(module)
        for entry in sorted(by_module[module], key=lambda x: x.line):
            indent = "  " * max(entry.depth, 1)
            label = entry.signature or f"{entry.kind} {entry.name}"
            doc = f"    # {entry.doc}" if entry.doc else ""
            lines.append(f"{indent}{label}{doc}")
    return "\n".join(lines)


def _module_of(graph: Graph, sym: Symbol) -> SymbolId:
    cur = sym
    while cur.parent is not None:
        parent = graph.symbol(cur.parent)
        if parent is None:
            break
        cur = parent
    return cur.id


def _tokens(sym: Symbol) -> int:
    text = f"{sym.signature or sym.name} {sym.doc or ''}"
    return max(1, len(text) // 4)
