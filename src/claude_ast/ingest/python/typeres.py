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
from .binding import bind, external_symbol, resolve_external_type_name, resolve_type_name
from .stubs import StubProvider


def resolve_value_types(
    files: Sequence[FileIndex],
    edges: Sequence[Edge],
    reexports: dict[str, dict[str, str]],
    internal_roots: set[str],
    stubs: StubProvider,
) -> tuple[list[Edge], list[Symbol]]:
    """Resolve value-typed receiver calls to ``possible`` edges — a confidence ladder:

    - ``self.m()`` -> the enclosing class's member (INFERENCE); ``self`` *is* that class.
    - ``u.m()`` with ``u: User`` -> ``User.m`` (ANNOTATION); the parameter's declared type.
    - ``x.m()`` after ``x = User()`` -> ``User.m`` (INFERENCE); the constructed type.
    - ``p.m()`` with ``p: Path`` (an *external* type) -> ``pathlib.Path.m`` if the stub
      provider knows the member (STUB); the external counterpart of the annotation case.
    - ``obj.m()`` with ``obj`` untyped -> a LOW name match to every ``*.m`` method
      (HEURISTIC), capped so an over-common name yields no edge rather than noise.

    The in-tree typed cases share one member lookup (own member, then in-tree bases) and
    produce exactly one MEDIUM edge; when in-tree resolution declines because the type is
    external, the stub provider is consulted for member existence, minting a MEDIUM STUB
    edge to an EXTERNAL member node (returned alongside the edges). The heuristic is a last
    resort (a typed receiver whose member isn't found stays silent, never falling through).
    Runs after syntactic binding so the INHERITS edges the base walk needs are already in
    ``edges``; ``local_root`` refs only, single attribute (chained ``a.b.c`` deferred).
    """
    by_id: dict[SymbolId, Symbol] = {sym.id: sym for fi in files for sym in fi.symbols}
    all_ids = set(by_id)
    members = _members(files)
    bases = _bases(edges, by_id)
    methods_by_name = _methods_by_name(files)
    returns = _return_types(files, reexports, by_id, all_ids)

    out: list[Edge] = []
    externals: list[Symbol] = []  # stub member nodes minted for out-of-tree receiver types
    for fi in files:
        module_defs: dict[str, str] = {}
        for s in fi.symbols:
            if s.parent == fi.module:
                module_defs.setdefault(s.name, s.id)
        for ref in fi.refs:
            if ref.chain and ref.local_root:
                # value-rooted call-return chain (`self.get().run()`): resolve the receiver's
                # class, look up the receiver member's return type, then thread the chain.
                r_root, _, r_member = ref.name.partition(".")
                if "." in r_member:
                    continue  # multi-member receiver (self.a.get) -> deferred
                cls = (
                    _self_class(ref.src, by_id) if r_root == "self"
                    else resolve_type_name(
                        ref.receiver_type, module_defs, fi.imports, all_ids, reexports, by_id
                    ) if ref.receiver_type is not None
                    else None
                )
                recv = _member_lookup(cls, r_member, members, bases) if cls else None
                typ: SymbolId | None = returns.get(recv) if recv else None
                for name in ref.chain[:-1]:
                    hop = _member_lookup(typ, name, members, bases) if typ else None
                    typ = returns.get(hop) if hop else None
                target = _member_lookup(typ, ref.chain[-1], members, bases) if typ else None
                if target is not None:
                    out.append(Edge(ref.src, target, ref.kind, Resolution.annotated(), ref.at))
                continue
            if not ref.local_root:
                continue
            root, _, attr = ref.name.partition(".")
            if not attr or "." in attr:
                continue  # single attribute only
            if root == "self":
                class_id = _self_class(ref.src, by_id)
                resolution = Resolution.inferred()
            elif ref.receiver_type is not None:
                class_id = resolve_type_name(
                    ref.receiver_type, module_defs, fi.imports, all_ids, reexports, by_id
                )
                if class_id is None:
                    # the receiver may be a *call* whose return type is an in-tree class:
                    # `s = make(); s.inner()` where `make() -> Service`. resolve_type_name declined
                    # because `make` is a function, not a class — so follow its return annotation.
                    callee = bind(
                        ref.receiver_type, module_defs, fi.imports,
                        all_ids, internal_roots, reexports,
                    )
                    if callee is not None and not callee[1]:
                        class_id = returns.get(callee[0])
                if class_id is None:
                    # external receiver type -> consult stubs. Only a CALLABLE member resolves as
                    # a call (`p.exists()`); a property/data attribute (`p.name()`) is not callable.
                    ext = resolve_external_type_name(
                        ref.receiver_type, module_defs, fi.imports,
                        all_ids, internal_roots, reexports,
                    )
                    member = stubs.type_member(ext, attr) if ext is not None else None
                    if member is not None and member[0] in ("method", "func", "class"):
                        member_id = f"{ext}.{attr}"
                        externals.append(external_symbol(member_id))
                        out.append(Edge(ref.src, member_id, ref.kind, Resolution.stubbed(), ref.at))
                    continue
                resolution = (
                    Resolution.inferred() if ref.receiver_inferred else Resolution.annotated()
                )
            else:
                # untyped receiver: last-resort name match, one LOW edge per candidate,
                # but only when the name is specific enough (<= cap) to report, not spam.
                candidates = methods_by_name.get(attr, ())
                if 0 < len(candidates) <= _HEURISTIC_CAP:
                    for target in candidates:
                        out.append(Edge(ref.src, target, ref.kind, Resolution.heuristic(), ref.at))
                continue
            if class_id is None:
                continue
            target = _member_lookup(class_id, attr, members, bases)
            if target is not None:
                out.append(Edge(ref.src, target, ref.kind, resolution, ref.at))
    return out, externals


def resolve_intree_chains(
    files: Sequence[FileIndex],
    edges: Sequence[Edge],
    reexports: dict[str, dict[str, str]],
    internal_roots: set[str],
) -> list[Edge]:
    """Call-return chains whose receiver returns an *in-tree* type: ``make().run()`` where
    ``make() -> Service`` -> ``Service.run`` (MEDIUM). The external counterpart lives in
    ``chains``; this one threads *in-tree* function return annotations through the same member
    lookup the value resolvers use.

    Runs after syntactic binding, so INHERITS edges exist for the base walk. Only ``chain`` refs
    whose receiver binds to an in-tree function are handled — external receivers are resolved in
    ``chains`` during binding; a receiver or hop with no resolvable return type declines the chain.
    """
    by_id: dict[SymbolId, Symbol] = {sym.id: sym for fi in files for sym in fi.symbols}
    all_ids = set(by_id)
    members = _members(files)
    bases = _bases(edges, by_id)
    returns = _return_types(files, reexports, by_id, all_ids)

    out: list[Edge] = []
    for fi in files:
        module_defs: dict[str, str] = {}
        for s in fi.symbols:
            if s.parent == fi.module:
                module_defs.setdefault(s.name, s.id)
        for ref in fi.refs:
            if not ref.chain:
                continue
            recv = bind(ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports)
            if recv is None or recv[1]:  # unresolved, or external (chains.py owns external)
                continue
            typ: SymbolId | None = returns.get(recv[0])
            for name in ref.chain[:-1]:
                member = _member_lookup(typ, name, members, bases) if typ else None
                typ = returns.get(member) if member else None
            target = _member_lookup(typ, ref.chain[-1], members, bases) if typ else None
            if target is not None:
                out.append(Edge(ref.src, target, ref.kind, Resolution.annotated(), ref.at))
    return out


def _return_types(
    files: Sequence[FileIndex],
    reexports: dict[str, dict[str, str]],
    by_id: dict[SymbolId, Symbol],
    all_ids: set[SymbolId],
) -> dict[SymbolId, SymbolId]:
    """function/method id -> the in-tree CLASS id its return annotation names, when resolvable."""
    returns: dict[SymbolId, SymbolId] = {}
    for fi in files:
        module_defs: dict[str, str] = {}
        for s in fi.symbols:
            if s.parent == fi.module:
                module_defs.setdefault(s.name, s.id)
        for sym in fi.symbols:
            if sym.return_type is None:
                continue
            cls = resolve_type_name(
                sym.return_type, module_defs, fi.imports, all_ids, reexports, by_id
            )
            if cls is not None:
                returns[sym.id] = cls
    return returns


# A name defined as a method on more than this many classes is too ambiguous for the
# heuristic to be a useful report, so it emits nothing rather than a wall of candidates.
_HEURISTIC_CAP = 8


def _methods_by_name(files: Sequence[FileIndex]) -> dict[str, list[SymbolId]]:
    """method name -> the ids of every method with that name, in deterministic order."""
    by_name: dict[str, list[SymbolId]] = {}
    for fi in files:
        for sym in fi.symbols:
            if sym.kind is SymbolKind.METHOD:
                by_name.setdefault(sym.name, []).append(sym.id)
    return by_name


def _self_class(src: SymbolId, by_id: dict[SymbolId, Symbol]) -> SymbolId | None:
    """The class ``self`` denotes: the class enclosing the method that made the ref, or
    None when the ref isn't inside an instance method (nested function, module scope)."""
    method = by_id.get(src)
    if method is None or method.kind is not SymbolKind.METHOD:
        return None
    cls = by_id.get(method.parent) if method.parent else None
    return cls.id if cls is not None and cls.kind is SymbolKind.CLASS else None


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
    shadows its own bases, so a normal (single-inheritance) override chain resolves to the
    nearest definition. If two in-tree bases on *different* branches define it, we DECLINE —
    this does not compute the C3 MRO (which would pick one deterministically), so we emit
    nothing rather than guess. That is an honest miss, never a wrong edge; computing the real
    MRO is a future refinement. A cycle guard makes the base walk total.
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
