"""Neutral relationship-query tests — over a Graph built from model primitives.

Edges are constructed directly, so these prove find_callers / find_references /
find_dependencies and confidence tiering independently of any backend.
"""

from claude_ast.model import (
    Confidence,
    Edge,
    EdgeKind,
    Graph,
    Resolution,
    ResolutionSource,
    Span,
    Symbol,
    SymbolKind,
)
from claude_ast.query import find_callers, find_dependencies, find_references

_LOW = Resolution(ResolutionSource.HEURISTIC, Confidence.LOW)


def _graph() -> Graph:
    graph = Graph()
    graph.add_symbol(Symbol("m.a", "a", SymbolKind.FUNCTION, Span("m.py", 1)))
    graph.add_symbol(Symbol("m.b", "b", SymbolKind.FUNCTION, Span("m.py", 5)))
    graph.add_symbol(Symbol("m.C", "C", SymbolKind.CLASS, Span("m.py", 9)))
    graph.add_edge(Edge("m.a", "m.b", EdgeKind.CALL, Resolution.syntactic()))  # a calls b
    graph.add_edge(Edge("m.C", "m.b", EdgeKind.CALL, Resolution.syntactic()))  # C calls b
    graph.add_edge(Edge("m.a", "m.C", EdgeKind.INHERITS, _LOW))  # a -> C, low confidence
    return graph


def test_find_callers_returns_inbound_calls_as_definite():
    refs = find_callers(_graph(), "m.b")
    assert {r.id for r in refs} == {"m.a", "m.C"}
    assert all(r.kind == "call" and r.tier == "definite" for r in refs)


def test_find_dependencies_returns_all_outbound_kinds_with_tiers():
    got = {(r.id, r.kind, r.tier) for r in find_dependencies(_graph(), "m.a", Confidence.LOW)}
    assert got == {("m.b", "call", "definite"), ("m.C", "inherits", "possible")}


def test_find_references_includes_non_call_edges():
    got = {(r.id, r.kind, r.tier) for r in find_references(_graph(), "m.C", Confidence.LOW)}
    assert got == {("m.a", "inherits", "possible")}


def test_low_confidence_surfaces_as_possible():
    (ref,) = find_dependencies(_graph(), "m.a", Confidence.LOW)[1:]  # the inherits edge
    assert ref.tier == "possible"


def test_min_confidence_gates_low_edges_by_default():
    g = _graph()
    # the default floor (MEDIUM) omits the LOW inherits edge — only the definite call shows
    assert {r.id for r in find_dependencies(g, "m.a")} == {"m.b"}
    # the consumer widens to fetch the low-confidence guess...
    assert {r.id for r in find_dependencies(g, "m.a", Confidence.LOW)} == {"m.b", "m.C"}
    # ...or tightens to definite-only
    assert {r.id for r in find_dependencies(g, "m.a", Confidence.HIGH)} == {"m.b"}
