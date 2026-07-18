"""Type resolvers — value-typed reference resolution behind the Python backend.

Two value resolvers, sharing one member lookup (``_member_lookup``):

- ``self.m()``  -> the enclosing class's member (``self``'s type is that class,
  reached structurally via ``parent``/member adjacency, never by parsing the id).
- ``u.m()`` with ``u: User`` -> ``User.m`` (the parameter's declared type, resolved
  through the file's imports/defs like any name).

Each emits a single MEDIUM (``possible``) edge: the statically named member is real,
but a subclass may override it at runtime, so the edge is honestly possible, not
definite — the payoff of "report, don't rule". The lookup tables these read are built
once in ``resolve_index`` (``ResolveIndex``); this module is only the resolution.

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

from ...model import Edge, EdgeKind, FlowKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex, RawRef
from .binding import bind, external_symbol, resolve_external_type_name, resolve_type_name
from .resolve_index import READABLE_DATA, ResolveIndex
from .stubs import StubProvider

# Stub member kinds that resolve as a CALL — a value's callable members. A READ accepts any kind
# (a property/data attribute too), so this gates the call path only.
_CALLABLE_STUB_KINDS = ("method", "func", "class")

# A name defined as a method on more than this many classes is too ambiguous for the
# heuristic to be a useful report, so it emits nothing rather than a wall of candidates.
_HEURISTIC_CAP = 8

# Members whose first parameter is the instance — so a ``self.x`` inside one resolves against the
# enclosing class. A METHOD or a PROPERTY getter qualifies; a ``@staticmethod`` (kind METHOD, but
# ``is_static``) does NOT — its ``self`` is just a misnamed parameter.
_SELF_BOUND = frozenset({SymbolKind.METHOD, SymbolKind.PROPERTY})


def _flowed(res: Resolution, flow: FlowKind) -> Resolution:
    """``res`` re-tagged with ``flow`` — a no-op for the common ``STABLE`` case (an ordinary,
    non-reassigned receiver), so only a reassignment-derived edge allocates a new resolution."""
    return res if flow is FlowKind.STABLE else res.with_flow(flow)


def resolve_value_types(
    files: Sequence[FileIndex],
    ctx: ResolveIndex,
    stubs: StubProvider,
) -> tuple[list[Edge], list[Symbol]]:
    """Resolve value-typed receiver calls to ``possible`` edges — a confidence ladder:

    - ``self.m()`` -> the enclosing class's member (INFERENCE); ``self`` *is* that class.
    - ``u.m()`` with ``u: User`` -> ``User.m`` (ANNOTATION); the parameter's declared type. A union
      (``u: User | Admin``) fans out to one edge per arm; ``User | None`` collapses to ``User``.
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
    not its return type. The last member is the target. A chain whose receiver ends in a call
    (``self.a.get().run()``) is handled too: the data-attribute prefix threads to the call's
    type, its return starts the trailing call chain. Runs after syntactic binding so the INHERITS
    edges the base walk needs are already in ``edges``; ``local_root`` refs only. Deferred: external
    intermediate hops, and a trailing hop that is a data attribute rather than a call.
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
    """A value-rooted call-return chain (`self.get().run()`, `self.a.get().run()`): resolve the
    receiver's class, thread the receiver's *data-attribute* prefix (``self.a`` — read via
    ``attr_types``) to the type its call member is on, take that member's return type, then
    thread the trailing members through their returns. Every trailing hop is a *call*, advancing
    through ``returns`` (a called method's return) — never ``attr_types``. A union receiver
    (`u: User | Admin`) threads each arm and unions the edges."""
    r_root, _, r_rest = ref.name.partition(".")
    r_members = r_rest.split(".") if r_rest else []
    if not r_members:
        return []  # the receiver is the bare root called (`foo().bar()`) — a value call, unmodeled
    seen: set[SymbolId] = set()
    primary = FlowKind.FLOW if ref.receiver_flow else FlowKind.STABLE
    out = _chain_arm_edges(
        ref, r_root, ref.receiver_types, primary, r_members, ctx, module_defs, imports, seen
    )
    if ref.receiver_may_types:  # union widening for a reassigned chain receiver
        out += _chain_arm_edges(
            ref, r_root, ref.receiver_may_types, FlowKind.MAY, r_members, ctx, module_defs, imports,
            seen,
        )
    return out


def _chain_arm_edges(
    ref: RawRef, r_root: str, type_names: tuple[str, ...], flow: FlowKind, r_members: list[str],
    ctx: ResolveIndex, module_defs: dict[str, str], imports: dict[str, str], seen: set[SymbolId],
) -> list[Edge]:
    """The call-return-chain edges for one set of receiver arms (``type_names``), tagged ``flow``.
    For each arm, threads the receiver data-attribute prefix (``r_members[:-1]`` via ``attr_types``)
    to a type, looks up the receiver call member (``r_members[-1]``) and takes its return, then
    threads the trailing members through their returns; dedups via ``seen``. Source provenance:
    ANNOTATION only if every fact used was declared — a self/inferred receiver or any body-inferred
    return/attribute hop makes it INFERENCE."""
    members, bases, returns = ctx.members, ctx.bases, ctx.returns
    out: list[Edge] = []
    for cls, arm_inferred in _receiver_classes(ref, r_root, type_names, ctx, module_defs, imports):
        recv_type, inferred = _thread_to_type(cls, r_members[:-1], ctx)
        inferred = arm_inferred or inferred
        recv = _member_lookup(recv_type, r_members[-1], members, bases) if recv_type else None
        typ, hop_inferred = returns.get(recv, (None, False)) if recv else (None, False)
        inferred = inferred or hop_inferred
        for name in ref.chain[:-1]:
            hop = _member_lookup(typ, name, members, bases) if typ else None
            typ, hop_inferred = returns.get(hop, (None, False)) if hop else (None, False)
            inferred = inferred or hop_inferred
        target = _member_lookup(typ, ref.chain[-1], members, bases) if typ else None
        if target is not None and target not in seen:
            seen.add(target)
            # A MAY (union-widening) arm is speculative -> INFERENCE, never ANNOTATION, even if it
            # threaded through declared returns (mirrors `_typed_arm_edges`).
            declared = not inferred and flow is not FlowKind.MAY
            res = Resolution.annotated() if declared else Resolution.inferred()
            out.append(Edge(ref.src, target, ref.kind, _flowed(res, flow), ref.at))
    return out


def _thread_to_type(
    class_id: SymbolId, members: list[str], ctx: ResolveIndex,
) -> tuple[SymbolId | None, bool]:
    """Thread a DATA-attribute member chain from a class to the in-tree type it reaches, advancing
    each hop through ``attr_types`` (its declared type). Returns ``(type, any-inferred)``, or
    ``(None, ...)`` when a hop is not a threadable data attribute (untyped / external / a method
    accessed-not-called). Empty ``members`` yields the class unchanged — the single-member-receiver
    case (``self.get()``), where there is no prefix to thread."""
    typ: SymbolId | None = class_id
    inferred = False
    for name in members:
        member = _member_lookup(typ, name, ctx.read_members, ctx.bases) if typ is not None else None
        if member is None:
            return None, inferred
        typ, hop_inferred = ctx.attr_types.get(member, (None, False))
        inferred = inferred or hop_inferred
    return typ, inferred


def _resolve_receiver_ref(
    ref: RawRef, ctx: ResolveIndex, module_defs: dict[str, str],
    imports: dict[str, str], stubs: StubProvider,
) -> tuple[list[Edge], list[Symbol]]:
    """A value receiver ``root.a.b…`` with no intermediate call. Resolve ``root`` to its in-tree
    class(es), thread the member chain on each to a target, and union the edges (one MEDIUM edge
    per distinct target — a union receiver ``u: User | Admin`` fans out to both arms). External
    arms of a *single* attribute additionally consult the stub provider. When ``root`` is untyped,
    a single attribute name-matches (heuristic, LOW) and a chain declines; a typed receiver that
    resolves no member stays silent. A READ may land on a data attribute; a CALL must be callable.
    """
    root, _, rest = ref.name.partition(".")
    if not rest:
        return [], []  # a bare local name (no attribute) — nothing to resolve
    chain = rest.split(".")  # `self.a.b` -> ["a", "b"]; `self.attr` -> ["attr"]
    is_read = ref.kind is EdgeKind.REFERENCE
    if not ref.receiver_types and not ref.receiver_may_types and root != "self":
        # Fully untyped receiver: a single attribute name-matches; a chain has no type to thread. A
        # reassigned local with an untyped *live* type but a nameable may-widening is NOT untyped —
        # it falls through so the widening resolves as MAY (union mode), not a spurious heuristic.
        if len(chain) > 1:
            return [], []
        return _heuristic_edges(ref, chain[0], is_read, ctx), []
    seen: set[SymbolId] = set()
    primary = FlowKind.FLOW if ref.receiver_flow else FlowKind.STABLE
    edges, externals, untyped_any = _typed_arm_edges(
        ref, root, ref.receiver_types, primary, chain, is_read, ctx, module_defs, imports, stubs,
        seen,
    )
    if ref.receiver_may_types:  # union widening — the other types a reassigned receiver takes
        may_edges, may_ext, _ = _typed_arm_edges(
            ref, root, ref.receiver_may_types, FlowKind.MAY, chain, is_read, ctx, module_defs,
            imports, stubs, seen,
        )
        edges += may_edges
        externals += may_ext
    if edges:
        return edges, externals
    if untyped_any:
        # every arm declined and one hit an intermediate data attribute of un-threadable type, so
        # the last member's receiver is untyped — name-match it (LOW), as a single-hop `obj.attr`.
        return _heuristic_edges(ref, chain[-1], is_read, ctx), []
    return [], []


def _typed_arm_edges(
    ref: RawRef, root: str, type_names: tuple[str, ...], flow: FlowKind, chain: list[str],
    is_read: bool, ctx: ResolveIndex, module_defs: dict[str, str], imports: dict[str, str],
    stubs: StubProvider, seen: set[SymbolId],
) -> tuple[list[Edge], list[Symbol], bool]:
    """The receiver edges for one set of arms (``type_names``), tagged ``flow`` and deduped via
    ``seen``. Threads each in-tree arm's member chain to a target; a single external attribute
    additionally consults the stub tables. A ``MAY`` (union widening) arm is always INFERENCE
    — a speculative alternative, not a declared fact. Returns ``(edges, externals, untyped_any)``,
    the last flagging that an intermediate data attribute was un-threadable (heuristic fallback)."""
    edges: list[Edge] = []
    externals: list[Symbol] = []
    untyped_any = False
    classes = _receiver_classes(ref, root, type_names, ctx, module_defs, imports)
    for class_id, inferred in classes:
        target, thr_inf, untyped = _thread_member_chain(class_id, chain, inferred, is_read, ctx)
        if target is not None and target not in seen:
            seen.add(target)
            declared = not thr_inf and flow is not FlowKind.MAY
            res = Resolution.annotated() if declared else Resolution.inferred()
            edges.append(Edge(ref.src, target, ref.kind, _flowed(res, flow), ref.at))
        untyped_any = untyped_any or untyped
    # External arms resolve a single attribute against the stub tables. An in-tree name is never a
    # stub target, so only an arm that did NOT resolve in-tree can be external — skip the lookup
    # entirely when every candidate type already bound to an in-tree class (the common case).
    if len(chain) == 1 and len(classes) < len(type_names):
        for member_id, ext in _stub_targets(chain[0], type_names, is_read, ctx, module_defs,
                                             imports, stubs):
            if member_id not in seen:
                seen.add(member_id)
                edges.append(Edge(ref.src, member_id, ref.kind, _flowed(Resolution.stubbed(), flow),
                                  ref.at))
                externals.append(ext)
    return edges, externals, untyped_any


def _receiver_classes(
    ref: RawRef, root: str, type_names: tuple[str, ...], ctx: ResolveIndex,
    module_defs: dict[str, str], imports: dict[str, str],
) -> list[tuple[SymbolId, bool]]:
    """The in-tree CLASS(es) a receiver ``root`` denotes, each with whether any fact used was
    inferred (INFERENCE vs ANNOTATION provenance). ``self`` -> its enclosing class (INFERENCE — the
    type is exact, but dispatch is open-world). Otherwise one entry per candidate type name: a union
    annotation yields several, deduped; an unresolved/external arm contributes nothing. An annotated
    arm is ANNOTATION; a constructed/factory one INFERENCE. Empty for an untyped receiver or a
    ``self`` outside a method."""
    if root == "self":
        cls = _self_class(ref.src, ctx.by_id)
        return [(cls, True)] if cls is not None else []
    out: list[tuple[SymbolId, bool]] = []
    seen: set[SymbolId] = set()
    for name in type_names:
        class_id = resolve_type_name(
            name, module_defs, imports, ctx.all_ids, ctx.reexports, ctx.by_id
        )
        inferred = ref.receiver_inferred
        if class_id is None:
            # the name may be a *call* whose return is an in-tree class: `s = make(); s.inner()`
            # where `make() -> Service`. resolve_type_name declined (make is a func, not a class),
            # so follow its return — a CALL, so ``returns`` (never a read-type ``attr_types``).
            callee = bind(
                name, module_defs, imports, ctx.all_ids, ctx.internal_roots, ctx.reexports
            )
            if callee is not None and not callee[1]:
                cls, ret_inferred = ctx.returns.get(callee[0], (None, False))
                class_id = cls
                inferred = ref.receiver_inferred or ret_inferred
        if class_id is not None and class_id not in seen:
            seen.add(class_id)
            out.append((class_id, inferred))
    return out


def _thread_member_chain(
    class_id: SymbolId, chain: list[str], inferred: bool, is_read: bool, ctx: ResolveIndex,
) -> tuple[SymbolId | None, bool, bool]:
    """Thread a member chain from a receiver class to its target member id, returning
    ``(target, inferred, untyped_receiver)``. Every member but the last is *read* as a value and
    advances through ``attr_types`` — the declared type of a DATA attribute.

    Two ways a hop can fail to thread, and they are NOT the same:
    - the intermediate member is a **data attribute whose type we can't thread** (untyped, external,
      or function-return) -> the last member's receiver is a real value of unknown type, like a
      single-hop ``obj.attr``, so the returned flag is ``True`` and the caller falls back to a LOW
      name-match on the last member.
    - the intermediate member is **not found on the (known) receiver type**, or is a method/class
      accessed-not-called (a bound method, not its return type) -> a typed receiver missing the
      member, so we stay silent (flag ``False``): never guess when the type is known.

    The last member is the target — a read may land on a data attribute, a call must be callable; a
    typed-receiver miss there is likewise silent (in-tree property detection is a deferred fix)."""
    read_members, bases, attr_types, by_id = ctx.read_members, ctx.bases, ctx.attr_types, ctx.by_id
    typ: SymbolId | None = class_id
    for name in chain[:-1]:
        hop = _member_lookup(typ, name, read_members, bases) if typ is not None else None
        if hop is None:
            return None, inferred, False  # not a member of a known type -> typed-missing, silent
        nxt, hop_inferred = attr_types.get(hop, (None, False))
        if nxt is None:
            # the member exists but its type isn't a threadable in-tree class. A DATA attribute or a
            # PROPERTY is a real value of unknown type (fall back); a method/class value is not
            # (stay silent — accessing it yields a bound method / the class object, not data).
            hop_sym = by_id.get(hop)
            data = hop_sym is not None and hop_sym.kind in READABLE_DATA
            return None, inferred, data
        inferred = inferred or hop_inferred
        typ = nxt
    lookup_members = read_members if is_read else ctx.members
    target = _member_lookup(typ, chain[-1], lookup_members, bases) if typ is not None else None
    return target, inferred, False


def _heuristic_edges(ref: RawRef, attr: str, is_read: bool, ctx: ResolveIndex) -> list[Edge]:
    """Untyped receiver: a last-resort LOW name-match to every member named ``attr``, but only when
    the name is specific enough (<= cap) to report, not spam. A read matches any readable class
    member; a call only methods."""
    candidates = (ctx.attrs_by_name if is_read else ctx.methods_by_name).get(attr, ())
    if not 0 < len(candidates) <= _HEURISTIC_CAP:
        return []
    return [Edge(ref.src, t, ref.kind, Resolution.heuristic(), ref.at) for t in candidates]


def _stub_targets(
    attr: str, type_names: tuple[str, ...], is_read: bool, ctx: ResolveIndex,
    module_defs: dict[str, str], imports: dict[str, str], stubs: StubProvider,
) -> list[tuple[SymbolId, Symbol]]:
    """External receiver arms: for each of ``type_names`` that resolves to an *external* type
    carrying ``attr`` in the stubs, the ``(member id, EXTERNAL member node)`` pair for a MEDIUM
    STUB edge. A union of externals (or a mixed union's external arms) yields several, deduped; an
    in-tree name contributes nothing (the caller resolved those). A call needs a callable member
    (`p.exists()`); a read accepts any member, including a property/data attribute (`p.name`)."""
    out: list[tuple[SymbolId, Symbol]] = []
    seen: set[SymbolId] = set()
    for name in type_names:
        ext = resolve_external_type_name(
            name, module_defs, imports, ctx.all_ids, ctx.internal_roots, ctx.reexports
        )
        member = stubs.type_member(ext, attr) if ext is not None else None
        if member is None or not (is_read or member[0] in _CALLABLE_STUB_KINDS):
            continue
        member_id = f"{ext}.{attr}"
        if member_id not in seen:
            seen.add(member_id)
            out.append((member_id, external_symbol(member_id)))
    return out


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


def _self_class(src: SymbolId, by_id: dict[SymbolId, Symbol]) -> SymbolId | None:
    """The class ``self`` denotes: the class enclosing the (instance) method/property that made the
    ref, or None when the ref isn't inside one (a nested function, module scope, or a staticmethod
    whose first parameter merely happens to be named ``self``)."""
    method = by_id.get(src)
    if method is None or method.kind not in _SELF_BOUND or method.is_static:
        return None
    cls = by_id.get(method.parent) if method.parent else None
    return cls.id if cls is not None and cls.kind is SymbolKind.CLASS else None


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
