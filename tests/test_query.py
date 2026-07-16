"""Neutral query-logic tests — over a Graph built from model primitives.

Deliberately backend-agnostic: the Graph is constructed directly from ``Symbol``
objects, never via a language backend, so these prove the query logic works
regardless of how the index was populated. A backend's end-to-end query pass
(parse -> assemble -> query) is tested per backend, in ``tests/backends/``.
"""

from claude_ast.model import Graph, Span, Symbol, SymbolKind
from claude_ast.query import find_definition, lookup_symbol, outline


def _sym(sid, name, kind, line, signature=None, doc=None, parent=None):
    return Symbol(sid, name, kind, Span("m.py", line), signature=signature, doc=doc, parent=parent)


def _auth_graph() -> Graph:
    graph = Graph()
    for sym in (
        _sym("auth", "auth", SymbolKind.MODULE, 1, doc="Auth module."),
        _sym("auth.authenticate", "authenticate", SymbolKind.FUNCTION, 4,
             signature="def authenticate(email: str) -> bool", parent="auth"),
        _sym("auth.User", "User", SymbolKind.CLASS, 8, signature="class User(Base)", parent="auth"),
        _sym("auth.User.save", "save", SymbolKind.METHOD, 11,
             signature="def save(self) -> None", parent="auth.User"),
        _sym("auth.Session", "Session", SymbolKind.CLASS, 15,
             signature="class Session(Base)", parent="auth"),
        _sym("auth.Session.save", "save", SymbolKind.METHOD, 16,
             signature="def save(self) -> None", parent="auth.Session"),
    ):
        graph.add_symbol(sym)
    return graph


def test_find_definition_by_qualified_id_is_exact():
    defs = find_definition(_auth_graph(), "auth.User")
    assert [d.id for d in defs] == ["auth.User"]
    assert defs[0].kind == "class"
    assert defs[0].signature == "class User(Base)"


def test_find_definition_by_bare_name_returns_all_matches():
    defs = find_definition(_auth_graph(), "save")
    assert {d.id for d in defs} == {"auth.User.save", "auth.Session.save"}


def test_find_definition_missing_is_empty():
    assert find_definition(_auth_graph(), "nope") == []


def test_outline_is_source_ordered_with_depth():
    entries = outline(_auth_graph(), "auth")
    assert (entries[0].id, entries[0].depth) == ("auth", 0)  # module first
    depth = {e.id: e.depth for e in entries}
    assert depth["auth.authenticate"] == 1
    assert depth["auth.User"] == 1
    assert depth["auth.User.save"] == 2
    order = [e.id for e in entries]
    assert order.index("auth.authenticate") < order.index("auth.User") < order.index("auth.Session")


def test_lookup_symbol_known_in_tree_has_no_suggestions():
    lk = lookup_symbol(_auth_graph(), "auth.User.save")
    assert lk.known is True and lk.suggestions == []


def test_lookup_symbol_unknown_suggests_by_exact_bare_name():
    # a right leaf name under a wrong/absent qualifier -> suggest the qualified id(s), best signal
    lk = lookup_symbol(_auth_graph(), "Session.save")
    assert lk.known is False
    assert set(lk.suggestions) == {"auth.User.save", "auth.Session.save"}


def test_lookup_symbol_bare_name_is_unknown_but_suggests_the_qualified_id():
    lk = lookup_symbol(_auth_graph(), "authenticate")
    assert lk.known is False and lk.suggestions == ["auth.authenticate"]


def test_lookup_symbol_typo_suggests_by_fuzzy_bare_name():
    # no exact bare-name match for the typo -> fall back to a fuzzy match on the leaf name
    lk = lookup_symbol(_auth_graph(), "auth.User.saev")
    assert lk.known is False
    assert "auth.User.save" in lk.suggestions


def test_lookup_symbol_treats_an_external_as_known():
    graph = _auth_graph()
    graph.add_external(Symbol("os.getcwd", "getcwd", SymbolKind.EXTERNAL, Span("<external>", 0)))
    lk = lookup_symbol(graph, "os.getcwd")
    assert lk.known is True and lk.suggestions == []  # a valid find_references target


def test_lookup_symbol_wholly_unknown_may_have_no_suggestions():
    lk = lookup_symbol(_auth_graph(), "zzz.qqq.wxyz")
    assert lk.known is False  # nothing close -> suggestions may legitimately be empty
    assert "auth.User" not in lk.suggestions


def test_outline_excludes_a_submodule_sharing_the_id_prefix():
    # `pkg` and `pkg.sub` are separate module roots (parent=None); outlining
    # `pkg` must return only pkg's own members, not the id-prefix-matching submodule.
    graph = Graph()
    graph.add_symbol(_sym("pkg", "pkg", SymbolKind.MODULE, 1))
    graph.add_symbol(_sym("pkg.top", "top", SymbolKind.FUNCTION, 2, parent="pkg"))
    graph.add_symbol(_sym("pkg.sub", "sub", SymbolKind.MODULE, 1))
    graph.add_symbol(_sym("pkg.sub.inner", "inner", SymbolKind.FUNCTION, 2, parent="pkg.sub"))

    ids = [e.id for e in outline(graph, "pkg")]
    assert ids == ["pkg", "pkg.top"]  # pkg.sub and pkg.sub.inner excluded
