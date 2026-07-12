"""Type resolvers — value-typed reference resolution behind the Python backend.

Two value resolvers, sharing one member lookup:

- ``self.m()``  -> the enclosing class's member (``self``'s type is that class,
  reached structurally via ``parent``/member adjacency, never by parsing the id).
- ``u.m()`` with ``u: User`` -> ``User.m`` (the parameter's declared type, resolved
  through the file's imports/defs like any name).

Each emits a single MEDIUM (``possible``) edge: the statically named member is real,
but a subclass may override it at runtime, so the edge is honestly possible, not
definite — the payoff of "report, don't rule".

Plain functions sharing ``_member_lookup``, not a Resolver protocol/pipeline: the two
resolvers validate the shared *member lookup*, not a uniform resolver interface, so no
registry/pipeline is invented before the stub/inference resolvers prove one is needed.

Known limitation: a ``@staticmethod`` whose first parameter is literally named ``self``
is indistinguishable from an instance method here (``symbols.py`` records no decorators),
so it can emit a spurious edge. Rare, and hedged by the MEDIUM (possible) tier; a
decorator-aware fix is deferred to when ``symbols.py`` tracks staticmethod-ness.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex
from .binding import follow_reexports


def resolve_value_types(
    files: Sequence[FileIndex], edges: Sequence[Edge], reexports: dict[str, dict[str, str]]
) -> list[Edge]:
    """Resolve value-typed receiver calls to MEDIUM (``possible``) edges — the two ways a
    receiver's type is statically known:

    - ``self.m()`` -> the enclosing class's member (INFERENCE); ``self`` *is* that class.
    - ``u.m()`` with ``u: User`` -> ``User.m`` (ANNOTATION); the parameter's declared type.

    Both share one member lookup (own member, then in-tree bases). Runs after syntactic
    binding so the INHERITS edges the base walk needs are already in ``edges``. Only
    ``local_root`` refs are considered, single attribute only (chained ``a.b.c`` deferred).
    """
    by_id: dict[SymbolId, Symbol] = {sym.id: sym for fi in files for sym in fi.symbols}
    all_ids = set(by_id)
    members = _members(files)
    bases = _bases(edges, by_id)

    out: list[Edge] = []
    for fi in files:
        module_defs: dict[str, str] = {}
        for s in fi.symbols:
            if s.parent == fi.module:
                module_defs.setdefault(s.name, s.id)
        for ref in fi.refs:
            if not ref.local_root:
                continue
            root, _, attr = ref.name.partition(".")
            if not attr or "." in attr:
                continue  # single attribute only
            if root == "self":
                class_id = _self_class(ref.src, by_id)
                resolution = Resolution.inferred()
            elif ref.receiver_type is not None:
                class_id = _resolve_type_name(
                    ref.receiver_type, module_defs, fi.imports, all_ids, reexports, by_id
                )
                resolution = Resolution.annotated()
            else:
                continue  # a value receiver we can't type yet (unannotated non-self)
            if class_id is None:
                continue
            target = _member_lookup(class_id, attr, members, bases)
            if target is not None:
                out.append(Edge(ref.src, target, ref.kind, resolution, ref.at))
    return out


def _self_class(src: SymbolId, by_id: dict[SymbolId, Symbol]) -> SymbolId | None:
    """The class ``self`` denotes: the class enclosing the method that made the ref, or
    None when the ref isn't inside an instance method (nested function, module scope)."""
    method = by_id.get(src)
    if method is None or method.kind is not SymbolKind.METHOD:
        return None
    cls = by_id.get(method.parent) if method.parent else None
    return cls.id if cls is not None and cls.kind is SymbolKind.CLASS else None


def _resolve_type_name(
    name: str,
    module_defs: dict[str, str],
    imports: dict[str, str],
    all_ids: set[SymbolId],
    reexports: dict[str, dict[str, str]],
    by_id: dict[SymbolId, Symbol],
) -> SymbolId | None:
    """A type name in a file's scope -> its in-tree CLASS id, or None.

    Resolves a bare or dotted annotation name (``User``, ``models.User``) through the
    file's own definitions and imports — the same inputs syntactic binding uses,
    including package re-exports — and keeps it only if it lands on an in-tree class.
    An external or non-class target is None: the members of an unindexed class can't be
    looked up here.
    """
    target = module_defs.get(name) or imports.get(name)
    if target is None:
        root, _, rest = name.partition(".")
        if rest:
            base = module_defs.get(root) or imports.get(root)
            if base is not None:
                target = f"{base}.{rest}"
    if target is None:
        return None
    target = follow_reexports(target, all_ids, reexports)
    sym = by_id.get(target)
    return target if sym is not None and sym.kind is SymbolKind.CLASS else None


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
