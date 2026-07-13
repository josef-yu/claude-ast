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
from pathlib import Path

from .ingest import Indexer, default_indexers, ingest_project
from .model import Graph
from .query import (
    Definition,
    OutlineEntry,
    Reference,
    RepoMapEntry,
    ResolutionMetrics,
    find_callers,
    find_definition,
    find_dependencies,
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


class Index:
    def __init__(
        self,
        graph: Graph,
        root: Path,
        skipped: Sequence[str] = (),
        metrics: ResolutionMetrics | None = None,
    ) -> None:
        self.graph = graph
        self.root = root
        self.skipped = list(skipped)  # paths that couldn't be read/parsed this build
        self.metrics = metrics or ResolutionMetrics(0, 0, {}, {})

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

        graph = Graph()
        for file_index in result.files:
            for symbol in file_index.symbols:
                graph.add_symbol(symbol)
        for backend in backends:
            backend_files = [
                fi for fi in result.files if Path(fi.path).suffix in backend.extensions
            ]
            resolved = backend.resolve(backend_files)
            for external in resolved.externals:
                graph.add_external(external)
            for edge in resolved.edges:
                graph.add_edge(edge)
        total_refs = sum(len(fi.refs) for fi in result.files)
        metrics = resolution_metrics(total_refs, graph)
        return cls(graph, root, skipped=result.skipped, metrics=metrics)

    def find_definition(self, name: str) -> list[Definition]:
        return find_definition(self.graph, name)

    def outline(self, module: str) -> list[OutlineEntry]:
        return outline(self.graph, module)

    def find_callers(self, symbol: str) -> list[Reference]:
        return find_callers(self.graph, symbol)

    def find_references(self, symbol: str) -> list[Reference]:
        return find_references(self.graph, symbol)

    def find_dependencies(self, symbol: str) -> list[Reference]:
        return find_dependencies(self.graph, symbol)

    def repo_map(self, budget: int = 2000, focus: str | None = None) -> list[RepoMapEntry]:
        return repo_map(self.graph, budget, focus)
