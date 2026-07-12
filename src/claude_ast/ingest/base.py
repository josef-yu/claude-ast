"""The language-backend seam and neutral file discovery.

An ``Indexer`` is a *language backend*: it turns source files into normalized
``FileIndex`` objects. This is the seam that keeps the engine language-neutral —
the store, query, and watch layers depend only on this protocol and the model,
never on ``ast``.

One implementation exists today (``PythonIndexer``). When a real second language
lands, add another backend and pass it in; the orchestrator already dispatches
files to whichever backend claims their extension. Deliberately **no** registry,
discovery, or plugin-loading machinery until then — that abstraction is validated
against the second implementation, not invented before the first.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from ..model import Edge
from .product import FileIndex

DEFAULT_EXCLUDE: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".git",
        ".claude-ast",
        "build",
        "dist",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "node_modules",
    }
)


class Indexer(Protocol):
    """A language backend producing FileIndexes for the files it claims."""

    name: str
    extensions: frozenset[str]  # file suffixes this backend handles, e.g. {".py"}

    def ingest_file(self, path: Path, root: Path) -> FileIndex | None:
        """Read + parse a file. Returns None on read/decode/parse failure (skipped)."""
        ...

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        """Parse in-memory source (the watcher path, source already in hand)."""
        ...

    def resolve(self, files: Sequence[FileIndex]) -> Iterable[Edge]:
        """Bind this backend's raw references into resolved edges.

        Backend-scoped: it only sees its own files and only produces edges
        between its own (namespaced) symbol ids, so backends never cross-bind.
        """
        ...


def iter_source_files(
    root: Path,
    extensions: frozenset[str],
    exclude: frozenset[str] = DEFAULT_EXCLUDE,
) -> Iterator[Path]:
    """Yield every file under ``root`` with a claimed extension, avoiding excluded dirs.

    Discovery is sorted (extensions, then paths) so symbol-insertion order — and
    therefore PageRank's float-summation order and repo_map's tie-breaks — is
    identical across machines and filesystems. ``rglob`` alone yields raw
    ``os.scandir`` order, which is platform-dependent; the ``deterministic &
    local`` guarantee is earned here, at the one place ordering originates.
    """
    for ext in sorted(extensions):
        for path in sorted(root.rglob(f"*{ext}")):
            if any(part in exclude for part in path.relative_to(root).parts):
                continue
            yield path
