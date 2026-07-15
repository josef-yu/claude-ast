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
    """Confidence-weighted PageRank, returned as ``symbol id -> score``.

    Int-indexed / CSR internally: nodes are numbered by their ``graph.symbols()`` order and the
    adjacency is flat ``(dst-index, weight)`` arrays, so the power iteration is list-indexed
    arithmetic instead of per-edge dict hashing — several times faster on a large graph. The
    operation order (nodes in symbol order, out-edges in ``out_edges`` order) and the fixed
    iteration count are preserved exactly, so the resulting scores are bit-identical to the
    reference dict implementation — repo_map's ``(-rank, id)`` ordering cannot shift.
    """
    ids = [sym.id for sym in graph.symbols()]
    n = len(ids)
    if n == 0:
        return {}

    index = {sid: k for k, sid in enumerate(ids)}
    teleport = _teleport(graph, ids, focus)
    tele = [teleport[sid] for sid in ids]

    # CSR adjacency: out-neighbour index + weight, sliced per node by `starts`.
    # Edges to EXTERNAL sinks carry no importance (and aren't ranked nodes), so they
    # neither flow rank nor count toward out-weight. Self-loops (a recursive call) are
    # dropped too — they would only feed a node's rank back into itself and inflate it.
    starts = [0] * (n + 1)
    dsts: list[int] = []
    weights: list[float] = []
    out_weight = [0.0] * n
    for k, sid in enumerate(ids):
        start = len(dsts)
        for e in graph.out_edges(sid):
            if e.kind in _RANK_KINDS:
                j = index.get(e.dst)
                if j is not None and j != k:  # in-tree, non-self
                    dsts.append(j)
                    weights.append(_WEIGHT[e.resolution.confidence])
        starts[k + 1] = len(dsts)
        # sum() (not an incremental +=): its compensated float summation matches the reference
        # exactly, so `share` below — and the resulting ranks — stay bit-identical.
        out_weight[k] = sum(weights[start:])

    base = [(1.0 - damping) * t for t in tele]  # the teleport term, constant across iterations
    dangling_idx = [k for k in range(n) if out_weight[k] == 0.0]
    rank = [1.0 / n] * n
    for _ in range(iterations):
        nxt = base[:]
        # dangling nodes (no outbound weight) redistribute their mass via teleport
        dangling = damping * sum(rank[k] for k in dangling_idx)
        for k in range(n):
            nxt[k] += dangling * tele[k]
            ow = out_weight[k]
            if ow > 0.0:
                share = damping * rank[k] / ow
                for p in range(starts[k], starts[k + 1]):
                    nxt[dsts[p]] += share * weights[p]
        rank = nxt
    return {sid: rank[k] for k, sid in enumerate(ids)}


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
