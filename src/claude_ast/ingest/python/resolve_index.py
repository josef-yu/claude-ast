"""The resolve index — the lookup tables the value resolvers read.

``ResolveIndex`` collects, once per graph assembly, everything the resolvers in ``typeres`` need:
class members (callable and readable), the base-class graph, name-match candidate pools, and the
two disjoint type maps (``returns`` for a call, ``attr_types`` for a read). Building it here keeps
``typeres`` about *resolution* — turning a receiver into edges — rather than table construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...model import Edge, EdgeKind, Symbol, SymbolId, SymbolKind
from ..product import FileIndex
from .binding import resolve_type_name

# A value CALL must resolve to something callable — a method, a nested function, or a
# class (instantiation) — so data attributes (class-level VARIABLE) and PROPERTYs (accessed, not
# called) are excluded: this keeps `self.count()` from forging a call to a variable, keeps a class
# var from masking a same-named method, and makes `obj.prop()` (calling a property) resolve nothing.
_CALLABLE = frozenset({SymbolKind.METHOD, SymbolKind.FUNCTION, SymbolKind.CLASS})

# Members accessed as a DATA value (not a bound method / class object): a variable or a property.
# When a chain hits one whose type can't be threaded, its receiver is a real value of unknown type,
# so the chain falls back to a LOW name-match on the last member (like a single-hop ``obj.attr``).
# Public: ``typeres`` reads it when threading a multi-member chain.
READABLE_DATA = frozenset({SymbolKind.VARIABLE, SymbolKind.PROPERTY})

# A bare attribute READ can land on any member, so its lookup adds the members the call map
# deliberately omits: a data attribute (VARIABLE) and a PROPERTY — both accessed as a value.
# `obj.count` / `obj.prop` are valid read targets where `obj.count()` / `obj.prop()` are not.
_READABLE = _CALLABLE | READABLE_DATA


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
            # A PROPERTY yields its type when READ (like a data attribute) -> attr_types; a
            # function/method yields it when CALLED -> returns.
            read_typed = sym.kind in (SymbolKind.VARIABLE, SymbolKind.PROPERTY)
            (attr_types if read_typed else returns)[sym.id] = (cls, sym.return_type_inferred)
    return returns, attr_types


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
