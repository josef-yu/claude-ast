"""PythonIndexer — the Python language backend.

Ties symbol + reference extraction into per-file ``FileIndex`` products, and
binds raw references into resolved edges. All binding is **backend-scoped**: it
only ever resolves against this backend's own files, and symbol ids are already
namespaced by module path, so a second language could never cross-bind.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from ...model import Edge, Resolution, Symbol
from ..product import FileIndex, ResolveResult
from .binding import bind, external_symbol
from .callsite import observe_arg_types
from .common import module_qualname
from .refs import extract_refs
from .stubs import STDLIB_STUBS, StubProvider
from .symbols import extract_symbols
from .typeres import resolve_value_types


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
        except OSError:
            return None
        try:
            return self.ingest_source(str(path), source, module_qualname(path, root))
        except SyntaxError:
            return None

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        try:
            return self.ingest_source(str(path), source, module_qualname(path, root))
        except SyntaxError:
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
        internal_roots = {fi.module.partition(".")[0] for fi in files}
        # Each module's import map doubles as its re-export table: a name imported into
        # module M is reachable as M.name, so `from pkg import X` follows pkg's __init__.
        reexports = {fi.module: fi.imports for fi in files}
        edges: list[Edge] = []
        externals: dict[str, Symbol] = {}
        for fi in files:
            # First definition wins when a name has same-qualname siblings (``#N``),
            # so binding is deterministic regardless of symbol order.
            module_defs: dict[str, str] = {}
            for s in fi.symbols:
                if s.parent == fi.module:
                    module_defs.setdefault(s.name, s.id)
            for ref in fi.refs:
                if ref.local_root:
                    continue  # value receiver — the type resolvers own it, not syntactic binding
                bound = bind(ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports)
                if bound is None:
                    continue
                dst, is_external = bound
                if is_external:
                    externals.setdefault(dst, external_symbol(dst))
                edges.append(Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at))
        # Value-typed pass: self.m() and annotated `u: User; u.m()` -> MEDIUM/possible edges,
        # plus stub-resolved members on external types (`p: Path; p.exists()`) as MEDIUM STUB
        # edges to external nodes. Runs after syntactic binding, so cross-file INHERITS present.
        value_edges, stub_externals = resolve_value_types(
            files, edges, reexports, internal_roots, self._stubs
        )
        edges.extend(value_edges)
        for ext in stub_externals:
            externals.setdefault(ext.id, ext)
        # Call-site observations: `g(User())` -> a definite `g RECEIVES_ARG User` edge. A
        # usage fact, independent of the dispatch passes above (no edges needed as input).
        by_id = {sym.id: sym for fi in files for sym in fi.symbols}
        edges.extend(observe_arg_types(files, all_ids, internal_roots, reexports, by_id))
        return ResolveResult(edges=edges, externals=list(externals.values()))
