"""PythonIndexer — the Python language backend (stdlib ``ast``).

The one ``Indexer`` implementation today. **All** Python-specific logic lives
here; nothing outside this module imports ``ast``. A second language would be a
sibling module implementing the same protocol.

This increment extracts *definitions* (module, classes, functions/methods,
module- and class-level variables) with signatures and docstring-lines. Raw
references (the edge sources) are the next increment.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..model import Span, Symbol, SymbolKind
from .product import FileIndex

# Statement blocks we descend into without opening a new scope — a `def` inside
# an `if`/`try`/`for` is still defined at the enclosing scope.
_BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)


class PythonIndexer:
    """Language backend for Python source."""

    name = "python"
    extensions = frozenset({".py"})

    def ingest_file(self, path: Path, root: Path) -> FileIndex | None:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return self.ingest_text(path, root, source)

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        try:
            return self.ingest_source(str(path), source, _module_qualname(path, root))
        except SyntaxError:
            return None

    def ingest_source(self, path: str, source: str, module: str) -> FileIndex:
        """Parse one module's source into a FileIndex. Raises ``SyntaxError`` on bad input.

        Kept public and module-name-explicit as a convenience for tests.
        """
        tree = ast.parse(source, filename=path)
        symbols: list[Symbol] = [
            Symbol(
                id=module,
                name=module.rsplit(".", 1)[-1],
                kind=SymbolKind.MODULE,
                span=Span(path, 1),
                doc=_docline(tree),
            )
        ]
        _visit(tree, module, "module", path, symbols)
        return FileIndex(path=path, module=module, symbols=symbols)


# --- Python-specific helpers ---


def _module_qualname(path: Path, root: Path) -> str:
    """Map a file path to a dotted module name (``pkg/mod.py`` -> ``pkg.mod``)."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else root.name


def _visit(node: ast.AST, prefix: str, container: str, path: str, out: list[Symbol]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            cid = f"{prefix}.{child.name}"
            out.append(
                Symbol(
                    cid,
                    child.name,
                    SymbolKind.CLASS,
                    _span(path, child),
                    signature=_class_sig(child),
                    doc=_docline(child),
                    parent=prefix,
                )
            )
            _visit(child, cid, "class", path, out)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            fid = f"{prefix}.{child.name}"
            kind = SymbolKind.METHOD if container == "class" else SymbolKind.FUNCTION
            out.append(
                Symbol(
                    fid,
                    child.name,
                    kind,
                    _span(path, child),
                    signature=_func_sig(child),
                    doc=_docline(child),
                    parent=prefix,
                )
            )
            _visit(child, fid, "function", path, out)
        elif isinstance(child, ast.Assign | ast.AnnAssign):
            if container in ("module", "class"):
                for name in _assigned_names(child):
                    out.append(
                        Symbol(
                            f"{prefix}.{name}",
                            name,
                            SymbolKind.VARIABLE,
                            _span(path, child),
                            parent=prefix,
                        )
                    )
        elif isinstance(child, _BLOCKS):
            _visit(child, prefix, container, path, out)


def _span(path: str, node: ast.stmt | ast.excepthandler) -> Span:
    return Span(path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


def _docline(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    for line in doc.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{keyword} {node.name}({args}){ret}"


def _class_sig(node: ast.ClassDef) -> str:
    parts = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    return f"class {node.name}" + (f"({', '.join(parts)})" if parts else "")


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            names += [e.id for e in target.elts if isinstance(e, ast.Name)]
    return names
