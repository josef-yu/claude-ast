"""Relationship queries — pure functions over a Graph's edges.

find_callers / find_references (inbound) and find_dependencies (outbound). Each
result carries its confidence **tier** (``definite`` / ``possible``) — the whole
point of the model. In P1 every edge is syntactic, so everything is ``definite``;
the P2 resolver stack introduces ``possible`` edges.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Edge, EdgeKind, Graph, Span, SymbolId


@dataclass(slots=True)
class Reference:
    """One end of a relationship: the *other* symbol, plus how/where and how sure."""

    id: SymbolId
    kind: str
    tier: str  # definite | possible
    at: Span | None
    external: bool = False  # the other symbol is a library/stdlib node, not in-tree


def find_callers(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Symbols that call ``symbol`` (inbound CALL edges)."""
    return [_ref(e.src, e) for e in graph.in_edges(symbol, EdgeKind.CALL)]


def find_references(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Every symbol that *references* ``symbol`` — a use (call, attribute read,
    inheritance, import), not mere structural containment.

    CONTAINS is excluded explicitly: a parent "containing" a symbol is structure,
    already carried by parent/children adjacency, not a reference. Stating the
    exclusion keeps the invariant honest once the P2 resolver stack starts
    emitting more edge kinds — 'references' won't silently absorb containment.
    """
    return [_ref(e.src, e) for e in graph.in_edges(symbol) if e.kind is not EdgeKind.CONTAINS]


def find_dependencies(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Everything ``symbol`` uses (all outbound edges), library/stdlib targets flagged."""
    return [_ref(e.dst, e, graph.is_external(e.dst)) for e in graph.out_edges(symbol)]


def _ref(other: SymbolId, edge: Edge, external: bool = False) -> Reference:
    return Reference(
        id=other,
        kind=edge.kind.value,
        tier=edge.resolution.confidence.tier,
        at=edge.at,
        external=external,
    )
