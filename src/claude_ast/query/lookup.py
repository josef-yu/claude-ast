"""Symbol-lookup queries — pure functions over a Graph.

These are the type-free (regime 1) queries: they read only symbols, so they're
deterministic and always high-confidence — no tiering needed. They depend on the
model alone, never on ``ast``, which keeps them trivially testable against a
hand-built Graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Graph, Span, SymbolId


@dataclass(slots=True)
class Definition:
    """A place a name is defined."""

    id: SymbolId
    kind: str
    span: Span
    signature: str | None


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
    syntactic and certain, so results are not tiered.
    """
    exact = graph.symbol(name)
    matches = [exact] if exact is not None else graph.by_name(name)
    return [Definition(s.id, s.kind.value, s.span, s.signature) for s in matches]


def outline(graph: Graph, module: SymbolId) -> list[OutlineEntry]:
    """A module's symbols, in source order, with nesting depth for indentation."""
    prefix = f"{module}."
    members = [s for s in graph.symbols() if s.id == module or s.id.startswith(prefix)]
    members.sort(key=lambda s: (s.span.file, s.span.line))
    base = module.count(".")
    return [
        OutlineEntry(
            id=s.id,
            name=s.name,
            kind=s.kind.value,
            signature=s.signature,
            doc=s.doc,
            depth=max(s.id.count(".") - base, 0),
        )
        for s in members
    ]
