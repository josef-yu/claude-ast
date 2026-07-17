"""Shared Python-backend helpers — source spans, module naming, and annotation reading."""

from __future__ import annotations

import ast
from pathlib import Path

from ...model import Span

# The special forms whose subscript is a type, not a container (``Optional[X]``, ``Union[X, Y]``),
# matched on the head's final component so ``Optional`` / ``typing.Optional`` / ``t.Optional`` all
# count. Every other subscript head (``list``, ``dict``) denotes a container, not the element type.
_OPTIONAL_UNION = frozenset({"Optional", "Union"})


def span(path: str, node: ast.stmt | ast.expr) -> Span:
    """A Span from any statement or expression node (1-based lines, 0-based cols)."""
    return Span(path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


def annotation_types(node: ast.expr | None) -> tuple[str, ...]:
    """The concrete type name(s) a type annotation denotes — the one authority both the parameter
    resolver (which fans a union receiver out to every arm) and the return/attribute capture (which
    keeps a single type, so a union there stays deferred) read.

    A bare/dotted name (``User`` / ``models.User``) or a string forward-ref (``"User"``) yields that
    one type. A union (``User | Admin``, ``Union[User, Admin]``) yields each concrete arm, and an
    Optional (``User | None``, ``Optional[User]``, ``Union[User, None]``) drops the ``None`` and so
    collapses to the single type. A generic container (``list[User]``) or any other form yields
    nothing — the variable's type is the container, not the element. Order-preserving, deduped.
    """
    out: list[str] = []
    _collect_annotation_types(node, out)
    seen: set[str] = set()
    return tuple(t for t in out if t not in seen and not seen.add(t))


def _collect_annotation_types(node: ast.expr | None, out: list[str]) -> None:
    """Walk an annotation, appending each concrete type name. ``X | Y`` (PEP 604) recurses both
    arms; ``Optional[X]`` / ``Union[...]`` recurse the subscript element(s); a bare ``None`` arm is
    dropped; a string constant is a forward-ref type; any other subscript (``list[X]``) is a
    container and contributes nothing; a plain/dotted name is the type itself."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        _collect_annotation_types(node.left, out)
        _collect_annotation_types(node.right, out)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            out.append(node.value)  # a string forward-ref (`"User"`) names the type
        return  # the ``None`` arm of an Optional / union, or another literal — not a type
    if isinstance(node, ast.Subscript):
        head = dotted_name(node.value)
        if head is not None and head.rsplit(".", 1)[-1] in _OPTIONAL_UNION:
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            for elt in elts:
                _collect_annotation_types(elt, out)
        return  # a container subscript (``list[X]``) denotes the container, not ``X`` — deferred
    dotted = dotted_name(node)
    if dotted is not None:
        out.append(dotted)


def dotted_name(node: ast.expr | None) -> str | None:
    """The dotted path of a name or attribute-chain rooted at a name (``os.path.join``), else
    ``None`` when rooted at a value (a call, subscript, or literal). ``self.x`` / ``obj.y`` return a
    path too (root ``self`` / ``obj``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return ".".join(parts)
    return None


def module_qualname(path: Path, root: Path) -> str:
    """Map a file path to a dotted module name (``pkg/mod.py`` -> ``pkg.mod``)."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else root.name
