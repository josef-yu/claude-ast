"""Resolution metrics — coverage + confidence/source distribution over the graph.

Derived in-process from the resolved graph and the raw-reference count: how many
reference sites bound to an edge (coverage), the definite/possible split, and each
``ResolutionSource``'s contribution. This is the measurement loop for the resolver
stack — "report, don't rule" applied to the dev process: you can't tune resolvers you
can't measure. A pure read over model primitives — no I/O, no ``ast``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..model import EdgeKind, Graph


@dataclass(frozen=True, slots=True)
class ResolutionMetrics:
    total_refs: int  # every captured reference site — the honest denominator
    bound_refs: int  # reference sites that produced at least one edge
    by_confidence: dict[str, int]  # confidence value (high/medium/low) -> edge count
    by_source: dict[str, int]  # ResolutionSource value -> edge count

    @property
    def coverage(self) -> float:
        """Fraction of reference sites that resolved to an edge (0..1)."""
        return self.bound_refs / self.total_refs if self.total_refs else 0.0


def resolution_metrics(total_refs: int, graph: Graph) -> ResolutionMetrics:
    """Summarize the resolved graph against the ``total_refs`` reference sites captured.

    ``bound_refs`` counts distinct call sites (``src, kind, span``), so heuristic
    multiplicity — many candidate edges for one site — collapses to one bound site and
    coverage stays within 0..1 even when the resolver stack fans out.

    ``RECEIVES_ARG`` edges are skipped: they are call-site *observations* derived from an
    already-counted ``CALL`` ref's arguments, not bindings of a reference of their own, so
    counting their sites would inflate ``bound_refs`` past ``total_refs``. This metric
    measures reference-binding coverage; the observed-type layer is a separate capability.
    """
    by_confidence: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    sites: set[tuple[object, ...]] = set()
    for sym in graph.symbols():
        for e in graph.out_edges(sym.id):
            if e.kind is EdgeKind.RECEIVES_ARG:
                continue
            by_confidence[e.resolution.confidence.value] += 1
            by_source[e.resolution.source.value] += 1
            at = e.at
            site = (e.src, e.kind, at.file, at.line, at.col) if at is not None else (e.src, e.kind)
            sites.add(site)
    return ResolutionMetrics(
        total_refs=total_refs,
        bound_refs=len(sites),
        by_confidence=dict(by_confidence),
        by_source=dict(by_source),
    )
