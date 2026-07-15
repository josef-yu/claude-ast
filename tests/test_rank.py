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


def test_pagerank_is_deterministic():
    # The int-indexed / CSR power iteration must be reproducible: same graph -> bit-identical
    # scores, run to run. This is what lets repo_map's (-rank, id) ordering be stable, so a
    # future change to the numeric core that perturbs scores trips this guard.
    graph = _hub_graph()
    assert pagerank(graph) == pagerank(graph)
    assert pagerank(graph, focus="m.a") == pagerank(graph, focus="m.a")


def test_confidence_weighted_flow_orders_by_edge_strength():
    # Two hubs, each with one inbound edge, differing only in confidence: the definite edge
    # must flow more importance than the heuristic one. Pins the confidence weighting the
    # ranker is built on, independent of the summation internals.
    graph = Graph()
    for name in ("caller", "strong", "weak"):
        graph.add_symbol(Symbol(f"m.{name}", name, SymbolKind.FUNCTION, Span("m.py", 1)))
    graph.add_edge(Edge("m.caller", "m.strong", EdgeKind.CALL, Resolution.syntactic()))  # HIGH
    graph.add_edge(Edge("m.caller", "m.weak", EdgeKind.CALL, Resolution.heuristic()))  # LOW
    ranks = pagerank(graph)
    assert ranks["m.strong"] > ranks["m.weak"]
