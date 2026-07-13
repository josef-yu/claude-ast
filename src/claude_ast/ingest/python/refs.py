"""Reference extraction — Python source -> raw (unbound) references.

Emits the syntactically-meaningful references:

- ``CALL``     — a call whose callee is a name (``foo()``) or an attribute chain
  rooted at a name (``os.path.join()``)
- ``INHERITS`` — a class base that is such a name or attribute chain (``abc.ABC``)

plus a module-level *import map* (local name -> target qualname) used later to
bind names that came from another module. Attribute chains rooted at a *local*
value (``self.save()``, ``u.save()``) are recorded with ``local_root=True`` — kept
OUT of syntactic binding (a local root may shadow an import, so binding it would
forge a wrong edge) and left for the P2 type resolvers. When the receiver's type is
known in scope — a parameter annotation or a local ``x = Foo()`` construction — the
ref carries it (``receiver_type``, with ``receiver_inferred`` telling the two apart)
so the type resolvers can bind it. A bare local call (``x()``) is still skipped.
A name-callee call also records the concrete types seen at its positional arguments
(``g(User())`` -> ``arg_types=("User",)``), which the call-site pass turns into definite
``RECEIVES_ARG`` observations — constructions only, so the observed type is exact.

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
from pathlib import Path

from ...model import EdgeKind
from ..product import RawRef
from .common import span


def extract_refs(
    tree: ast.Module, module: str, path: str, node_ids: dict[ast.AST, str]
) -> tuple[list[RawRef], dict[str, str]]:
    refs: list[RawRef] = []
    # Module-body value binders shadow-protect module-scope value receivers, just as a
    # function's locals do inside it (a `for json in ...` must not bind through `import json`).
    _visit(tree, module, path, refs, [_binder_names(tree.body)], {}, node_ids)
    return refs, _collect_imports(tree, _package(module, path))


def _visit(
    node: ast.AST,
    enclosing: str,
    path: str,
    refs: list[RawRef],
    locals_: list[frozenset[str]],
    types: dict[str, tuple[str, bool]],  # local name -> (type name, from-inference?)
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
            _visit(deco, enclosing, path, refs, locals_, types, node_ids)
        for base in node.bases:
            dotted = _dotted_name(base)
            if dotted is not None:
                if not _local(dotted.partition(".")[0], locals_):
                    refs.append(RawRef(cid, EdgeKind.INHERITS, dotted, span(path, base)))
            else:
                _visit(base, enclosing, path, refs, locals_, types, node_ids)  # e.g. Generic[T]
        for kw in node.keywords:
            _visit(kw.value, enclosing, path, refs, locals_, types, node_ids)
        # class-body value binders shadow-protect receivers in the body (a class var
        # named like an import must not bind through it) — parent==class VARIABLEs are
        # absent from module_defs, so without this they would forge a definite edge.
        class_locals = [*locals_, _binder_names(node.body)]
        for stmt in node.body:
            _visit(stmt, cid, path, refs, class_locals, types, node_ids)
        return

    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        fid = node_ids.get(node) or f"{enclosing}.{node.name}"
        # decorators, default values, and annotations evaluate in the ENCLOSING scope
        for deco in node.decorator_list:
            _visit(deco, enclosing, path, refs, locals_, types, node_ids)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                _visit(default, enclosing, path, refs, locals_, types, node_ids)
        for arg in _all_args(node.args):
            if arg.annotation is not None:
                _visit(arg.annotation, enclosing, path, refs, locals_, types, node_ids)
        if node.returns is not None:
            _visit(node.returns, enclosing, path, refs, locals_, types, node_ids)
        # the body runs in the function's own scope: its locals shadow module names, and
        # its parameter annotations + local constructions extend the receiver-type map.
        # A name this scope rebinds drops the outer type (an untyped shadow must not inherit
        # a stale annotation); precedence: a declared annotation beats an inferred
        # `x = Foo()`, both beat an outer-scope type that survives (a genuine closure read).
        fn_locals = _function_locals(node)
        inner = [*locals_, fn_locals]
        inner_types = {
            **{n: v for n, v in types.items() if n not in fn_locals},
            **{name: (t, True) for name, t in _inferred_types(node).items()},
            **{name: (t, False) for name, t in _annotated_types(node).items()},
        }
        for stmt in node.body:
            _visit(stmt, fid, path, refs, inner, inner_types, node_ids)
        return

    if isinstance(node, ast.Lambda):
        # A lambda is a scope: its parameters shadow outer names in its body, exactly as a
        # def's do. Without this branch `_visit` recurses into the body carrying the ENCLOSING
        # locals, so a lambda param named like a module symbol (`lambda User: g(User())`) is
        # not seen as a local — and the shadowed name would forge a wrong edge, including a
        # confidently-wrong *definite* RECEIVES_ARG observation. Defaults evaluate in the
        # enclosing scope; the body in the lambda's own. (Lambda params carry no annotations,
        # so they add no receiver types — they only drop shadowed outer ones.)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                _visit(default, enclosing, path, refs, locals_, types, node_ids)
        lambda_locals = frozenset(a.arg for a in _all_args(node.args))
        inner = [*locals_, lambda_locals]
        inner_types = {n: v for n, v in types.items() if n not in lambda_locals}
        _visit(node.body, enclosing, path, refs, inner, inner_types, node_ids)
        return

    if isinstance(node, ast.Call):
        callee = _dotted_name(node.func)
        if callee is not None:
            root = callee.partition(".")[0]
            if not _local(root, locals_):
                refs.append(
                    RawRef(
                        enclosing,
                        EdgeKind.CALL,
                        callee,
                        span(path, node.func),
                        arg_types=_arg_types(node, locals_),
                    )
                )
            elif "." in callee:
                # value receiver (self.save(), u.save()): record for the type resolvers,
                # flagged so syntactic binding never mis-resolves a shadowing local, and
                # stamped with the receiver's type (annotated or inferred) when known.
                rtype, rinferred = types.get(root, (None, False))
                refs.append(
                    RawRef(
                        enclosing,
                        EdgeKind.CALL,
                        callee,
                        span(path, node.func),
                        local_root=True,
                        receiver_type=rtype,
                        receiver_inferred=rinferred,
                    )
                )

    for child in ast.iter_child_nodes(node):
        _visit(child, enclosing, path, refs, locals_, types, node_ids)


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


def _arg_types(node: ast.Call, locals_: list[frozenset[str]]) -> tuple[str | None, ...]:
    """The concrete types observed flowing into a call's positional parameters.

    Element *k* is the class name of a bare-name construction at arg *k* (``User`` for
    ``User()``), else ``None``. Constructions only — a value observed *exactly*, so the
    edge it feeds is honestly definite. Deferred (as ``None``, or by truncation): a
    dotted constructor (``mod.User()``), a factory/return value (``make()`` — filtered
    later when the name doesn't bind to a class), a shadowed constructor name, and every
    positional past the first ``*args`` (a splat destroys index↔param alignment). Trailing
    ``None``s are trimmed so a call passing no constructions stores the empty tuple.
    """
    names: list[str | None] = []
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            break  # positional alignment is lost past a splat — defer the rest
        ctor: str | None = None
        if isinstance(arg, ast.Call):
            dotted = _dotted_name(arg.func)
            if dotted is not None and "." not in dotted and not _local(dotted, locals_):
                ctor = dotted
        names.append(ctor)
    while names and names[-1] is None:
        names.pop()
    return tuple(names)


def _annotated_types(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    """Parameter name -> its annotated type name (bare or dotted, e.g. ``User`` /
    ``models.User``), for the annotation resolver.

    Only annotations that are a plain name or attribute chain are recorded; subscripts
    and unions (``list[User]``, ``User | None``) yield no fact via ``_dotted_name`` and
    are left for a later resolver. Parameters only this increment — annotated local
    assignments and ``x = User()`` inference are deferred.
    """
    types: dict[str, str] = {}
    for arg in _all_args(fn.args):
        if arg.annotation is not None:
            annotated = _dotted_name(arg.annotation)
            if annotated is not None:
                types[arg.arg] = annotated
    return types


def _inferred_types(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    """Local name -> the type it is constructed from (``x = User()`` -> ``x: User``), for
    the inference resolver.

    Flow-insensitive and conservative: a name assigned from more than one distinct
    constructor is *dropped* (ambiguous — don't guess). The candidate is the callee name;
    the resolver keeps it only if it resolves to a class (``x = helper()`` where helper is
    a function yields no edge). Nested scopes are not descended; reassignment and
    non-construction RHS (``x = other()``) are deferred to a flow-sensitive resolver.
    """
    candidates: dict[str, set[str]] = {}

    def scan(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return  # a separate scope — its assignments are not this function's
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            ctor = _dotted_name(node.value.func)
            if ctor is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        candidates.setdefault(target.id, set()).add(ctor)
        for child in ast.iter_child_nodes(node):
            scan(child)

    for stmt in fn.body:
        scan(stmt)
    return {name: next(iter(ctors)) for name, ctors in candidates.items() if len(ctors) == 1}


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
    A bare ``global``/``nonlocal x`` binds nothing, so ``x`` is absent here and refers
    outward — an outer import/class binds normally. But ``global x; x = ...`` *reassigns*
    that outer name to an unknown value, so ``x`` IS captured as a local shadow (via the
    assignment): else a rebound import/class name would forge a confidently-wrong edge
    (``global os; os = f(); os.g()`` must not bind ``os`` to the stdlib module, and
    ``global User; User = 5; h(User())`` must not report ``User`` as a passed type).
    """
    names: set[str] = {arg.arg for arg in _all_args(fn.args)}

    def process(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)  # bound here; its body is a separate scope, not descended
        elif isinstance(node, ast.Lambda):
            pass  # lambda body is a separate scope
        elif isinstance(node, ast.Global | ast.Nonlocal):
            pass  # binds nothing itself; a reassignment `x = ...` is caught as an Assign below
        else:
            _add_bound(node, names)
            for child in ast.iter_child_nodes(node):
                process(child)

    for stmt in fn.body:
        process(stmt)
    return frozenset(names)


def _binder_names(body: list[ast.stmt]) -> frozenset[str]:
    """Names value-bound in a module or class body — assignment / for / with / except /
    comprehension / walrus / match targets — used to shadow-protect value receivers at
    module and class scope, as ``_function_locals`` does inside functions. Imports are
    excluded (they are resolution targets, not shadows); nested scopes are not descended.
    """
    names: set[str] = set()

    def process(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return  # a separate scope — its bindings are its own
        if isinstance(node, ast.Import | ast.ImportFrom):
            return  # a resolution target, not a shadowing local
        _add_bound(node, names)
        for child in ast.iter_child_nodes(node):
            process(child)

    for stmt in body:
        process(stmt)
    return frozenset(names)


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
    elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
        names.add(node.name)  # `case <name>:` / `case [*<name>]` binds a local
    elif isinstance(node, ast.MatchMapping) and node.rest:
        names.add(node.rest)  # `case {**<rest>}` binds a local


def _add_target(target: ast.expr, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Starred):
        _add_target(target.value, names)
    elif isinstance(target, ast.Tuple | ast.List):
        for elt in target.elts:
            _add_target(elt, names)


def _package(module: str, path: str) -> str:
    """The importing module's package — Python's ``__package__``, the anchor a relative
    import resolves against. A package's ``__init__`` is its own package; a regular
    module's package is its parent (``""`` for a top-level module)."""
    if Path(path).name == "__init__.py":
        return module
    return module.rpartition(".")[0]


def _relative_module(package: str, level: int, module: str | None) -> str | None:
    """Resolve a relative import's target module qualname by Python's own rule, or
    ``None`` if the ``level`` dots walk above the top-level package."""
    bits = package.rsplit(".", level - 1)
    if not package or len(bits) < level:
        return None
    base = bits[0]
    return f"{base}.{module}" if module else base


def _collect_imports(tree: ast.Module, package: str) -> dict[str, str]:
    """Map each MODULE-level imported name to the qualname it refers to.

    Absolute and relative imports both resolve to an absolute qualname; a relative
    import is anchored on ``package`` (the importing module's ``__package__``).
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
                if child.level:  # `from . import x` / `from ..pkg import y`
                    mod = _relative_module(package, child.level, child.module)
                    if mod is None:
                        continue  # walks above the top-level package — unresolvable
                else:
                    mod = child.module or ""
                for alias in child.names:
                    local = alias.asname or alias.name
                    imports[local] = f"{mod}.{alias.name}" if mod else alias.name
            else:
                scan(child)

    scan(tree)
    return imports
