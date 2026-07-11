"""Ingester — parse Python source with the stdlib ``ast`` into the normalized model.

Produces symbols and *syntactic* edges (imports, definitions, direct calls) —
the always-on, high-confidence base. This is the only expensive traversal in the
system; everything downstream is cheap in-memory work.  [P1]
"""
