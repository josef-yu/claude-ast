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

from ...model import Edge, Resolution, Span, Symbol, SymbolKind
from ..product import FileIndex, ResolveResult
from .common import module_qualname
from .refs import extract_refs
from .symbols import extract_symbols
from .typeres import resolve_self


class PythonIndexer:
    """Language backend for Python source."""

    name = "python"
    extensions = frozenset({".py"})

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
                bound = _bind(ref.name, module_defs, fi.imports, all_ids, internal_roots)
                if bound is None:
                    continue
                dst, is_external = bound
                if is_external:
                    externals.setdefault(dst, _external_symbol(dst))
                edges.append(Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at))
        # Value-typed pass: self.m() -> the enclosing class's member, as MEDIUM/possible edges.
        # Runs last, so the cross-file INHERITS edges it walks are already in `edges`.
        edges.extend(resolve_self(files, edges))
        return ResolveResult(edges=edges, externals=list(externals.values()))


def _bind(
    name: str,
    module_defs: dict[str, str],
    imports: dict[str, str],
    all_ids: set[str],
    internal_roots: set[str],
) -> tuple[str, bool] | None:
    """Resolve a reference name to ``(target_id, is_external)`` or ``None``.

    Handles a bare name and an attribute chain (``os.path.join``): the root is bound
    via the module's own defs or its imports, then the trailing attribute path is
    appended and classified.
    """
    if name in module_defs:
        return _classify(module_defs[name], all_ids, internal_roots)
    if name in imports:
        return _classify(imports[name], all_ids, internal_roots)
    root, _, rest = name.partition(".")
    if rest:
        base = module_defs.get(root) or imports.get(root)
        if base is not None:
            return _classify(f"{base}.{rest}", all_ids, internal_roots)
    return None


def _classify(
    target: str, all_ids: set[str], internal_roots: set[str]
) -> tuple[str, bool] | None:
    """A resolved qualname -> ``(target, is_external)``, or ``None`` to defer to P2.

    An indexed symbol is a definite in-tree edge; a target whose top package is not
    in the project is a definite external edge; a target rooted *in* the project but
    not (yet) a known symbol is a value/dynamic attribute the P2 type resolvers own —
    deferred rather than minted as a bogus external.
    """
    if target in all_ids:
        return target, False
    if target.partition(".")[0] in internal_roots:
        return None
    return target, True


def _external_symbol(qualname: str) -> Symbol:
    """A leaf node for a library/stdlib target: an edge sink with no in-tree source.

    The id is the imported qualname — versionless, because one Python environment
    resolves each package to a single version (unlike npm). A JS/TS backend is free
    to mint a richer external id; the neutral layer treats it as opaque.
    """
    name = qualname.rsplit(".", 1)[-1]
    return Symbol(qualname, name, SymbolKind.EXTERNAL, Span("<external>", 0))
