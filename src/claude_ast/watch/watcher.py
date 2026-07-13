"""Filesystem watcher — feeds changed files into an ``IndexSession`` so the served view stays fresh.

Uses ``watchfiles`` (a Rust-backed watcher). Runs in a background thread off the MCP server's
event loop; on any batch of ``.py`` changes it calls ``session.patch()``, which re-ingests the
changed files and atomically swaps in a fresh index. The swap is GIL-atomic, so no lock is needed
between this writer thread and the reader (query) threads. Ambient by design — the launch context
starts it, never the model.
"""

from __future__ import annotations

import threading

from watchfiles import Change, watch

from ..index import IndexSession


def _python_only(_change: Change, path: str) -> bool:
    return path.endswith(".py")


def run_watcher(session: IndexSession, stop_event: threading.Event | None = None) -> None:
    """Block on ``.py`` changes under the session's root, patching on each batch.

    Meant to run in a daemon thread. ``stop_event`` ends the loop (for shutdown / tests).
    ``watch_filter`` keeps the snapshot db and non-source churn from triggering rebuilds.
    """
    for _changes in watch(session.root, watch_filter=_python_only, stop_event=stop_event):
        session.patch()
