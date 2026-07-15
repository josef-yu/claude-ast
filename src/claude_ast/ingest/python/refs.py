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

# Node-class groups hoisted to module scope. An inline ``ast.A | ast.B`` in a hot
# per-node predicate rebuilds a ``types.UnionType`` on *every* call (millions of
# them across a large tree); a module-level tuple is built once and matched in C
# by ``isinstance`` (which also keeps the type-narrowing the branch bodies rely
# on). Same idiom as ``symbols._BLOCKS``.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_FUNCTIONS_OR_CLASS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LAMBDA_OR_GLOBALS = (ast.Lambda, ast.Global, ast.Nonlocal)  # nested-scope/rebinds-nothing skip
_IMPORTS = (ast.Import, ast.ImportFrom)
_SEQ_TARGETS = (ast.Tuple, ast.List)
# _add_bound groups: node types that bind their target via ``node.target``, and
# the two-member ``with`` / ``match``-name groups.
_TARGET_NODES = (
    ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension,
)
_WITH_NODES = (ast.With, ast.AsyncWith)
_MATCH_NAME_NODES = (ast.MatchAs, ast.MatchStar)


def extract_refs(
    tree: ast.Module, module: str, path: str, node_ids: dict[ast.AST, str]
) -> tuple[list[RawRef], dict[str, str]]:
    refs: list[RawRef] = []
    package = _package(module, path)
    # Module-body value binders shadow-protect module-scope value receivers, just as a
    # function's locals do inside it (a `for json in ...` must not bind through `import json`).
    _visit(tree, module, path, refs, [_binder_names(tree.body)], {}, node_ids)
    refs.extend(_import_refs(tree, module, package, path))
    return refs, _collect_imports(tree, package)


def _import_refs(tree: ast.Module, module: str, package: str, path: str) -> list[RawRef]:
    """Module-level imports as ``IMPORT`` refs: importing module -> the module(s) it depends on.

    ``import a.b`` and ``from a.b import c`` both yield the from-module ``a.b`` (relative imports
    anchored on ``package``); a from-import additionally yields each imported name's *candidate*
    qualname (``from pkg import sub`` -> ``pkg.sub``), because the name may be a submodule —
    binding keeps only targets that resolve to an in-tree module, so a plain-symbol import's
    candidate simply drops. The graph thus carries the internal module-dependency edges — the
    thing text search can't cheaply give (especially the reverse: *who imports this module*).
    Module scope only — a function-local import is not a module-wide dependency.
    """
    refs: list[RawRef] = []

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue  # a separate scope — its imports are not module-wide
            if isinstance(child, ast.Import):
                for alias in child.names:
                    refs.append(RawRef(module, EdgeKind.IMPORT, alias.name, span(path, child)))
            elif isinstance(child, ast.ImportFrom):
                if child.level:  # `from . import x` / `from ..pkg import y`
                    mod = _relative_module(package, child.level, child.module)
                else:
                    mod = child.module or ""
                if mod:
                    refs.append(RawRef(module, EdgeKind.IMPORT, mod, span(path, child)))
                    for alias in child.names:
                        if alias.name != "*":
                            refs.append(RawRef(
                                module, EdgeKind.IMPORT, f"{mod}.{alias.name}", span(path, child)
                            ))
            else:
                scan(child)

    scan(tree)
    return refs


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

    if isinstance(node, _FUNCTIONS):
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
        fn_locals, inferred = _function_scope(node, locals_)
        inner = [*locals_, fn_locals]
        inner_types = {
            **{n: v for n, v in types.items() if n not in fn_locals},
            **{name: (t, True) for name, t in inferred.items()},
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
        else:
            # callee rooted at a *value*, not a name. One case we can still resolve: a call whose
            # receiver is itself a name-rooted call, `re.compile(p).match(s).group()` — record the
            # receiver call (`re.compile`) + the ordered members reached on its return
            # (`("match", "group")`) for the chain resolver.
            chain = _call_chain(node)
            if chain is not None:
                receiver, members = chain
                if not _local(receiver.partition(".")[0], locals_):
                    refs.append(RawRef(
                        enclosing, EdgeKind.CALL, receiver, span(path, node.func), chain=members
                    ))
                elif "." in receiver:
                    # value-rooted chain (`self.get().run()`): the receiver is a value-typed call,
                    # so the type resolvers own it — flagged, with the receiver's type when known.
                    rtype, rinferred = types.get(receiver.partition(".")[0], (None, False))
                    refs.append(RawRef(
                        enclosing, EdgeKind.CALL, receiver, span(path, node.func), chain=members,
                        local_root=True, receiver_type=rtype, receiver_inferred=rinferred,
                    ))

    for child in ast.iter_child_nodes(node):
        _visit(child, enclosing, path, refs, locals_, types, node_ids)


def _local(name: str, locals_: list[frozenset[str]]) -> bool:
    return any(name in scope for scope in locals_)


def _call_chain(node: ast.Call) -> tuple[str, tuple[str, ...]] | None:
    """Decompose a call whose receiver is itself a name-rooted call into the receiver call plus
    the ordered members reached on its return. ``re.compile(p).match(s).group()`` ->
    ``("re.compile", ("match", "group"))``; ``Path.cwd().exists()`` -> ``("Path.cwd", ("exists",))``
    (the receiver call ``re.compile`` / ``Path.cwd`` is captured separately as the flat ref).

    The postfix chain flattens to a root name + a list of ops (attribute access / call). The
    receiver is the root plus the accesses before the *first* call; the members after it are the
    chain (the last, always a call, is what this ref invokes). A value-rooted chain (``obj.m()``),
    a subscript, or no leading call yields ``None`` — left for the value resolvers or later work.
    """
    ops: list[str | None] = []  # str = attribute access; None = a call
    cur: ast.expr = node
    while True:
        if isinstance(cur, ast.Call):
            ops.append(None)
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            ops.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            root = cur.id
            break
        else:
            return None  # rooted at a subscript / literal / value
    ops.reverse()
    if None not in ops or ops[-1] is not None:
        return None  # no leading call, or the outermost op isn't the call we're resolving
    first_call = ops.index(None)
    lead = ops[:first_call]
    if any(o is None for o in lead):  # the receiver prefix must be a plain dotted name
        return None
    receiver = ".".join([root, *(str(o) for o in lead)])
    members = tuple(o for o in ops[first_call + 1 :] if o is not None)
    return (receiver, members) if members else None


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


def _function_scope(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, outer: list[frozenset[str]]
) -> tuple[frozenset[str], dict[str, str]]:
    """One subtree walk yielding both a function's bound-name set (``fn_locals``) and its
    ``local name -> constructed type`` inference map (``x = User()`` -> ``x: User``).

    Fuses the two passes that each independently walked the whole function body:
    ``_function_locals`` (every name bound in ``fn``'s own scope — over-broad by design; see
    ``_local`` callers) and ``_inferred_types`` (flow-insensitive, conservative construction
    inference). One walk over the body instead of two.

    **Accumulate-then-filter.** The inference's constructor-shadow test — ``x = User()`` yields
    a type only if ``User`` is not shadowed by a local — needs the COMPLETE ``fn_locals``,
    because a ctor name can be rebound *later* in the body (``x = User(); User = 5``). So ctor
    assignments are recorded during the walk (``typed``) and the candidate/rebound decision is
    made afterwards, once ``fn_locals`` is final. Inference stays conservative: a name assigned
    from two distinct constructors, a shadowed ctor, or a name rebound by anything else is
    dropped, never guessed — the resolver later keeps only ctors that resolve to a class.

    ``names``/``rebound`` bookkeeping mirrors the two originals exactly, including the
    Assign-with-Call asymmetry: the target subtree feeds only ``fn_locals`` (as
    ``_function_locals`` did), the value subtree feeds both (a walrus there binds a local the
    old ``_inferred_types`` also saw). Nested scopes contribute only their *name*; their bodies
    are not descended.
    """
    names: set[str] = {arg.arg for arg in _all_args(fn.args)}
    rebound: set[str] = set()  # names also bound to a value we can't type
    typed: list[tuple[ast.Assign, str]] = []  # (assign, ctor) — filtered after names is complete

    def add_names(node: ast.AST) -> None:
        # _function_locals bookkeeping: bound names + nested def/class names, skip nested scopes.
        if isinstance(node, _FUNCTIONS_OR_CLASS):
            names.add(node.name)
            return
        if isinstance(node, _LAMBDA_OR_GLOBALS):
            return
        _add_bound(node, names)
        for child in ast.iter_child_nodes(node):
            add_names(child)

    def walk(node: ast.AST) -> None:
        if isinstance(node, _SCOPES):
            add_names(node)  # a separate scope, but its NAME still binds a local
            return
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            _add_bound(node, names)  # simple targets -> fn_locals
            for target in node.targets:  # rare walrus inside a target -> fn_locals only
                for child in ast.iter_child_nodes(target):
                    add_names(child)
            ctor = _dotted_name(node.value.func)
            if ctor is not None:
                typed.append((node, ctor))  # defer the ctor-shadow decision
            else:
                _add_bound(node, rebound)  # a value we can't name rebinds the targets
            for child in ast.iter_child_nodes(node.value):
                walk(child)  # a walrus inside the call's own args still binds (names + rebound)
            return
        _add_bound(node, names)
        _add_bound(node, rebound)
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in fn.body:
        walk(stmt)

    fn_locals = frozenset(names)
    scopes = [*outer, fn_locals]  # ctor-root locality is tested against the complete scope
    candidates: dict[str, set[str]] = {}
    for node, ctor in typed:
        if not _local(ctor.partition(".")[0], scopes):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    candidates.setdefault(target.id, set()).add(ctor)
                else:
                    _add_target(target, rebound)  # unpacking a call result — untyped
        else:
            _add_bound(node, rebound)  # ctor shadowed by a local -> value we can't name
    inferred = {
        name: next(iter(ctors))
        for name, ctors in candidates.items()
        if len(ctors) == 1 and name not in rebound
    }
    return fn_locals, inferred


def _all_args(args: ast.arguments) -> list[ast.arg]:
    result = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        result.append(args.vararg)
    if args.kwarg is not None:
        result.append(args.kwarg)
    return result


def _binder_names(body: list[ast.stmt]) -> frozenset[str]:
    """Names value-bound in a module or class body — assignment / for / with / except /
    comprehension / walrus / match targets — used to shadow-protect value receivers at
    module and class scope, as ``_function_scope`` does inside functions. Imports are
    excluded (they are resolution targets, not shadows); nested scopes are not descended.

    A function's own scope is over-broad by design: a bare ``global``/``nonlocal x`` binds
    nothing (``x`` refers outward), but ``global x; x = ...`` reassigns that outer name to an
    unknown value, so ``x`` IS captured as a local shadow via the assignment — else a rebound
    import/class name would forge a confidently-wrong edge (``global os; os = f(); os.g()``
    must not bind the stdlib module; ``global User; User = 5; h(User())`` must not report a type).
    """
    names: set[str] = set()

    def process(node: ast.AST) -> None:
        if isinstance(node, _SCOPES):
            return  # a separate scope — its bindings are its own
        if isinstance(node, _IMPORTS):
            return  # a resolution target, not a shadowing local
        _add_bound(node, names)
        for child in ast.iter_child_nodes(node):
            process(child)

    for stmt in body:
        process(stmt)
    return frozenset(names)


def _add_bound(node: ast.AST, names: set[str]) -> None:
    # The hottest per-node call in ingest (~2M/build). Branches are type-disjoint,
    # so order is immaterial; AnnAssign/AugAssign/For/AsyncFor/NamedExpr/comprehension
    # all bind via ``.target`` and collapse to ``_TARGET_NODES``. All predicates use
    # hoisted tuples so no ``UnionType`` is built per call.
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _add_target(target, names)
    elif isinstance(node, _TARGET_NODES):
        _add_target(node.target, names)
    elif isinstance(node, _WITH_NODES):
        for item in node.items:
            if item.optional_vars is not None:
                _add_target(item.optional_vars, names)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            names.add(node.name)
    elif isinstance(node, _IMPORTS):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, _MATCH_NAME_NODES) and node.name:
        names.add(node.name)  # `case <name>:` / `case [*<name>]` binds a local
    elif isinstance(node, ast.MatchMapping) and node.rest:
        names.add(node.rest)  # `case {**<rest>}` binds a local


def _add_target(target: ast.expr, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Starred):
        _add_target(target.value, names)
    elif isinstance(target, _SEQ_TARGETS):
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
            if isinstance(child, _SCOPES):
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
