"""Resolution metrics over a hand-built graph — neutral, model primitives only."""

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
from claude_ast.query import resolution_metrics


def _graph() -> Graph:
    g = Graph()
    g.add_symbol(Symbol("m.f", "f", SymbolKind.FUNCTION, Span("m.py", 1)))
    g.add_symbol(Symbol("m.g", "g", SymbolKind.FUNCTION, Span("m.py", 5)))
    g.add_edge(Edge("m.f", "m.g", EdgeKind.CALL, Resolution.syntactic(), Span("m.py", 2)))
    g.add_edge(Edge("m.f", "m.g", EdgeKind.CALL, Resolution.inferred(), Span("m.py", 3)))
    return g


def test_metrics_count_edges_by_confidence_and_source():
    m = resolution_metrics(total_refs=4, graph=_graph())
    assert m.total_refs == 4
    assert m.bound_refs == 2  # two distinct call sites (lines 2 and 3)
    assert m.coverage == 0.5
    assert m.by_confidence == {"high": 1, "medium": 1}
    assert m.by_source == {"syntactic": 1, "inference": 1}


def test_metrics_collapse_multiplicity_to_one_bound_site():
    # Multiple candidate edges for ONE call site count once toward bound_refs, so
    # coverage stays <= 1.0 even when the resolver stack fans out.
    g = Graph()
    g.add_symbol(Symbol("m.f", "f", SymbolKind.FUNCTION, Span("m.py", 1)))
    for cls in ("A", "B"):
        g.add_symbol(Symbol(f"m.{cls}.save", "save", SymbolKind.METHOD, Span("m.py", 1)))
        g.add_edge(
            Edge(
                "m.f",
                f"m.{cls}.save",
                EdgeKind.CALL,
                Resolution(ResolutionSource.HEURISTIC, Confidence.LOW),
                Span("m.py", 2),  # same site — one obj.save() fanning out
            )
        )
    m = resolution_metrics(total_refs=1, graph=g)
    assert m.bound_refs == 1  # one site, two candidate edges
    assert m.coverage == 1.0
    assert m.by_confidence == {"low": 2}


def test_metrics_empty_graph_is_zero_coverage():
    m = resolution_metrics(total_refs=0, graph=Graph())
    assert m.total_refs == 0 and m.bound_refs == 0
    assert m.coverage == 0.0  # no division by zero
