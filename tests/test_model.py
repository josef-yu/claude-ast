"""Tier-1 correctness — the model contract and graph adjacency.

Small, deterministic, known-answer. (Django is the Tier-2 usefulness/scale
fixture, not a unit-correctness one.)
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


def test_confidence_maps_to_two_tier_surface():
    assert Confidence.HIGH.tier == "definite"
    assert Confidence.MEDIUM.tier == "possible"
    assert Confidence.LOW.tier == "possible"


def test_syntactic_base_is_always_certain():
    r = Resolution.syntactic()
    assert r.source is ResolutionSource.SYNTACTIC
    assert r.confidence is Confidence.HIGH


def test_graph_forward_and_reverse_adjacency():
    g = Graph()
    caller = Symbol("m.caller", "caller", SymbolKind.FUNCTION, Span("m.py", 1))
    callee = Symbol("m.callee", "callee", SymbolKind.FUNCTION, Span("m.py", 5))
    g.add_symbol(caller)
    g.add_symbol(callee)
    g.add_edge(Edge("m.caller", "m.callee", EdgeKind.CALL, Resolution.syntactic()))

    # find_dependencies(caller) -> outbound
    assert [e.dst for e in g.out_edges("m.caller", EdgeKind.CALL)] == ["m.callee"]
    # find_callers(callee) -> inbound
    assert [e.src for e in g.in_edges("m.callee", EdgeKind.CALL)] == ["m.caller"]
    assert len(g) == 2


def test_heuristic_multiplicity_is_multiple_low_edges():
    # An unresolved `obj.save()` fans out to every candidate `*.save`, each LOW.
    g = Graph()
    for cls in ("User", "Post", "Session"):
        g.add_symbol(Symbol(f"m.{cls}.save", "save", SymbolKind.METHOD, Span("m.py", 1)))
        g.add_edge(
            Edge(
                "m.caller",
                f"m.{cls}.save",
                EdgeKind.CALL,
                Resolution(ResolutionSource.HEURISTIC, Confidence.LOW),
            )
        )
    possible = [e for e in g.out_edges("m.caller") if e.resolution.confidence.tier == "possible"]
    assert len(possible) == 3


def test_symbols_scoped_by_file():
    g = Graph()
    g.add_symbol(Symbol("m.a", "a", SymbolKind.FUNCTION, Span("m.py", 1)))
    g.add_symbol(Symbol("n.b", "b", SymbolKind.FUNCTION, Span("n.py", 1)))
    assert [s.id for s in g.symbols_in_file("m.py")] == ["m.a"]


def test_external_nodes_are_edge_sinks_not_indexed_symbols():
    # An EXTERNAL target is addressable as an edge dst but must stay out of
    # enumeration, name lookup, and the indexed-symbol count.
    g = Graph()
    g.add_symbol(Symbol("m.f", "f", SymbolKind.FUNCTION, Span("m.py", 1)))
    g.add_external(Symbol("os.path.join", "join", SymbolKind.EXTERNAL, Span("<external>", 0)))
    g.add_edge(Edge("m.f", "os.path.join", EdgeKind.CALL, Resolution.syntactic()))

    assert g.symbol("os.path.join") is not None and g.is_external("os.path.join")
    assert [s.id for s in g.symbols()] == ["m.f"]  # enumeration excludes externals
    assert g.by_name("join") == []                  # name lookup excludes externals
    assert len(g) == 1                              # externals are not indexed symbols
    # the edge still points at it
    assert [e.dst for e in g.out_edges("m.f")] == ["os.path.join"]

    g.add_external(Symbol("os.path.join", "join", SymbolKind.EXTERNAL, Span("<external>", 0)))
    assert [s.id for s in g.externals()] == ["os.path.join"]  # idempotent


def test_id_collision_keeps_first_and_records_without_corrupting_indexes():
    # A submodule `pkg/helpers.py` and a `class helpers` in `pkg/__init__.py` both mint the id
    # `pkg.helpers`. The flat id keyspace can't tell them apart; the guard keeps the first
    # deterministically, records the clash, and — critically — never lets the second poison the
    # by-file / by-name / children indexes (the old last-write-wins left them with a stale id).
    g = Graph()
    module = Symbol("pkg.helpers", "helpers", SymbolKind.MODULE, Span("pkg/helpers.py", 1))
    klass = Symbol("pkg.helpers", "helpers", SymbolKind.CLASS, Span("pkg/__init__.py", 3))
    g.add_symbol(module)
    g.add_symbol(klass)  # collides — dropped

    assert g.symbol("pkg.helpers") is module          # first wins
    assert g.collisions() == ["pkg.helpers"]           # the clash is surfaced, not silent
    assert len(g) == 1                                  # the loser is not counted
    # the loser's file must not carry a stale id that resolves to the winner (wrong file)
    assert g.symbols_in_file("pkg/__init__.py") == []
    assert [s.id for s in g.symbols_in_file("pkg/helpers.py")] == ["pkg.helpers"]
    assert [s.id for s in g.by_name("helpers")] == ["pkg.helpers"]  # counted once


def test_id_collisions_dedupe_in_first_seen_order():
    g = Graph()
    g.add_symbol(Symbol("m.a", "a", SymbolKind.FUNCTION, Span("m.py", 1)))
    g.add_symbol(Symbol("m.b", "b", SymbolKind.FUNCTION, Span("m.py", 2)))
    # `m.a` clashes twice, `m.b` once — each id reported once, in first-seen order.
    g.add_symbol(Symbol("m.a", "a", SymbolKind.CLASS, Span("n.py", 1)))
    g.add_symbol(Symbol("m.b", "b", SymbolKind.CLASS, Span("n.py", 2)))
    g.add_symbol(Symbol("m.a", "a", SymbolKind.CLASS, Span("o.py", 1)))
    assert g.collisions() == ["m.a", "m.b"]
