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


def find_callers(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Symbols that call ``symbol`` (inbound CALL edges)."""
    return [_ref(e.src, e) for e in graph.in_edges(symbol, EdgeKind.CALL)]


def find_references(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Every symbol that references ``symbol`` (all inbound edges)."""
    return [_ref(e.src, e) for e in graph.in_edges(symbol)]


def find_dependencies(graph: Graph, symbol: SymbolId) -> list[Reference]:
    """Everything ``symbol`` uses (all outbound edges)."""
    return [_ref(e.dst, e) for e in graph.out_edges(symbol)]


def _ref(other: SymbolId, edge: Edge) -> Reference:
    return Reference(
        id=other,
        kind=edge.kind.value,
        tier=edge.resolution.confidence.tier,
        at=edge.at,
    )
