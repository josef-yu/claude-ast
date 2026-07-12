"""The Store seam — persistence behind an interface so SQLite is swappable.

The in-memory ``Graph`` is the source of truth for queries; a Store only caches
per-file parse products (``CachedFile``) so a warm start can skip reparsing
unchanged files. One implementation today (SQLite); Postgres would be a later
swap, not a rewrite.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..ingest.product import CachedFile, FileIndex, FileStamp


class Store(Protocol):
    def load(self) -> dict[str, CachedFile]:
        """All persisted parse products, keyed by path."""
        ...

    def upsert(self, path: str, stamp: FileStamp, file: FileIndex) -> None:
        """Persist a freshly parsed file."""
        ...

    def delete(self, paths: Iterable[str]) -> None:
        """Drop files that no longer exist."""
        ...

    def close(self) -> None:
        """Commit and release the backend."""
        ...
