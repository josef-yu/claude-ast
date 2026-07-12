"""Project ingestion — walk the tree and dispatch each file to a language backend.

Language-neutral: it knows only the ``Indexer`` protocol, never a concrete
language. Today the default backend list is ``(PythonIndexer(),)``; a second
language is added by passing another backend, no registry required.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .base import DEFAULT_EXCLUDE, Indexer, iter_source_files
from .product import FileIndex, ProjectIngest
from .python import PythonIndexer


def default_indexers() -> tuple[Indexer, ...]:
    """The backends enabled by default. Grows here (not via discovery) per language."""
    return (PythonIndexer(),)


def ingest_project(
    root: Path,
    indexers: Sequence[Indexer] | None = None,
    exclude: frozenset[str] = DEFAULT_EXCLUDE,
) -> ProjectIngest:
    """Ingest every claimed source file under ``root`` via the given backends."""
    backends = tuple(indexers) if indexers is not None else default_indexers()
    extensions = frozenset[str]().union(*(ix.extensions for ix in backends))

    files: list[FileIndex] = []
    skipped: list[str] = []
    for path in iter_source_files(root, extensions, exclude):
        backend = _dispatch(backends, path)
        if backend is None:
            continue
        fi = backend.ingest_file(path, root)
        if fi is None:
            skipped.append(str(path))
        else:
            files.append(fi)
    return ProjectIngest(files=files, skipped=skipped)


def _dispatch(backends: Sequence[Indexer], path: Path) -> Indexer | None:
    for backend in backends:
        if path.suffix in backend.extensions:
            return backend
    return None
