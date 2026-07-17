"""Scope & binding analysis for the Python backend.

Decides which names are *local* in a scope — so a value receiver (``self.save()``, ``u.save()``)
is never mis-bound through a shadowing import/class name — and what a function's own scope declares,
constructs, and reassigns. The public helpers here (``is_local`` / ``binder_names`` /
``assign_effect`` / ``function_scope`` / ``param_types``) are shared by reference extraction
(``refs``) and the flow-sensitive reassignment pass (``flow``); ``ast`` details stay confined to
this backend. Leading-underscore names below are private to this module.
"""

from __future__ import annotations

import ast

from .common import annotation_types, dotted_name

# Node-class groups hoisted to module scope. An inline ``ast.A | ast.B`` in a hot per-node predicate
# rebuilds a ``types.UnionType`` on *every* call (millions across a large tree); a module-level
# tuple is built once and matched in C by ``isinstance`` (which keeps the type-narrowing the branch
# bodies rely on). Same idiom as ``symbols._BLOCKS``.
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FUNCTIONS_OR_CLASS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LAMBDA_OR_GLOBALS = (ast.Lambda, ast.Global, ast.Nonlocal)  # nested-scope/rebinds-nothing skip
_IMPORTS = (ast.Import, ast.ImportFrom)
_SEQ_TARGETS = (ast.Tuple, ast.List)
_ASSIGN_NODES = (ast.Assign, ast.AnnAssign, ast.AugAssign)  # the name-assignment forms flow tracks
# _add_bound groups: node types that bind their target via ``node.target``, and
# the two-member ``with`` / ``match``-name groups.
_TARGET_NODES = (
    ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension,
)
_WITH_NODES = (ast.With, ast.AsyncWith)
_MATCH_NAME_NODES = (ast.MatchAs, ast.MatchStar)

# A local variable's known type at a program point, shared with ``flow``: (type names,
# from-inference?); the empty tuple means untyped.
type VarType = tuple[tuple[str, ...], bool]
UNTYPED: VarType = ((), False)


def is_local(name: str, locals_: list[frozenset[str]]) -> bool:
    return any(name in scope for scope in locals_)


def all_args(args: ast.arguments) -> list[ast.arg]:
    result = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        result.append(args.vararg)
    if args.kwarg is not None:
        result.append(args.kwarg)
    return result


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


def binder_names(body: list[ast.stmt]) -> frozenset[str]:
    """Names value-bound in a module or class body — assignment / for / with / except /
    comprehension / walrus / match targets — used to shadow-protect value receivers at
    module and class scope, as ``function_scope`` does inside functions. Imports are
    excluded (they are resolution targets, not shadows); nested scopes are not descended.

    A function's own scope is over-broad by design: a bare ``global``/``nonlocal x`` binds
    nothing (``x`` refers outward), but ``global x; x = ...`` reassigns that outer name to an
    unknown value, so ``x`` IS captured as a local shadow via the assignment — else a rebound
    import/class name would forge a confidently-wrong edge (``global os; os = f(); os.g()``
    must not bind the stdlib module; ``global User; User = 5; h(User())`` must not report a type).
    """
    names: set[str] = set()

    def process(node: ast.AST) -> None:
        if isinstance(node, SCOPES):
            return  # a separate scope — its bindings are its own
        if isinstance(node, _IMPORTS):
            return  # a resolution target, not a shadowing local
        _add_bound(node, names)
        for child in ast.iter_child_nodes(node):
            process(child)

    for stmt in body:
        process(stmt)
    return frozenset(names)


def assign_effect(
    node: ast.AST, locals_: list[frozenset[str]]
) -> tuple[list[str], VarType] | None:
    """A statement's effect on the names it assigns: ``(names, type)`` where ``type`` is a declared
    annotation, a construction, or ``UNTYPED`` (a value we can't name — the names are *cleared*).
    ``None`` when the node is not a name assignment. Mirrors the parameter/annotated-local rules:
    ``x: T`` -> the annotation type(s); ``x = Ctor()`` -> the construction; anything else clears."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        ts = annotation_types(node.annotation)
        return ([node.target.id], (ts, False) if ts else UNTYPED)
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return ([node.target.id], UNTYPED)  # `x += …` rebinds x to a value we can't type
    if isinstance(node, ast.Assign):
        names: set[str] = set()
        simple = all(isinstance(t, ast.Name) for t in node.targets)
        for target in node.targets:
            _add_target(target, names)
        if not names:
            return None
        if simple and isinstance(node.value, ast.Call):
            ctor = dotted_name(node.value.func)
            if ctor is not None and not is_local(ctor.partition(".")[0], locals_):
                return (list(names), ((ctor,), True))
        return (list(names), UNTYPED)
    return None


def param_types(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, tuple[str, ...]]:
    """Parameter name -> the receiver type name(s) its annotation denotes, for the annotation
    resolver. A plain/dotted name (``User`` / ``models.User``) yields one type; a union
    (``User | Admin``, ``Union[User, Admin]``) yields each concrete arm — the resolver fans a
    member call out to one edge per arm — and an Optional (``User | None``, ``Optional[User]``)
    collapses to the single non-``None`` type. A generic container (``list[User]``) or any other
    form yields nothing (the variable's type is the container, not the element — left deferred).
    Parameters only: annotated local assignments (``x: User = …``) and ``x = User()`` inference are
    captured by ``function_scope`` and merged into the same declared/inferred type map.
    """
    types: dict[str, tuple[str, ...]] = {}
    for arg in all_args(fn.args):
        if arg.annotation is not None:
            names = annotation_types(arg.annotation)
            if names:
                types[arg.arg] = names
    return types


def function_scope(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, outer: list[frozenset[str]]
) -> tuple[frozenset[str], dict[str, str], dict[str, tuple[str, ...]], frozenset[str]]:
    """One subtree walk yielding four things about a function's own scope: the bound-name set
    (``fn_locals``); the ``name -> constructed type`` inference map (``x = User()``); the
    ``name -> declared type(s)`` from annotated assignments; and ``flow_vars`` — the locals
    reassigned at >= 2 sites with >= 1 typed value (the flow gate, computed in this same walk so a
    reassignment-free function needs no extra traversal; empty means no flow analysis is needed).

    The annotated-assignment map mirrors a *parameter* annotation — declared, so it beats a
    construction inference and fans a union out: ``x: User`` keeps ``User``, ``x: User | Admin``
    yields both arms, ``x: User | None`` collapses to ``User``. A container / unresolvable
    annotation (``x: list[User]``) records nothing; a name annotated twice with conflicting types
    is dropped, not guessed (flow-insensitive: one scope-wide type or none).

    Fuses the passes that each independently walked the whole function body:
    ``_function_locals`` (every name bound in ``fn``'s own scope — over-broad by design; see
    ``is_local`` callers), ``_inferred_types`` (flow-insensitive, conservative construction
    inference), and the annotated-assignment scan. One walk over the body instead of three.

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
    names: set[str] = {arg.arg for arg in all_args(fn.args)}
    rebound: set[str] = set()  # names also bound to a value we can't type
    typed: list[tuple[ast.Assign, str]] = []  # (assign, ctor) — filtered after names is complete
    annotated: dict[str, tuple[str, ...]] = {}  # local `x: T` -> declared type(s)
    conflicting: set[str] = set()  # a name annotated with two conflicting types -> dropped
    assigned: set[str] = set()  # names seen assigned once — for reassignment detection (flow gate)
    reassigned: set[str] = set()  # names assigned at >= 2 sites
    typed_assign: set[str] = set()  # names with >= 1 typed (annotation/construction) assignment

    def record_assign(node: ast.AST) -> None:
        # Fused reassignment detection: which locals are assigned at >= 2 sites with >= 1 typed
        # value — the flow gate. Shares this walk so a reassignment-free function pays no extra
        # traversal. ``outer`` locals suffice: a ctor shadowed by a later local may over-include a
        # name here, but the flow pass re-checks with the full scope, so that is only wasted work.
        eff = assign_effect(node, outer)
        if eff is None:
            return
        for name in eff[0]:
            (reassigned if name in assigned else assigned).add(name)
        if eff[1][0]:
            typed_assign.update(eff[0])

    def record_annotation(node: ast.AnnAssign) -> None:
        # A local annotated assignment `x: T` (with or without a value) declares x's type, just as a
        # parameter annotation does. Only a bare-name target is a receiver (an attribute target
        # `self.x: T` is an instance attribute, owned by symbols.py). Flow-insensitive: two
        # annotations of the *same* type are fine; two *different* types can't both hold scope-wide,
        # so the name is dropped (never guess one).
        if not isinstance(node.target, ast.Name):
            return
        name = node.target.id
        if name in conflicting:
            return
        ts = annotation_types(node.annotation)
        if not ts:
            return  # a container / unresolvable annotation (`x: list[T]`) declares no receiver type
        prior = annotated.get(name)
        if prior is not None and prior != ts:
            del annotated[name]
            conflicting.add(name)
        else:
            annotated[name] = ts

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
        if isinstance(node, SCOPES):
            add_names(node)  # a separate scope, but its NAME still binds a local
            return
        if isinstance(node, _ASSIGN_NODES):
            record_assign(node)  # flow-gate bookkeeping (shares this traversal)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            _add_bound(node, names)  # simple targets -> fn_locals
            for target in node.targets:  # rare walrus inside a target -> fn_locals only
                for child in ast.iter_child_nodes(target):
                    add_names(child)
            ctor = dotted_name(node.value.func)
            if ctor is not None:
                typed.append((node, ctor))  # defer the ctor-shadow decision
            else:
                _add_bound(node, rebound)  # a value we can't name rebinds the targets
            for child in ast.iter_child_nodes(node.value):
                walk(child)  # a walrus inside the call's own args still binds (names + rebound)
            return
        if isinstance(node, ast.AnnAssign):
            record_annotation(node)  # `x: T` — record the type, then bind + recurse as usual
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
        if not is_local(ctor.partition(".")[0], scopes):
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
    flow_vars = frozenset(n for n in reassigned if n in typed_assign)
    return fn_locals, inferred, annotated, flow_vars
