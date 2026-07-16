"""The Index — the engine orchestrator.

Owns the in-memory Graph and answers queries. Built from a project ingest,
warm-started from a SQLite snapshot so unchanged files are never reparsed. The
facade the CLI drives today and the MCP server will wrap later. Language-neutral —
it speaks the Indexer protocol and the model, never ``ast``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path

from .ingest import Indexer, default_indexers, ingest_project
from .ingest.product import CachedFile, FileIndex
from .model import Confidence, EdgeKind, Graph
from .query import (
    DEFAULT_MIN_CONFIDENCE,
    Definition,
    OutlineEntry,
    Reference,
    RepoMapEntry,
    ResolutionMetrics,
    find_callers,
    find_definition,
    find_dependencies,
    find_importers,
    find_references,
    outline,
    repo_map,
    resolution_metrics,
)
from .store import SqliteStore


def store_path(root: Path) -> Path:
    """Where the snapshot lives: in-repo by default, or keyed under CLAUDE_AST_CACHE_DIR."""
    override = os.environ.get("CLAUDE_AST_CACHE_DIR")
    if override:
        key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        return Path(override) / key / "index.db"
    return root / ".claude-ast" / "index.db"


def _assemble(
    files: Sequence[FileIndex], backends: Sequence[Indexer]
) -> tuple[Graph, int]:
    """Build the in-memory graph from per-file products: symbols first, then each backend's edges.

    The language-neutral assembly shared by the one-shot ``Index.build`` and the long-lived
    ``IndexSession``. Edges are rebuilt from the persisted products on every assembly — the
    reason warm == cold, and the reason a session patch resolves *globally* (a new file can
    newly-bind references in files that did not themselves change).

    Returns the graph and the raw-reference count (the coverage denominator); the metrics
    summary itself is derived lazily by ``Index.metrics``, off the serving and patch paths.
    """
    graph = Graph()
    for backend in backends:
        backend_files = [fi for fi in files if Path(fi.path).suffix in backend.extensions]
        # Cross-file finalization (e.g. globally-unique ids) before symbols are added or refs
        # resolved — the products the graph and the resolver both see must be the finalized ones.
        backend_files = backend.finalize(backend_files)
        for fi in backend_files:
            for symbol in fi.symbols:
                graph.add_symbol(symbol)
        resolved = backend.resolve(backend_files)
        for external in resolved.externals:
            graph.add_external(external)
        for edge in resolved.edges:
            graph.add_edge(edge)
    # IMPORT refs are module-dependency edges, not the reference-binding the coverage metric
    # measures, so they're excluded from the denominator (as their edges are from the numerator).
    total_refs = sum(1 for fi in files for r in fi.refs if r.kind is not EdgeKind.IMPORT)
    return graph, total_refs


class Index:
    def __init__(
        self,
        graph: Graph,
        root: Path,
        skipped: Sequence[str] = (),
        total_refs: int = 0,
    ) -> None:
        self.graph = graph
        self.root = root
        self.skipped = list(skipped)  # paths that couldn't be read/parsed this build
        self._total_refs = total_refs  # coverage denominator; metrics is derived from it lazily

    @cached_property
    def metrics(self) -> ResolutionMetrics:
        """Coverage + confidence/source distribution — computed on first access, not at build.

        A dev/reporting diagnostic (``claude-ast index``, the calibration harness): it walks
        every edge, so deriving it lazily keeps it off the query and watcher-patch hot paths,
        which never read it. Cached per ``Index`` — a patch builds a fresh ``Index``, so a new
        view recomputes against its own graph.
        """
        return resolution_metrics(self._total_refs, self.graph)

    @classmethod
    def build(
        cls,
        root: Path,
        indexers: Sequence[Indexer] | None = None,
        use_store: bool = True,
    ) -> Index:
        """Ingest a project (warm-started from the snapshot) and assemble its Graph.

        Unchanged files are reused from the snapshot; only fresh parses are
        re-persisted and deleted files pruned. Symbols are added neutrally; edges
        come from each backend's own (backend-scoped) ``resolve``.
        """
        root = root.resolve()  # normalize so cache keys are consistent across spellings
        backends = tuple(indexers) if indexers is not None else default_indexers()
        store = SqliteStore(store_path(root)) if use_store else None
        try:
            cache = store.load() if store is not None else {}
            result = ingest_project(root, backends, cache=cache)
            if store is not None:
                for path, cached in result.fresh.items():
                    store.upsert(path, cached.stamp, cached.file)
                store.delete(set(cache) - result.present)
        finally:
            if store is not None:
                store.close()  # commit + release even if ingest raised

        graph, total_refs = _assemble(result.files, backends)
        return cls(graph, root, skipped=result.skipped, total_refs=total_refs)

    def find_definition(self, name: str) -> list[Definition]:
        return find_definition(self.graph, name)

    def outline(self, module: str, focus: str | None = None) -> list[OutlineEntry]:
        return outline(self.graph, module, focus)

    def find_callers(
        self, symbol: str, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
    ) -> list[Reference]:
        return find_callers(self.graph, symbol, min_confidence)

    def find_references(
        self, symbol: str, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
    ) -> list[Reference]:
        return find_references(self.graph, symbol, min_confidence)

    def find_importers(self, module: str) -> list[Reference]:
        return find_importers(self.graph, module)

    def find_dependencies(
        self, symbol: str, min_confidence: Confidence = DEFAULT_MIN_CONFIDENCE
    ) -> list[Reference]:
        return find_dependencies(self.graph, symbol, min_confidence)

    def repo_map(self, budget: int = 2000, focus: str | None = None) -> list[RepoMapEntry]:
        return repo_map(self.graph, budget, focus)


class IndexSession:
    """A long-lived, patchable index — the live view the MCP server serves and the watcher feeds.

    Holds the parsed per-file products in memory and rebuilds the graph on demand. ``patch``
    re-ingests changed files (unchanged ones reused by their ``(mtime, size)`` stamp) and
    atomically swaps in a fresh ``Index``. Single-writer (the watcher / patch caller),
    many-readers (query handlers read ``current``): the swap is a plain attribute assignment —
    atomic under the GIL — so a reader always sees a fully-built index, never a half-patched one.

    Because assembly re-resolves every edge (``_assemble``), a patch is *globally* correct: adding
    a file can newly-bind references that live in files which did not themselves change. It is
    warm-seeded once from the snapshot; edits during the session are held in memory (persisting
    them back is a later refinement — a restart re-parses only what the snapshot shows as changed).
    """

    __slots__ = ("root", "current", "_backends", "_cache")

    def __init__(
        self, root: Path, indexers: Sequence[Indexer] | None = None, use_store: bool = True
    ) -> None:
        self.root = root.resolve()
        self._backends = tuple(indexers) if indexers is not None else default_indexers()
        self._cache: dict[str, CachedFile] = {}
        if use_store:
            store = SqliteStore(store_path(self.root))
            try:
                self._cache = store.load()  # warm seed; the initial patch re-parses what changed
            finally:
                store.close()
        self.current = self._rebuild()

    def _rebuild(self) -> Index:
        result = ingest_project(self.root, self._backends, cache=self._cache)
        # keep the in-memory cache in step: drop deletions, fold in fresh parses.
        self._cache = {p: c for p, c in self._cache.items() if p in result.present}
        self._cache.update(result.fresh)
        graph, total_refs = _assemble(result.files, self._backends)
        return Index(graph, self.root, skipped=result.skipped, total_refs=total_refs)

    def patch(self) -> Index:
        """Re-ingest changed files and atomically swap in a fresh index; returns the new view."""
        self.current = self._rebuild()
        return self.current
