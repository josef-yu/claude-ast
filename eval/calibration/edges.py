"""Flatten a built ``Index`` into a list of judgeable edge records.

The calibration harness scores the resolver's *output*, so it needs every edge as a
flat row carrying what both oracles ask about: the confidence tier + source (what is
being calibrated), the call-site span (how the runtime oracle joins), and the target's
kind + externality (how the static oracle and the construction/override buckets decide).

A pure read over the model — no ``ast``, no I/O beyond ``realpath`` normalization so an
edge's ``at.file`` matches a runtime frame's ``co_filename``.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass

from claude_ast.model import EdgeKind, Graph, SymbolKind


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    src: str
    dst: str
    kind: str  # EdgeKind value
    tier: str  # definite | possible
    source: str  # ResolutionSource value
    confidence: str  # high | medium | low
    file: str | None  # realpath of the site (at.file), for the runtime join
    line: int | None
    external: bool  # target is a library/stdlib node
    dst_kind: str | None  # SymbolKind of the target, when in-tree/known


def enumerate_edges(graph: Graph) -> list[EdgeRecord]:
    """Every out-edge of every in-tree symbol as an :class:`EdgeRecord`."""
    records: list[EdgeRecord] = []
    for sym in graph.symbols():
        for e in graph.out_edges(sym.id):
            target = graph.symbol(e.dst)
            records.append(
                EdgeRecord(
                    src=e.src,
                    dst=e.dst,
                    kind=e.kind.value,
                    tier=e.resolution.confidence.tier,
                    source=e.resolution.source.value,
                    confidence=e.resolution.confidence.value,
                    file=os.path.realpath(e.at.file) if e.at is not None else None,
                    line=e.at.line if e.at is not None else None,
                    external=graph.is_external(e.dst),
                    dst_kind=target.kind.value if target is not None else None,
                )
            )
    return records


def module_ids(graph: Graph) -> frozenset[str]:
    """The ids of all MODULE symbols — the set a backend resolves a symbol's module against."""
    return frozenset(s.id for s in graph.symbols() if s.kind is SymbolKind.MODULE)


def ancestors(graph: Graph, class_id: str) -> frozenset[str]:
    """A class plus its in-tree base classes, via INHERITS out-edges (subclass -> base).

    The set that can supply an *inherited* constructor: a ``Foo()`` site dispatches to
    ``Foo.__init__`` or the nearest base's — never a subclass's — so construction
    confirmation is anchored here, not in the wider related set.
    """
    seen = {class_id}
    frontier: deque[str] = deque([class_id])
    while frontier:
        cur = frontier.popleft()
        for e in graph.out_edges(cur, EdgeKind.INHERITS):
            if e.dst not in seen:
                seen.add(e.dst)
                frontier.append(e.dst)
    return frozenset(seen)


def related_classes(graph: Graph, class_id: str) -> frozenset[str]:
    """A class plus its in-tree ancestors *and* descendants, via INHERITS adjacency.

    The set within which a same-named method is a genuine *override* (polymorphic
    dispatch to a super- or sub-class), as opposed to an unrelated name collision.
    """
    seen = set(ancestors(graph, class_id))
    frontier: deque[str] = deque([class_id])
    while frontier:  # descendants: follow INHERITS in-edges (base <- subclass)
        cur = frontier.popleft()
        for e in graph.in_edges(cur, EdgeKind.INHERITS):
            if e.src not in seen:
                seen.add(e.src)
                frontier.append(e.src)
    return frozenset(seen)
