"""The Index — the engine orchestrator (seed).

Owns the in-memory Graph and answers queries. Built from a project ingest; the
Store, resolver stack, and watcher hang off this as they land. This is the facade
the CLI drives today and the MCP server will wrap later. It is language-neutral —
it speaks the Indexer protocol and the model, never ``ast``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .ingest import Indexer, ingest_project
from .model import Graph
from .query import Definition, OutlineEntry, find_definition, outline


class Index:
    def __init__(self, graph: Graph, root: Path) -> None:
        self.graph = graph
        self.root = root

    @classmethod
    def build(cls, root: Path, indexers: Sequence[Indexer] | None = None) -> Index:
        """Ingest a project and assemble its Graph.

        This increment adds symbols only; the reference/edge layer (and the
        resolver stack) populate edges next.
        """
        result = ingest_project(root, indexers)
        graph = Graph()
        for file_index in result.files:
            for symbol in file_index.symbols:
                graph.add_symbol(symbol)
        return cls(graph, root)

    def find_definition(self, name: str) -> list[Definition]:
        return find_definition(self.graph, name)

    def outline(self, module: str) -> list[OutlineEntry]:
        return outline(self.graph, module)
