"""Ingester — language backends turning source into the normalized model.

The ``Indexer`` protocol is the language seam; ``PythonIndexer`` is the one
backend today. Downstream code depends on the protocol and the model, never on
``ast``. Produces symbols and (soon) syntactic edges — the always-on,
high-confidence base.  [P1]
"""

from .base import DEFAULT_EXCLUDE, Indexer, iter_source_files
from .product import FileIndex, ProjectIngest, RawRef
from .project import default_indexers, ingest_project
from .python import PythonIndexer

__all__ = [
    "DEFAULT_EXCLUDE",
    "FileIndex",
    "Indexer",
    "ProjectIngest",
    "PythonIndexer",
    "RawRef",
    "default_indexers",
    "ingest_project",
    "iter_source_files",
]
