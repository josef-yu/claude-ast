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

from ..product import FileIndex, ResolveResult
from .common import module_qualname
from .finalize import ensure_unique_ids
from .refs import extract_refs
from .resolver import IncrementalResolver
from .stubs import STDLIB_STUBS, StubProvider
from .symbols import extract_symbols

logger = logging.getLogger(__name__)


class PythonIndexer:
    """Language backend for Python source."""

    name = "python"
    extensions = frozenset({".py"})

    def __init__(self, stubs: StubProvider = STDLIB_STUBS) -> None:
        # The stub provider is injected here (not a global) so a future environment-aware
        # provider — which needs construction-time config — slots in without touching resolve.
        # The resolver is stateful: a long-lived (session) backend reuses per-file edges across
        # patches; a one-shot (Index.build) backend just does one full resolve.
        self._stubs = stubs
        self._resolver = IncrementalResolver(stubs)

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

    def finalize(self, files: Sequence[FileIndex]) -> list[FileIndex]:
        """Make symbol ids globally unique before assembly (per-file ``#N`` extended cross-file)."""
        return ensure_unique_ids(files)

    def resolve(self, files: Sequence[FileIndex]) -> ResolveResult:
        """Bind raw references to symbol ids, emitting SYNTACTIC/high-confidence edges plus the
        value-typed / stub / call-site edges of the P2 resolver stack.

        The orchestration lives in ``resolver`` (a file whose refs bind against this backend's
        own files only, so backends never cross-bind); this is the thin backend entry point.
        Incremental across patches when this backend is long-lived (see ``IncrementalResolver``).
        """
        return self._resolver.resolve(files)
