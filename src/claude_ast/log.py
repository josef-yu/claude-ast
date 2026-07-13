"""The logging seam — diagnostics go to stderr, never stdout.

``stdout`` is a data/protocol channel: the CLI writes query results there, and the P3
MCP server will speak the stdio protocol on it. So diagnostics (skipped files, warnings)
must never touch it. Library modules just call ``logging.getLogger(__name__)`` and log;
each *entry point* (the CLI today, the MCP server in P3) calls ``configure()`` once to
route those records to stderr.

Without ``configure()`` — e.g. when embedded as a library, or under pytest — Python's
own handling still sends ``WARNING`` and above to stderr, so nothing is silently lost;
``configure()`` only makes the destination explicit and the format ours.
"""

from __future__ import annotations

import logging
import sys


def configure(level: int = logging.WARNING) -> None:
    """Route diagnostics to stderr. Idempotent — a no-op if logging is already configured,
    so each entry point can call it without stepping on an embedding application's setup."""
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="claude-ast: %(levelname)s: %(message)s",
    )
