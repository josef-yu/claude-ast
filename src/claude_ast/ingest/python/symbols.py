"""Definition extraction — Python source -> normalized Symbols.

Modules, classes, functions/methods, and module- and class-level variables, with
signatures and docstring-lines. No type work; these are the always-present base.
"""

from __future__ import annotations

import ast

from ...model import Span, Symbol, SymbolKind
from .common import span

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


def extract_symbols(tree: ast.Module, module: str, path: str) -> list[Symbol]:
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
    return symbols


def _visit(node: ast.AST, prefix: str, container: str, path: str, out: list[Symbol]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            cid = f"{prefix}.{child.name}"
            out.append(
                Symbol(cid, child.name, SymbolKind.CLASS, span(path, child),
                       signature=_class_sig(child), doc=_docline(child), parent=prefix)
            )
            _visit(child, cid, "class", path, out)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            fid = f"{prefix}.{child.name}"
            kind = SymbolKind.METHOD if container == "class" else SymbolKind.FUNCTION
            out.append(
                Symbol(fid, child.name, kind, span(path, child),
                       signature=_func_sig(child), doc=_docline(child), parent=prefix)
            )
            _visit(child, fid, "function", path, out)
        elif isinstance(child, ast.Assign | ast.AnnAssign):
            if container in ("module", "class"):
                for name in _assigned_names(child):
                    out.append(
                        Symbol(f"{prefix}.{name}", name, SymbolKind.VARIABLE,
                               span(path, child), parent=prefix)
                    )
        elif isinstance(child, _BLOCKS):
            _visit(child, prefix, container, path, out)


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
