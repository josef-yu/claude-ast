"""The ranker — confidence-weighted PageRank over the symbol graph.

Importance flows along reference edges toward the referenced symbol, so a
widely-used function or base class floats up. Each edge contributes in
proportion to its **confidence**, so a `definite` reference counts for more than
a `heuristic` guess — the ranking stays stable instead of being inflated by
low-confidence noise. An optional ``focus`` seeds a personalized teleport
distribution, biasing the ranking toward that symbol's neighbourhood.
"""

from __future__ import annotations

from ..model import Confidence, Graph, SymbolId

_WEIGHT = {Confidence.HIGH: 1.0, Confidence.MEDIUM: 0.5, Confidence.LOW: 0.2}


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
    adjacency: dict[SymbolId, list[tuple[SymbolId, float]]] = {}
    out_weight: dict[SymbolId, float] = {}
    for i in ids:
        pairs = [(e.dst, _WEIGHT[e.resolution.confidence]) for e in graph.out_edges(i)]
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
    if focus:
        seed = {
            s.id for s in graph.symbols() if s.id == focus or s.id.startswith(f"{focus}.")
        } & set(ids)
        if seed:
            return {i: (1.0 / len(seed) if i in seed else 0.0) for i in ids}
    return dict.fromkeys(ids, 1.0 / n)
