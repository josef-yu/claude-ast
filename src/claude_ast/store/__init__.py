"""Store — the persistence seam.

The in-memory ``Graph`` is the source of truth for queries and ranking; SQLite is
the snapshot behind it (warm restart + per-file incremental). Behind a ``Store``
interface so Postgres stays a later swap, never a rewrite. Lives at
``<root>/.claude-ast/index.db``, self-ignoring.  [P1]
"""
