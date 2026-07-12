"""Type resolvers — value-typed reference resolution behind the Python backend.

The first, always-safe value resolver: ``self.m()`` -> the enclosing class's member
``m`` (or an in-tree base's). It needs no type-name resolution — ``self``'s type is
the enclosing class, reached structurally via ``parent``/member adjacency, never by
parsing the dotted id. It emits a single MEDIUM (``possible``) edge: the statically
named member is real, but a subclass may override it at runtime, so the edge is
honestly possible, not definite — the payoff of "report, don't rule".

Plain functions, not a Resolver protocol/pipeline: with one value resolver there is
no abstraction to validate yet. The annotation/inference/stub resolvers land later;
the shared shape (a member lookup) is factored only when the second one proves it.

Known limitation: a ``@staticmethod`` whose first parameter is literally named ``self``
is indistinguishable from an instance method here (``symbols.py`` records no decorators),
so it can emit a spurious edge. Rare, and hedged by the MEDIUM (possible) tier; a
decorator-aware fix is deferred to when ``symbols.py`` tracks staticmethod-ness.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex


def resolve_self(files: Sequence[FileIndex], edges: Sequence[Edge]) -> list[Edge]:
    """Bind ``self.<attr>()`` refs to the enclosing class's member (own, then in-tree bases).

    ``edges`` is the syntactic pass's output — its INHERITS edges give the in-tree base
    classes, so this must run *after* syntactic binding. Only ``local_root`` refs (value
    receivers) are considered; a receiver other than ``self``, a chained receiver
    (``self.repo.save``), or a call outside an instance method yields no edge.
    """
    by_id: dict[SymbolId, Symbol] = {sym.id: sym for fi in files for sym in fi.symbols}
    members = _members(files)
    bases = _bases(edges, by_id)

    out: list[Edge] = []
    for fi in files:
        for ref in fi.refs:
            if not ref.local_root:
                continue
            root, _, attr = ref.name.partition(".")
            if root != "self" or not attr or "." in attr:
                continue  # only `self.<attr>` with a single attribute this increment
            method = by_id.get(ref.src)
            if method is None or method.kind is not SymbolKind.METHOD:
                continue  # not inside a method (nested function, module scope) -> no instance
            cls = by_id.get(method.parent) if method.parent else None
            if cls is None or cls.kind is not SymbolKind.CLASS:
                continue
            target = _member_lookup(cls.id, attr, members, bases)
            if target is not None:
                out.append(Edge(ref.src, target, ref.kind, Resolution.inferred(), ref.at))
    return out


# A value CALL must resolve to something callable — a method, a nested function, or a
# class (instantiation) — so data attributes (class-level VARIABLE) are excluded: this
# keeps `self.count()` from forging a call to a variable, and keeps a class var from
# masking a same-named method.
_CALLABLE = frozenset({SymbolKind.METHOD, SymbolKind.FUNCTION, SymbolKind.CLASS})


def _members(files: Sequence[FileIndex]) -> dict[SymbolId, dict[str, SymbolId]]:
    """parent id -> {member name -> callable member id}, first-def wins (mirrors module_defs)."""
    members: dict[SymbolId, dict[str, SymbolId]] = {}
    for fi in files:
        for sym in fi.symbols:
            if sym.parent is not None and sym.kind in _CALLABLE:
                members.setdefault(sym.parent, {}).setdefault(sym.name, sym.id)
    return members


def _bases(edges: Sequence[Edge], by_id: dict[SymbolId, Symbol]) -> dict[SymbolId, list[SymbolId]]:
    """class id -> its in-tree base class ids, in declaration order (from INHERITS edges).

    External bases carry no in-tree members, so they are excluded (their dst is not a
    known symbol), which truncates the chain honestly rather than guessing.
    """
    bases: dict[SymbolId, list[SymbolId]] = {}
    for e in edges:
        if e.kind is EdgeKind.INHERITS and e.dst in by_id:
            bases.setdefault(e.src, []).append(e.dst)
    return bases


def _member_lookup(
    class_id: SymbolId,
    attr: str,
    members: dict[SymbolId, dict[str, SymbolId]],
    bases: dict[SymbolId, list[SymbolId]],
) -> SymbolId | None:
    """``attr`` on ``class_id``: the class's own member wins; otherwise the attribute must
    resolve to exactly ONE member across its in-tree bases. A class that defines ``attr``
    shadows its own bases, so a normal override chain resolves to the nearest definition;
    but if two bases on different branches define it, the real target is MRO-dependent, so
    we emit nothing rather than guess a possibly-wrong one (report, don't rule). A cycle
    guard makes the base walk total.
    """
    own = members.get(class_id, {}).get(attr)
    if own is not None:
        return own
    found: set[SymbolId] = set()
    seen: set[SymbolId] = {class_id}
    stack = list(bases.get(class_id, ()))
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        hit = members.get(cid, {}).get(attr)
        if hit is not None:
            found.add(hit)  # this class defines it -> shadows its own bases; stop this branch
        else:
            stack.extend(bases.get(cid, ()))
    return next(iter(found)) if len(found) == 1 else None
