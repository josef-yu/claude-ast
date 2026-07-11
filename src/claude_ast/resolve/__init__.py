"""Resolvers — the config-driven registry that refines syntactic edges with types.

An ordered, enable/disable/reorderable pipeline: annotation -> local inference ->
stub -> heuristic -> call-site. Each edge it emits carries a
``Resolution(source, confidence)``. External engines (Pyright/Serena/SCIP) can
slot in later as additional resolvers with no rework.  [P2]
"""
