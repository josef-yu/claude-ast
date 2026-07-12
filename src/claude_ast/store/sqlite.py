"""SqliteStore — the persistent snapshot backing the in-memory index.

Lives at ``<root>/.claude-ast/index.db`` (self-ignoring). A cache, not a system
of record: a schema-version bump rebuilds from scratch rather than migrating.
WAL + a busy timeout keep the rare two-sessions-on-one-repo case from erroring.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..ingest.product import CachedFile, FileIndex, FileStamp
from .base import Store
from .serialize import from_json, to_json

# Bump whenever the persisted parse products change shape OR the parser's output
# semantics change (e.g. the refs/scope rewrite) — a bump discards stale caches.
_SCHEMA_VERSION = 8  # relative imports now resolved into the import map (changes stored imports)


class SqliteStore(Store):
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _write_self_ignore(db_path.parent)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            self._conn.executescript(
                "DROP TABLE IF EXISTS files;"
                "CREATE TABLE files ("
                "  path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, data TEXT"
                ");"
                f"PRAGMA user_version={_SCHEMA_VERSION};"
            )
            self._conn.commit()

    def load(self) -> dict[str, CachedFile]:
        rows = self._conn.execute("SELECT path, mtime_ns, size, data FROM files")
        return {
            path: CachedFile((mtime_ns, size), from_json(data))
            for path, mtime_ns, size, data in rows
        }

    def upsert(self, path: str, stamp: FileStamp, file: FileIndex) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO files (path, mtime_ns, size, data) VALUES (?, ?, ?, ?)",
            (path, stamp[0], stamp[1], to_json(file)),
        )

    def delete(self, paths: Iterable[str]) -> None:
        self._conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


def _write_self_ignore(directory: Path) -> None:
    """Drop a ``.gitignore`` of ``*`` so the cache dir ignores itself, repo-wide."""
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
