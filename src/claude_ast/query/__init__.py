"""Query engine + ranker.

find_definition / find_references / find_callers / find_dependencies, plus
repo_map (signatures + docstring lines + confidence-weighted PageRank + token
budget). All of it is rank-and-render over the same normalized model —
orientation is not a separate subsystem, it's the graph, ranked.  [P1/P2]
"""

from .lookup import (
    Definition,
    OutlineEntry,
    SymbolLookup,
    find_definition,
    lookup_symbol,
    outline,
)
from .metrics import ResolutionMetrics, resolution_metrics
from .rank import pagerank
from .relations import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_REASSIGNMENTS,
    ReassignMode,
    Reference,
    Suppressed,
    find_callers,
    find_dependencies,
    find_importers,
    find_references,
    suppression,
)
from .repomap import RepoMapEntry, render_repo_map, repo_map

__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_REASSIGNMENTS",
    "Definition",
    "OutlineEntry",
    "ReassignMode",
    "Reference",
    "RepoMapEntry",
    "ResolutionMetrics",
    "Suppressed",
    "SymbolLookup",
    "find_callers",
    "find_definition",
    "find_dependencies",
    "find_importers",
    "find_references",
    "lookup_symbol",
    "outline",
    "pagerank",
    "render_repo_map",
    "repo_map",
    "resolution_metrics",
    "suppression",
]
