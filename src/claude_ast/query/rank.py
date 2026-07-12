"""The ranker — confidence-weighted PageRank over the symbol graph.

Importance flows along reference edges toward the referenced symbol, so a
widely-used function or base class floats up. Each edge contributes in
proportion to its **confidence**, so a `definite` reference counts for more than
a `heuristic` guess — the ranking stays stable instead of being inflated by
low-confidence noise. An optional ``focus`` seeds a personalized teleport
distribution, biasing the ranking toward that symbol's neighbourhood.
"""

from __future__ import annotations

from ..model import Confidence, EdgeKind, Graph, SymbolId

_WEIGHT = {Confidence.HIGH: 1.0, Confidence.MEDIUM: 0.5, Confidence.LOW: 0.2}

# Only genuine reference edges flow importance. Containment is structure (a module
# does not "use" the functions it holds) and imports are module-level plumbing, so
# both are excluded — otherwise a container floats up merely for what it contains.
# The set is explicit, not "all kinds", so a future resolver adding IMPORT/CONTAINS
# edges opts into ranking deliberately rather than silently distorting it.
_RANK_KINDS = frozenset({EdgeKind.CALL, EdgeKind.REFERENCE, EdgeKind.INHERITS})


def pagerank(
    graph: Graph,
    focus: str | None = None,
    *,
    damping: float = 0.85,
    iterations: int = 40,
) -> dict[SymbolId, float]:
    ids = [sym.id for sym in graph.symbols()]
    n = len(ids)
    if n == 0:
        return {}

    teleport = _teleport(graph, ids, focus)
    internal = set(ids)  # externals are excluded from graph.symbols(), so not ranked
    adjacency: dict[SymbolId, list[tuple[SymbolId, float]]] = {}
    out_weight: dict[SymbolId, float] = {}
    for i in ids:
        # Edges to EXTERNAL sinks carry no importance (and aren't ranked nodes), so
        # they neither flow rank nor count toward this node's out-weight.
        pairs = [
            (e.dst, _WEIGHT[e.resolution.confidence])
            for e in graph.out_edges(i)
            if e.kind in _RANK_KINDS and e.dst in internal
        ]
        adjacency[i] = pairs
        out_weight[i] = sum(w for _, w in pairs)

    rank = dict.fromkeys(ids, 1.0 / n)
    for _ in range(iterations):
        nxt = {i: (1.0 - damping) * teleport[i] for i in ids}
        # dangling nodes (no outbound weight) redistribute their mass via teleport
        dangling = damping * sum(rank[i] for i in ids if out_weight[i] == 0.0)
        for i in ids:
            nxt[i] += dangling * teleport[i]
            if out_weight[i] > 0.0:
                share = damping * rank[i] / out_weight[i]
                for dst, w in adjacency[i]:
                    nxt[dst] += share * w
        rank = nxt
    return rank


def _teleport(graph: Graph, ids: list[SymbolId], focus: str | None) -> dict[SymbolId, float]:
    n = len(ids)
    if focus and graph.symbol(focus) is not None:
        # The focus symbol and everything nested under it (a class's methods, a
        # module's members) — walked via the tree, never matched on id text.
        seed = set(_subtree_ids(graph, focus)) & set(ids)
        if seed:
            return {i: (1.0 / len(seed) if i in seed else 0.0) for i in ids}
    return dict.fromkeys(ids, 1.0 / n)


def _subtree_ids(graph: Graph, root: SymbolId) -> list[SymbolId]:
    out: list[SymbolId] = []
    stack = [root]
    while stack:
        sid = stack.pop()
        out.append(sid)
        stack.extend(child.id for child in graph.children(sid))
    return out
