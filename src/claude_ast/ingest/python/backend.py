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
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return self.ingest_text(path, root, source)

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        try:
            return self.ingest_source(str(path), source, module_qualname(path, root))
        except SyntaxError:
            return None

    def ingest_source(self, path: str, source: str, module: str) -> FileIndex:
        """Parse one module's source into a FileIndex. Raises ``SyntaxError`` on bad input."""
        tree = ast.parse(source, filename=path)
        symbols = extract_symbols(tree, module, path)
        refs, imports = extract_refs(tree, module, path)
        return FileIndex(path=path, module=module, symbols=symbols, refs=refs, imports=imports)

    def resolve(self, files: Sequence[FileIndex]) -> Iterable[Edge]:
        """Bind raw references to symbol ids, emitting SYNTACTIC/high-confidence edges.

        Resolves each reference against the same module's top-level definitions,
        then its imports (to a cross-file target that exists in the index).
        Unresolved names — builtins, third-party, dynamic — yield no edge.
        """
        all_ids = {sym.id for fi in files for sym in fi.symbols}
        for fi in files:
            module_defs = {s.name: s.id for s in fi.symbols if s.parent == fi.module}
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
