"""Neutral query-logic tests — over a Graph built from model primitives.

Deliberately backend-agnostic: the Graph is constructed directly from ``Symbol``
objects, never via a language backend, so these prove the query logic works
regardless of how the index was populated. A backend's end-to-end query pass
(parse -> assemble -> query) is tested per backend, in ``tests/backends/``.
"""

from claude_ast.model import Graph, Span, Symbol, SymbolKind
from claude_ast.query import find_definition, outline


def _sym(sid, name, kind, line, signature=None, doc=None, parent=None):
    return Symbol(sid, name, kind, Span("m.py", line), signature=signature, doc=doc, parent=parent)


def _auth_graph() -> Graph:
    graph = Graph()
    for sym in (
        _sym("auth", "auth", SymbolKind.MODULE, 1, doc="Auth module."),
        _sym("auth.authenticate", "authenticate", SymbolKind.FUNCTION, 4,
             signature="def authenticate(email: str) -> bool"),
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
