"""Reference extraction — Python source -> raw (unbound) references.

This increment emits the syntactically-meaningful references:

- ``CALL``     — a direct call ``foo()`` where ``foo`` is a bare name
- ``INHERITS`` — a class base that is a bare name

plus an *import map* (local name -> target qualname) used later to bind names
that came from another module. Attribute/method calls (``obj.save()``) need a
value's type, so they are left for the resolver stack (P2).

Names that are locally bound (function parameters) are skipped — they are not
navigable symbols, and skipping them avoids binding a local to a same-named
module-level definition. Full scope analysis (assignments, comprehensions,
``global``/``nonlocal``) is a P2 refinement.
"""

from __future__ import annotations

import ast

from ...model import EdgeKind
from ..product import RawRef
from .common import span


def extract_refs(tree: ast.Module, module: str, path: str) -> tuple[list[RawRef], dict[str, str]]:
    refs: list[RawRef] = []
    _walk(tree, module, path, refs, [])
    return refs, _collect_imports(tree)


def _collect_imports(tree: ast.Module) -> dict[str, str]:
    """Map each imported local name to the qualname it refers to."""
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = alias.name  # `import a.b as x` -> x: a.b
                else:
                    top = alias.name.split(".")[0]  # `import a.b` binds `a` -> a
                    imports[top] = top
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — needs package context, deferred (P2)
                continue
            mod = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                imports[local] = f"{mod}.{alias.name}" if mod else alias.name
    return imports


def _walk(
    node: ast.AST, enclosing: str, path: str, refs: list[RawRef], locals_: list[frozenset[str]]
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            cid = f"{enclosing}.{child.name}"
            for base in child.bases:
                if isinstance(base, ast.Name) and not _local(base.id, locals_):
                    refs.append(RawRef(cid, EdgeKind.INHERITS, base.id, span(path, base)))
            _walk(child, cid, path, refs, locals_)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            fid = f"{enclosing}.{child.name}"
            _walk(child, fid, path, refs, [*locals_, _params(child.args)])
        else:
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                name = child.func.id
                if not _local(name, locals_):
                    refs.append(RawRef(enclosing, EdgeKind.CALL, name, span(path, child.func)))
            _walk(child, enclosing, path, refs, locals_)


def _params(args: ast.arguments) -> frozenset[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return frozenset(names)


def _local(name: str, locals_: list[frozenset[str]]) -> bool:
    return any(name in scope for scope in locals_)
