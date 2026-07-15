"""PythonIndexer — the Python language backend.

Ties symbol + reference extraction into per-file ``FileIndex`` products, and
binds raw references into resolved edges. All binding is **backend-scoped**: it
only ever resolves against this backend's own files, and symbol ids are already
namespaced by module path, so a second language could never cross-bind.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Sequence
from pathlib import Path

from ...model import Edge, EdgeKind, Resolution, Symbol, SymbolKind
from ..product import FileIndex, ResolveResult
from .binding import bind, external_symbol
from .callsite import observe_arg_types
from .chains import KEEP, resolve_call_chain, resolve_external_chain
from .common import module_qualname
from .refs import extract_refs
from .stubs import STDLIB_STUBS, StubProvider
from .symbols import extract_symbols
from .typeres import (
    module_defs_map,
    resolution_index,
    resolve_intree_chains,
    resolve_value_types,
)

logger = logging.getLogger(__name__)


class PythonIndexer:
    """Language backend for Python source."""

    name = "python"
    extensions = frozenset({".py"})

    def __init__(self, stubs: StubProvider = STDLIB_STUBS) -> None:
        # The stub provider is injected here (not a global) so a future environment-aware
        # provider — which needs construction-time config — slots in without touching resolve.
        self._stubs = stubs

    def ingest_file(self, path: Path, root: Path) -> FileIndex | None:
        # Read bytes and let ast.parse honor a UTF-8 BOM and any PEP 263 coding
        # cookie itself — decoding as strict UTF-8 first would drop valid files.
        try:
            source = path.read_bytes()
        except OSError as exc:
            logger.warning("skipping %s: %s", path, exc)
            return None
        try:
            return self.ingest_source(str(path), source, module_qualname(path, root))
        except SyntaxError as exc:
            logger.warning("skipping %s: %s", path, exc)
            return None

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        try:
            return self.ingest_source(str(path), source, module_qualname(path, root))
        except SyntaxError as exc:
            logger.warning("skipping %s: %s", path, exc)
            return None

    def ingest_source(self, path: str, source: str | bytes, module: str) -> FileIndex:
        """Parse one module's source into a FileIndex. Raises ``SyntaxError`` on bad input."""
        tree = ast.parse(source, filename=path)
        # One id-assignment authority: symbols mints ids, refs consumes the same
        # node->id map so an edge's src is exactly its enclosing symbol's id.
        symbols, node_ids = extract_symbols(tree, module, path)
        refs, imports = extract_refs(tree, module, path, node_ids)
        return FileIndex(path=path, module=module, symbols=symbols, refs=refs, imports=imports)

    def resolve(self, files: Sequence[FileIndex]) -> ResolveResult:
        """Bind raw references to symbol ids, emitting SYNTACTIC/high-confidence edges.

        Resolves each reference against the same module's top-level definitions,
        then its imports — including attribute chains (``os.path.join``) whose root
        is a module def or import. A target that exists in the index is an in-tree
        edge; one whose top package is outside the project is a ``definite`` edge to
        an EXTERNAL node (the reference is genuinely there; only P2's value-type work
        is uncertain, so a direct/module-rooted reference is honestly definite). A
        chain rooted in the project but not (yet) a known symbol — an attribute on a
        value — is left for the P2 resolver stack. Bare names with no def/import
        (builtins, dynamic, value receivers) still yield nothing.
        """
        all_ids = {sym.id for fi in files for sym in fi.symbols}
        by_id = {sym.id: sym for fi in files for sym in fi.symbols}
        internal_roots = {fi.module.partition(".")[0] for fi in files}
        # Each module's import map doubles as its re-export table: a name imported into
        # module M is reachable as M.name, so `from pkg import X` follows pkg's __init__.
        reexports = {fi.module: fi.imports for fi in files}
        # Each module's top-level {name -> id} (first-def wins), built once and reused by the
        # loop below and every value pass — the shared ResolveIndex assembled after this loop.
        module_defs_by_module = module_defs_map(files)
        edges: list[Edge] = []
        externals: dict[str, Symbol] = {}
        for fi in files:
            module_defs = module_defs_by_module[fi.module]
            for ref in fi.refs:
                if ref.kind is EdgeKind.IMPORT:
                    # module dependency: keep only imports that land on an in-tree module.
                    target = by_id.get(ref.name)
                    if target is not None and target.kind is SymbolKind.MODULE:
                        edges.append(
                            Edge(ref.src, ref.name, EdgeKind.IMPORT, Resolution.syntactic(), ref.at)
                        )
                    continue
                if ref.local_root:
                    continue  # value receiver — the type resolvers own it, not syntactic binding
                if ref.chain:
                    # call-return chain (`re.compile(p).match(s).group()`): thread the receiver's
                    # return type through the members -> a possible STUB edge on the last, else
                    # nothing. Only external (typeshed-typed) receivers are threadable today.
                    recv = bind(
                        ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports
                    )
                    if recv is None or not recv[1]:
                        continue
                    target = resolve_call_chain(recv[0], ref.chain, self._stubs)
                    if target is None:
                        continue
                    externals.setdefault(target, external_symbol(target))
                    edges.append(Edge(ref.src, target, ref.kind, Resolution.stubbed(), ref.at))
                    continue
                bound = bind(ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports)
                if bound is None:
                    continue
                dst, is_external = bound
                # An external CALL chain that crosses into a value (`sys.stdout.getvalue`) is not a
                # definite module fact — walk it through the typeshed tables to keep the module-fact
                # prefix definite, downgrade a value member to a possible STUB edge, or decline it.
                if is_external and ref.kind is EdgeKind.CALL and "." in dst:
                    decision = resolve_external_chain(dst, self._stubs)
                    if decision is None:
                        continue  # type-dependent member we can't confirm -> report nothing
                    if decision is not KEEP:
                        _, target = decision
                        externals.setdefault(target, external_symbol(target))
                        edges.append(Edge(ref.src, target, ref.kind, Resolution.stubbed(), ref.at))
                        continue
                if is_external:
                    externals.setdefault(dst, external_symbol(dst))
                edges.append(Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at))
        # `from pkg import submodule` now arrives as a spanned candidate ref; what the refs still
        # don't carry is the *top-name* binding of a dotted plain import (`import a.b` binds `a` —
        # a real dependency on package `a` with no ref of its own). The import map resolved each
        # bound name to a qualname; add the in-tree-module targets, deduped against the spanned
        # edges above (span-less here).
        seen_imports = {(e.src, e.dst) for e in edges if e.kind is EdgeKind.IMPORT}
        for fi in files:
            for target in fi.imports.values():
                sym = by_id.get(target)
                if sym is None or sym.kind is not SymbolKind.MODULE:
                    continue
                if (fi.module, target) in seen_imports:
                    continue
                seen_imports.add((fi.module, target))
                edge = Edge(fi.module, target, EdgeKind.IMPORT, Resolution.syntactic(), None)
                edges.append(edge)
        # Assemble the shared lookup tables once, now that the syntactic edges (INHERITS) exist
        # for the base walk. The three value passes below read these instead of each rebuilding
        # by_id / members / bases / returns / module_defs from scratch.
        ctx = resolution_index(
            files,
            edges,
            by_id=by_id,
            all_ids=all_ids,
            internal_roots=internal_roots,
            reexports=reexports,
            module_defs=module_defs_by_module,
        )
        # Value-typed pass: self.m() and annotated `u: User; u.m()` -> MEDIUM/possible edges,
        # plus stub-resolved members on external types (`p: Path; p.exists()`) as MEDIUM STUB
        # edges to external nodes. Runs after syntactic binding, so cross-file INHERITS present.
        value_edges, stub_externals = resolve_value_types(files, ctx, self._stubs)
        edges.extend(value_edges)
        for ext in stub_externals:
            externals.setdefault(ext.id, ext)
        # Call-return chains whose receiver returns an in-tree type (`make() -> Service`;
        # `make().run()` -> Service.run). Uses the INHERITS edges already in `edges`.
        edges.extend(resolve_intree_chains(files, ctx))
        # Call-site observations: `g(User())` -> a definite `g RECEIVES_ARG User` edge. A
        # usage fact, independent of the dispatch passes above (no edges needed as input).
        edges.extend(observe_arg_types(files, ctx))
        return ResolveResult(edges=edges, externals=list(externals.values()))
