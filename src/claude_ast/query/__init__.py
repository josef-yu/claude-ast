"""Query engine + ranker.

find_definition / find_references / find_callers / find_dependencies / impact_of,
plus repo_map (signatures + docstring lines + confidence-weighted PageRank +
token budget). All of it is rank-and-render over the same normalized model —
orientation is not a separate subsystem, it's the graph, ranked.  [P1/P2]
"""

from .lookup import Definition, OutlineEntry, find_definition, outline
from .relations import Reference, find_callers, find_dependencies, find_references

__all__ = [
    "Definition",
    "OutlineEntry",
    "Reference",
    "find_callers",
    "find_definition",
    "find_dependencies",
    "find_references",
    "outline",
]
