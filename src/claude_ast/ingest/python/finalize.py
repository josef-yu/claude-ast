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
    """Return ``files`` with globally-unique symbol ids (and refs repointed to match).

    Ids are reformed from ``(remapped parent).name`` — so a subtree whose root is suffixed carries
    the suffix down — with an ``#N`` appended on any residual clash. Only symbols that actually
    collide move; every non-colliding id is reproduced exactly.
    """
    all_ids = [s.id for fi in files for s in fi.symbols]
    if len(set(all_ids)) == len(all_ids):
        return list(files)  # no collision — the common case, returned untouched (identity kept)

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
        for s in fi.symbols:
            if s.kind is SymbolKind.MODULE:
                new_id = module_id[id(fi)]
            else:
                parent = remap.get(s.parent, s.parent) if s.parent is not None else None
                base = f"{parent}.{s.name}" if parent is not None else s.name
                new_id = _claim(base, seen)
            remap[s.id] = new_id
            new_parent = remap.get(s.parent, s.parent) if s.parent is not None else None
            new_symbols.append(replace(s, id=new_id, parent=new_parent))
        new_refs = [replace(r, src=remap.get(r.src, r.src)) for r in fi.refs]
        out.append(replace(fi, symbols=new_symbols, refs=new_refs))
    return out


def _claim(base: str, seen: set[str]) -> str:
    """The first free id at/after ``base`` (``base``, then ``base#2`` …); reserves it."""
    candidate = base
    n = 2
    while candidate in seen:
        candidate = f"{base}#{n}"
        n += 1
    seen.add(candidate)
    return candidate
