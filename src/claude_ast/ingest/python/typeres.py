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
from dataclasses import dataclass

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex, RawRef
from .binding import bind, external_symbol, resolve_external_type_name, resolve_type_name
from .stubs import StubProvider


@dataclass(frozen=True, slots=True)
class ResolveIndex:
    """The lookup tables shared, unchanged, across every resolve pass — built once here.

    The syntactic main loop and the three value passes (value-types, in-tree chains, call-site
    observations) each used to rebuild these independently from ``files``/``edges``; nothing
    invalidates them between passes, so they collapse to one construction. ``bases`` is the one
    table derived from edges (INHERITS): it is filled after the syntactic loop, which is safe
    because no value pass emits an INHERITS edge, so it is already complete there.
    """

    by_id: dict[SymbolId, Symbol]
    all_ids: set[SymbolId]
    internal_roots: set[str]
    reexports: dict[str, dict[str, str]]
    module_defs: dict[str, dict[str, str]]  # module id -> {top-level name -> symbol id}
    members: dict[SymbolId, dict[str, SymbolId]]  # callable members — for a CALL receiver
    read_members: dict[SymbolId, dict[str, SymbolId]]  # + data attributes — for a READ receiver
    methods_by_name: dict[str, list[SymbolId]]  # callable candidates — the CALL name-match
    attrs_by_name: dict[str, list[SymbolId]]  # any readable class member — the READ name-match
    # A symbol's type is context-specific, so it lives in two disjoint maps, never one: ``returns``
    # is the class you get by *calling* a function/method (its return); ``attr_types`` the class you
    # get by *reading* a data attribute (its declared type). Calling a variable (``make()`` where
    # ``make: Service``) invokes ``__call__``, NOT the read-type — so a CALL consumer must read
    # ``returns`` and a data-attribute chain ``attr_types``; conflating them forges wrong edges.
    returns: dict[SymbolId, tuple[SymbolId, bool]]  # func/method id -> (return class, inferred?)
    attr_types: dict[SymbolId, tuple[SymbolId, bool]]  # data-attr id -> (declared class, inferred?)
    bases: dict[SymbolId, list[SymbolId]]


def module_defs_map(files: Sequence[FileIndex]) -> dict[str, dict[str, str]]:
    """module id -> its top-level ``{name -> symbol id}`` (first-def wins), for every file.

    A module's top-level defs, keyed for O(1) reuse. First definition wins when a name has
    same-qualname siblings (``#N``), so binding is deterministic regardless of symbol order —
    the same ``setdefault`` rule every pass applied when it rebuilt this inline.
    """
    out: dict[str, dict[str, str]] = {}
    for fi in files:
        defs: dict[str, str] = {}
        for s in fi.symbols:
            if s.parent == fi.module:
                defs.setdefault(s.name, s.id)
        out[fi.module] = defs
    return out


def resolution_index(
    files: Sequence[FileIndex],
    edges: Sequence[Edge],
    *,
    by_id: dict[SymbolId, Symbol],
    all_ids: set[SymbolId],
    internal_roots: set[str],
    reexports: dict[str, dict[str, str]],
    module_defs: dict[str, dict[str, str]],
) -> ResolveIndex:
    """Assemble the shared ``ResolveIndex`` once from the file-derived tables the main loop
    already built plus the tables derived here (members, methods-by-name, returns, bases)."""
    returns, attr_types = _typed_symbol_maps(files, module_defs, reexports, by_id, all_ids)
    return ResolveIndex(
        by_id=by_id,
        all_ids=all_ids,
        internal_roots=internal_roots,
        reexports=reexports,
        module_defs=module_defs,
        members=_members(files, _CALLABLE),
        read_members=_members(files, _READABLE),
        methods_by_name=_methods_by_name(files),
        attrs_by_name=_attrs_by_name(files, by_id),
        returns=returns,
        attr_types=attr_types,
        bases=_bases(edges, by_id),
    )


def resolve_value_types(
    files: Sequence[FileIndex],
    ctx: ResolveIndex,
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

    Handles both CALL refs and bare-attribute-READ (REFERENCE) refs through the *same* ladder,
    with one difference: a read can land on a data attribute, so it looks members up in the wider
    ``read_members`` / ``attrs_by_name`` maps (methods + variables + nested classes) and accepts any
    stub member kind, where a call is restricted to callables. The edge carries ``ref.kind``, so a
    read yields a REFERENCE edge and a call a CALL edge, both at the tier the rung dictates.

    The in-tree typed cases share one member lookup (own member, then in-tree bases) and
    produce exactly one MEDIUM edge; when in-tree resolution declines because the type is
    external, the stub provider is consulted for member existence, minting a MEDIUM STUB
    edge to an EXTERNAL member node (returned alongside the edges). The heuristic is a last
    resort (a typed receiver whose member isn't found stays silent, never falling through).

    A **multi-member chain** (``self.a.b``, ``u.a.b()``) threads one hop per member: each member
    but the last is read as a value and advances to its declared type (via ``attr_types``) — but
    only through a DATA attribute (``a: T``), since a method accessed-not-called is a bound method,
    not its return type. The last member is the target. Runs after syntactic binding so the INHERITS
    edges the base walk needs are already in ``edges``; ``local_root`` refs only. Deferred: external
    and method/property intermediate hops, and chains with an intermediate call (self.a.get()).
    """
    out: list[Edge] = []
    externals: list[Symbol] = []  # stub member nodes minted for out-of-tree receiver types
    for fi in files:
        module_defs = ctx.module_defs[fi.module]
        for ref in fi.refs:
            if not ref.local_root:
                continue
            if ref.chain:
                # a value-rooted call-return chain (`self.get().run()`) — an intermediate call.
                out.extend(_resolve_call_return_chain(ref, ctx, module_defs, fi.imports))
            else:
                # a plain member reference (`self.a.b`, `u.attr()`) — no intermediate call.
                edges, exts = _resolve_receiver_ref(ref, ctx, module_defs, fi.imports, stubs)
                out.extend(edges)
                externals.extend(exts)
    return out, externals


def _resolve_call_return_chain(
    ref: RawRef, ctx: ResolveIndex, module_defs: dict[str, str], imports: dict[str, str]
) -> list[Edge]:
    """A value-rooted call-return chain (`self.get().run()`): resolve the receiver's class, look up
    the receiver member's return type, then thread the trailing members through their returns. Every
    hop is a *call*, so it looks up callable members and advances through ``returns`` (a called
    method's return type) — never ``attr_types``. A multi-member *receiver* (`self.a.get()`) is
    deferred."""
    r_root, _, r_member = ref.name.partition(".")
    if "." in r_member:
        return []  # multi-member receiver (self.a.get) -> deferred
    members, bases, returns = ctx.members, ctx.bases, ctx.returns
    if r_root == "self":
        cls = _self_class(ref.src, ctx.by_id)
    elif ref.receiver_type is not None:
        cls = resolve_type_name(
            ref.receiver_type, module_defs, imports, ctx.all_ids, ctx.reexports, ctx.by_id
        )
    else:
        cls = None
    # Source provenance: ANNOTATION only if every fact used was declared — a self/inferred receiver
    # or a body-inferred return hop makes it INFERENCE.
    inferred = r_root == "self" or ref.receiver_inferred
    recv = _member_lookup(cls, r_member, members, bases) if cls else None
    typ, hop_inferred = returns.get(recv, (None, False)) if recv else (None, False)
    inferred = inferred or hop_inferred
    for name in ref.chain[:-1]:
        hop = _member_lookup(typ, name, members, bases) if typ else None
        typ, hop_inferred = returns.get(hop, (None, False)) if hop else (None, False)
        inferred = inferred or hop_inferred
    target = _member_lookup(typ, ref.chain[-1], members, bases) if typ else None
    if target is None:
        return []
    res = Resolution.inferred() if inferred else Resolution.annotated()
    return [Edge(ref.src, target, ref.kind, res, ref.at)]


def _resolve_receiver_ref(
    ref: RawRef, ctx: ResolveIndex, module_defs: dict[str, str],
    imports: dict[str, str], stubs: StubProvider,
) -> tuple[list[Edge], list[Symbol]]:
    """A value receiver ``root.a.b…`` with no intermediate call. Resolve ``root`` to its in-tree
    class, then thread the member chain to a target (one MEDIUM edge). When ``root`` has no in-tree
    class, a *single* attribute still gets the last-resort rungs — an untyped receiver name-matches
    (heuristic, LOW), an external one consults stubs; a multi-member chain has no type to thread and
    declines. A READ (REFERENCE) may land on a data attribute, a CALL must be callable."""
    root, _, rest = ref.name.partition(".")
    if not rest:
        return [], []  # a bare local name (no attribute) — nothing to resolve
    chain = rest.split(".")  # `self.a.b` -> ["a", "b"]; `self.attr` -> ["attr"]
    is_read = ref.kind is EdgeKind.REFERENCE
    class_id, inferred = _receiver_class(ref, root, ctx, module_defs, imports)
    if class_id is None:
        # No in-tree receiver class. A chain (or a `self` outside a method) declines; a single
        # attribute falls to the last-resort rungs.
        if len(chain) > 1 or root == "self":
            return [], []
        if ref.receiver_type is None:
            return _heuristic_edges(ref, chain[0], is_read, ctx), []
        return _stub_edge(ref, chain[0], is_read, ctx, module_defs, imports, stubs)
    target, inferred = _thread_member_chain(class_id, chain, inferred, is_read, ctx)
    if target is None:
        return [], []
    res = Resolution.inferred() if inferred else Resolution.annotated()
    return [Edge(ref.src, target, ref.kind, res, ref.at)], []


def _receiver_class(
    ref: RawRef, root: str, ctx: ResolveIndex,
    module_defs: dict[str, str], imports: dict[str, str],
) -> tuple[SymbolId | None, bool]:
    """The in-tree CLASS a receiver ``root`` denotes, plus whether any fact used was inferred
    (INFERENCE vs ANNOTATION provenance). ``None`` class for an untyped/external/unresolved root, or
    a ``self`` outside a method — the caller picks the fallback. ``self`` is INFERENCE (its type is
    exact, but dispatch is open-world); an annotated receiver ANNOTATION unless constructed."""
    if root == "self":
        return _self_class(ref.src, ctx.by_id), True
    if ref.receiver_type is None:
        return None, False  # an untyped receiver
    class_id = resolve_type_name(
        ref.receiver_type, module_defs, imports, ctx.all_ids, ctx.reexports, ctx.by_id
    )
    if class_id is not None:
        return class_id, ref.receiver_inferred
    # the receiver may be a *call* whose return type is an in-tree class: `s = make(); s.inner()`
    # where `make() -> Service`. resolve_type_name declined (make is a function, not a class), so
    # follow its return type — a CALL, so ``returns`` (never a variable's read-type ``attr_types``).
    callee = bind(
        ref.receiver_type, module_defs, imports, ctx.all_ids, ctx.internal_roots, ctx.reexports
    )
    if callee is not None and not callee[1]:
        cls, ret_inferred = ctx.returns.get(callee[0], (None, False))
        if cls is not None:
            return cls, ref.receiver_inferred or ret_inferred
    return None, False


def _thread_member_chain(
    class_id: SymbolId, chain: list[str], inferred: bool, is_read: bool, ctx: ResolveIndex,
) -> tuple[SymbolId | None, bool]:
    """Thread a member chain from a receiver class to its target member id (+ the running INFERENCE
    flag). Every member but the last is *read* as a value, so it advances through ``attr_types`` —
    the declared type of a DATA attribute. A method/property accessed-not-called is absent from
    ``attr_types`` (it's a bound method, not its return type), so it declines rather than forging a
    wrong edge (in-tree property detection is the deferred fix). The last member is the target — a
    read may land on a data attribute, a call must be callable."""
    read_members, bases, attr_types = ctx.read_members, ctx.bases, ctx.attr_types
    typ: SymbolId | None = class_id
    for name in chain[:-1]:
        hop = _member_lookup(typ, name, read_members, bases) if typ is not None else None
        typ, hop_inferred = attr_types.get(hop, (None, False)) if hop is not None else (None, False)
        inferred = inferred or hop_inferred
        if typ is None:
            return None, inferred
    lookup_members = read_members if is_read else ctx.members
    target = _member_lookup(typ, chain[-1], lookup_members, bases) if typ is not None else None
    return target, inferred


def _heuristic_edges(ref: RawRef, attr: str, is_read: bool, ctx: ResolveIndex) -> list[Edge]:
    """Untyped receiver: a last-resort LOW name-match to every member named ``attr``, but only when
    the name is specific enough (<= cap) to report, not spam. A read matches any readable class
    member; a call only methods."""
    candidates = (ctx.attrs_by_name if is_read else ctx.methods_by_name).get(attr, ())
    if not 0 < len(candidates) <= _HEURISTIC_CAP:
        return []
    return [Edge(ref.src, t, ref.kind, Resolution.heuristic(), ref.at) for t in candidates]


def _stub_edge(
    ref: RawRef, attr: str, is_read: bool, ctx: ResolveIndex,
    module_defs: dict[str, str], imports: dict[str, str], stubs: StubProvider,
) -> tuple[list[Edge], list[Symbol]]:
    """External receiver type: consult the stub provider for ``attr`` and mint a MEDIUM STUB edge to
    an EXTERNAL member node. A call needs a callable member (`p.exists()`); a read accepts any
    member, including a property/data attribute (`p.name`) a call would decline."""
    if ref.receiver_type is None:
        return [], []
    ext = resolve_external_type_name(
        ref.receiver_type, module_defs, imports, ctx.all_ids, ctx.internal_roots, ctx.reexports
    )
    member = stubs.type_member(ext, attr) if ext is not None else None
    if member is None or not (is_read or member[0] in _CALLABLE_STUB_KINDS):
        return [], []
    member_id = f"{ext}.{attr}"
    edge = Edge(ref.src, member_id, ref.kind, Resolution.stubbed(), ref.at)
    return [edge], [external_symbol(member_id)]


def resolve_intree_chains(
    files: Sequence[FileIndex],
    ctx: ResolveIndex,
) -> list[Edge]:
    """Call-return chains whose receiver returns an *in-tree* type: ``make().run()`` where
    ``make() -> Service`` -> ``Service.run`` (MEDIUM). The external counterpart lives in
    ``chains``; this one threads *in-tree* function return annotations through the same member
    lookup the value resolvers use. The edge's source is honest provenance: ANNOTATION when
    every return hop was declared, INFERENCE the moment any hop was body-inferred.

    Runs after syntactic binding, so INHERITS edges exist for the base walk. Only ``chain`` refs
    whose receiver binds to an in-tree function are handled — external receivers are resolved in
    ``chains`` during binding; a receiver or hop with no resolvable return type declines the chain.
    """
    all_ids, internal_roots, reexports = ctx.all_ids, ctx.internal_roots, ctx.reexports
    members, bases, returns = ctx.members, ctx.bases, ctx.returns

    out: list[Edge] = []
    for fi in files:
        module_defs = ctx.module_defs[fi.module]
        for ref in fi.refs:
            if not ref.chain:
                continue
            recv = bind(ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports)
            if recv is None or recv[1]:  # unresolved, or external (chains.py owns external)
                continue
            typ, inferred = returns.get(recv[0], (None, False))
            for name in ref.chain[:-1]:
                member = _member_lookup(typ, name, members, bases) if typ else None
                typ, hop_inferred = returns.get(member, (None, False)) if member else (None, False)
                inferred = inferred or hop_inferred
            target = _member_lookup(typ, ref.chain[-1], members, bases) if typ else None
            if target is not None:
                res = Resolution.inferred() if inferred else Resolution.annotated()
                out.append(Edge(ref.src, target, ref.kind, res, ref.at))
    return out


def _typed_symbol_maps(
    files: Sequence[FileIndex],
    module_defs: dict[str, dict[str, str]],
    reexports: dict[str, dict[str, str]],
    by_id: dict[SymbolId, Symbol],
    all_ids: set[SymbolId],
) -> tuple[dict[SymbolId, tuple[SymbolId, bool]], dict[SymbolId, tuple[SymbolId, bool]]]:
    """``(returns, attr_types)`` — the two disjoint symbol-id -> ``(in-tree CLASS id, inferred?)``
    maps a chain threads through. Both read the same ``return_type`` field but are keyed by kind,
    because the type it denotes is context-specific and must NOT be conflated:

    - ``returns`` — a FUNCTION/METHOD's *return* type: the class you get by **calling** it.
    - ``attr_types`` — a VARIABLE's *declared* type (``svc: Service``): the class you get by
      **reading** it. Calling a variable invokes ``__call__`` (unmodeled) — a different type.

    A call-return chain (``make().run()``) reads ``returns``; a data-attribute chain (``self.a.b``)
    reads ``attr_types``. Keeping them separate is what stops a called class-typed variable
    (``make: Service = Service(); make().run()``) from forging an edge to ``Service.run``.

    The flag is the type's provenance (declared annotation vs body-inferred), carried so a chain
    edge threaded through it can be stamped with an honest source — ANNOTATION only when every
    fact used was declared.
    """
    returns: dict[SymbolId, tuple[SymbolId, bool]] = {}
    attr_types: dict[SymbolId, tuple[SymbolId, bool]] = {}
    for fi in files:
        defs = module_defs[fi.module]
        for sym in fi.symbols:
            if sym.return_type is None:
                continue
            cls = resolve_type_name(sym.return_type, defs, fi.imports, all_ids, reexports, by_id)
            if cls is None:
                continue
            target = attr_types if sym.kind is SymbolKind.VARIABLE else returns
            target[sym.id] = (cls, sym.return_type_inferred)
    return returns, attr_types


# Stub member kinds that resolve as a CALL — a value's callable members. A READ accepts any kind
# (a property/data attribute too), so this gates the call path only.
_CALLABLE_STUB_KINDS = ("method", "func", "class")

# A name defined as a method on more than this many classes is too ambiguous for the
# heuristic to be a useful report, so it emits nothing rather than a wall of candidates.
_HEURISTIC_CAP = 8


def _methods_by_name(files: Sequence[FileIndex]) -> dict[str, list[SymbolId]]:
    """method name -> the ids of every method with that name, in deterministic order — the CALL
    heuristic's candidate pool (a value call dispatches to a method)."""
    by_name: dict[str, list[SymbolId]] = {}
    for fi in files:
        for sym in fi.symbols:
            if sym.kind is SymbolKind.METHOD:
                by_name.setdefault(sym.name, []).append(sym.id)
    return by_name


def _attrs_by_name(
    files: Sequence[FileIndex], by_id: dict[SymbolId, Symbol]
) -> dict[str, list[SymbolId]]:
    """attribute name -> the ids of every readable *class member* with that name, in deterministic
    order — the READ heuristic's candidate pool. A bare ``obj.attr`` on an untyped receiver could
    name any instance member: a method, a class-level variable, or a nested class. Module-level
    defs are excluded (they aren't reachable as ``instance.attr``), so this is the read counterpart
    of ``methods_by_name`` widened past callables, not a flat name index."""
    by_name: dict[str, list[SymbolId]] = {}
    for fi in files:
        for sym in fi.symbols:
            if sym.kind not in _READABLE or sym.parent is None:
                continue
            parent = by_id.get(sym.parent)
            if parent is not None and parent.kind is SymbolKind.CLASS:
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

# A bare attribute READ can land on any member, so its lookup adds the data attribute
# (class-level VARIABLE) the call map deliberately omits: `obj.count` (a variable) IS a
# valid read target, where `obj.count()` (a call) is not.
_READABLE = _CALLABLE | {SymbolKind.VARIABLE}


def _members(
    files: Sequence[FileIndex], kinds: frozenset[SymbolKind]
) -> dict[SymbolId, dict[str, SymbolId]]:
    """parent id -> {member name -> member id} for members whose kind is in ``kinds``, first-def
    wins (mirrors module_defs). ``_CALLABLE`` builds the CALL map; ``_READABLE`` the wider READ
    map. Only ever queried with a CLASS parent, so module-level entries are harmless ballast."""
    members: dict[SymbolId, dict[str, SymbolId]] = {}
    for fi in files:
        for sym in fi.symbols:
            if sym.parent is not None and sym.kind in kinds:
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
