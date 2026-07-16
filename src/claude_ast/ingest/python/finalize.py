"""Cross-file finalization of the Python backend's per-file products.

Per-file ingest can't see cross-file id collisions: a submodule ``pkg/helpers.py`` and a class
``helpers`` in ``pkg/__init__.py`` both mint the id ``pkg.helpers`` — and, because the collision
cascades through the shared dotted namespace, every symbol under them (``pkg.helpers.run`` …)
collides too. Left alone, ``Graph.add_symbol`` last-wins-clobbers one and leaves the other's ids
duplicated in the name/child indexes.

``finalize`` runs once over all of a backend's files at assembly and rewrites ids so they are
globally unique — extending the per-file ``#N`` disambiguation (see ``symbols._unique``) to the
cross-file case. Module symbols keep their canonical qualname (they are the import targets a ref
binds to); a colliding *member* is the one suffixed. The common case — no collision anywhere — is
detected up front and the files are returned untouched (same objects), so warm==cold and the
incremental cache's file-identity check are unaffected on the overwhelming majority of projects.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ...model import SymbolKind
from ..product import FileIndex

logger = logging.getLogger(__name__)


def ensure_unique_ids(files: Sequence[FileIndex]) -> list[FileIndex]:
    """Return ``files`` with globally-unique symbol ids and correct module-tree parents.

    Two cross-file fix-ups, both of which only rebuild the files they actually touch (a file none
    of whose symbols moved is returned **as the same object**, so the incremental cache keeps
    reusing it):

    - **unique ids** — ids are reformed from ``(remapped parent).name``, so a subtree whose root is
      suffixed carries the suffix down, with an ``#N`` on any residual clash. Bare canonical ids
      that don't clash are reproduced exactly; an ``#N`` sibling sharing a base with a cross-file
      winner may be renumbered (still deterministic, still unique).
    - **parent correction** — ingest sets a module's parent to its qualname prefix (a stable
      per-file guess). If that prefix is not itself a real module — a PEP 420 namespace package
      with no ``__init__``, or a same-named class/func/var in the parent's ``__init__`` — the parent
      is corrected here to the nearest ancestor that *is* a module, so the subtree stays reachable
      and never hangs off a non-module. Only the (rare) misparented modules are rebuilt.
    """
    all_ids = [s.id for fi in files for s in fi.symbols]
    module_ids = {fi.module for fi in files}
    has_dup = len(set(all_ids)) != len(all_ids)
    # a module whose qualname prefix is not itself a module needs its parent corrected (below).
    has_gap = any((pkg := fi.module.rpartition(".")[0]) and pkg not in module_ids for fi in files)
    if not has_dup and not has_gap:
        return list(files)  # nothing to do — the common case, returned untouched (identity kept)

    seen: set[str] = set()
    # Pass 1: reserve the module ids first — they are import targets, so a member colliding with a
    # module is the one that yields, never the module. (Two files claiming one module qualname is
    # an invalid layout; it still deterministically deduplicates + warns below.)
    module_id: dict[int, str] = {}
    collided_modules: list[str] = []
    for fi in files:
        rid = _claim(fi.module, seen)
        if rid != fi.module:
            collided_modules.append(fi.module)
        module_id[id(fi)] = rid
    if collided_modules:
        logger.warning(
            "ambiguous module name(s) %s — multiple files map to one qualname; disambiguated with "
            "an #N suffix (an invalid layout: a package and a same-named module)",
            sorted(set(collided_modules)),
        )

    # Pass 2: reform every symbol id from its (remapped) parent + name, suffixing on any clash.
    out: list[FileIndex] = []
    for fi in files:
        remap: dict[str, str] = {fi.module: module_id[id(fi)]}
        new_symbols = []
        moved = False  # did anything in THIS file actually change?
        for s in fi.symbols:
            if s.kind is SymbolKind.MODULE:
                new_id = module_id[id(fi)]
                # correct a namespace-gap / same-named-non-module parent to the nearest real package
                corrected = (
                    s.parent if s.parent is None or s.parent in module_ids
                    else _nearest_package(fi.module, module_ids)
                )
                new_parent = remap.get(corrected, corrected) if corrected is not None else None
            else:
                new_parent = remap.get(s.parent, s.parent) if s.parent is not None else None
                base = f"{new_parent}.{s.name}" if new_parent is not None else s.name
                new_id = _claim(base, seen)  # still reserve the id even if unchanged
            remap[s.id] = new_id
            moved = moved or new_id != s.id or new_parent != s.parent
            new_symbols.append(replace(s, id=new_id, parent=new_parent))
        # Confine new objects to files a collision actually touched. Every other file is returned
        # UNCHANGED (same object) so the incremental cache's identity check keeps reusing it — a
        # collision elsewhere must not silently drop the whole project to full re-resolve.
        if not moved:
            out.append(fi)
            continue
        new_refs = [replace(r, src=remap.get(r.src, r.src)) for r in fi.refs]
        out.append(replace(fi, symbols=new_symbols, refs=new_refs))
    return out


def _nearest_package(module: str, module_ids: set[str]) -> str | None:
    """The nearest ancestor of ``module`` that is itself a module — skipping namespace-package gaps
    and same-named non-modules — or ``None`` if there is none."""
    prefix = module.rpartition(".")[0]
    while prefix:
        if prefix in module_ids:
            return prefix
        prefix = prefix.rpartition(".")[0]
    return None


def _claim(base: str, seen: set[str]) -> str:
    """The first free id at/after ``base`` (``base``, then ``base#2`` …); reserves it."""
    candidate = base
    n = 2
    while candidate in seen:
        candidate = f"{base}#{n}"
        n += 1
    seen.add(candidate)
    return candidate
