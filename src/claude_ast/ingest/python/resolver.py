"""Resolution orchestration — binds a project's raw refs into the resolved edge set.

Extracted from the Python backend so the global *and* incremental resolution logic lives in one
concern. ``_resolve_files`` is the full pass. ``IncrementalResolver.resolve`` recomputes only a
dirty file set and reuses cached per-file edges for the rest, then reassembles the exact same edge
order — edge order is user-visible (it drives query result order and the PageRank summation), so
the incremental path must reproduce it byte for byte.

Each file's edges are held split by pass (``FileEdges``) because the global edge list is ordered
pass-first, then file: ``[all files' syntactic] + [all imports] + [all value-types] + [all
in-tree chains] + [all call-site observations]``. Keeping the split lets a clean file's
contribution slot back into that order verbatim during a patch.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex, ResolveResult
from .binding import bind, external_symbol
from .callsite import observe_arg_types
from .chains import KEEP, resolve_call_chain, resolve_external_chain
from .resolve_index import module_defs_map, resolution_index
from .stubs import StubProvider
from .typeres import resolve_intree_chains, resolve_value_types


@dataclass(slots=True)
class _Tables:
    """The file-derived tables the syntactic pass reads (built from *all* files each patch)."""

    all_ids: set[SymbolId]
    by_id: dict[SymbolId, Symbol]
    internal_roots: set[str]
    reexports: dict[str, dict[str, str]]
    module_defs: dict[str, dict[str, str]]


def _base_tables(files: Sequence[FileIndex]) -> _Tables:
    return _Tables(
        all_ids={sym.id for fi in files for sym in fi.symbols},
        by_id={sym.id: sym for fi in files for sym in fi.symbols},
        internal_roots={fi.module.partition(".")[0] for fi in files},
        reexports={fi.module: fi.imports for fi in files},
        module_defs=module_defs_map(files),
    )


@dataclass(slots=True)
class FileEdges:
    """One file's resolved edges, split by pass. ``main`` carries the INHERITS edges the base
    walk needs; ``externals_syn`` / ``externals_val`` are the external nodes minted while
    resolving this file (deduped globally at reassembly, in mint order)."""

    main: list[Edge]
    imports: list[Edge]
    externals_syn: list[Symbol]
    value_types: list[Edge]
    externals_val: list[Symbol]
    intree: list[Edge]
    callsite: list[Edge]


def _syntactic_file(
    fi: FileIndex, t: _Tables, stubs: StubProvider
) -> tuple[list[Edge], list[Edge], list[Symbol]]:
    """Syntactic binding for ONE file: its main-loop edges, its import top-name edges, and the
    externals it mints. Reads only ``fi`` + the global tables, so a file resolves identically
    whether processed alone (incremental) or in the full sweep."""
    module_defs = t.module_defs[fi.module]
    main: list[Edge] = []
    externals: dict[str, Symbol] = {}
    for ref in fi.refs:
        if ref.kind is EdgeKind.IMPORT:
            # module dependency: keep only imports that land on an in-tree module.
            target = t.by_id.get(ref.name)
            if target is not None and target.kind is SymbolKind.MODULE:
                main.append(
                    Edge(ref.src, ref.name, EdgeKind.IMPORT, Resolution.syntactic(), ref.at)
                )
            continue
        if ref.local_root:
            continue  # value receiver — the type resolvers own it, not syntactic binding
        if ref.chain:
            # call-return chain (`re.compile(p).match(s).group()`): thread the receiver's
            # return type through the members -> a possible STUB edge on the last, else nothing.
            recv = bind(ref.name, module_defs, fi.imports, t.all_ids, t.internal_roots, t.reexports)
            if recv is None or not recv[1]:
                continue
            target_id = resolve_call_chain(recv[0], ref.chain, stubs)
            if target_id is None:
                continue
            externals.setdefault(target_id, external_symbol(target_id))
            main.append(Edge(ref.src, target_id, ref.kind, Resolution.stubbed(), ref.at))
            continue
        bound = bind(ref.name, module_defs, fi.imports, t.all_ids, t.internal_roots, t.reexports)
        if bound is None:
            continue
        dst, is_external = bound
        # An external CALL/READ chain that crosses into a value (`sys.stdout.getvalue`) is not a
        # definite module fact — walk it through the typeshed tables to keep the module-fact
        # prefix definite, downgrade a value member to a possible STUB edge, or decline it. A read
        # of a *module-level* value (`os.EX_OK`) stays definite where a call would decline — the
        # one place the read/call decision diverges (``is_call``).
        if is_external and ref.kind in (EdgeKind.CALL, EdgeKind.REFERENCE) and "." in dst:
            decision = resolve_external_chain(dst, stubs, is_call=ref.kind is EdgeKind.CALL)
            if decision is None:
                continue  # type-dependent member we can't confirm -> report nothing
            if decision is not KEEP:
                _, target_id = decision
                externals.setdefault(target_id, external_symbol(target_id))
                main.append(Edge(ref.src, target_id, ref.kind, Resolution.stubbed(), ref.at))
                continue
        if is_external:
            externals.setdefault(dst, external_symbol(dst))
        main.append(Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at))

    # `import a.b` binds the top name `a` — a real dependency with no ref of its own. Add the
    # in-tree-module targets, deduped against this file's own spanned IMPORT edges above.
    imports: list[Edge] = []
    seen = {(e.src, e.dst) for e in main if e.kind is EdgeKind.IMPORT}
    for target in fi.imports.values():
        sym = t.by_id.get(target)
        if sym is None or sym.kind is not SymbolKind.MODULE:
            continue
        if (fi.module, target) in seen:
            continue
        seen.add((fi.module, target))
        imports.append(Edge(fi.module, target, EdgeKind.IMPORT, Resolution.syntactic(), None))
    return main, imports, list(externals.values())


def _resolve_files(
    files: Sequence[FileIndex],
    stubs: StubProvider,
    reuse: dict[SymbolId, FileEdges] | None = None,
    dirty: set[str] | None = None,
) -> dict[SymbolId, FileEdges]:
    """Resolve files into their per-pass ``FileEdges``. Syntactic first (so the INHERITS edges the
    base walk needs exist), then the value passes over the shared ``ResolveIndex``.

    The single engine for both the full and incremental paths: with ``dirty=None`` every file is
    (re)computed; otherwise only modules in ``dirty`` are, and the rest are taken from ``reuse``
    (their edges are unaffected by the patch — see ``_dirty_set``). ``ResolveIndex`` is still built
    over *all* files each call, so a clean file's reused value edges see the same tables a full
    resolve would, keeping the result byte-identical."""
    t = _base_tables(files)

    def stale(module: str) -> bool:
        return dirty is None or module in dirty

    recs: dict[SymbolId, FileEdges] = {}
    for fi in files:
        if stale(fi.module):
            main, imports, ext_syn = _syntactic_file(fi, t, stubs)
            recs[fi.module] = FileEdges(main, imports, ext_syn, [], [], [], [])
        else:
            recs[fi.module] = reuse[fi.module]  # type: ignore[index]  # reuse is set when dirty is
    all_main = [e for fi in files for e in recs[fi.module].main]  # INHERITS live only here
    ctx = resolution_index(
        files, all_main, by_id=t.by_id, all_ids=t.all_ids, internal_roots=t.internal_roots,
        reexports=t.reexports, module_defs=t.module_defs,
    )
    for fi in files:
        if stale(fi.module):
            rec = recs[fi.module]
            rec.value_types, rec.externals_val = resolve_value_types([fi], ctx, stubs)
            rec.intree = resolve_intree_chains([fi], ctx)
            rec.callsite = observe_arg_types([fi], ctx)
    return recs


def reassemble(files: Sequence[FileIndex], recs: dict[SymbolId, FileEdges]) -> ResolveResult:
    """Flatten per-file edges into the canonical pass-then-file order, deduping externals in the
    order they were minted (syntactic before value). Identical bytes to the full sweep."""
    order = [fi.module for fi in files]
    edges: list[Edge] = []
    for attr in ("main", "imports", "value_types", "intree", "callsite"):
        for m in order:
            edges.extend(getattr(recs[m], attr))
    externals: dict[str, Symbol] = {}
    for attr in ("externals_syn", "externals_val"):
        for m in order:
            for x in getattr(recs[m], attr):
                externals.setdefault(x.id, x)
    return ResolveResult(edges=edges, externals=list(externals.values()))


# --- incremental resolution ---------------------------------------------------------------
#
# A patch changes a few files; re-resolving all of them is O(project). But a changed file can
# only alter *another* file's edges through two channels, so those — and only those — need
# recomputing:
#   1. imports — every resolver binds a ref through the file's own defs/imports, so a
#      cross-module reference must be imported. A change to module M can only affect files that
#      (transitively, through re-export chains) import M -> the reverse-import closure.
#   2. the heuristic name-match — an untyped ``obj.m()`` binds to every in-tree ``*.m`` method (a
#      bare read ``obj.attr`` to every readable ``*.attr`` member) with no import, one LOW edge per
#      candidate in candidate order. So ANY change to the global ``m`` population — add/remove,
#      crossing the ambiguity cap, OR a rename/move/reorder/kind-flip that changes a candidate's
#      *id*, *kind*, or *position* at constant count — flips those edges in every file with an
#      untyped ``.m()`` call or ``.m`` read. We therefore track each name's ordered ``(id, kind)``
#      tuple, not its count/id alone (a weaker proxy silently misses these -> stale/dangling edges).
# The "dirty" set is the changed files plus both closures; every other file's cached edges are
# reused verbatim. A wrong (too-small) dirty set would silently corrupt the graph, so the
# incremental==full fuzz test is the load-bearing guard, and we fall back to a full resolve
# whenever the dirty set is large enough that incrementality doesn't pay.


@dataclass(slots=True)
class _Cache:
    products: dict[SymbolId, FileIndex]  # module -> its FileIndex (identity => unchanged)
    recs: dict[SymbolId, FileEdges]  # module -> its resolved edges (reused when not dirty)


def _surface(fi: FileIndex) -> tuple:
    """The part of a file that *other* files' resolution reads: symbol shape + import map. If this
    is unchanged, a body-only edit can't change how anyone else binds against this module."""
    shape = tuple(
        (s.id, s.kind, s.parent, s.return_type, s.return_type_inferred) for s in fi.symbols
    )
    return shape, tuple(sorted(fi.imports.items()))


# Class-member kinds a heuristic edge can bind to: a value CALL matches a METHOD; a bare attribute
# READ matches any readable member (a data VARIABLE or nested CLASS too). Tracking the read superset
# covers the call pool as well. Mirrors ``resolve_index._READABLE`` for class members, kept local so
# incremental dirty-set doesn't reach into the resolver's internals.
_HEURISTIC_MEMBER_KINDS = frozenset(
    {SymbolKind.METHOD, SymbolKind.VARIABLE, SymbolKind.CLASS, SymbolKind.PROPERTY}
)


def _attr_ids(fi: FileIndex) -> dict[str, tuple[tuple[SymbolId, str], ...]]:
    """Class-member name -> the ordered ``(id, kind)`` of every readable member with that name in
    this file — the file's contribution to the global heuristic name-match population. A value CALL
    name-matches methods; a bare-read REFERENCE name-matches any readable member, so tracking the
    wider read pool (methods + variables + nested classes) also covers the call pool. Comparing the
    tuple (not just its length) catches a rename/move/reorder that changes a candidate's id or
    position at constant count — a stale cached heuristic edge otherwise.

    ``kind`` is part of the tracked tuple, not just ``id``, because a symbol id does NOT encode
    kind: a same-qualname flip between a METHOD and a VARIABLE/nested CLASS (``def foo`` -> ``foo =
    1``) keeps the id but changes which heuristic pool the member is in — it *leaves* the
    METHOD-only call pool while staying in the read pool. Without the kind, that flip is invisible,
    so a non-importing untyped ``obj.foo()`` caller keeps a cached call edge to what is now a data
    attribute (a real incremental != full divergence). Only members whose parent is a class in
    *this* file count (a class's members live beside it), matching the ``attrs_by_name`` pool the
    read heuristic actually consults."""
    classes = {s.id for s in fi.symbols if s.kind is SymbolKind.CLASS}
    ids: dict[str, list[tuple[SymbolId, str]]] = {}
    for s in fi.symbols:
        if s.kind in _HEURISTIC_MEMBER_KINDS and s.parent in classes:
            ids.setdefault(s.name, []).append((s.id, s.kind.value))
    return {name: tuple(v) for name, v in ids.items()}


def _dep_modules(fi: FileIndex) -> set[str]:
    """Every module qualname this file could bind against — all dotted prefixes of its import
    targets and module-import refs (an over-approximation: safe, since it only widens the set)."""
    deps: set[str] = set()

    def add_prefixes(qualname: str) -> None:
        parts = qualname.split(".")
        for i in range(1, len(parts) + 1):
            deps.add(".".join(parts[:i]))

    for q in fi.imports.values():
        add_prefixes(q)
    for ref in fi.refs:
        if ref.kind is EdgeKind.IMPORT:
            add_prefixes(ref.name)
    return deps


def _untyped_method_names(fi: FileIndex) -> set[str]:
    """Attribute names this file name-matches via the heuristic resolver, so a change to any of
    their global member populations must re-resolve this file. Two heuristic rungs contribute:

    - a single attribute on an *untyped* receiver (``obj.m()`` / ``obj.attr``) -> that attribute;
    - a **multi-member chain** (``self.a.b``) -> its LAST member, because when an intermediate hop
      is an untyped data attribute the chain falls back to a LOW name-match on the last member —
      this holds regardless of the root's own type, so it is NOT gated on ``receiver_type``.

    Kind-agnostic (call vs read) on purpose: the read pool ``_attr_ids`` tracks is a superset of the
    call pool. An over-approximation (it may track a name a chain won't fall back on) — safe, it
    only widens the dirty set. ``ref.chain`` (call-return) chains don't heuristic-fall-back."""
    names: set[str] = set()
    for ref in fi.refs:
        if not ref.local_root or ref.chain:
            continue
        _, _, rest = ref.name.partition(".")
        if not rest:
            continue
        if "." in rest:
            names.add(rest.rsplit(".", 1)[-1])  # multi-member chain -> LOW fallback on last member
        elif not ref.receiver_types:
            names.add(rest)  # single untyped attribute
    return names


def _dirty_set(
    files: Sequence[FileIndex], cache: _Cache, changed: set[str], deleted: set[str]
) -> set[str]:
    """The modules whose edges must be recomputed: the changed files, everything transitively
    importing a module whose resolution surface changed, and everything with an untyped call to a
    method name whose global population changed."""
    old = cache.products
    # (1) modules whose surface changed (or vanished) can affect their importers.
    surface_changed = set(deleted)
    for fi in files:
        if fi.module in changed:
            prior = old.get(fi.module)
            if prior is None or _surface(prior) != _surface(fi):
                surface_changed.add(fi.module)
    # (2) member names whose (id, kind)-tuple changed anywhere (add/remove/rename/move/reorder/
    #     kind-flip) can flip the heuristic edges bound to them, so re-resolve every untyped
    #     caller/reader of the name.
    changed_names: set[str] = set()
    for fi in files:
        if fi.module in changed:
            new_c = _attr_ids(fi)
            old_c = _attr_ids(old[fi.module]) if fi.module in old else {}
            changed_names |= {n for n in set(new_c) | set(old_c) if new_c.get(n) != old_c.get(n)}
    for m in deleted:
        changed_names |= set(_attr_ids(old[m]))

    dirty = set(changed)
    # reverse-import closure over the (transitive) import graph.
    imported_by: dict[str, set[str]] = defaultdict(set)
    for fi in files:
        for dep in _dep_modules(fi):
            imported_by[dep].add(fi.module)
    stack = list(surface_changed)
    seen: set[str] = set()
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        for importer in imported_by.get(m, ()):
            dirty.add(importer)
            stack.append(importer)  # transitive: re-export chains carry the change further
    # heuristic-name closure.
    if changed_names:
        callers: dict[str, set[str]] = defaultdict(set)
        for fi in files:
            for name in _untyped_method_names(fi):
                callers[name].add(fi.module)
        for name in changed_names:
            dirty |= callers.get(name, set())
    return dirty


class IncrementalResolver:
    """Stateful resolver: reuses per-file edges across patches, recomputing only the dirty set.

    Held by a long-lived backend (the session's), so its cache persists between patches. A
    one-shot backend (``Index.build``) makes a fresh one, so its first (and only) call is a full
    resolve — warm==cold is preserved because the incremental result is byte-identical to it.
    """

    __slots__ = ("_stubs", "_cache")

    def __init__(self, stubs: StubProvider) -> None:
        self._stubs = stubs
        self._cache: _Cache | None = None

    def _emit(self, files: Sequence[FileIndex], recs: dict[SymbolId, FileEdges]) -> ResolveResult:
        self._cache = _Cache({fi.module: fi for fi in files}, recs)
        return reassemble(files, recs)

    def resolve(self, files: Sequence[FileIndex]) -> ResolveResult:
        cache = self._cache
        if cache is None:  # first call — nothing to reuse
            return self._emit(files, _resolve_files(files, self._stubs))
        # A file is unchanged iff ingest handed back the *same* FileIndex object (it reuses cached
        # products for unchanged files and mints new ones for changed).
        changed = {fi.module for fi in files if cache.products.get(fi.module) is not fi}
        deleted = set(cache.products) - {fi.module for fi in files}
        if len(changed) + len(deleted) > len(files) // 2:
            return self._emit(files, _resolve_files(files, self._stubs))  # too much churn

        dirty = _dirty_set(files, cache, changed, deleted)
        if len(dirty) > len(files) // 2:  # incrementality no longer worth the bookkeeping
            return self._emit(files, _resolve_files(files, self._stubs))

        # Recompute only the dirty modules through the same engine, reusing the rest.
        recs = _resolve_files(files, self._stubs, reuse=cache.recs, dirty=dirty)
        return self._emit(files, recs)
