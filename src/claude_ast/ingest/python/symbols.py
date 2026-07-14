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
    ast.Match,
    ast.match_case,
)


def extract_symbols(
    tree: ast.Module, module: str, path: str
) -> tuple[list[Symbol], dict[ast.AST, str]]:
    """Extract a module's symbols and the authoritative ``def/class node -> id`` map.

    The map is the single id-assignment authority: reference extraction consumes
    it (rather than re-deriving ids by concatenation) so an edge's ``src`` is
    always the exact id of its enclosing symbol — including the ``#N`` suffix of a
    same-qualname sibling, which a second traversal cannot reproduce on its own.
    """
    symbols: list[Symbol] = [
        Symbol(
            id=module,
            name=module.rsplit(".", 1)[-1],
            kind=SymbolKind.MODULE,
            span=Span(path, 1),
            doc=_docline(tree),
        )
    ]
    node_ids: dict[ast.AST, str] = {}
    _visit(tree, module, "module", path, symbols, {module}, node_ids)
    return symbols, node_ids


def _visit(
    node: ast.AST,
    prefix: str,
    container: str,
    path: str,
    out: list[Symbol],
    seen: set[str],
    node_ids: dict[ast.AST, str],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            cid = _unique(f"{prefix}.{child.name}", seen)
            node_ids[child] = cid
            out.append(
                Symbol(cid, child.name, SymbolKind.CLASS, span(path, child),
                       signature=_class_sig(child), doc=_docline(child), parent=prefix)
            )
            _visit(child, cid, "class", path, out, seen, node_ids)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            fid = _unique(f"{prefix}.{child.name}", seen)
            node_ids[child] = fid
            kind = SymbolKind.METHOD if container == "class" else SymbolKind.FUNCTION
            rtype, rtype_inferred = _return_type_of(child)
            out.append(
                Symbol(fid, child.name, kind, span(path, child),
                       signature=_func_sig(child), doc=_docline(child), parent=prefix,
                       return_type=rtype, return_type_inferred=rtype_inferred)
            )
            _visit(child, fid, "function", path, out, seen, node_ids)
        elif isinstance(child, ast.Assign | ast.AnnAssign):
            if container in ("module", "class"):
                for name in _assigned_names(child):
                    vid = f"{prefix}.{name}"
                    if vid in seen:
                        continue  # reassignment of the same name — one variable, not two
                    seen.add(vid)
                    out.append(
                        Symbol(vid, name, SymbolKind.VARIABLE, span(path, child), parent=prefix)
                    )
        elif isinstance(child, _BLOCKS):
            _visit(child, prefix, container, path, out, seen, node_ids)


def _unique(base: str, seen: set[str]) -> str:
    """A collision-free id: distinct same-qualname *defs* (conditional/overloaded
    functions, redefinitions) each keep their own symbol instead of one silently
    overwriting another in the graph. First keeps ``base``; the rest get ``#N``.
    """
    if base not in seen:
        seen.add(base)
        return base
    n = 2
    while f"{base}#{n}" in seen:
        n += 1
    uid = f"{base}#{n}"
    seen.add(uid)
    return uid


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


def _return_type_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str | None, bool]:
    """A function's return type as ``(resolvable name, from-body-inference?)``: the annotation
    if present, else inferred from the body — a single unambiguous ``return Ctor(...)`` names its
    class. Un-annotated functions are common, and this feeds the same chaining/assignment
    resolution as annotations; the flag keeps the provenance honest (an edge built through an
    inferred return must be stamped INFERENCE, not ANNOTATION)."""
    if fn.returns is not None:
        return _annotation_name(fn.returns), False
    ctors: set[str] = set()
    ambiguous = False

    def scan(node: ast.AST) -> None:
        nonlocal ambiguous
        if isinstance(node, ast.Return):
            v = node.value
            if v is None or (isinstance(v, ast.Constant) and v.value is None):
                return  # `return` / `return None` -> ignore (an Optional path)
            if isinstance(v, ast.Call) and (name := _annotation_name(v.func)) is not None:
                ctors.add(name)
            else:
                ambiguous = True  # returns a value we can't name -> don't guess a type
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return  # a nested scope's returns are its own
        for child in ast.iter_child_nodes(node):
            scan(child)

    for stmt in fn.body:
        scan(stmt)
    if len(ctors) == 1 and not ambiguous:
        return next(iter(ctors)), True
    return None, False


def _annotation_name(node: ast.expr | None) -> str | None:
    """A return/type annotation as a resolvable name: a bare name (``Service``), an attribute
    chain (``models.User``), or a string forward-ref (``"Service"``). A subscript / union
    (``list[Service]``, ``Service | None``) yields ``None`` — left for later work."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
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
