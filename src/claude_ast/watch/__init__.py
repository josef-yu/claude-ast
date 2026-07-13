"""Watcher — watchfiles-driven live refresh of an ``IndexSession``.

The launch context (the MCP server) starts ``run_watcher`` in a background thread; on each
batch of ``.py`` edits it patches the session, which atomically swaps in a fresh index — so a
query is never stale, even in the edit -> query tightloop. Ambient by design: the model never
triggers watching. Scope-filtered to ``.py`` (excludes the snapshot db and non-source churn).
"""

from .watcher import run_watcher

__all__ = ["run_watcher"]
