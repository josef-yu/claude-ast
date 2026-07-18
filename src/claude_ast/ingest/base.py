"""The language-backend seam and neutral file discovery.

An ``Indexer`` is a *language backend*: it turns source files into normalized
``FileIndex`` objects. This is the seam that keeps the engine language-neutral —
the store, query, and watch layers depend only on this protocol and the model,
never on ``ast``.

The seam is a ``Protocol`` whose methods are ``@abstractmethod``: a real backend
**subclasses** ``Indexer`` and so *cannot be constructed* until it implements the
whole contract (a missing method is a ``TypeError`` at instantiation, not a silent
gap discovered downstream). Structural conformance still holds for test doubles
that only need to match the shape — they don't have to inherit.

One implementation exists today (``PythonIndexer``). When a real second language
lands, add another backend and pass it in; the orchestrator already dispatches
files to whichever backend claims their extension. Deliberately **no** registry,
discovery, or plugin-loading machinery until then — that abstraction is validated
against the second implementation, not invented before the first.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

from .product import FileIndex, ResolveResult

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

    @abstractmethod
    def ingest_file(self, path: Path, root: Path) -> FileIndex | None:
        """Read + parse a file. Returns None on read/decode/parse failure (skipped)."""
        ...

    @abstractmethod
    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        """Parse in-memory source (the watcher path, source already in hand)."""
        ...

    @abstractmethod
    def finalize(self, files: Sequence[FileIndex]) -> list[FileIndex]:
        """Cross-file post-pass over *all* this backend's files, run once at assembly before
        symbols are added or refs resolved.

        **Postcondition (required):** every returned symbol carries a *globally-unique* id. Per-file
        ingest can't see cross-file clashes, so resolving them is this pass's job — and *how* is
        language-specific: Python suffixes a colliding member (a submodule ``pkg/helpers.py`` vs a
        ``class helpers`` in ``pkg/__init__``), whereas a language with declaration merging
        (TS ``interface`` + ``namespace``) or overload sets *combines* the clashing declarations
        into one symbol instead. The neutral core stays policy-free: it only *checks* the
        postcondition (``Graph.collisions()`` records any id two symbols both minted) — it never
        merges or renames on a backend's behalf.

        Must be a pure, deterministic function of ``files``; return the same objects unchanged when
        there is nothing to do (that identity is what keeps the incremental cache warm). A backend
        with no cross-file concern returns ``list(files)``.
        """
        ...

    @abstractmethod
    def resolve(self, files: Sequence[FileIndex]) -> ResolveResult:
        """Bind this backend's raw references into resolved edges plus the external
        (library/stdlib) target nodes those edges reference.

        Backend-scoped: it only sees its own files and only produces edges
        between its own (namespaced) symbol ids, so backends never cross-bind.
        References whose target is outside the indexed project become edges to
        ``EXTERNAL`` nodes the backend mints (its own id scheme) rather than being
        dropped.
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
