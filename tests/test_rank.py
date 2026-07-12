"""Neutral ranker tests — PageRank over a Graph built from model primitives."""

from claude_ast.model import Edge, EdgeKind, Graph, Resolution, Span, Symbol, SymbolKind
from claude_ast.query import pagerank


def _hub_graph() -> Graph:
    # a, b, c all call hub; hub is the most-referenced symbol.
    graph = Graph()
    for name in ("a", "b", "c", "hub"):
        graph.add_symbol(Symbol(f"m.{name}", name, SymbolKind.FUNCTION, Span("m.py", 1)))
    for caller in ("a", "b", "c"):
        graph.add_edge(Edge(f"m.{caller}", "m.hub", EdgeKind.CALL, Resolution.syntactic()))
    return graph


def test_widely_referenced_symbol_ranks_highest():
    ranks = pagerank(_hub_graph())
    assert max(ranks, key=lambda k: ranks[k]) == "m.hub"


def test_ranks_form_a_distribution():
    ranks = pagerank(_hub_graph())
    assert abs(sum(ranks.values()) - 1.0) < 1e-6


def test_empty_graph_ranks_empty():
    assert pagerank(Graph()) == {}


def test_focus_lifts_the_focused_symbol():
    graph = _hub_graph()
    base = pagerank(graph)
    focused = pagerank(graph, focus="m.a")
    assert focused["m.a"] > base["m.a"]
