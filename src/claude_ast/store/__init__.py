"""Store — the persistence seam.

The in-memory ``Graph`` is the source of truth for queries and ranking; a Store
is the snapshot behind it (warm restart + per-file incremental). Behind a
``Store`` protocol so Postgres stays a later swap, never a rewrite.  [P1]
"""

from .base import Store
from .sqlite import SqliteStore

__all__ = ["SqliteStore", "Store"]
