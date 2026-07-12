"""The in-memory index: symbols keyed by id, with forward and reverse adjacency.

Single source of truth for queries and ranking. Built for the concurrency
invariant — single writer, many readers: the watcher builds the next graph and
swaps it in atomically, so readers holding a reference always see a consistent
view and never a half-patched one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from .core import Edge, EdgeKind, Symbol, SymbolId


class Graph:
    """Symbols + directed edges, with O(1) forward/reverse neighbour lookup.

    Forward edges answer "what does X use?" (dependencies); reverse edges answer
    "who uses X?" (callers/references). Both are needed — reverse edges are also
    what lets incremental invalidation find edges pointing *into* a changed file.
    """

    __slots__ = ("_symbols", "_out", "_in", "_by_file", "_by_name")

    def __init__(self) -> None:
        self._symbols: dict[SymbolId, Symbol] = {}
        self._out: dict[SymbolId, list[Edge]] = defaultdict(list)
        self._in: dict[SymbolId, list[Edge]] = defaultdict(list)
        self._by_file: dict[str, list[SymbolId]] = defaultdict(list)
        self._by_name: dict[str, list[SymbolId]] = defaultdict(list)

    # --- mutation (single writer) ---

    def add_symbol(self, sym: Symbol) -> None:
        self._symbols[sym.id] = sym
        self._by_file[sym.span.file].append(sym.id)
        self._by_name[sym.name].append(sym.id)

    def add_edge(self, edge: Edge) -> None:
        self._out[edge.src].append(edge)
        self._in[edge.dst].append(edge)

    # --- lookup (many readers) ---

    def symbol(self, sid: SymbolId) -> Symbol | None:
        return self._symbols.get(sid)

    def symbols(self) -> Iterator[Symbol]:
        return iter(self._symbols.values())

    def symbols_in_file(self, file: str) -> list[Symbol]:
        return [self._symbols[s] for s in self._by_file.get(file, ())]

    def by_name(self, name: str) -> list[Symbol]:
        """All symbols sharing a bare name — the basis for find_definition('User')."""
        return [self._symbols[s] for s in self._by_name.get(name, ())]

    def out_edges(self, sid: SymbolId, kind: EdgeKind | None = None) -> list[Edge]:
        """Outbound edges — the basis for find_dependencies."""
        edges = self._out.get(sid, ())
        return [e for e in edges if kind is None or e.kind is kind]

    def in_edges(self, sid: SymbolId, kind: EdgeKind | None = None) -> list[Edge]:
        """Inbound edges — the basis for find_callers / find_references."""
        edges = self._in.get(sid, ())
        return [e for e in edges if kind is None or e.kind is kind]

    def __len__(self) -> int:
        return len(self._symbols)
