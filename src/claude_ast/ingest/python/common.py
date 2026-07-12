"""Shared Python-backend helpers — source spans and module naming."""

from __future__ import annotations

import ast
from pathlib import Path

from ...model import Span


def span(path: str, node: ast.stmt | ast.expr) -> Span:
    """A Span from any statement or expression node (1-based lines, 0-based cols)."""
    return Span(path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


def module_qualname(path: Path, root: Path) -> str:
    """Map a file path to a dotted module name (``pkg/mod.py`` -> ``pkg.mod``)."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else root.name
