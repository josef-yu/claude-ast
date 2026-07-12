"""PythonIndexer — the Python language backend.

Ties symbol + reference extraction into per-file ``FileIndex`` products, and
binds raw references into resolved edges. All binding is **backend-scoped**: it
only ever resolves against this backend's own files, and symbol ids are already
namespaced by module path, so a second language could never cross-bind.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from pathlib import Path

from ...model import Edge, Resolution
from ..product import FileIndex
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

    def resolve(self, files: Sequence[FileIndex]) -> Iterable[Edge]:
        """Bind raw references to symbol ids, emitting SYNTACTIC/high-confidence edges.

        Resolves each reference against the same module's top-level definitions,
        then its imports (to a cross-file target that exists in the index).
        Unresolved names — builtins, third-party, dynamic — yield no edge.
        """
        all_ids = {sym.id for fi in files for sym in fi.symbols}
        for fi in files:
            # First definition wins when a name has same-qualname siblings (``#N``),
            # so binding is deterministic regardless of symbol order.
            module_defs: dict[str, str] = {}
            for s in fi.symbols:
                if s.parent == fi.module:
                    module_defs.setdefault(s.name, s.id)
            for ref in fi.refs:
                dst = _bind(ref.name, module_defs, fi.imports, all_ids)
                if dst is not None:
                    yield Edge(ref.src, dst, ref.kind, Resolution.syntactic(), ref.at)


def _bind(
    name: str, module_defs: dict[str, str], imports: dict[str, str], all_ids: set[str]
) -> str | None:
    if name in module_defs:
        return module_defs[name]
    if name in imports:
        target = imports[name]
        return target if target in all_ids else None
    return None
