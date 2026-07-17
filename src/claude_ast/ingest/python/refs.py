"""Reference extraction — Python source -> raw (unbound) references.

Emits the syntactically-meaningful references:

- ``CALL``      — a call whose callee is a name (``foo()``) or an attribute chain
  rooted at a name (``os.path.join()``)
- ``REFERENCE`` — a bare attribute *read* (``obj.attr`` with no call): the same
  attribute forms as a callee, but *loaded* as a value rather than invoked. It flows
  through the identical receiver ladder as a call (name-rooted -> syntactic binding;
  value-rooted ``self.attr`` / ``u.attr`` -> the type resolvers), only it can land on a
  data attribute, not just a callable. Store/Del targets are not reads and are skipped.
- ``INHERITS``  — a class base that is such a name or attribute chain (``abc.ABC``)

plus a module-level *import map* (local name -> target qualname) used later to
bind names that came from another module. Attribute chains rooted at a *local*
value (``self.save()``, ``u.save()``) are recorded with ``local_root=True`` — kept
OUT of syntactic binding (a local root may shadow an import, so binding it would
forge a wrong edge) and left for the P2 type resolvers. When the receiver's type is
known in scope — a parameter annotation or a local ``x = Foo()`` construction — the
ref carries it (``receiver_types``, with ``receiver_inferred`` telling annotation from
inference apart) so the type resolvers can bind it. A union annotation carries several
types (``u: User | Admin`` -> ``("User", "Admin")``); ``X | None`` / ``Optional[X]``
collapse to the one non-``None`` type. A *reassigned* local's type is position-specific,
so each top-level statement takes its own view from the ``flow`` pass. A bare local call
(``x()``) is still skipped. A name-callee call also records the concrete types seen at its
positional arguments (``g(User())`` -> ``arg_types=("User",)``), which the call-site pass
turns into definite ``RECEIVES_ARG`` observations — constructions only, so exact.

Scope handling is conservative in service of honest confidence: before binding a
bare name we skip it if it is bound as a local anywhere in an enclosing function
(parameters, assignments, ``for``/``with``/``except`` targets, comprehension and
walrus targets, nested defs, local imports) — see ``scope``. We would rather emit no
edge than a confidently-wrong one. Import collection is likewise module-scoped — a
function-local import must not leak a module-wide binding. Relative imports and
full type-aware resolution remain P2.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ...model import EdgeKind
from ..product import RawRef
from .common import dotted_name, span
from .flow import flow_types
from .scope import (
    EMPTY_REC,
    SCOPES,
    RecType,
    all_args,
    binder_names,
    function_scope,
    is_local,
    param_types,
)

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)  # narrows a def node; used only here


def extract_refs(
    tree: ast.Module, module: str, path: str, node_ids: dict[ast.AST, str]
) -> tuple[list[RawRef], dict[str, str]]:
    refs: list[RawRef] = []
    package = _package(module, path)
    # Module-body value binders shadow-protect module-scope value receivers, just as a
    # function's locals do inside it (a `for json in ...` must not bind through `import json`).
    _visit(tree, module, path, refs, [binder_names(tree.body)], {}, node_ids)
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
            if isinstance(child, SCOPES):
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
    types: dict[str, RecType],  # local name -> its receiver type (see scope.RecType)
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
            dotted = dotted_name(base)
            if dotted is not None:
                if not is_local(dotted.partition(".")[0], locals_):
                    refs.append(RawRef(cid, EdgeKind.INHERITS, dotted, span(path, base)))
            else:
                _visit(base, enclosing, path, refs, locals_, types, node_ids)  # e.g. Generic[T]
        for kw in node.keywords:
            _visit(kw.value, enclosing, path, refs, locals_, types, node_ids)
        # class-body value binders shadow-protect receivers in the body (a class var
        # named like an import must not bind through it) — parent==class VARIABLEs are
        # absent from module_defs, so without this they would forge a definite edge.
        class_locals = [*locals_, binder_names(node.body)]
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
        for arg in all_args(node.args):
            if arg.annotation is not None:
                _visit(arg.annotation, enclosing, path, refs, locals_, types, node_ids)
        if node.returns is not None:
            _visit(node.returns, enclosing, path, refs, locals_, types, node_ids)
        # the body runs in the function's own scope: its locals shadow module names, and its
        # parameter annotations, local constructions, and annotated local assignments extend the
        # receiver-type map. A name this scope rebinds drops the outer type (an untyped shadow must
        # not inherit a stale annotation); precedence: a declared annotation (parameter or local
        # `x: T = …`) beats an inferred `x = Foo()`, both beat a surviving outer-scope type (a
        # genuine closure read). A local `x: T` re-annotating a parameter wins (later declaration).
        fn_locals, inferred, annotated_locals, flow_vars = function_scope(node, locals_)
        inner = [*locals_, fn_locals]
        declared = {**param_types(node), **annotated_locals}
        # A closure over an outer *reassigned* local (`flow=True`) must NOT inherit that variable's
        # positional flow view: a nested body runs at call time, not at this def's position, so the
        # live type here is stale. Drop those entries — the variable reverts to untyped in the
        # nested scope (its honest flow-insensitive state), exactly as a non-flow local would carry.
        inner_types = {
            **{n: v for n, v in types.items() if n not in fn_locals and not v.flow},
            **{name: RecType((t,), True) for name, t in inferred.items()},
            **{name: RecType(ts, False) for name, ts in declared.items()},
        }
        # Flow-sensitive reassignment: a reassigned local's type is position-specific, so each
        # top-level statement carries its own receiver-type view (empty for reassignment-free
        # functions — the common case — so `inner_types` is passed through unchanged).
        flow = flow_types(node, inner_types, inner, flow_vars)
        for stmt in node.body:
            stmt_types = {**inner_types, **flow[id(stmt)]} if flow else inner_types
            _visit(stmt, fid, path, refs, inner, stmt_types, node_ids)
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
        lambda_locals = frozenset(a.arg for a in all_args(node.args))
        inner = [*locals_, lambda_locals]
        # Drop flow-tagged (reassigned) outer vars, as the nested-def branch does: a lambda body
        # runs at call time, so the enclosing statement's positional live type is stale for it.
        inner_types = {n: v for n, v in types.items() if n not in lambda_locals and not v.flow}
        _visit(node.body, enclosing, path, refs, inner, inner_types, node_ids)
        return

    if isinstance(node, ast.Call):
        callee = dotted_name(node.func)
        if callee is not None:
            root = callee.partition(".")[0]
            if not is_local(root, locals_):
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
                # stamped with the receiver's type(s) (annotated or inferred) when known.
                rec = types.get(root, EMPTY_REC)
                refs.append(
                    RawRef(
                        enclosing,
                        EdgeKind.CALL,
                        callee,
                        span(path, node.func),
                        local_root=True,
                        receiver_types=rec.types,
                        receiver_inferred=rec.inferred,
                        receiver_flow=rec.flow,
                        receiver_may_types=rec.may,
                    )
                )
            # The callee is a flat dotted name — pure Name/Attribute leaves, with no nested refs
            # AND no bare read (it is *invoked*, not loaded). Recurse only into the arguments; NOT
            # into ``node.func``, or the REFERENCE branch below would mis-capture the callee as a
            # read (``os.path.join()`` must not also emit an ``os.path.join`` reference).
            for child in ast.iter_child_nodes(node):
                if child is not node.func:
                    _visit(child, enclosing, path, refs, locals_, types, node_ids)
            return
        # Callee rooted at a *value*, not a name. One case we can still resolve: a call whose
        # receiver is itself a name-rooted call, `re.compile(p).match(s).group()` — record the
        # receiver call (`re.compile`) + the ordered members reached on its return
        # (`("match", "group")`) for the chain resolver.
        chain = _call_chain(node)
        if chain is not None:
            receiver, members = chain
            if not is_local(receiver.partition(".")[0], locals_):
                refs.append(RawRef(
                    enclosing, EdgeKind.CALL, receiver, span(path, node.func), chain=members
                ))
            elif "." in receiver:
                # value-rooted chain (`self.get().run()`): the receiver is a value-typed call,
                # so the type resolvers own it — flagged, with the receiver's type(s) when known.
                rec = types.get(receiver.partition(".")[0], EMPTY_REC)
                refs.append(RawRef(
                    enclosing, EdgeKind.CALL, receiver, span(path, node.func), chain=members,
                    local_root=True, receiver_types=rec.types, receiver_inferred=rec.inferred,
                    receiver_flow=rec.flow, receiver_may_types=rec.may,
                ))
        # A value-rooted callee has no flat dotted name, so recursing into ``node.func`` lets the
        # REFERENCE branch descend it for nested calls (`re.compile(p)`) WITHOUT emitting a
        # spurious read (its outermost attribute is value-rooted -> the descend-only path).
        for child in ast.iter_child_nodes(node):
            _visit(child, enclosing, path, refs, locals_, types, node_ids)
        return

    if isinstance(node, ast.Attribute):
        # A bare attribute READ (`obj.attr` with no call). Reached only OUTSIDE a callee position
        # (the Call branch owns and does not re-descend its callee), so this attribute is loaded as
        # a value. Mirror the CALL machinery, but emit a REFERENCE — a read can land on a data
        # attribute, not only a callable, so the resolver widens the member set for this kind.
        dotted = dotted_name(node)
        if dotted is not None and isinstance(node.ctx, ast.Load):
            root = dotted.partition(".")[0]
            if not is_local(root, locals_):
                # name-rooted read (`os.path`, `models.User`) -> syntactic binding, like a call.
                refs.append(RawRef(enclosing, EdgeKind.REFERENCE, dotted, span(path, node)))
            elif "." in dotted:
                # value receiver (`self.attr`, `u.attr`): the type resolvers own it, stamped with
                # the receiver's type(s) when known — exactly as a value-receiver CALL is.
                rec = types.get(root, EMPTY_REC)
                refs.append(RawRef(
                    enclosing, EdgeKind.REFERENCE, dotted, span(path, node),
                    local_root=True, receiver_types=rec.types, receiver_inferred=rec.inferred,
                    receiver_flow=rec.flow, receiver_may_types=rec.may,
                ))
            return  # the whole dotted chain is one read — don't re-descend its own segments
        # Value-rooted (`f().attr`) or a Store/Del target (not a read): descend into the receiver
        # so nested calls/reads inside it are still captured; emit nothing for this attribute.
        for child in ast.iter_child_nodes(node):
            _visit(child, enclosing, path, refs, locals_, types, node_ids)
        return

    for child in ast.iter_child_nodes(node):
        _visit(child, enclosing, path, refs, locals_, types, node_ids)


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
            dotted = dotted_name(arg.func)
            if dotted is not None and "." not in dotted and not is_local(dotted, locals_):
                ctor = dotted
        names.append(ctor)
    while names and names[-1] is None:
        names.pop()
    return tuple(names)


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
            if isinstance(child, SCOPES):
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
