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

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

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


def iter_source_files(
    root: Path,
    extensions: frozenset[str],
    exclude: frozenset[str] = DEFAULT_EXCLUDE,
) -> Iterator[Path]:
    """Yield every file under ``root`` with a claimed extension, avoiding excluded dirs."""
    for ext in extensions:
        for path in root.rglob(f"*{ext}"):
            if any(part in exclude for part in path.relative_to(root).parts):
                continue
            yield path
