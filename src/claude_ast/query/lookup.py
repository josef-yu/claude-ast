"""Symbol-lookup queries — pure functions over a Graph.

These are the type-free (regime 1) queries: they read only symbols, so they're
deterministic and always high-confidence — no tiering needed. They depend on the
model alone, never on ``ast``, which keeps them trivially testable against a
hand-built Graph.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from ..model import Graph, Span, Symbol, SymbolId, SymbolKind


@dataclass(slots=True)
class Definition:
    """A place a name is defined."""

    id: SymbolId
    kind: str
    span: Span
    signature: str | None


@dataclass(slots=True)
class SymbolLookup:
    """Whether a query id names a *known* symbol, plus near-miss ids when it doesn't.

    Relationship queries (callers/dependencies/references/importers) take an id as input and return
    an empty list both when the id is genuinely unused AND when it doesn't exist at all — two very
    different answers. This resolves the ambiguity so a caller can say "no such symbol; did you
    mean…?" instead of presenting a mistyped id as a real, empty result.
    """

    query: SymbolId
    known: bool
    suggestions: list[SymbolId]  # nearest known ids when unknown; empty when known


def lookup_symbol(graph: Graph, query: SymbolId) -> SymbolLookup:
    """Resolve a query id to known/unknown. *Known* means an in-tree symbol OR an external
    (library/stdlib) edge sink — both are valid relationship targets (``find_references`` works on
    ``os.path.join``). An unknown id carries near-miss suggestions; a known one carries none."""
    if graph.symbol(query) is not None:
        return SymbolLookup(query, True, [])
    return SymbolLookup(query, False, _near_misses(graph, query))


def _near_misses(graph: Graph, query: SymbolId, limit: int = 5) -> list[SymbolId]:
    """The known ids closest to an unknown ``query``, best first — two cheap signals that catch the
    common mistakes. (1) Exact **bare-name** match: the leaf name is right but the qualifier wrong
    or missing (``reach`` / ``Hub.reach`` for ``m.Hub.reach``) — the frequent case, so it wins. (2)
    Else a **fuzzy** bare-name match: a typo in the leaf (``reachh`` for ``reach``). In-tree symbols
    only (an external id isn't something you 'meant' to navigate to). Runs only on the miss path."""
    bare = query.rpartition(".")[2]
    exact = [s.id for s in graph.by_name(bare)]
    if exact:
        return exact[:limit]
    names = {s.name for s in graph.symbols()}
    close = difflib.get_close_matches(bare, names, n=limit, cutoff=0.7)
    return [s.id for name in close for s in graph.by_name(name)][:limit]


@dataclass(slots=True)
class OutlineEntry:
    """One line of a module outline. ``depth`` is nesting for indentation."""

    id: SymbolId
    name: str
    kind: str
    signature: str | None
    doc: str | None
    depth: int


def find_definition(graph: Graph, name: str) -> list[Definition]:
    """Resolve a name to its definition(s).

    A fully-qualified id (``auth.models.User``) matches exactly; a bare name
    (``User``) returns every symbol with that short name. Definitions are
    syntactic and certain, so results are not tiered. External (library/stdlib)
    nodes are excluded — they have no in-tree definition to point at.
    """
    exact = graph.symbol(name)
    if exact is not None and not graph.is_external(exact.id):
        matches = [exact]
    else:
        matches = graph.by_name(name)  # bare-name lookup; by_name never holds externals
    return [Definition(s.id, s.kind.value, s.span, s.signature) for s in matches]


def outline(
    graph: Graph, module: SymbolId, focus: SymbolId | None = None
) -> list[OutlineEntry]:
    """A module's symbols with nesting depth for indentation (own members in source order,
    submodule leaves in file-discovery order) — **shallow by
    default, revealing the area around a focus on demand**.

    A module's own definitions are shown in full (a class's methods nest one deeper). A child
    *sub*module is normally a **collapsed leaf** — one line naming it, a table-of-contents entry —
    so ``outline(pkg)`` stays a readable overview even for a large package instead of dumping its
    whole subtree.

    Given a ``focus`` symbol somewhere under ``module``, the single branch on the path to it is
    **expanded** to its whole submodule, revealing the focus's structural neighbourhood (its
    siblings, its own members) while every other submodule stays collapsed — progressive
    disclosure: shallow context everywhere, detail where you're working. A ``focus`` that is not a
    symbol under ``module`` (a typo, or an out-of-subtree id) simply yields the shallow view.
    """
    root = graph.symbol(module)
    if root is None:
        return []
    reveal = _reveal_spine(graph, focus, module) if focus is not None else frozenset()
    entries: list[OutlineEntry] = []

    def walk(sym: Symbol, depth: int) -> None:
        entries.append(
            OutlineEntry(sym.id, sym.name, sym.kind.value, sym.signature, sym.doc, depth)
        )
        for child in graph.children(sym.id):
            if child.kind is SymbolKind.MODULE and child.id not in reveal:
                # a submodule off the focus path — a collapsed table-of-contents leaf, not descended
                entries.append(
                    OutlineEntry(
                        child.id, child.name, child.kind.value,
                        child.signature, child.doc, depth + 1,
                    )
                )
            else:
                walk(child, depth + 1)  # own member, or the submodule on the path to the focus

    walk(root, 0)
    return entries


def _reveal_spine(graph: Graph, focus: SymbolId, module: SymbolId) -> frozenset[SymbolId]:
    """The ids from ``focus`` up to ``module`` (inclusive) — the branch ``outline`` expands. Empty
    when ``focus`` is not a symbol under ``module`` (an unknown or out-of-subtree id), so the caller
    degrades to the shallow view rather than erroring."""
    spine: set[SymbolId] = set()
    cur = graph.symbol(focus)
    while cur is not None:
        spine.add(cur.id)
        if cur.id == module:
            return frozenset(spine)
        cur = graph.symbol(cur.parent) if cur.parent is not None else None
    return frozenset()
