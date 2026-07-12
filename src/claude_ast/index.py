"""The Index — the engine orchestrator (seed).

Owns the in-memory Graph and answers queries. Built from a project ingest; the
Store, resolver stack, and watcher hang off this as they land. This is the facade
the CLI drives today and the MCP server will wrap later. It is language-neutral —
it speaks the Indexer protocol and the model, never ``ast``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .ingest import Indexer, default_indexers, ingest_project
from .model import Graph
from .query import (
    Definition,
    OutlineEntry,
    Reference,
    RepoMapEntry,
    find_callers,
    find_definition,
    find_dependencies,
    find_references,
    outline,
    repo_map,
)


class Index:
    def __init__(self, graph: Graph, root: Path) -> None:
        self.graph = graph
        self.root = root

    @classmethod
    def build(cls, root: Path, indexers: Sequence[Indexer] | None = None) -> Index:
        """Ingest a project and assemble its Graph — symbols, then edges.

        Symbols are added neutrally; edges come from each backend's own
        (backend-scoped) ``resolve``. P2's resolver stack extends the edges with
        type-dependent, confidence-graded ones.
        """
        backends = tuple(indexers) if indexers is not None else default_indexers()
        result = ingest_project(root, backends)

        graph = Graph()
        for file_index in result.files:
            for symbol in file_index.symbols:
                graph.add_symbol(symbol)
        for backend in backends:
            backend_files = [
                fi for fi in result.files if Path(fi.path).suffix in backend.extensions
            ]
            for edge in backend.resolve(backend_files):
                graph.add_edge(edge)
        return cls(graph, root)

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
