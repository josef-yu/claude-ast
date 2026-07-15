"""Neutral repo_map tests — over a Graph built from model primitives."""

from claude_ast.model import Edge, EdgeKind, Graph, Resolution, Span, Symbol, SymbolKind
from claude_ast.query import render_repo_map, repo_map


def _graph() -> Graph:
    graph = Graph()
    graph.add_symbol(Symbol("m", "m", SymbolKind.MODULE, Span("m.py", 1)))
    graph.add_symbol(
        Symbol("m.CONST", "CONST", SymbolKind.VARIABLE, Span("m.py", 2), parent="m")
    )
    for line, name in enumerate(("hub", "a", "b", "c"), start=3):
        graph.add_symbol(
            Symbol(
                f"m.{name}",
                name,
                SymbolKind.FUNCTION,
                Span("m.py", line),
                signature=f"def {name}()",
                parent="m",
            )
        )
    for caller in ("a", "b", "c"):
        graph.add_edge(Edge(f"m.{caller}", "m.hub", EdgeKind.CALL, Resolution.syntactic()))
    return graph


def test_ranks_most_referenced_first_and_omits_module_and_vars():
    entries = repo_map(_graph(), budget=1000)
    ids = [e.id for e in entries]
    assert "m" not in ids  # module is a header, not an entry
    assert "m.CONST" not in ids  # variables omitted as noise
    assert entries[0].id == "m.hub"  # most-referenced ranks first
    assert set(ids) == {"m.hub", "m.a", "m.b", "m.c"}


def test_budget_limits_entries_but_always_keeps_the_top():
    entries = repo_map(_graph(), budget=1)  # smaller than any single entry
    assert len(entries) == 1
    assert entries[0].id == "m.hub"


def test_render_leads_with_module_header_and_shows_signatures():
    text = render_repo_map(repo_map(_graph()))
    assert text.splitlines()[0] == "m"  # module header
    assert "def hub()" in text


def test_ordering_is_stable_across_calls_and_independent_graphs():
    # The rendered ordering is the determinism guarantee repo_map makes. A repeat call (a cache
    # hit) and a second, independently-built identical graph (a cache miss) must both reproduce
    # the exact same ordering — so the memoization can never leak between graphs or drift.
    order = [e.id for e in repo_map(_graph())]
    assert [e.id for e in repo_map(_graph())] == order  # fresh graph, cache miss -> identical
    g = _graph()
    assert [e.id for e in repo_map(g)] == order
    assert [e.id for e in repo_map(g)] == order  # same graph, cache hit -> identical


def test_no_focus_result_is_memoized_and_a_focus_query_bypasses_it():
    from claude_ast.query.repomap import _NO_FOCUS_CACHE  # white-box: guard the cache itself

    g = _graph()
    assert g not in _NO_FOCUS_CACHE
    no_focus = [e.id for e in repo_map(g)]
    assert g in _NO_FOCUS_CACHE  # the no-focus ranks + candidates are cached for reuse

    repo_map(g, focus="m.a")  # a focus query must not read or overwrite the no-focus cache
    assert [e.id for e in repo_map(g)] == no_focus  # no-focus result still intact after it
