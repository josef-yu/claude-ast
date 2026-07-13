"""Relationship queries — pure functions over a Graph's edges.

find_callers / find_references (inbound) and find_dependencies (outbound). Each
result carries its confidence **tier** (``definite`` / ``possible``), and each query
takes ``min_confidence``: the consumer's dial from the reliable default (``MEDIUM`` —
definite plus typed guesses) down to the ``LOW`` name-match heuristics, fetched only
when it needs the recall. "Report, don't rule" with the caller in control of how much.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Confidence, Edge, EdgeKind, Graph, Span, SymbolId

# The default floor: definite + typed-possible (annotation/inference), but not the
# LOW name-match guesses — those are one explicit `min_confidence=Confidence.LOW` away.
DEFAULT_MIN_CONFIDENCE = Confidence.MEDIUM


@dataclass(slots=True)
class Reference:
    """One end of a relationship: the *other* symbol, plus how/where and how sure."""

    id: SymbolId
    kind: str
    tier: str  # definite | possible
    at: Span | None
    external: bool = False  # the other symbol is a library/stdlib node, not in-tree


def find_callers(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
) -> list[Reference]:
    """Symbols that call ``symbol`` (inbound CALL edges) at least ``min_confidence`` sure."""
    return [
        _ref(e.src, e)
        for e in graph.in_edges(symbol, EdgeKind.CALL)
        if e.resolution.confidence.rank >= min_confidence.rank
    ]


def find_references(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
) -> list[Reference]:
    """Every symbol that *references* ``symbol`` — a use (call, attribute read,
    inheritance, import), not mere structural containment.

    CONTAINS is excluded explicitly: a parent "containing" a symbol is structure,
    already carried by parent/children adjacency, not a reference. Stating the
    exclusion keeps the invariant honest once the P2 resolver stack starts
    emitting more edge kinds — 'references' won't silently absorb containment.
    """
    return [
        _ref(e.src, e)
        for e in graph.in_edges(symbol)
        if e.kind is not EdgeKind.CONTAINS and e.resolution.confidence.rank >= min_confidence.rank
    ]


def find_dependencies(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
) -> list[Reference]:
    """Everything ``symbol`` uses (outbound edges) at least ``min_confidence`` sure,
    library/stdlib targets flagged."""
    return [
        _ref(e.dst, e, graph.is_external(e.dst))
        for e in graph.out_edges(symbol)
        if e.resolution.confidence.rank >= min_confidence.rank
    ]


def _ref(other: SymbolId, edge: Edge, external: bool = False) -> Reference:
    return Reference(
        id=other,
        kind=edge.kind.value,
        tier=edge.resolution.confidence.tier,
        at=edge.at,
        external=external,
    )
