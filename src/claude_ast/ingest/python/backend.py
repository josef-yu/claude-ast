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
        then its imports. An import to a target that exists in the index becomes an
        in-tree edge; an import to a target that does not (stdlib, third-party) is
        *not dropped* — it becomes a ``definite`` edge to an EXTERNAL node. The
        reference is genuinely there; only P2's attribute/type work is uncertain, so
        a direct import edge is honestly definite. Bare names with no import
        (builtins, dynamic) still yield nothing.
        """
        all_ids = {sym.id for fi in files for sym in fi.symbols}
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
                bound = _bind(ref.name, module_defs, fi.imports, all_ids)
                if bound is None:
                    continue
                dst, is_external = bound
                if is_external:
                    externals.setdefault(dst, _external_symbol(dst))
                edges.append(Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at))
        return ResolveResult(edges=edges, externals=list(externals.values()))


def _bind(
    name: str, module_defs: dict[str, str], imports: dict[str, str], all_ids: set[str]
) -> tuple[str, bool] | None:
    """Resolve a reference name to ``(target_id, is_external)`` or ``None``.

    ``is_external`` is True when an import points outside the indexed project — the
    signal to mint an EXTERNAL node rather than an in-tree edge.
    """
    if name in module_defs:
        return module_defs[name], False
    if name in imports:
        target = imports[name]
        return target, target not in all_ids
    return None


def _external_symbol(qualname: str) -> Symbol:
    """A leaf node for a library/stdlib target: an edge sink with no in-tree source.

    The id is the imported qualname — versionless, because one Python environment
    resolves each package to a single version (unlike npm). A JS/TS backend is free
    to mint a richer external id; the neutral layer treats it as opaque.
    """
    name = qualname.rsplit(".", 1)[-1]
    return Symbol(qualname, name, SymbolKind.EXTERNAL, Span("<external>", 0))
