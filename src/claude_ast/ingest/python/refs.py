"""Reference extraction — Python source -> raw (unbound) references.

Emits the syntactically-meaningful references:

- ``CALL``     — a call whose callee is a name (``foo()``) or an attribute chain
  rooted at a name (``os.path.join()``)
- ``INHERITS`` — a class base that is such a name or attribute chain (``abc.ABC``)

plus a module-level *import map* (local name -> target qualname) used later to
bind names that came from another module. Attribute chains rooted at a *value*
(``obj.save()`` — the receiver's type is unknown) are still emitted, but only the
module-rooted ones bind here; typing a value receiver is the resolver stack's job
(P2).

Scope handling is conservative in service of honest confidence: before binding a
bare name we skip it if it is bound as a local anywhere in an enclosing function
(parameters, assignments, ``for``/``with``/``except`` targets, comprehension and
walrus targets, nested defs, local imports). We would rather emit no edge than a
confidently-wrong one. Import collection is likewise module-scoped — a
function-local import must not leak a module-wide binding. Relative imports and
full type-aware resolution remain P2.
"""

from __future__ import annotations

import ast

from ...model import EdgeKind
from ..product import RawRef
from .common import span


def extract_refs(
    tree: ast.Module, module: str, path: str, node_ids: dict[ast.AST, str]
) -> tuple[list[RawRef], dict[str, str]]:
    refs: list[RawRef] = []
    _visit(tree, module, path, refs, [], node_ids)
    return refs, _collect_imports(tree)


def _visit(
    node: ast.AST,
    enclosing: str,
    path: str,
    refs: list[RawRef],
    locals_: list[frozenset[str]],
    node_ids: dict[ast.AST, str],
) -> None:
    if isinstance(node, ast.ClassDef):
        # Enclosing-symbol id comes from the shared authority (symbols.py), so a
        # ref's src matches the def's own id, `#N` and all. The concat fallback
        # only covers a node the symbol pass somehow didn't map — the pre-fix
        # behaviour, never a crash mid-index.
        cid = node_ids.get(node) or f"{enclosing}.{node.name}"
        # decorators, bases, and keywords evaluate in the ENCLOSING scope
        for deco in node.decorator_list:
            _visit(deco, enclosing, path, refs, locals_, node_ids)
        for base in node.bases:
            dotted = _dotted_name(base)
            if dotted is not None:
                if not _local(dotted.partition(".")[0], locals_):
                    refs.append(RawRef(cid, EdgeKind.INHERITS, dotted, span(path, base)))
            else:
                _visit(base, enclosing, path, refs, locals_, node_ids)  # e.g. Generic[T], a call
        for kw in node.keywords:
            _visit(kw.value, enclosing, path, refs, locals_, node_ids)
        for stmt in node.body:
            _visit(stmt, cid, path, refs, locals_, node_ids)
        return

    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        fid = node_ids.get(node) or f"{enclosing}.{node.name}"
        # decorators, default values, and annotations evaluate in the ENCLOSING scope
        for deco in node.decorator_list:
            _visit(deco, enclosing, path, refs, locals_, node_ids)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                _visit(default, enclosing, path, refs, locals_, node_ids)
        for arg in _all_args(node.args):
            if arg.annotation is not None:
                _visit(arg.annotation, enclosing, path, refs, locals_, node_ids)
        if node.returns is not None:
            _visit(node.returns, enclosing, path, refs, locals_, node_ids)
        # the body runs in the function's own scope, whose locals shadow module names
        inner = [*locals_, _function_locals(node)]
        for stmt in node.body:
            _visit(stmt, fid, path, refs, inner, node_ids)
        return

    if isinstance(node, ast.Call):
        callee = _dotted_name(node.func)
        if callee is not None and not _local(callee.partition(".")[0], locals_):
            refs.append(RawRef(enclosing, EdgeKind.CALL, callee, span(path, node.func)))

    for child in ast.iter_child_nodes(node):
        _visit(child, enclosing, path, refs, locals_, node_ids)


def _local(name: str, locals_: list[frozenset[str]]) -> bool:
    return any(name in scope for scope in locals_)


def _dotted_name(node: ast.expr) -> str | None:
    """The dotted path of a name or attribute-chain rooted at a name (``os.path.join``),
    else ``None`` when rooted at a *value* — a call, subscript, or literal whose type
    only P2 can resolve. ``self.x`` / ``obj.y`` return a path too (root ``self`` /
    ``obj``); they just won't bind until a resolver can type the receiver.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _all_args(args: ast.arguments) -> list[ast.arg]:
    result = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        result.append(args.vararg)
    if args.kwarg is not None:
        result.append(args.kwarg)
    return result


def _function_locals(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Every name bound in ``fn``'s own scope (over-broad by design — see module docstring).

    Includes parameters, all binding targets in the body, and nested def/class
    names, but does not descend into nested scopes (their locals are their own).
    ``global``/``nonlocal`` names are excluded — they refer outward.
    """
    names: set[str] = {arg.arg for arg in _all_args(fn.args)}
    declared_outer: set[str] = set()

    def process(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)  # bound here; its body is a separate scope, not descended
        elif isinstance(node, ast.Lambda):
            pass  # lambda body is a separate scope
        elif isinstance(node, ast.Global | ast.Nonlocal):
            declared_outer.update(node.names)
        else:
            _add_bound(node, names)
            for child in ast.iter_child_nodes(node):
                process(child)

    for stmt in fn.body:
        process(stmt)
    return frozenset(names - declared_outer)


def _add_bound(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _add_target(target, names)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor):
        _add_target(node.target, names)
    elif isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            if item.optional_vars is not None:
                _add_target(item.optional_vars, names)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            names.add(node.name)
    elif isinstance(node, ast.NamedExpr | ast.comprehension):
        _add_target(node.target, names)
    elif isinstance(node, ast.Import | ast.ImportFrom):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])


def _add_target(target: ast.expr, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Starred):
        _add_target(target.value, names)
    elif isinstance(target, ast.Tuple | ast.List):
        for elt in target.elts:
            _add_target(elt, names)


def _collect_imports(tree: ast.Module) -> dict[str, str]:
    """Map each MODULE-level imported name to the qualname it refers to.

    Deliberately does not descend into function or class bodies — an import there
    binds a local/class name, not a module-wide one.
    """
    imports: dict[str, str] = {}

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
            ):
                continue  # a separate scope — its imports are not module-wide
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.asname:
                        imports[alias.asname] = alias.name  # `import a.b as x` -> x: a.b
                    else:
                        top = alias.name.split(".")[0]  # `import a.b` binds `a` -> a
                        imports[top] = top
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    continue  # relative import — needs package resolution, deferred (P2)
                mod = child.module or ""
                for alias in child.names:
                    local = alias.asname or alias.name
                    imports[local] = f"{mod}.{alias.name}" if mod else alias.name
            else:
                scan(child)

    scan(tree)
    return imports
