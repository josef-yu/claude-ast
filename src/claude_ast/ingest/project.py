"""Project ingestion — walk the tree and dispatch each file to a language backend.

Language-neutral: it knows only the ``Indexer`` protocol, never a concrete
language. With a ``cache`` of previously-parsed files it does the incremental
work — a file whose (mtime, size) stamp is unchanged is reused without reparsing,
which is the whole point of the snapshot. It reports what it freshly parsed
(``fresh``, to persist) and every path currently present (``present``, to prune
deletions).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .base import DEFAULT_EXCLUDE, Indexer, iter_source_files
from .product import CachedFile, FileIndex, ProjectIngest
from .python import PythonIndexer


def default_indexers() -> tuple[Indexer, ...]:
    """The backends enabled by default. Grows here (not via discovery) per language."""
    return (PythonIndexer(),)


def ingest_project(
    root: Path,
    indexers: Sequence[Indexer] | None = None,
    exclude: frozenset[str] = DEFAULT_EXCLUDE,
    cache: dict[str, CachedFile] | None = None,
) -> ProjectIngest:
    """Ingest every claimed source file under ``root``, reusing unchanged files from ``cache``."""
    backends = tuple(indexers) if indexers is not None else default_indexers()
    extensions = frozenset[str]().union(*(ix.extensions for ix in backends))
    cache = cache or {}

    files: list[FileIndex] = []
    skipped: list[str] = []
    fresh: dict[str, CachedFile] = {}
    present: set[str] = set()

    for path in iter_source_files(root, extensions, exclude):
        sp = str(path)
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished between walk and stat
        present.add(sp)
        stamp = (stat.st_mtime_ns, stat.st_size)

        cached = cache.get(sp)
        if cached is not None and cached.stamp == stamp:
            files.append(cached.file)  # unchanged — reuse, no reparse
            continue

        backend = _dispatch(backends, path)
        if backend is None:
            continue
        fi = backend.ingest_file(path, root)
        if fi is None:
            skipped.append(sp)
            continue
        files.append(fi)
        fresh[sp] = CachedFile(stamp, fi)

    return ProjectIngest(files=files, skipped=skipped, fresh=fresh, present=present)


def _dispatch(backends: Sequence[Indexer], path: Path) -> Indexer | None:
    for backend in backends:
        if path.suffix in backend.extensions:
            return backend
    return None
