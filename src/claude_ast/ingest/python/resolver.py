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

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolId, SymbolKind
from ..product import FileIndex, ResolveResult
from .binding import bind, external_symbol
from .callsite import observe_arg_types
from .chains import KEEP, resolve_call_chain, resolve_external_chain
from .stubs import StubProvider
from .typeres import (
    module_defs_map,
    resolution_index,
    resolve_intree_chains,
    resolve_value_types,
)

logger = logging.getLogger(__name__)


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


def _warn_ambiguous_modules(files: Sequence[FileIndex]) -> None:
    """Surface an invalid layout where two files share a module qualname (e.g. ``pkg.py`` beside
    ``pkg/__init__.py``). The resolver keys per-module tables by that qualname, so a collision
    collapses them — but it is a layout Python itself rejects, so we warn rather than support it."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for fi in files:
        (dupes if fi.module in seen else seen).add(fi.module)
    if dupes:
        logger.warning(
            "ambiguous module name(s) %s — multiple files map to one qualname (a package and a "
            "same-named module?); their edges may be dropped or misresolved",
            sorted(dupes),
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
        # An external CALL chain that crosses into a value (`sys.stdout.getvalue`) is not a
        # definite module fact — walk it through the typeshed tables to keep the module-fact
        # prefix definite, downgrade a value member to a possible STUB edge, or decline it.
        if is_external and ref.kind is EdgeKind.CALL and "." in dst:
            decision = resolve_external_chain(dst, stubs)
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
    if dirty is None:  # full pass — the one place to surface an invalid same-qualname layout
        _warn_ambiguous_modules(files)
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
#   2. the heuristic name-match — an untyped ``obj.m()`` binds to every in-tree ``*.m`` method
#      with no import, one LOW edge per candidate in candidate order. So ANY change to the global
#      ``m`` population — add/remove, crossing the ambiguity cap, OR a rename/move/reorder that
#      changes a candidate's *id* or *position* at constant count — flips those edges in every
#      file with an untyped ``.m()`` call. We therefore track each name's ordered id-tuple, not
#      just its count (a count proxy silently misses rename/move/reorder -> stale/dangling edges).
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


def _method_ids(fi: FileIndex) -> dict[str, tuple[SymbolId, ...]]:
    """Method name -> the ids of every method with that name in this file, in symbol order — the
    file's ordered contribution to the global name-match population. A heuristic edge binds to
    these ids in this order, so comparing the id-tuple (not just its length) is what catches a
    rename/move/reorder that changes a candidate's id or position at constant count."""
    ids: dict[str, list[SymbolId]] = {}
    for s in fi.symbols:
        if s.kind is SymbolKind.METHOD:
            ids.setdefault(s.name, []).append(s.id)
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
    """Method names this file calls on an *untyped* receiver — the ones the heuristic resolver
    would name-match, so a change to their global population must re-resolve this file."""
    names: set[str] = set()
    for ref in fi.refs:
        if ref.local_root and ref.receiver_type is None:
            _, _, attr = ref.name.partition(".")
            if attr and "." not in attr:
                names.add(attr)
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
    # (2) method names whose id-tuple changed anywhere (add/remove/rename/move/reorder) can flip
    #     the heuristic edges bound to those ids, so re-resolve every untyped caller of the name.
    changed_names: set[str] = set()
    for fi in files:
        if fi.module in changed:
            new_c = _method_ids(fi)
            old_c = _method_ids(old[fi.module]) if fi.module in old else {}
            changed_names |= {n for n in set(new_c) | set(old_c) if new_c.get(n) != old_c.get(n)}
    for m in deleted:
        changed_names |= set(_method_ids(old[m]))

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
