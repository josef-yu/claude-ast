"""Relationship queries — pure functions over a Graph's edges.

find_callers / find_references (inbound) and find_dependencies (outbound). Each
result carries its confidence **tier** (``definite`` / ``possible``), and each query
takes ``min_confidence``: the consumer's dial from the reliable default (``MEDIUM`` —
definite plus typed guesses) down to the ``LOW`` name-match heuristics, fetched only
when it needs the recall.

A second dial, ``reassignments``, governs edges whose receiver is a *reassigned* local
(see ``ingest/python/flow.py``): ``split`` (default) shows the type live at each use,
``off`` drops those flow-derived guesses entirely, ``union`` adds the may-set widening.
A query never silently trims — ``suppression`` reports how many edges each dial hid.
"Report, don't rule" with the caller in control of how much.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ..model import Confidence, Edge, EdgeKind, FlowKind, Graph, Span, SymbolId

# The default floor: definite + typed-possible (annotation/inference), but not the
# LOW name-match guesses — those are one explicit `min_confidence=Confidence.LOW` away.
DEFAULT_MIN_CONFIDENCE = Confidence.MEDIUM


class ReassignMode(StrEnum):
    """How a reassigned local's edges surface — the ``FlowKind`` axis, dialled by the caller."""

    SPLIT = "split"  # default: STABLE + FLOW — the type live at each use
    OFF = "off"      # STABLE only — no reassignment-derived edges
    UNION = "union"  # STABLE + FLOW + MAY — the whole may-set (every type the variable takes)


DEFAULT_REASSIGNMENTS = ReassignMode.SPLIT

_MODE_ALLOWS: dict[ReassignMode, frozenset[FlowKind]] = {
    ReassignMode.OFF: frozenset({FlowKind.STABLE}),
    ReassignMode.SPLIT: frozenset({FlowKind.STABLE, FlowKind.FLOW}),
    ReassignMode.UNION: frozenset({FlowKind.STABLE, FlowKind.FLOW, FlowKind.MAY}),
}


@dataclass(slots=True)
class Reference:
    """One end of a relationship: the *other* symbol, plus how/where and how sure."""

    id: SymbolId
    kind: str
    tier: str  # definite | possible
    at: Span | None
    external: bool = False  # the other symbol is a library/stdlib node, not in-tree


@dataclass(slots=True)
class Suppressed:
    """How many edges a query hid, so a trimmed result is never a silent truncation. Reported
    alongside the results by the delivery layer (CLI line, MCP field)."""

    confidence: int = 0    # dropped for being below ``min_confidence``
    reassignment: int = 0  # dropped by the ``reassignments`` mode (a FLOW/MAY edge it excludes)

    def any(self) -> bool:
        return bool(self.confidence or self.reassignment)


def _shown(e: Edge, min_confidence: Confidence, allowed: frozenset[FlowKind]) -> bool:
    return e.resolution.flow in allowed and e.resolution.confidence.rank >= min_confidence.rank


def find_callers(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE,
    reassignments: ReassignMode = DEFAULT_REASSIGNMENTS,
) -> list[Reference]:
    """Symbols that call ``symbol`` (inbound CALL edges) at least ``min_confidence`` sure."""
    allowed = _MODE_ALLOWS[reassignments]
    return [
        _ref(e.src, e)
        for e in graph.in_edges(symbol, EdgeKind.CALL)
        if _shown(e, min_confidence, allowed)
    ]


def find_references(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE,
    reassignments: ReassignMode = DEFAULT_REASSIGNMENTS,
) -> list[Reference]:
    """Every symbol that *references* ``symbol`` — a use (call, attribute read,
    inheritance, import), not mere structural containment.

    CONTAINS is excluded explicitly: a parent "containing" a symbol is structure,
    already carried by parent/children adjacency, not a reference. Stating the
    exclusion keeps the invariant honest once the P2 resolver stack starts
    emitting more edge kinds — 'references' won't silently absorb containment.
    """
    allowed = _MODE_ALLOWS[reassignments]
    return [
        _ref(e.src, e)
        for e in graph.in_edges(symbol)
        if e.kind is not EdgeKind.CONTAINS and _shown(e, min_confidence, allowed)
    ]


def find_dependencies(
    graph: Graph, symbol: SymbolId, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE,
    reassignments: ReassignMode = DEFAULT_REASSIGNMENTS,
) -> list[Reference]:
    """Everything ``symbol`` uses (outbound edges) at least ``min_confidence`` sure,
    library/stdlib targets flagged."""
    allowed = _MODE_ALLOWS[reassignments]
    return [
        _ref(e.dst, e, graph.is_external(e.dst))
        for e in graph.out_edges(symbol)
        if _shown(e, min_confidence, allowed)
    ]


def find_importers(graph: Graph, module: SymbolId) -> list[Reference]:
    """Modules that import ``module`` — the reverse import graph (inbound IMPORT edges).

    The direction text search does worst: a module's importers spell it many ways
    (``import a.b`` / ``from a.b import c`` / aliases / relative ``from ..a.b import c`` that
    doesn't even contain the name), all of which binding already resolved to one qualname.
    Imports are always definite/stable, so no confidence or reassignment dial applies.
    """
    return [_ref(e.src, e) for e in graph.in_edges(module, EdgeKind.IMPORT)]


def suppression(
    graph: Graph, symbol: SymbolId, which: str,
    min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE,
    reassignments: ReassignMode = DEFAULT_REASSIGNMENTS,
) -> Suppressed:
    """How many of ``which`` (``callers`` / ``references`` / ``dependencies``) edges the two dials
    hid — the honest companion to the ``find_*`` results. Confidence is checked *first*: an edge
    below the floor is reported there even when the mode would also exclude it, because relaxing the
    mode alone would not reveal it — lowering the floor is the operative first step, and once above
    it the mode-exclusion re-surfaces in a later query. Each hidden edge is counted exactly once."""
    allowed = _MODE_ALLOWS[reassignments]
    out = Suppressed()
    for e in _candidates(graph, symbol, which):
        if e.resolution.confidence.rank < min_confidence.rank:
            out.confidence += 1
        elif e.resolution.flow not in allowed:
            out.reassignment += 1
    return out


def _candidates(graph: Graph, symbol: SymbolId, which: str) -> Iterable[Edge]:
    """The edges a relation query considers before filtering — matched to each ``find_*``."""
    if which == "callers":
        return graph.in_edges(symbol, EdgeKind.CALL)
    if which == "dependencies":
        return graph.out_edges(symbol)
    if which == "references":
        return [e for e in graph.in_edges(symbol) if e.kind is not EdgeKind.CONTAINS]
    return ()


def _ref(other: SymbolId, edge: Edge, external: bool = False) -> Reference:
    return Reference(
        id=other,
        kind=edge.kind.value,
        tier=edge.resolution.confidence.tier,
        at=edge.at,
        external=external,
    )
